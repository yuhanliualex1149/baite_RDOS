"""Versioned human task notes derived only from a confirmed task action.

New versions are new documents: an editor's comments on an older note survive.
The task authority succeeds independently; a note failure stays visible as stale.
"""
from __future__ import annotations
import asyncio
from server import central_store as central, phase3_store as local, phase3_backend as backends, db


_RULE_LABELS = {'receipt_recorded': '核对执行结果与回执后完成', 'artifact_present': '核对约定成果后完成',
                'source_versions_match': '核对指定资料版本后完成'}
_AUDIENCE_LABELS = {'admin': '管理员', 'project_members': '项目成员', 'researcher': '研发人员'}


def _sources(references):
    documents = {value['id']: value for value in backends.get_backend().registry().get('documents', [])}
    result = []
    for reference in references:
        identifier = reference.get('document_id', reference.get('id'))
        item = local.get('document_cache', identifier) or documents.get(identifier, {})
        title = item.get('title') or '已登记资料'
        version = '（版本 ' + str(reference.get('revision', '')) + '）'
        url = item.get('source_url', '')
        result.append('[' + title + '](' + url + ')' + version if url.startswith(('https://', 'http://')) else title + version)
    return '；'.join(result) or '无'


def _execution_names(task):
    names = {}
    for item in local.node_registry():
        label = item.get('display_name') or item.get('label') or '已登记执行节点'
        names[item['node_id']] = label
        if item.get('public_id'): names[item['public_id']] = label
    return '、'.join(names.get(value, '已登记执行节点') for value in task.get('executor_node_ids', []))


async def after_task_action(task: dict, operation_id: str) -> dict | None:
    operation = central.get('operation', operation_id) or {}
    is_summary = operation.get('action') == 'phase3_task:manual_complete' and task.get('close_reason') == 'manual_completed'
    if not is_summary and 'human' not in task.get('dispatch_to', []):
        return None
    authorization = operation.get('authorization') or {}
    planned = operation.get('planned_task') or {}
    if (operation.get('action') not in ('phase3_task:start', 'phase3_task:update', 'phase3_task:manual_complete')
            or authorization.get('source') not in ('authenticated_confirmation', 'authenticated_user_request')
            or authorization.get('target_id') != task.get('task_id')
            or planned.get('requirements_hash') != task.get('requirements_hash')):
        raise ValueError('任务说明只能由已确认的具体任务版本生成')
    key = task['task_id'] + '@' + str(task['requirements_version'])
    if is_summary:
        key += ':closed'
    child = operation_id + (':stage-summary' if is_summary else ':human-note')
    body = '\n'.join([
        '# 任务说明',
        '依据：当前任务登记第 ' + str(task['requirements_version']) + ' 版',
        '生成时间：' + operation.get('confirmed_at', db.utc_now()),
        '标题：' + task['title'], '目标：' + task['goal'],
        '执行节点：' + _execution_names(task),
        '交付：' + task['deliverable'],
        '交付对象：' + '、'.join(_AUDIENCE_LABELS.get(value, value) for value in task.get('deliverable_audience', ['admin', 'project_members'])),
        '产物位置：' + ('项目协作区' if task['output_zone'] == 'project' else 'Agent 协作区'),
        '完成方式：' + _RULE_LABELS.get(task['completion_rule']['kind'], '按任务登记核对'),
        '输入依据：' + _sources(task.get('input_refs', [])),
        '要求变更理由：' + task.get('change_reason', ''),
        '此文档是任务登记的说明副本；人工意见可保留在此，变更任务须由管理员通过 RDOS 办理。'])
    if is_summary:
        body = '\n'.join(['# 工作总结', '任务：' + task['title'],
            '要求版本：' + str(task['requirements_version']), '标题：' + task['title'],
            '结论：管理员已标记工作完成并结束任务。',
            '成果：' + _sources(task.get('result_refs', [])),
            '旧版成果和协作意见保留，可供后续查阅。'])
    old = local.get('presentation', key)
    # Existing published notes are historical originals, including human edits.
    # A presentation renderer update must not silently rewrite them.
    if old and old.get('status') == 'verified':
        return old
    backend = backends.get_backend()
    if old and old.get('prepared_body'):
        body = old['prepared_body']
    fingerprint = central.digest([task['requirements_hash'], body])
    if old and old['input_hash'] != fingerprint:
        # A pre-upgrade lost response may already have created the old note.
        # Recover that exact body; do not create its replacement under the same ID.
        finder = getattr(backend, 'find_created_document', None)
        existing = await asyncio.to_thread(finder, child) if old.get('dispatched_at') and finder else None
        actual = await asyncio.to_thread(backend.read_document, existing['id']) if existing else None
        original_body = actual.get('body_content', actual['content']) if actual else ''
        if not actual or central.digest([task['requirements_hash'], original_body]) != old['input_hash']:
            raise ValueError('同一要求版本的说明正文不能变更')
        body, fingerprint = original_body, old['input_hash']
    state = old or {'id': key, 'task_id': task['task_id'], 'requirements_version': task['requirements_version'],
        'input_hash': fingerprint, 'operation_id': child, 'generated_at': db.utc_now(), 'status': 'prepared',
        'prepared_body': body, 'requirements_hash': task['requirements_hash']}
    local.put('presentation', key, state)
    try:
        if state.get('dispatched_at'):
            finder = getattr(backend, 'find_created_document', None)
            document = await asyncio.to_thread(finder, child) if finder else None
            if not document:
                raise ValueError('说明创建结果待核对，未重复创建')
        else:
            state.update(status='pending', dispatched_at=db.utc_now())
            local.put('presentation', key, state)
            document = await asyncio.to_thread(backend.create_document, 'project',
                ('工作总结｜' if is_summary else '任务说明｜') + task['title'] + '（第' + str(task['requirements_version']) + '版）', body, child,
                **({'project_id':task['project_id']} if task['project_id'] != local.PROJECT_ID else {}))
        actual = await asyncio.to_thread(backend.read_document, document['id'])
        actual_body = actual.get('body_content', actual['content'])
        if actual_body.replace('\r\n', '\n').strip('\n') != body.replace('\r\n', '\n').strip('\n'):
            raise ValueError('说明文档正文未完整读回')
        state.update(status='verified', document_id=actual['id'], revision=actual['revision'],
            content_hash=actual['content_hash'], source_url=actual.get('source_url', ''), error='')
        local.put('document_cache', actual['id'], actual)
    except Exception as exc:
        state.update(status='uncertain', error_code=getattr(exc, 'code', 'readback_pending'),
                     error='任务已生效；任务说明待核对。')
    return local.put('presentation', key, state)
