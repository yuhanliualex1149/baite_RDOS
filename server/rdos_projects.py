"""Registered business projects sharing the Default Agent interface.

This module manages local project registration. Feishu task records remain the
business authority; deactivation calls the existing confirmed pause operation.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
from urllib.parse import urlsplit
from server import central_store as central, phase3_store as local, db

DEFAULT_PROJECT_ID = local.PROJECT_ID
PROJECT_FIELDS = {'name', 'description', 'node_ids', 'folder_url'}
TENANT = 'https://scn33386s7ui.feishu.cn'
_ACTIVE_DEACTIVATIONS = set()


class ProjectError(ValueError):
    def __init__(self, code, message, status=409):
        self.code, self.status = code, status
        super().__init__(message)


def _error(code, message, status=409):
    raise ProjectError(code, message, status)


def _admin(actor):
    user = central.user(actor.get('id', '')) if isinstance(actor, dict) else None
    if not user or not user.get('enabled') or 'admin' not in user.get('roles', []):
        _error('authorization_failed', '此操作需要当前 Panel 管理员身份', 403)
    return user


def _reader(actor):
    user = central.user(actor.get('id', '')) if isinstance(actor, dict) else None
    if not user or not user.get('enabled') or not set(user.get('roles', [])).intersection({'admin', 'researcher'}):
        _error('authorization_failed', '当前账号不能读取项目', 403)
    return user


def freeze_default_nodes(conn):
    """One-time upgrade from registry-wide membership to an explicit list.

    Accept the caller's transaction so creating a member cannot race with the
    snapshot and accidentally grant that member the Default project.
    """
    row = conn.execute("SELECT payload FROM central_records WHERE kind='project' AND id=?", (DEFAULT_PROJECT_ID,)).fetchone()
    if not row:
        return
    project = json.loads(row[0])
    if project.get('rdos_project_schema') != 1 or project.get('nodes_explicit'):
        return
    table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='phase3_local'").fetchone()
    nodes = [json.loads(value[0]) for value in conn.execute("SELECT payload FROM phase3_local WHERE kind='node' ORDER BY updated_at,id")] if table else []
    nodes = [node for node in nodes if node.get('enabled', True)]
    project.update(node_ids=[node['node_id'] for node in nodes],
                   member_ids=list(dict.fromkeys(node['member_id'] for node in nodes if node.get('member_id'))),
                   nodes_explicit=True, revision=project.get('revision', 1) + 1)
    _put(conn, 'project', DEFAULT_PROJECT_ID, project)


def initialize(registry=None):
    """Idempotent metadata migration; no task creation or remote provisioning."""
    item = central.get('project', DEFAULT_PROJECT_ID)
    if not item:
        _error('not_found', 'Default 项目尚未初始化', 503)
    if item.get('rdos_project_schema') != 1:
        original_nodes = [n for n in local.node_registry() if n.get('registration_source') != 'member_account']
        item.update(rdos_project_schema=1, is_default=True, revision=1,
                    name='RDOS建设', status='active', node_ids=[n['node_id'] for n in original_nodes],
                    member_ids=list(dict.fromkeys(n['member_id'] for n in original_nodes if n.get('member_id'))),
                    owner_id=local.OWNER_ID,
                    nodes_explicit=True, folder_source='explicit', folder_url='', folder_id='',
                    description='RDOS 工作协作与公共 Agent 接口。')
        central.put('project', DEFAULT_PROJECT_ID, item)
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        freeze_default_nodes(conn)
    item = central.get('project', DEFAULT_PROJECT_ID)
    if registry:
        zone = registry.get('zones', {}).get('project', {})
        updates = {'folder_id': zone.get('folder_id', ''), 'folder_url': zone.get('source_url', ''),
                   'source_environment': registry.get('mode', 'unknown')}
        if any(item.get(k) != v for k, v in updates.items()):
            item.update(updates)
            central.put('project', DEFAULT_PROJECT_ID, item)
    if __import__('os').environ.get('RDOS_RUNTIME_MODE') == 'production':
        from cutover import production_config as formal
        legacy_ai = central.get('project', formal.LEGACY_AI_PROJECT)
        if legacy_ai and not legacy_ai.get('merged_into'):
            legacy_ai.update(merged_into=DEFAULT_PROJECT_ID,
                             status_before_merge=legacy_ai.get('status', 'active'), status='inactive')
            central.put('project', formal.LEGACY_AI_PROJECT, legacy_ai)
        finance = central.get('project', formal.FINANCE_PROJECT)
        if finance is None:
            nodes = [n for n in local.node_registry() if n.get('enabled', True)]
            central.put('project', formal.FINANCE_PROJECT, {
                'id': formal.FINANCE_PROJECT, 'rdos_project_schema': 1,
                'name': '财赋', 'description': '沿用既有财赋工作范围。',
                'status': 'active', 'is_default': False, 'revision': 1,
                'owner_id': local.OWNER_ID,
                'node_ids': [n['node_id'] for n in nodes],
                'member_ids': [n['member_id'] for n in nodes],
                'nodes_explicit': True, 'folder_source': 'explicit',
                'folder_url': formal.FINANCE_URL, 'folder_id': formal.FINANCE_FOLDER,
                'source_environment': 'feishu_production',
                'migration_source': 'existing_project:rdos-self',
            })
        else:
            if finance.get('folder_id', finance.get('folder_token')) != formal.FINANCE_FOLDER:
                _error('out_of_scope', '财赋项目目录与原范围不符，停止启动', 503)
            if finance.get('rdos_project_schema') != 1:
                # Keep the existing project name, members, body and history.
                members = list(finance.get('member_ids', []))
                finance.update(rdos_project_schema=1, is_default=False, revision=1,
                    owner_id=finance.get('owner_id') or local.OWNER_ID,
                    node_ids=[n['node_id'] for n in local.node_registry() if n.get('member_id') in members],
                    nodes_explicit=True, folder_source='explicit',
                    folder_id=formal.FINANCE_FOLDER, folder_url=formal.FINANCE_URL,
                    source_environment='feishu_production', migration_source='existing_project:rdos-self')
                central.put('project', formal.FINANCE_PROJECT, finance)
    # Add registration ownership to already migrated projects without touching
    # task requirements, confirmation versions, or administrator-edited fields.
    for project in central.items('project'):
        if project.get('rdos_project_schema') == 1 and not project.get('owner_id'):
            project['owner_id'] = local.OWNER_ID if project.get('is_default') else project.get('created_by') or local.OWNER_ID
            central.put('project', project['id'], project)


def _record(project_id):
    item = central.get('project', project_id)
    if not item or item.get('rdos_project_schema') != 1:
        _error('out_of_scope', '项目未登记在当前工作空间', 404)
    return item


def is_registered(project_id):
    item = central.get('project', project_id) if isinstance(project_id, str) else None
    return bool(item and item.get('rdos_project_schema') == 1 and not item.get('merged_into'))


def project_node_ids(project_id):
    item = _record(project_id)
    if item.get('is_default') and not item.get('nodes_explicit'):
        return [node['node_id'] for node in local.node_registry()]
    return list(item.get('node_ids', []))


def require_read(project_id, actor):
    user = _reader(actor)
    project = _record(project_id)
    if 'admin' in user['roles']:
        return user
    own_nodes = {node['node_id'] for node in local.node_registry() if node.get('member_id') == user['id']}
    if (user['id'] not in project.get('member_ids', []) and project.get('owner_id') != user['id']
            and not own_nodes.intersection(project_node_ids(project_id))):
        _error('authorization_failed', '当前账号未参与此项目', 403)
    return user


def readable_project_ids(actor):
    user = _reader(actor)
    result = []
    for item in central.items('project'):
        if item.get('rdos_project_schema') != 1:
            continue
        try:
            require_read(item['id'], user)
        except ProjectError as exc:
            if exc.status != 403:
                raise
        else:
            result.append(item['id'])
    return result


def get_project(project_id, actor=None):
    if actor is not None:
        require_read(project_id, actor)
    item = copy.deepcopy(_record(project_id))
    item.setdefault('owner_id', local.OWNER_ID if item.get('is_default') else item.get('created_by') or local.OWNER_ID)
    default = _record(DEFAULT_PROJECT_ID)
    fallback = item.get('folder_source') == 'default'
    item.update(node_ids=project_node_ids(project_id),
                resolved_folder_id=(default if fallback else item).get('folder_id', ''),
                resolved_folder_url=(default if fallback else item).get('folder_url', ''),
                agent_interface_project_id=DEFAULT_PROJECT_ID)
    resume = _resume_operation(item)
    if resume:
        item['deactivation']['expected_revision'] = resume['expected_revision']
    if not fallback:
        item['folder_source'] = 'explicit'
    return item


def _resume_operation(project):
    """The original confirmation baseline survives browser and process restarts."""
    if project.get('status') != 'deactivating':
        return None
    identifier = (project.get('deactivation') or {}).get('operation_id')
    operation = central.get('project_operation', identifier) if identifier else None
    request = (operation or {}).get('request') or {}
    if (not operation or operation.get('action') != 'deactivate'
            or request.get('project_id') != project['id']
            or type(request.get('expected_revision')) is not int):
        return None
    return {'operation_id': identifier, 'expected_revision': request['expected_revision']}


def list_projects(actor, include_inactive=False):
    permitted = None if actor is None else set(readable_project_ids(actor))
    values = [get_project(item['id']) for item in central.items('project')
              if item.get('rdos_project_schema') == 1 and (permitted is None or item['id'] in permitted)
              and (include_inactive or item.get('status') != 'inactive')]
    return sorted(values, key=lambda item: (not item.get('is_default'), item.get('created_at', ''), item['id']))


def require_active(project_id):
    project = get_project(project_id)
    if project['status'] != 'active':
        _error('project_inactive', '项目正在停用或已停用，不能新增或继续派发工作')
    return project


def resource_scope(project_id, registry, *, published_tasks=None):
    """Resolve one Human folder, shared Agent interfaces, and registered sources."""
    project = get_project(project_id)
    result = copy.deepcopy(registry)
    result['project_id'] = project_id
    project_zone = result.setdefault('zones', {}).setdefault('project', {})
    project_zone.update(folder_id=project['resolved_folder_id'], source_url=project['resolved_folder_url'], name='项目工作文件夹')
    tasks = local.items('task_cache') if published_tasks is None else published_tasks
    referenced_ids = {reference.get('document_id', reference.get('id'))
        for task in tasks if task.get('project_id') == project_id and not task.get('integrity_error')
        for reference in task.get('input_refs', []) if isinstance(reference, dict)}
    result['documents'] = [document for document in result.get('documents', []) if isinstance(document, dict)
        and (document.get('id', document.get('object_id')) in referenced_ids
             or document.get('project_id', DEFAULT_PROJECT_ID) == project_id
             or (document.get('zone') == 'agent' and document.get('project_id', DEFAULT_PROJECT_ID) == DEFAULT_PROJECT_ID
                 and not document.get('task_id')))]
    return result


def _folder(value, previous=None):
    if not isinstance(value, str):
        _error('invalid_project', '项目工作文件夹须为飞书目录链接', 422)
    if not value.strip():
        return '', ''
    parsed = urlsplit(value.strip())
    match = re.fullmatch(r'/drive/folder/([A-Za-z0-9]+)', parsed.path.rstrip('/'))
    # Editing a migrated project does not re-register its unchanged location.
    if (previous and value.strip() == previous.get('folder_url')
            and parsed.scheme == 'https' and not parsed.fragment and match
            and re.fullmatch(r'[A-Za-z0-9-]+\.feishu\.cn', parsed.netloc)
            and match[1] == previous.get('folder_id')):
        return previous['folder_url'], previous['folder_id']
    if (parsed.scheme != 'https' or parsed.netloc != 'scn33386s7ui.feishu.cn'
            or not match or parsed.fragment):
        _error('invalid_project', '请使用当前飞书空间的项目文件夹链接', 422)
    return TENANT + '/drive/folder/' + match[1], match[1]


def _fields(payload, previous=None):
    if not isinstance(payload, dict) or set(payload) - PROJECT_FIELDS:
        _error('invalid_project', '项目包含未支持字段', 422)
    previous = previous or {}
    name = payload.get('name', previous.get('name', ''))
    description = payload.get('description', previous.get('description', ''))
    nodes = payload.get('node_ids', previous.get('node_ids', []))
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
        _error('invalid_project', '请填写不超过 120 字的项目名称', 422)
    if not isinstance(description, str) or len(description) > 6000:
        _error('invalid_project', '项目说明须为不超过 6000 字的文本', 422)
    registered = {node['node_id'] for node in local.node_registry()}
    if not isinstance(nodes, list) or any(not isinstance(node, str) or node not in registered for node in nodes):
        _error('invalid_project', '请选择已登记的参与节点', 422)
    folder_url, folder_id = _folder(payload.get('folder_url', previous.get('folder_url', '')), previous)
    return {'name': name.strip(), 'description': description.strip(), 'node_ids': list(dict.fromkeys(nodes)),
            'member_ids': [node['member_id'] for node in local.node_registry() if node['node_id'] in nodes],
            'folder_url': folder_url, 'folder_id': folder_id,
            'folder_source': 'explicit' if folder_url else 'default', 'nodes_explicit': True}


def _revision(project, expected):
    if type(expected) is not int or project.get('revision') != expected:
        _error('version_conflict', '项目设置已变化，请刷新后重试')


def _operation(operation_id, action, request, conn):
    if not isinstance(operation_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}', operation_id):
        _error('invalid_operation', '操作标识无效', 422)
    fingerprint = central.digest({'action': action, 'request': request})
    row = conn.execute("SELECT payload FROM central_records WHERE kind='project_operation' AND id=?", (operation_id,)).fetchone()
    if row:
        old = json.loads(row[0])
        if old['input_hash'] != fingerprint:
            _error('operation_conflict', '同一操作标识不能用于不同项目内容')
        return old
    result = {'id': operation_id, 'action': action, 'input_hash': fingerprint,
              'request': request, 'status': 'pending', 'created_at': db.utc_now()}
    _put(conn, 'project_operation', operation_id, result)
    return result


def _put(conn, kind, identifier, value):
    conn.execute('INSERT INTO central_records VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at',
                 (kind, identifier, json.dumps(value, ensure_ascii=False), db.utc_now()))


def _transaction_project(conn, project_id):
    row = conn.execute("SELECT payload FROM central_records WHERE kind='project' AND id=?", (project_id,)).fetchone()
    if not row:
        _error('not_found', '项目不存在', 404)
    value = json.loads(row[0])
    if value.get('rdos_project_schema') != 1:
        _error('out_of_scope', '项目未登记在当前工作空间', 404)
    return value


def _nonempty(project_id):
    if any(item.get('project_id') == project_id for kind in ('task_cache', 'draft', 'document_cache') for item in local.items(kind)):
        return True
    return any((operation.get('planned_task') or {}).get('project_id') == project_id for operation in central.items('operation'))


def _replayed_result(operation):
    result = copy.deepcopy(operation['result'])
    if not result.get('owner_id'):
        record = _record(result['id'])
        result['owner_id'] = record.get('owner_id') or (local.OWNER_ID if record.get('is_default') else record.get('created_by') or local.OWNER_ID)
    return result


def create_project(payload, actor, operation_id):
    user = _admin(actor)
    fields = _fields(payload)
    project_id = 'project_' + central.digest(operation_id)[:20]
    request = {'actor_id': user['id'], 'project_id': project_id, 'fields': fields}
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        op = _operation(operation_id, 'create', request, conn)
        if op['status'] == 'succeeded':
            return _replayed_result(op)
        if conn.execute("SELECT 1 FROM central_records WHERE kind='project' AND id=?", (project_id,)).fetchone():
            _error('operation_conflict', '项目标识已存在')
        item = {'id': project_id, **fields, 'rdos_project_schema': 1, 'is_default': False,
                'status': 'active', 'revision': 1, 'created_at': db.utc_now(), 'created_by': user['id'],
                'owner_id': user['id'],
                'source_environment': _record(DEFAULT_PROJECT_ID).get('source_environment', 'unknown')}
        _put(conn, 'project', project_id, item)
        default = _record(DEFAULT_PROJECT_ID)
        result = {**item, 'resolved_folder_url': fields['folder_url'] or default.get('folder_url', ''),
                  'resolved_folder_id': fields['folder_id'] or default.get('folder_id', ''),
                  'agent_interface_project_id': DEFAULT_PROJECT_ID}
        op.update(status='succeeded', result=result, completed_at=db.utc_now())
        _put(conn, 'project_operation', operation_id, op)
    return result


def update_project(project_id, payload, actor, operation_id, expected_revision):
    user = _admin(actor)
    request = {'actor_id': user['id'], 'project_id': project_id, 'payload': payload, 'expected_revision': expected_revision}
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        op = _operation(operation_id, 'update', request, conn)
        if op['status'] == 'succeeded':
            return _replayed_result(op)
        previous = _transaction_project(conn, project_id)
        _revision(previous, expected_revision)
        if previous['status'] == 'deactivating':
            _error('project_busy', '请先完成当前项目停用操作')
        fields = _fields(payload, {**previous, 'node_ids': project_node_ids(project_id)})
        if 'node_ids' not in payload:
            fields['nodes_explicit'] = previous.get('nodes_explicit', True)
        if previous.get('is_default'):
            # Default supplies the fallback; it cannot itself fall back or move.
            if 'folder_url' in payload and fields['folder_id'] != previous.get('folder_id'):
                _error('default_project_protected', 'Default 项目工作文件夹在本轮保持原位置')
            fields.update(folder_url=previous.get('folder_url', ''), folder_id=previous.get('folder_id', ''), folder_source='explicit')
        elif fields['folder_id'] != previous.get('folder_id') and _nonempty(project_id):
            _error('project_not_empty', '项目已有任务或成果，本轮不迁移工作文件夹')
        removed = set(project_node_ids(project_id)) - set(fields['node_ids'])
        candidates = local.items('task_cache')
        current_heads = {task['task_id']: task for task in candidates}
        candidates += [op['planned_task'] for op in central.items('operation')
            if op.get('status') != 'succeeded' and op.get('planned_task')
            and op['planned_task'].get('record_revision', 0) > current_heads.get(op['planned_task'].get('task_id'), {}).get('record_revision', 0)]
        if any(task.get('project_id') == project_id and task.get('business_status') != 'closed'
               and removed.intersection(task.get('executor_node_ids', [])) for task in candidates):
            _error('node_has_open_tasks', '移除参与节点前，请先结束或调整其未结束任务')
        item = {**previous, **fields, 'revision': previous['revision'] + 1, 'updated_at': db.utc_now(), 'updated_by': user['id']}
        _put(conn, 'project', project_id, item)
        default = _record(DEFAULT_PROJECT_ID)
        result = {**item, 'resolved_folder_url': fields['folder_url'] or default.get('folder_url', ''),
                  'resolved_folder_id': fields['folder_id'] or default.get('folder_id', ''), 'agent_interface_project_id': DEFAULT_PROJECT_ID}
        op.update(status='succeeded', result=result, completed_at=db.utc_now())
        _put(conn, 'project_operation', operation_id, op)
    return result


async def _live_tasks(project_id):
    from server import phase3_backend, phase3_service
    rows = await asyncio.to_thread(phase3_backend.get_backend().list_tasks)
    tasks = [phase3_service._validate_task(row) for row in rows if isinstance(row, dict) and row.get('project_id') == project_id]
    observed = {task['task_id'] for task in tasks}
    cached = {task['task_id']: task for task in local.items('task_cache')}
    required = {task_id for task_id, task in cached.items() if task.get('project_id') == project_id
                and task.get('business_status') in ('active', 'pending_review')}
    for operation in central.items('operation'):
        planned = operation.get('prepared_task') or {}
        if (planned.get('project_id') == project_id and operation.get('task_dispatched')
                and planned.get('task_id') not in cached):
            required.add(planned['task_id'])
    if required - observed:
        _error('write_uncertain', '已有相关任务未能从任务登记完整核对，不能宣称项目已停用')
    return tasks


async def preview_deactivate(project_id, actor):
    _admin(actor)
    project = get_project(project_id)
    if project.get('is_default'):
        _error('default_project_protected', 'Default 项目始终保持启用')
    tasks = await _live_tasks(project_id)
    return {'project_id': project_id, 'revision': project['revision'], 'status': project['status'],
            'resume_operation': _resume_operation(project),
            'tasks': [{key: task[key] for key in ('task_id', 'title', 'business_status', 'record_revision', 'requirements_version')}
                      for task in tasks if task['business_status'] in ('active', 'pending_review')]}


async def deactivate_project(project_id, actor, operation_id, expected_revision):
    if project_id in _ACTIVE_DEACTIVATIONS:
        _error('project_busy', '项目停用仍在处理中，请稍后查看进度')
    _ACTIVE_DEACTIVATIONS.add(project_id)
    try:
        return await _deactivate_project(project_id, actor, operation_id, expected_revision)
    finally:
        _ACTIVE_DEACTIVATIONS.discard(project_id)


async def _deactivate_project(project_id, actor, operation_id, expected_revision):
    from server import phase3_service as service, phase1_operations as operations
    from server.central_service import WRITE_LOCK
    user = _admin(actor)
    request = {'actor_id': user['id'], 'project_id': project_id, 'expected_revision': expected_revision}
    # First mark and snapshot while the same task-write lock is held. Later
    # pause actions use that lock independently, so failures can resume safely.
    if WRITE_LOCK.locked() or operations.ACTIVE_WRITES:
        _error('write_busy', '另一写入尚未结束，请稍后重试')
    async with WRITE_LOCK:
        project = get_project(project_id)
        if project.get('is_default'):
            _error('default_project_protected', 'Default 项目始终保持启用')
        with db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            op = _operation(operation_id, 'deactivate', request, conn)
        if op.get('status') == 'succeeded':
            return get_project(project_id)
        if project['status'] == 'deactivating':
            if (project.get('deactivation') or {}).get('operation_id') != operation_id:
                _error('project_busy', '请继续原项目停用操作，不要创建新操作')
        else:
            _revision(project, expected_revision)
            if project['status'] != 'active':
                _error('invalid_transition', '项目已经停用')
        tasks = await _live_tasks(project_id)
        if project['status'] == 'active':
            progress = {'operation_id': operation_id, 'expected_revision': expected_revision,
                        'status': 'running', 'paused_task_ids': [],
                        'pending_task_ids': [], 'plans': [], 'error': ''}
            for task in tasks:
                if task['business_status'] in ('active', 'pending_review'):
                    progress['plans'].append({'task_id': task['task_id'], 'expected_revision': task['record_revision'],
                        'expected_requirements_version': task['requirements_version'],
                        'operation_id': 'project-pause-' + central.digest([operation_id, task['task_id']])[:32]})
            progress['pending_task_ids'] = [plan['task_id'] for plan in progress['plans']]
            project = {**_record(project_id), 'status': 'deactivating', 'revision': project['revision'] + 1,
                       'deactivation': progress, 'updated_at': db.utc_now()}
            central.put('project', project_id, project)
        else:
            project = _record(project_id)
    progress = project['deactivation']
    try:
        for plan in progress['plans']:
            task_id = plan['task_id']
            if task_id not in progress['pending_task_ids']:
                continue
            current = await service._read_task(task_id)
            pause_operation = central.get('operation', plan['operation_id'])
            if current['business_status'] in ('active', 'pending_review') or (pause_operation and pause_operation.get('status') != 'succeeded'):
                current = await service.apply_task_action(task_id, 'pause', {'reason': '项目停用，暂停当前工作。'}, user,
                    plan['operation_id'], plan['expected_revision'], plan['expected_requirements_version'])
            elif current['business_status'] not in ('paused', 'closed'):
                _error('write_uncertain', '相关任务暂停结果待核对')
            progress['pending_task_ids'].remove(task_id)
            if current['business_status'] == 'closed':
                ended = progress.setdefault('ended_task_ids', [])
                if task_id not in ended:ended.append(task_id)
            elif task_id not in progress['paused_task_ids']:
                progress['paused_task_ids'].append(task_id)
            progress.update(error='', status='running')
            central.put('project', project_id, {**project, 'deactivation': copy.deepcopy(progress)})
        live = await _live_tasks(project_id)
        if any(task['business_status'] in ('active', 'pending_review') for task in live):
            _error('write_uncertain', '仍有相关任务未暂停，项目尚未停用')
        # Nothing new can publish while deactivating; preserve all task state.
        with db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current = _transaction_project(conn, project_id)
            progress.update(status='completed', error='')
            current.update(status='inactive', revision=current['revision'] + 1, deactivation=progress, updated_at=db.utc_now())
            _put(conn, 'project', project_id, current)
            op.update(status='succeeded', completed_at=db.utc_now())
            _put(conn, 'project_operation', operation_id, op)
    except (Exception, asyncio.CancelledError) as exc:
        progress.update(status='partial', error='相关任务暂停未完成，请继续原操作核对。', error_code=getattr(exc, 'code', 'interrupted'))
        central.put('project', project_id, {**_record(project_id), 'deactivation': progress})
        if isinstance(exc, asyncio.CancelledError):
            raise
    return get_project(project_id)


def reactivate_project(project_id, actor, operation_id, expected_revision):
    user = _admin(actor)
    request = {'actor_id': user['id'], 'project_id': project_id, 'expected_revision': expected_revision}
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        op = _operation(operation_id, 'reactivate', request, conn)
        if op.get('status') == 'succeeded':
            return _replayed_result(op)
        item = _transaction_project(conn, project_id)
        _revision(item, expected_revision)
        if item['status'] != 'inactive':
            _error('invalid_transition', '只能重新启用已停用项目')
        item.update(status='active', revision=item['revision'] + 1, updated_at=db.utc_now())
        _put(conn, 'project', project_id, item)
        default = _record(DEFAULT_PROJECT_ID)
        result = {**item, 'node_ids': project_node_ids(project_id),
                  'resolved_folder_url': item.get('folder_url') or default.get('folder_url', ''),
                  'resolved_folder_id': item.get('folder_id') or default.get('folder_id', ''),
                  'agent_interface_project_id': DEFAULT_PROJECT_ID}
        op.update(status='succeeded', result=result, completed_at=db.utc_now())
        _put(conn, 'project_operation', operation_id, op)
    return result
