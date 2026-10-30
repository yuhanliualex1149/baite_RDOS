"""Small task/record domain for phase 3. Feishu/backend records are authoritative.

Panel decisions and task-authorized execution are separate authorization paths.
No node HTTP endpoint is exposed here and no model text creates human authority.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
from urllib.parse import urlparse
from server import db, central_store as central, phase3_store as local, phase3_backend as backends
from server import phase1_operations as ops
from server import rdos_rules as rules
from server import rdos_projects as projects

PROJECT_ID, OWNER_ID, NODE_IDS = local.PROJECT_ID, local.OWNER_ID, local.NODE_IDS
_FORMAL = os.environ.get('RDOS_RUNTIME_MODE') == 'production'
_NODE_CAPABILITY = object()
TASK_FIELDS = rules.LEGACY_TASK_FIELDS + rules.BUSINESS_TASK_FIELDS
EVENT_TYPES = {'read', 'started', 'progress', 'submission', 'review', 'blocker', 'clarification',
               'correction', 'paused_ack', 'closed_ack', 'check_result', 'project_feedback'}
STATES = {'active', 'pending_review', 'paused', 'closed'}


class Phase3Error(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _error(code, message):
    raise Phase3Error(code, message)


def environment():
    from server.phase1_runtime import environment as current
    return current()


def initialize():
    local.init()
    projects.initialize(backends.get_backend().registry())
    project=central.get('project',PROJECT_ID)
    project['source_environment']=backends.get_backend().registry().get('mode','unknown')
    central.put('project',PROJECT_ID,project)
    from server.draft_lifecycle import migrate_local_drafts
    migrate_local_drafts()


def node_registry():
    return local.node_registry()


def _node_ids(project_id=None):
    registered = {node['node_id'] for node in node_registry()}
    return registered if project_id is None else registered.intersection(projects.project_node_ids(project_id))


def _admin(actor):
    current = central.user(actor.get('id', '')) if isinstance(actor, dict) else None
    if not current or not current.get('enabled') or 'admin' not in current.get('roles', []):
        _error('authorization_failed', '此操作需要当前 Panel 管理员身份')
    return current


def make_node_actor(node_id: str) -> dict:
    if node_id not in _node_ids():
        _error('authorization_failed', '节点未登记')
    return {'id': node_id, 'node_id': node_id, 'owner_id': OWNER_ID,
            'identity_mode': 'shared_account_claim' if _FORMAL else 'role_test',
            'verified_origin': 'shared_feishu_account_claim' if _FORMAL else 'internal_simulation',
            '_capability': _NODE_CAPABILITY}


def _node(actor):
    if not isinstance(actor, dict) or actor.get('_capability') is not _NODE_CAPABILITY or actor.get('node_id') not in _node_ids():
        _error('authorization_failed', '不能以自报节点或角色取得执行授权')
    return actor


def _principal(actor):
    if isinstance(actor, dict) and actor.get('_capability') is _NODE_CAPABILITY:
        value = _node(actor)
        return {'id': value['node_id'], 'kind': value.get('verified_origin', 'internal_simulation'),
                'identity_mode': value['identity_mode']}
    value = _admin(actor)
    return {'id': value['id'], 'kind': 'authenticated_panel', 'identity_mode': value.get('identity_mode', 'role_test')}


def _requirements(task):
    return rules.requirements(task)


def _check_scope(value):
    if not isinstance(value, dict) or not projects.is_registered(value.get('project_id')) or any(value.get(k) != v for k, v in environment().items()):
        _error('out_of_scope', '记录不属于当前工作空间和项目')


def _authority_key(task):
    return task['task_id'] + '@' + str(task['record_revision'])


def _remember_authority(task, opid):
    local.immutable('task_authority', _authority_key(task), {'hash': central.digest(backends.payload(task)), 'operation_id': opid})


def _validate_task(task):
    _check_scope(task)
    if task.get('task_id') != task.get('id') or task.get('business_status') not in STATES:
        _error('invalid_task', '任务标识或业务状态无效')
    if type(task.get('requirements_version')) is not int or task['requirements_version'] < 1 or type(task.get('record_revision')) is not int or task['record_revision'] < 1:
        _error('invalid_task', '任务版本无效')
    head = local.get('task_head', task['task_id'])
    if head and task['record_revision'] < head['record_revision']:
        _error('task_rollback', '远端任务低于已独立读回的最新记录版本；停止执行并保留现场')
    if task.get('dispatch_source') == 'peer':
        from server import peer_tasks
        peer_tasks.accept_task(task)
        return task
    if central.digest(_requirements(task)) != task.get('requirements_hash'):
        _error('task_tampered', '任务要求正文与版本指纹不一致')
    proof = local.get('task_authority', _authority_key(task))
    if not proof or proof['hash'] != central.digest(backends.payload(task)):
        _error('task_tampered', '任务当前内容不是已核对的授权操作结果；未以缓存覆盖飞书')
    return task


async def _read_task(task_id, required=True):
    item = await asyncio.to_thread(backends.get_backend().read_task, task_id)
    if not item:
        if local.get('task_head', task_id): _error('task_missing', '已发布任务从远端消失，未按新任务重建')
        if required: _error('not_found', '任务不存在')
        return None
    return _validate_task(item)


def _normalize_fields(payload, previous=None, project_id=PROJECT_ID):
    previous = previous or {}
    allowed = set(TASK_FIELDS) | {'task_id', 'change_reason'}
    if not isinstance(payload, dict) or set(payload) - allowed:
        _error('invalid_fields', '任务包含未支持字段')
    fields = {key: copy.deepcopy(payload.get(key, previous.get(key))) for key in TASK_FIELDS}
    fields['title'] = str(fields.get('title') or '').strip()
    fields['goal'] = str(fields.get('goal') or '').strip()
    fields['deliverable'] = str(fields.get('deliverable') or '').strip()
    if not fields['title'] or not fields['goal'] or not fields['deliverable']:
        _error('invalid_task', '请填写标题、目标和交付要求')
    project_owner = projects.get_project(project_id).get('owner_id') or OWNER_ID
    fields['owner_id'] = fields.get('owner_id') or project_owner
    if fields['owner_id'] != project_owner:
        _error('out_of_scope', '负责人必须是已登记的项目负责人')
    nodes = fields.get('executor_node_ids') or []
    permitted_nodes = _node_ids(project_id)
    if previous.get('dispatch_source') == 'peer':
        permitted_nodes = permitted_nodes.union(previous.get('executor_node_ids',[]))
    if not isinstance(nodes, list) or not nodes or any(n not in permitted_nodes for n in nodes):
        _error('invalid_task', '请选择本项目已登记的参与节点')
    fields['executor_node_ids'] = list(dict.fromkeys(nodes))
    for key in ('dispatch_to', 'return_to'):
        values = fields.get(key) or ['agent']
        if not isinstance(values, list) or not values or any(v not in ('agent', 'human') for v in values):
            _error('invalid_task', '下发和回传对象必须明确为 agent 或 human')
        fields[key] = list(dict.fromkeys(values))
    fields['output_zone'] = fields.get('output_zone') or 'project'
    if fields['output_zone'] not in ('project', 'agent'):
        _error('out_of_scope', '产物只能位于已登记的项目区或 Agent 区')
    fields['task_type'] = fields.get('task_type') or rules.task_type(fields)
    if fields['task_type'] not in rules.TASK_TYPES:
        _error('invalid_task', '任务类别必须为 action 或 deliverable')
    audience = fields.get('deliverable_audience') or ['admin', 'project_members']
    if not isinstance(audience, list) or not audience or any(item not in rules.AUDIENCES for item in audience):
        _error('invalid_task', '请明确交付受众')
    fields['deliverable_audience'] = list(dict.fromkeys(audience))
    if fields.get('periodicity') is None:
        fields.pop('periodicity', None)
    else:
        try:
            rules.period_key(fields)
        except rules.RuleViolation as exc:
            _error(exc.code, str(exc))
        if fields['task_type'] != 'action':
            _error('invalid_task', '周期回执首版只适用于执行类任务')
    rule = fields.get('completion_rule') or {'kind': 'receipt_recorded'}
    if not isinstance(rule, dict) or set(rule) != {'kind'} or rule['kind'] not in rules.COMPLETION_KINDS:
        _error('invalid_task', '完成规则必须为执行回执核对或固定程序核验')
    fields['completion_rule'] = rule
    fields['input_refs'] = fields.get('input_refs') or []
    fields['editable_document_ids'] = fields.get('editable_document_ids') or []
    if not isinstance(fields['input_refs'], list) or not isinstance(fields['editable_document_ids'], list):
        _error('invalid_task', '资料引用与允许修改文档须为列表')
    _document_refs(fields['input_refs'], 'invalid_task')
    if rule['kind']=='source_versions_match' and not fields['input_refs']:
        _error('invalid_task','资料版本核对任务须预先指定版本依据')
    if any(not isinstance(value, str) or not value for value in fields['editable_document_ids']):
        _error('invalid_task', '允许修改文档必须使用明确文档标识')
    fields['due_at'] = str(fields.get('due_at') or '')
    return fields


def _document_refs(refs, code='invalid_artifact'):
    if not isinstance(refs, list): _error(code, '资料引用必须为列表')
    for ref in refs:
        if not isinstance(ref, dict): _error(code, '每条资料引用须为对象')
        identifier = ref.get('document_id') or ref.get('id')
        revision = ref.get('revision')
        if (not isinstance(identifier, str) or not identifier or type(revision) not in (int, str)
                or (isinstance(revision, str) and not revision) or (type(revision) is int and revision < 0)
                or not isinstance(ref.get('content_hash'), str) or not ref['content_hash']):
            _error(code, '资料引用必须包含文档标识、明确版本和正文指纹')


def _event_shape(raw):
    _check_scope(raw)
    if not isinstance(raw.get('id'), str) or not raw['id'] or not isinstance(raw.get('event_type'), str) or raw['event_type'] not in EVENT_TYPES | {'task_change'}:
        _error('invalid_event', '事件 ID 或类型无效')
    if not isinstance(raw.get('body'), str) or not isinstance(raw.get('task_refs', []), list):
        _error('invalid_event', '事件正文或任务引用无效')
    for ref in raw.get('task_refs', []):
        if (not isinstance(ref, dict) or not isinstance(ref.get('task_id'), str) or not ref['task_id']
                or type(ref.get('requirements_version')) is not int or ref['requirements_version'] < 1):
            _error('invalid_event', '任务引用必须包含任务标识和正整数要求版本')
    _document_refs(raw.get('artifact_refs', []), 'invalid_event')
    if 'result_status' in raw and raw['result_status'] not in rules.RESULT_STATUSES:
        _error('invalid_event', '执行结果状态无效')
    if raw.get('correction_of') is not None and not isinstance(raw['correction_of'], str):
        _error('invalid_event', '更正引用必须是记录标识')
    if 'recipients' in raw and (not isinstance(raw['recipients'], list) or any(x not in ('agent', 'human') for x in raw['recipients'])):
        _error('invalid_event', '接收对象无效')


def _document_version_key(document):
    return document['id']+'@'+str(document['revision'])+':'+document['content_hash']


def _remember_document_version(document,task,node):
    local.immutable('document_version_grant',_document_version_key(document),{
        'document_id':document['id'],'revision':document['revision'],'content_hash':document['content_hash'],
        'task_id':task['task_id'],'requirements_version':task['requirements_version'],'node_id':node['node_id']})


async def _verify_refs(refs, task=None, require_output=False, claimed_node_id=None):
    _document_refs(refs)
    result = []
    registry = projects.resource_scope((task or {}).get('project_id', PROJECT_ID), backends.get_backend().registry())
    zone = registry.get('zones', {}).get((task or {}).get('output_zone', ''), {})
    for ref in refs:
        identifier = ref.get('document_id') or ref.get('id')
        document = await asyncio.to_thread(backends.get_backend().read_document, identifier)
        if str(document['revision']) != str(ref.get('revision')) or document['content_hash'] != ref.get('content_hash'):
            _error('version_conflict', '资料或产物版本已变化，请核对当前正文')
        if require_output and zone.get('folder_id') and document.get('folder_id') != zone['folder_id']:
            _error('out_of_scope', '产物不在此任务授权的资料区')
        if require_output:
            proof=(local.get('document_version_grant',_document_version_key(document)) or local.get('document_grant',identifier)
                   or next((d for d in registry.get('documents',[]) if isinstance(d,dict) and d.get('id')==identifier),{}))
            node_id=proof.get('node_id') or proof.get('claimed_node_id')
            if (proof.get('task_id')!=task['task_id'] or proof.get('requirements_version')!=task['requirements_version']
                    or node_id not in task['executor_node_ids'] or (claimed_node_id and claimed_node_id!=node_id)):
                _error('out_of_scope','产物未绑定当前任务要求和相应执行节点，不能以同目录的其他文档代替')
        if require_output and not document.get('content', '').strip():
            _error('invalid_artifact', '产物正文为空')
        local.put('document_cache', document['id'], document)
        result.append({'document_id': document['id'], 'revision': document['revision'], 'content_hash': document['content_hash'],
                       'source_url': document.get('source_url', ''), 'title': document.get('title', '')})
    return result


async def prepare_task(payload, actor, operation_id, project_id=PROJECT_ID):
    user = _admin(actor)
    if not isinstance(payload, dict): _error('invalid_fields', '任务要求必须是对象')
    projects.require_active(project_id)
    existing = await _read_task(payload['task_id'], False) if payload.get('task_id') else None
    if existing and existing['project_id'] != project_id:
        _error('out_of_scope', '任务不属于当前所选项目')
    fields = _normalize_fields(payload, existing, project_id)
    await _verify_refs(fields['input_refs'])
    if existing and not str(payload.get('change_reason') or '').strip():
        _error('invalid_task', '调整任务需要说明变更原因')
    if existing and existing['business_status'] == 'closed':
        _error('task_closed', '已结束任务不能修改要求；请另建任务并引用原记录')
    task_id = payload.get('task_id') or 'task_p3_' + central.digest(operation_id)[:20]
    request = {'actor': user['id'], 'project_id': project_id, 'task_id': task_id, 'fields': fields, 'change_reason': payload.get('change_reason', '')}
    draft_id = 'draft_p3_' + central.digest(operation_id)[:20]
    old = local.get('draft', draft_id)
    if old:
        if old['request_hash'] != central.digest(request):
            _error('operation_conflict', '相同草稿操作标识对应不同要求')
        return old
    projects.require_active(project_id)
    if any(node not in _node_ids(project_id) for node in fields['executor_node_ids']) and not (existing or {}).get('dispatch_source') == 'peer':
        _error('authorization_failed', '参与节点已变化，请重新准备任务')
    draft = {'id': draft_id, 'task_id': task_id, **environment(), 'project_id': project_id, 'payload': fields,
        'base_revision': existing['record_revision'] if existing else 0,
        'base_requirements_version': existing['requirements_version'] if existing else 0,
        'change_reason': request['change_reason'], 'created_by': user['id'], 'created_at': db.utc_now(),
        'request_hash': central.digest(request), 'status': 'draft'}
    draft['draft_hash'] = central.digest({k: draft[k] for k in ('task_id','payload','base_revision','base_requirements_version','change_reason')})
    return local.immutable('draft', draft_id, draft)


async def edit_draft(draft_id, payload, actor, expected_hash):
    from server.central_service import WRITE_LOCK
    user = _admin(actor)
    old = local.get('draft', draft_id)
    if not old or old['status'] != 'draft' or old['draft_hash'] != expected_hash:
        _error('version_conflict', '草稿已变化或已确认，请重新读取')
    projects.require_active(old['project_id'])
    current = await _read_task(old['task_id'], False)
    fields = _normalize_fields({**old['payload'], **payload},previous=current,project_id=old['project_id'])
    await _verify_refs(fields['input_refs'])
    async with WRITE_LOCK:
        projects.require_active(old['project_id'])
        if any(node not in _node_ids(old['project_id']) for node in fields['executor_node_ids']) and not (current or {}).get('dispatch_source')=='peer':
            _error('authorization_failed', '参与节点已变化，请重新编辑任务')
        latest = local.get('draft', draft_id)
        if not latest or latest['draft_hash'] != expected_hash or latest['status'] != 'draft':
            _error('version_conflict', '另一编辑已改变草稿')
        updated = {**latest, 'payload': fields, 'base_revision': current['record_revision'] if current else 0,
            'base_requirements_version': current['requirements_version'] if current else 0,
            'change_reason': payload.get('change_reason', old.get('change_reason','')), 'edited_by':user['id']}
        updated['draft_hash'] = central.digest({k:updated[k] for k in ('task_id','payload','base_revision','base_requirements_version','change_reason')})
        return local.put('draft',draft_id,updated)


def _check_expected(task, expected_revision, expected_requirements_version):
    revision, version = (task['record_revision'], task['requirements_version']) if task else (0, 0)
    if type(expected_revision) is not int or type(expected_requirements_version) is not int or revision != expected_revision or version != expected_requirements_version:
        _error('version_conflict', '任务要求或状态已变化，请重新查看后操作')


def _event(task, opid, event_type, body, principal, *, refs=None):
    event = {'id': 'event_p3_' + central.digest(opid)[:24], **environment(), 'project_id': task.get('project_id', PROJECT_ID),
        'event_type': event_type, 'task_refs': [{'task_id': task['task_id'], 'requirements_version': task['requirements_version']}],
        'claimed_node_id': principal['id'] if principal['id'] in _node_ids() else '', 'verified_actor': principal,
        'identity_mode': 'actual' if _FORMAL else 'role_test', 'body': body,
        'artifact_refs': refs or [], 'recipients': ['agent','human'],
        'origin': 'panel' if principal['kind'] == 'authenticated_panel' else 'task_execution_grant', 'created_at': db.utc_now(),
        'operation_id': opid}
    if event_type == 'task_change':
        event.update(requirements_snapshot=copy.deepcopy(_requirements(task)), requirements_hash=task['requirements_hash'],
                     record_revision=task['record_revision'], change_reason=task.get('change_reason', ''),
                     old_results_policy='retained_historical_not_accepted_for_new_version')
    return event


async def _commit_task(task, event, opid, mark):
    backend = backends.get_backend()
    prepared = central.get('operation', opid)
    task = {**task, 'operation_id': opid + ':task'}
    if task.get('dispatch_source') == 'peer':
        from server import peer_tasks
        task = peer_tasks.sign_task_update(task)
    event = {**event, 'operation_id': opid + ':event'}
    prepared.update(prepared_task=task, prepared_event=event)
    central.put('operation', opid, prepared)
    _remember_authority(task, opid)
    local.immutable('trusted_event', event['id'], {'hash': central.digest(backends.payload(event)), 'operation_id': opid})
    mark()
    current = await asyncio.to_thread(backend.read_task, task['task_id'])
    if not current or backends.payload(current) != backends.payload(task):
        if current and current.get('record_revision', 0) >= task['record_revision']:
            _error('version_conflict', '任务已被另一操作更新，未覆盖')
        sent = central.get('operation', opid)
        if sent.get('task_dispatched'):
            _error('write_uncertain', '原任务写入已经发出但未见目标结果；未重发')
        sent['task_dispatched'] = True
        central.put('operation', opid, sent)
        await asyncio.to_thread(backend.write_task, task, opid + ':task')
    current = await asyncio.to_thread(backend.read_task, task['task_id'])
    if not current or backends.payload(current) != backends.payload(task):
        _error('write_uncertain', '任务关键字段未完整读回')
    local.confirm_task_head(current, central.digest(backends.payload(current)))
    authorizing = central.get('operation', task.get('grant_operation_id','')) or {}
    plan = authorizing.get('planned_task') or {}
    if authorizing.get('authorization') and plan.get('requirements_hash') == task['requirements_hash']:
        local.put('grant',task['task_id']+'@'+str(task['requirements_version']),{
            'operation_id':task['grant_operation_id'],'requirements_hash':task['requirements_hash'],
            'owner_id':authorizing['authorization']['actor_id']})
    # Distinct idempotency key: a receipt failure must never recreate the task.
    matches = [e for e in await asyncio.to_thread(backend.list_events) if e.get('id') == event['id']]
    if not matches:
        sent = central.get('operation', opid)
        if sent.get('event_dispatched'):
            _error('write_uncertain', '原回执已经发出但未见结果；未重复追加')
        sent['event_dispatched'] = True
        central.put('operation', opid, sent)
        await asyncio.to_thread(backend.append_event, event, opid + ':event')
        matches = [e for e in await asyncio.to_thread(backend.list_events) if e.get('id') == event['id']]
    if not matches or any(backends.payload(e) != backends.payload(event) for e in matches):
        _error('write_uncertain', '任务已写入，协作回执尚未完整核对')
    local.remember_version(task['task_id'], task['requirements_version'], _requirements(task))
    local.put('task_cache', task['task_id'], current)
    _ingest_event(matches[0], {task['task_id']: task})
    return current


async def _reconcile_task(opid):
    op = central.get('operation', opid) or {}
    expected = op.get('prepared_task')
    if not expected: return {'status':'unknown'}
    current = await asyncio.to_thread(backends.get_backend().read_task, expected['task_id'])
    if not current: return {'status':'unknown'}
    if backends.payload(current) != backends.payload(expected): return {'status':'conflict'}
    result = await _commit_task(expected, op['prepared_event'], opid, lambda: None)
    return {'status':'verified', 'result':result}


def _publish_verified_draft(task, opid, action):
    """Restore only the draft projection bound to the original verified input.

    Reconciliation already preserves the task grant in _commit_task. A lost
    response must also retire its exact draft, including on a later replay of
    an operation that has succeeded. This never sends business I/O.
    """
    if action not in ('start', 'update'):
        return
    operation = central.get('operation', opid) or {}
    binding = operation.get('authorization') or {}
    if (operation.get('status') != 'succeeded' or not binding
            or backends.payload(operation.get('prepared_task') or {}) != backends.payload(task)):
        return
    base = binding.get('base') or {}
    for draft in local.items('draft'):
        if draft.get('task_id') != task['task_id'] or draft.get('status') != 'draft':
            continue
        request = {'task_id': task['task_id'], 'action': action,
            'payload': {'draft_id': draft['id'], 'expected_hash': draft['draft_hash']},
            'actor_id': binding.get('actor_id'), 'expected_revision': base.get('record_revision'),
            'expected_requirements_version': base.get('requirements_version')}
        if (central.digest(request) != binding.get('input_hash')
                or central.digest({'action': operation['action'], 'payload': request}) != operation['payload_hash']):
            continue
        with db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT payload FROM phase3_local WHERE kind='draft' AND id=?", (draft['id'],)).fetchone()
            if row and json.loads(row[0]) == draft:
                draft['status'] = 'published'
                conn.execute("UPDATE phase3_local SET payload=?,updated_at=? WHERE kind='draft' AND id=?",
                    (json.dumps(draft, ensure_ascii=False), db.utc_now(), draft['id']))


async def _presentation(task, opid, action):
    _publish_verified_draft(task, opid, action)
    if action not in ('start','update','manual_complete'):
        return task
    try:
        import inspect
        from server.phase3_presentations import after_task_action
        result=after_task_action(task,opid)
        if inspect.isawaitable(result):await result
    except ModuleNotFoundError as exc:
        if exc.name != 'server.phase3_presentations':raise
    except Exception as exc:
        key=task['task_id']+'@'+str(task['requirements_version'])+(':closed' if action=='manual_complete' else '')
        local.put('presentation',key,{'task_id':task['task_id'],'requirements_version':task['requirements_version'],
            'status':'needs_attention','error':str(exc)[:400],'operation_id':opid})
    return task


async def apply_task_action(task_id, action, payload, actor, operation_id,
                            expected_revision=None, expected_requirements_version=None, project_id=None,
                            request_authorization=None):
    from server.central_service import WRITE_LOCK
    user = _admin(actor)
    if action not in ('start','update','pause','resume','submit','manual_complete','cancel'):
        _error('invalid_action', '任务动作不支持')
    request = {'task_id':task_id, 'action':action, 'payload':payload, 'actor_id':user['id'],
               'expected_revision':expected_revision, 'expected_requirements_version':expected_requirements_version}
    op = central.operation(operation_id, 'phase3_task:' + action, request)
    # Exact replay is safe even after the task advanced; altered input was rejected above.
    if op.get('authorization'):
        bound_project = op['authorization'].get('project_id', PROJECT_ID)
        if project_id is not None and project_id != bound_project:
            _error('out_of_scope', '任务不属于当前所选项目')
        async def replay():
            if action in ('start', 'update', 'resume') and not op.get('dispatched_at'):
                projects.require_active(bound_project)
            live=await _read_task(task_id,False)
            _check_expected(live,expected_revision,expected_requirements_version)
            if action in ('start','update') and not op.get('dispatched_at'):
                latest = local.get('draft', payload.get('draft_id',''))
                if not latest or latest.get('status') != 'draft' or latest.get('draft_hash') != payload.get('expected_hash'):
                    _error('version_conflict', '草稿已变化或删除，未发布')
            return await _commit_task(op['planned_task'],op['planned_event'],operation_id,lambda:ops.mark_dispatched(operation_id))
        result = await ops.run_write(operation_id, WRITE_LOCK, replay,reconcile=lambda:_reconcile_task(operation_id))
        return await _presentation(result,operation_id,action)
    current = await _read_task(task_id, False)
    if current and project_id is not None and current['project_id'] != project_id:
        _error('out_of_scope', '任务不属于当前所选项目')
    _check_expected(current, expected_revision, expected_requirements_version)
    task = copy.deepcopy(current) if current else {'id':task_id,'task_id':task_id,**environment(),'project_id':PROJECT_ID}
    if action in ('start','update'):
        draft = local.get('draft', payload.get('draft_id',''))
        if not draft or draft['task_id'] != task_id or draft['draft_hash'] != payload.get('expected_hash') or draft['status'] != 'draft':
            _error('version_conflict', '请确认当前具体草稿')
        if ((project_id is not None and draft['project_id'] != project_id)
                or (current and current['project_id'] != draft['project_id'])):
            _error('out_of_scope', '草稿或任务不属于当前所选项目')
        projects.require_active(draft['project_id'])
        task['project_id'] = draft['project_id']
        if draft['base_revision'] != expected_revision or draft['base_requirements_version'] != expected_requirements_version:
            _error('version_conflict', '草稿基线已过期')
        if (action == 'start' and current) or (action == 'update' and not current):
            _error('invalid_action', '新建和调整动作不匹配当前任务')
        if current and current['business_status'] == 'closed': _error('task_closed','已结束任务不能重新派发')
        await _verify_refs(draft['payload']['input_refs'])
        task.update(draft['payload'], requirements_version=expected_requirements_version+1, requirements_hash=central.digest(draft['payload']),
                    business_status='paused' if current and current['business_status']=='paused' else 'active',
                    grant_operation_id=operation_id, result_refs=[], review_evidence=None, submission_evidence=None,
                    execution_result=None, result_status='', receipt_status='',
                    decision=None, completion_evidence=None, submission_event_id='', close_reason='', change_reason=draft.get('change_reason',''))
        if current and current['business_status']=='paused':task['resume_status']='active'
    else:
        if not current: _error('not_found','任务不存在')
        state = current['business_status']
        if state == 'closed': _error('task_closed','已结束任务不接受新的状态动作')
        if action == 'pause':
            if state not in ('active','pending_review'): _error('invalid_transition','当前状态不能暂停')
            task.update(business_status='paused', resume_status=state, stop_receipt='待节点确认；中央未远程终止客户端')
        elif action == 'resume':
            projects.require_active(current['project_id'])
            if state != 'paused': _error('invalid_transition','任务未暂停')
            task.update(business_status=task.get('resume_status','active'),stop_receipt='')
        elif action == 'submit':
            if state != 'active': _error('invalid_transition','仅当前已启动要求可提交')
            if task['completion_rule']['kind']=='receipt_recorded':
                _error('invalid_action','此任务以结构化执行结果和独立读回收尾，请提交协作记录')
            refs = await _verify_refs(payload.get('result_refs',[]),task,True)
            if not refs: _error('invalid_artifact','提交需要可独立读取的实际产物')
            task.update(business_status='closed', close_reason='completed', result_refs=refs)
        elif action == 'manual_complete':
            if request_authorization is not None:
                _error('authorization_failed', '人工标记完成只能由管理员在任务页面办理')
            if set(payload) - {'note'} or ('note' in payload and not isinstance(payload['note'], str)):
                _error('invalid_fields', '完成备注须为文字')
            task.update(business_status='closed', close_reason='manual_completed',
                        close_note=payload.get('note', '').strip(),
                        manual_completion={'actor_id':user['id'], 'at':db.utc_now(), 'source':'authenticated_panel'},
                        stop_receipt='本地结束情况待节点确认')
        elif action == 'cancel':
            if not str(payload.get('reason') or '').strip(): _error('invalid_action','取消需要说明原因')
            reason=payload.get('close_reason','cancelled')
            if reason not in ('cancelled','superseded','replaced'):_error('invalid_action','取消原因只能为取消或被替代')
            task.update(business_status='closed',close_reason='superseded' if reason=='replaced' else reason,close_note=payload['reason'],stop_receipt='本地停止情况待节点确认')
    for field in ('quality_status','decision','review_evidence'):
        task.pop(field,None)
    task.update(record_revision=(current['record_revision'] if current else 0)+1,updated_at=db.utc_now())
    event = _event(task, operation_id + ':event', 'task_change', action + '：' + str(payload.get('reason') or task.get('change_reason') or ''), _principal(user), refs=task.get('result_refs'))
    if request_authorization is not None:
        event['origin'] = 'central_user_request'
    op.update(planned_task=task, planned_event=event)
    central.put('operation',operation_id,op)
    binding = dict(actor=user,project_id=task['project_id'],target_kind='phase3_task',target_id=task_id,
        draft_hash=central.digest(request),base={'record_revision':expected_revision,'requirements_version':expected_requirements_version},input_fields=request)
    if request_authorization is not None:
        ops.bind_user_request(op, request_authorization=request_authorization, **binding)
    else:
        ops.bind_confirmation(op, **binding)
    async def work():
        if action in ('start', 'update', 'resume'):
            projects.require_active(task['project_id'])
            if any(node not in _node_ids(task['project_id']) for node in task['executor_node_ids']) and task.get('dispatch_source') != 'peer':
                _error('authorization_failed', '参与节点已变化，请重新确认任务')
        live = await _read_task(task_id,False)
        _check_expected(live,expected_revision,expected_requirements_version)
        if action in ('start','update'):
            latest = local.get('draft', payload.get('draft_id',''))
            if not latest or latest.get('status') != 'draft' or latest.get('draft_hash') != payload.get('expected_hash'):
                _error('version_conflict', '草稿已变化或删除，未发布')
        if live and any(live.get(field) is not None for field in ('quality_status','decision','review_evidence')):
            local.immutable('task_quality_history',task_id+'@'+str(live['record_revision']), {'task':live})
        result = await _commit_task(task,event,operation_id,lambda:ops.mark_dispatched(operation_id))
        if action in ('start','update'):
            local.put('grant',task_id+'@'+str(task['requirements_version']),{'operation_id':operation_id,'requirements_hash':task['requirements_hash'],'owner_id':user['id']})
            draft['status']='published';local.put('draft',draft['id'],draft)
        return result
    result=await ops.run_write(operation_id,WRITE_LOCK,work,reconcile=lambda:_reconcile_task(operation_id))
    return await _presentation(result,operation_id,action)


async def _grant(task_id, version, actor, *, allow_ended=False):
    node = _node(actor)
    if not isinstance(task_id,str) or not task_id or type(version) is not int or version < 1:
        _error('authorization_failed','执行授权须绑定任务标识和明确的正整数要求版本')
    task = await _read_task(task_id)
    if allow_ended and task['requirements_version'] != version:
        previous = next((v for v in local.history(task_id) if v['requirements_version'] == version), None)
        if previous:
            task = {**task, **previous, 'requirements_hash':central.digest(_requirements(previous))}
    if task['requirements_version'] != version or node['node_id'] not in task['executor_node_ids']:
        _error('authorization_failed','节点未获当前任务版本授权')
    if not allow_ended and task['business_status'] != 'active':
        _error('task_not_active','任务已暂停、待迁移或结束，不再派发工作')
    if not allow_ended:
        projects.require_active(task['project_id'])
        if node['node_id'] not in _node_ids(task['project_id']) and task.get('dispatch_source') != 'peer':
            _error('authorization_failed', '节点已不在此项目参与范围')
        plan=rules.execution_plan(task,node['node_id'])
        if plan['missing_conditions']:
            _error('invalid_task','任务缺少执行条件：'+', '.join(plan['missing_conditions']))
    grant = local.get('grant',task_id+'@'+str(version))
    if not grant or grant['requirements_hash'] != task['requirements_hash']:
        _error('authorization_failed','任务缺少可核对的事前授权')
    return task,grant


async def _node_write(operation_id, request, actor, work, reconcile):
    """Same lock/strong task ownership as phase 1; authority is a task grant."""
    from server.central_service import WRITE_LOCK
    principal = _principal(_node(actor))
    op = central.operation(operation_id,'phase3_task_execution',{'actor':principal,'request':request})
    ops._accepting_writes()
    if operation_id in ops.ACTIVE_WRITES: _error('write_in_progress','原操作仍在执行')
    if op['status']=='succeeded': return op['result']
    if op['status']=='conflict': _error('operation_conflict','原操作与当前目标冲突')
    if WRITE_LOCK.locked() or ops.ACTIVE_WRITES: _error('write_busy','另一写入尚未结束')
    async def owned():
        if WRITE_LOCK.locked(): _error('write_busy','写入通道当前忙')
        async with WRITE_LOCK:
            current=central.get('operation',operation_id)
            uncertain=bool(current.get('dispatched_at') or current['status']=='uncertain')
            task_project = (local.get('task_cache', request.get('task_id', '')) or {}).get('project_id', PROJECT_ID)
            current.update(project_id=request.get('project_id', task_project),**environment(),dispatch_protocol=1,status='running',
                           authority_kind='task_execution_grant',actor=principal)
            current.setdefault('attempts',[]).append({'mode':'reconcile' if uncertain else 'write','started_at':db.utc_now()})
            central.put('operation',operation_id,current)
            def mark(task,grant):
                item=central.get('operation',operation_id)
                item.update(dispatched_at=item.get('dispatched_at') or db.utc_now(),grant_operation_id=grant['operation_id'],
                    task_id=task['task_id'],project_id=task.get('project_id', request.get('project_id', PROJECT_ID)),
                    requirements_version=task['requirements_version'],requirements_hash=task['requirements_hash'])
                if not task['task_id']:item['authority_kind']='registered_node_report'
                central.put('operation',operation_id,item)
            try:
                if uncertain:
                    receipt=await reconcile()
                    if receipt.get('status')!='verified': _error('write_uncertain','原操作尚不能完整读回，未重发')
                    result=receipt['result']
                else: result=await work(mark)
                central.finish_operation(central.get('operation',operation_id),result)
                return result
            except BaseException as exc:
                latest=central.get('operation',operation_id)
                if latest.get('status')!='succeeded':
                    latest.update(status='uncertain' if latest.get('dispatched_at') else 'failed',error_code=getattr(exc,'code','interrupted'))
                    central.put('operation',operation_id,latest)
                raise
    task=asyncio.create_task(owned());ops.ACTIVE_WRITES[operation_id]=task
    def done(t):
        if ops.ACTIVE_WRITES.get(operation_id) is t: ops.ACTIVE_WRITES.pop(operation_id,None)
        if not t.cancelled():t.exception()
    task.add_done_callback(done)
    return await asyncio.shield(task)


def _ingest_event(raw,tasks):
    if raw.get('event_type') in ('node_registry_snapshot','peer_capability_ack','peer_enrollment_request'):
        from server import peer_tasks
        return peer_tasks.ingest_system_event(raw,tasks=tasks)
    _event_shape(raw)
    if local.get('archived_event', raw['id']):
        _error('archived_event','已归档记录仅用于追查历史，不能作为新的执行依据')
    proof=local.get('trusted_event',raw['id'])
    trusted=bool(proof and proof['hash']==central.digest(backends.payload(raw)))
    if raw['event_type']=='task_change' and not trusted:
        _error('authorization_failed','外部自报任务变更或人工批准不能生效')
    node=raw.get('claimed_node_id','')
    if not trusted and node not in _node_ids(): _error('invalid_event','请声明已登记的执行节点')
    if not trusted and not raw.get('task_refs') and node not in _node_ids(raw['project_id']):
        _error('authorization_failed', '节点未参与此项目')
    late=False
    for ref in raw.get('task_refs',[]):
        task=tasks.get(ref.get('task_id')) or local.get('task_cache',ref.get('task_id',''))
        versions=local.history(ref.get('task_id',''))
        if not task or not any(v['requirements_version']==ref.get('requirements_version') for v in versions):
            _error('invalid_event','关联任务或要求版本不存在')
        if task['project_id'] != raw['project_id']:
            _error('out_of_scope', '协作记录不能跨项目引用任务')
        version = next(v for v in versions if v['requirements_version']==ref['requirements_version'])
        if not trusted and node not in version['executor_node_ids']: _error('authorization_failed','事件节点不在相应任务版本分配范围')
        if not trusted and task.get('dispatch_source')=='peer':
            from server import peer_tasks
            peer_tasks.validate_result_origin(raw,task)
        late |= task['requirements_version']!=ref['requirements_version'] or task['business_status'] in ('paused','closed')
    if raw.get('correction_of'):
        original=next((e for e in local.events() if e['id']==raw['correction_of']),None)
        if not original or original.get('claimed_node_id')!=node or original.get('project_id')!=raw['project_id']: _error('authorization_failed','更正只能指向本节点在当前项目的原记录')
    event={**raw,'verified_actor':raw.get('verified_actor') if trusted else {'kind':'shared_feishu_account','verified_node':False,'identity_mode':'shared_account_claim' if _FORMAL else 'role_test'},
           'evidence_status':({'panel':'authenticated_panel_operation','central_user_request':'authenticated_user_request_operation'}.get(raw.get('origin'),'task_execution_grant')) if trusted else ('节点归属为自报；飞书账号共用，产物另行读回' if _FORMAL else '节点归属为自报；同账号测试，产物另行读回'),
           'late_or_inactive':late,'trusted_decision':trusted and raw.get('origin')=='panel'}
    return local.index_event(event,backends.payload(raw))


async def submit_event(payload,actor,operation_id):
    node=_node(actor)
    if not isinstance(payload,dict):_error('invalid_event','事件必须为对象')
    fields=copy.deepcopy(payload)
    references=fields.get('task_refs') or []
    if (not isinstance(references, list) or any(not isinstance(ref, dict) or not ref.get('task_id') for ref in references)):
        _error('invalid_event', '任务引用必须包含任务标识')
    referenced_task = await _read_task(references[0]['task_id']) if references else None
    event_project_id = (referenced_task or {}).get('project_id', fields.get('project_id', PROJECT_ID))
    if fields.get('project_id', event_project_id) != event_project_id:
        _error('out_of_scope', '回执项目与任务不匹配')
    if not references and node['node_id'] not in _node_ids(event_project_id):
        _error('authorization_failed', '节点未参与此项目')
    fields.setdefault('id','event_p3_'+central.digest(operation_id)[:24])
    fields.update(**environment(),project_id=event_project_id,claimed_node_id=node['node_id'],identity_mode=node['identity_mode'],operation_id=operation_id)
    fields.setdefault('task_refs',[]);fields.setdefault('artifact_refs',[]);fields.setdefault('body','');fields.setdefault('origin','node_report')
    if local.get('archived_event', fields['id']):
        _error('archived_event','已归档事件标识不能重新用于执行回执')
    _event_shape(fields)
    # Claimed human/admin fields remain untrusted data; no lifecycle action occurs here.
    if fields.get('event_type') not in EVENT_TYPES: _error('invalid_event','节点不能发布任务决定事件')
    references=fields['task_refs']
    if not references and fields['event_type'] != 'project_feedback': _error('invalid_event','执行回执需要授权任务引用；一般项目反馈可不关联任务')
    async def work(mark):
        task,grant=(await _grant(references[0]['task_id'],references[0]['requirements_version'],node,allow_ended=True)) if references else (
            {'task_id':'','requirements_version':0,'requirements_hash':''},{'operation_id':'registered_node_report'})
        for ref in references[1:]:
            linked, _ = await _grant(ref['task_id'],ref['requirements_version'],node,allow_ended=True)
            if linked['project_id'] != event_project_id:
                _error('out_of_scope', '协作记录不能跨项目引用任务')
        mark(task,grant)
        result=await asyncio.to_thread(backends.get_backend().append_event,fields,operation_id)
        matches=[e for e in await asyncio.to_thread(backends.get_backend().list_events) if e.get('id')==fields['id']]
        if not matches or any(backends.payload(e)!=backends.payload(fields) for e in matches): _error('write_uncertain','事件正文未读回')
        _ingest_event(result,{task['task_id']:task})
        return result
    async def reconcile():
        matches=[e for e in await asyncio.to_thread(backends.get_backend().list_events) if e.get('id')==fields['id']]
        if len(matches) and all(backends.payload(e)==backends.payload(fields) for e in matches):return {'status':'verified','result':matches[0]}
        return {'status':'unknown'}
    return await _node_write(operation_id,fields,node,work,reconcile)


async def create_document(payload,actor,operation_id):
    node=_node(actor)
    if not payload.get('title') or not isinstance(payload.get('content'),str) or not payload['content'].strip(): _error('invalid_document','产物需要标题和正文')
    async def complete(mark=None):
        task,grant=await _grant(payload['task_id'],payload['requirements_version'],node,allow_ended=mark is None)
        if payload.get('zone',task['output_zone'])!=task['output_zone']: _error('out_of_scope','产物目录不在任务授权范围')
        if mark: mark(task,grant)
        if mark:
            kwargs = {'project_id': task['project_id']} if task['project_id'] != PROJECT_ID else {}
            document=await asyncio.to_thread(backends.get_backend().create_document,task['output_zone'],payload['title'],payload['content'],operation_id+':document', **kwargs)
        else:
            document=await asyncio.to_thread(backends.get_backend().find_created_document,operation_id+':document')
            if not document:_error('write_uncertain','原文档创建尚不能只读定位；未再次创建')
        readback=await asyncio.to_thread(backends.get_backend().read_document,document['id'])
        if (readback['content']!=document['content'] or readback['content_hash']!=document['content_hash']
                or str(readback['revision'])!=str(document['revision'])): _error('write_uncertain','产物完整正文未独立读回')
        if backends.get_backend().registry().get('mode')=='local_mock' and readback['content']!=payload['content']:
            _error('write_uncertain','产物正文与请求不同')
        # Persist the committed artifact before touching the separate event table.
        local.put('document_cache',document['id'],readback)
        local.immutable('document_grant',document['id'],{'task_id':task['task_id'],'requirements_version':task['requirements_version'],'node_id':node['node_id']})
        _remember_document_version(readback,task,node)
        partial=local.get('partial',operation_id) or {'id':operation_id,'document':readback}
        local.put('partial',operation_id,partial)
        event=partial.get('event') or _event(task,operation_id+':receipt','progress','产物已创建并独立读回。',_principal(node),
                    refs=[{'document_id':readback['id'],'revision':readback['revision'],'content_hash':readback['content_hash']}])
        partial['event']=event;local.put('partial',operation_id,partial)
        matches=[e for e in await asyncio.to_thread(backends.get_backend().list_events) if e.get('id')==event['id']]
        if not matches:
            if partial.get('receipt_dispatched'):_error('write_uncertain','原产物回执已发出但未读回；未重复追加')
            partial['receipt_dispatched']=True;local.put('partial',operation_id,partial)
            await asyncio.to_thread(backends.get_backend().append_event,event,operation_id+':receipt')
            matches=[e for e in await asyncio.to_thread(backends.get_backend().list_events) if e.get('id')==event['id']]
        if not matches or any(backends.payload(e)!=backends.payload(event) for e in matches): _error('write_uncertain','文档已成功，回执尚未完整核对')
        partial['complete']=True;local.put('partial',operation_id,partial)
        _ingest_event(matches[0],{task['task_id']:task})
        return readback
    async def reconcile():return {'status':'verified','result':await complete()}
    return await _node_write(operation_id,payload,node,complete,reconcile)


async def patch_document(document_id,payload,actor,operation_id,expected_revision=None,expected_hash=None):
    node=_node(actor)
    async def complete(mark=None):
        task,grant=await _grant(payload['task_id'],payload['requirements_version'],node,allow_ended=mark is None)
        owner=local.get('document_grant',document_id) or {}
        if not (owner.get('node_id')==node['node_id'] and owner.get('task_id')==task['task_id']) and document_id not in task['editable_document_ids']:
            _error('out_of_scope','任务未授权修改此文档')
        if mark:
            current=await asyncio.to_thread(backends.get_backend().read_document,document_id)
            if str(current['revision'])!=str(expected_revision) or current['content_hash']!=expected_hash:
                _error('version_conflict','文档基准已变化，原文未覆盖')
            if not payload.get('old_text') or current['content'].count(payload['old_text'])!=1:
                _error('patch_conflict','局部修改必须唯一匹配原文')
            local.immutable('patch_expected',operation_id,{'document_id':document_id,
                'content':current['content'].replace(payload['old_text'],payload['new_text'],1),'base_revision':expected_revision})
        if mark:mark(task,grant)
        result=await asyncio.to_thread(backends.get_backend().patch_document,document_id,expected_revision,expected_hash,payload['old_text'],payload['new_text'],operation_id)
        check=await asyncio.to_thread(backends.get_backend().read_document,document_id)
        if check['content_hash']!=result['content_hash'] or str(check['revision'])!=str(result['revision']):_error('write_uncertain','局部修改未独立读回')
        _remember_document_version(check,task,node)
        local.put('document_cache',document_id,check)
        return check
    async def reconcile():
        expected=local.get('patch_expected',operation_id)
        if not expected:return {'status':'unknown'}
        current=await asyncio.to_thread(backends.get_backend().read_document,document_id)
        if current['content']!=expected['content'] or str(current['revision'])==str(expected['base_revision']):
            return {'status':'unknown'}
        task,grant=await _grant(payload['task_id'],payload['requirements_version'],node,allow_ended=True)
        _remember_document_version(current,task,node)
        local.put('document_cache',document_id,current)
        return {'status':'verified','result':current}
    return await _node_write(operation_id,{'document_id':document_id,**payload,'expected_revision':expected_revision,'expected_hash':expected_hash},node,complete,reconcile)


async def _readback_event(event):
    matches = [item for item in await asyncio.to_thread(backends.get_backend().list_events)
               if item.get('id') == event['id']]
    original_hash = local.event_fingerprint(event['id'])
    if (len(matches) != 1 or not original_hash
            or central.digest(backends.payload(matches[0])) != original_hash):
        _error('write_uncertain', '执行结果未从同一协作记录表完整独立读回')
    return {'event_id': event['id'], 'content_hash': central.digest(matches[0]['body']),
            'event_hash': original_hash, 'requirements_version': next(
                (ref['requirements_version'] for ref in event.get('task_refs', [])
                 if ref.get('task_id')), 0)}


async def _derive_submission(event,task):
    if local.get('archived_event',event.get('id','')):return
    if task['business_status']!='active' or event.get('claimed_node_id') not in task['executor_node_ids']:return
    if not any(r.get('task_id')==task['task_id'] and r.get('requirements_version')==task['requirements_version'] for r in event.get('task_refs',[])):return
    if event.get('event_type') not in ('submission','review','check_result'):return
    try:
        result_period = rules.valid_result_period(event, task)
    except rules.RuleViolation as exc:
        _error(exc.code, str(exc))
    prior_result = task.get('execution_result') or {}
    if (result_period and prior_result.get('period_key') == result_period
            and prior_result.get('result_status') == 'completed'):
        if prior_result.get('event_id') == event['id']:
            return
        _error('period_conflict', '同一工作周已有核对完成的回执，保留新记录待核对')
    period_record = local.get('period_receipt', task['task_id'] + '@' + result_period) if result_period else None
    if period_record:
        if period_record.get('event_id') == event['id']:
            return
        _error('period_conflict', '同一工作周已有核对完成的回执，保留新记录待核对')
    node=make_node_actor(event['claimed_node_id']);node['verified_origin']='shared_feishu_account_claim'
    opid='phase3-receipt:'+central.digest([event['id'],task['task_id'],task['requirements_version']])[:32]
    async def work(mark):
        live,grant=await _grant(task['task_id'],task['requirements_version'],node)
        kind=rules.completion_kind(live)
        if live.get('completion_rule',{}).get('kind') in ('human','human_review') or live.get('task_type')=='review':
            _error('legacy_migration_required','旧任务完成规则需要先迁移，原成果和回执保留')
        result_status = event.get('result_status')
        if kind == 'receipt_recorded' or result_status in ('partial','blocked'):
            result_status=rules.validate_result(event,live)
            if event.get('review_of') is not None:
                await _verify_refs([rules.review_target(event,live)])
        elif kind == 'source_versions_match':
            if event['event_type']!='check_result' or not live['input_refs']:
                _error('invalid_rule','资料核对任务须提交指定版本的核对结果')
            await _verify_refs(live['input_refs'])
            result_status=result_status or 'completed'
        elif kind == 'artifact_present':
            result_status=result_status or 'completed'
        else:
            _error('invalid_rule','任务完成规则不支持')
        record_proof=await _readback_event(event)
        record_proof['requirements_version']=live['requirements_version']
        registrar=getattr(backends.get_backend(),'register_event_document',None)
        if registrar:
            for reference in event.get('artifact_refs',[]):
                await asyncio.to_thread(registrar,event,live,reference)
        refs=await _verify_refs(event.get('artifact_refs',[]),live,True,event['claimed_node_id'])
        if kind=='artifact_present' and result_status=='completed' and not refs:
            _error('invalid_artifact','提交事件缺少实际可读产物')
        state='closed' if result_status=='completed' and not result_period else 'active'
        changed={**live,'business_status':state,'record_revision':live['record_revision']+1,'result_refs':refs,
                 'submission_event_id':event['id'],'submission_evidence':record_proof,'receipt_status':'verified',
                 'updated_at':db.utc_now(), 'result_status':result_status,
                 'execution_result':{'result_status':result_status,'event_id':event['id'],'summary':_event_summary(event),
                     'requirements_version':live['requirements_version'],'verified_at':db.utc_now(),
                     **({'period_key': result_period} if result_period else {})}}
        for field in ('quality_status','decision','review_evidence'):
            changed.pop(field,None)
        if state=='closed':
            changed.update(close_reason='completed',completion_evidence={
                'kind':kind,'event_id':event['id'],'grant_operation_id':grant['operation_id'],
                'event_hash':record_proof['event_hash'],'requirements_version':live['requirements_version']},
                stop_receipt='本地结束情况待节点确认')
        receipt=_event(changed,opid+':event','task_change',
                       '执行结果已核对并记录。',
                       _principal(node),refs=refs)
        committed = await _commit_task(changed,receipt,opid,lambda:mark(live,grant))
        if result_period and result_status == 'completed':
            local.immutable('period_receipt', task['task_id'] + '@' + result_period,
                            {'task_id': task['task_id'], 'period_key': result_period,
                             'event_id': event['id'], 'event_hash': record_proof['event_hash']})
        return committed
    await _node_write(opid,{'event_id':event['id'],'task_id':task['task_id'],'requirements_version':task['requirements_version']},node,work,lambda:_reconcile_task(opid))


RECEIPT_MIGRATION_TASK_IDS = frozenset(('task_p3_7b4e31512aedc14334b2', 'task_p3_cf53fbf0cd16681b732e'))


async def migrate_receipt_completion(task_id, event_id, actor, operation_id,
                                     expected_revision, expected_requirements_version,
                                     *, fields=None, expected_event_hash=None):
    """One-time, explicitly authorized migration of the two configuration tasks.

    Source evidence remains evidence for its original version. This does not
    fabricate a new node submission or open a general human-acceptance bypass.
    """
    from server.central_service import WRITE_LOCK
    user = _admin(actor)
    if task_id not in RECEIPT_MIGRATION_TASK_IDS:
        _error('not_allowed', '此迁移仅适用于已登记的定时配置任务')
    overrides = fields or {}
    if set(overrides) - {'title', 'goal', 'deliverable', 'task_type', 'deliverable_audience', 'completion_rule', 'input_refs'}:
        _error('invalid_fields', '迁移不能修改任务责任、交付位置或执行节点')
    if 'input_refs' in overrides and overrides['input_refs'] != []:
        _error('invalid_fields', '配置任务迁移仅可移除旧接入资料引用，不能替换或增加输入')
    if (overrides.get('task_type', 'action') != 'action'
            or overrides.get('completion_rule', {'kind': 'receipt_recorded'}) != {'kind': 'receipt_recorded'}):
        _error('invalid_fields', '迁移只将配置任务改为执行回执核对')
    request = {'task_id':task_id,'event_id':event_id,'actor_id':user['id'],
               'expected_revision':expected_revision,'expected_requirements_version':expected_requirements_version,
               'fields':overrides,'expected_event_hash':expected_event_hash}
    op = central.operation(operation_id, 'phase3_task:migrate_receipt_completion', request)
    if op.get('authorization'):
        async def replay():
            return await _commit_task(op['planned_task'],op['planned_event'],operation_id,
                                      lambda:ops.mark_dispatched(operation_id))
        return await ops.run_write(operation_id,WRITE_LOCK,replay,reconcile=lambda:_reconcile_task(operation_id))
    current=await _read_task(task_id)
    _check_expected(current,expected_revision,expected_requirements_version)
    proof=current.get('submission_evidence') or {}
    if (current.get('completion_rule') != {'kind':'human'} or current['business_status']!='pending_review'
            or current.get('submission_event_id')!=event_id or proof.get('event_id')!=event_id
            or proof.get('requirements_version')!=expected_requirements_version):
        _error('version_conflict','原配置任务与已核对提交依据不一致，停止迁移')
    events=[item for item in await asyncio.to_thread(backends.get_backend().list_events) if item.get('id')==event_id]
    if len(events)!=1:
        _error('write_uncertain','迁移前必须独立读取原提交记录')
    source=events[0]
    if (source.get('event_type')!='submission' or source.get('artifact_refs')
            or source.get('claimed_node_id') not in current['executor_node_ids']
            or not source.get('body','').strip()
            or not any(ref.get('task_id')==task_id and ref.get('requirements_version')==expected_requirements_version
                       for ref in source.get('task_refs',[]))
            or central.digest(backends.payload(source))!=proof.get('event_hash')
            or central.digest(source['body'])!=proof.get('content_hash')
            or (expected_event_hash and expected_event_hash!=proof.get('event_hash'))):
        _error('write_uncertain','原配置回执正文、节点或版本未独立核对，停止迁移')
    normalized=_normalize_fields({**overrides,'task_type':'action','completion_rule':{'kind':'receipt_recorded'}},current)
    task={**current,**normalized,'requirements_version':expected_requirements_version+1,
          'requirements_hash':central.digest(normalized),'record_revision':expected_revision+1,
          'business_status':'closed','close_reason':'completed','updated_at':db.utc_now(),
          'grant_operation_id':operation_id,'result_status':'completed','receipt_status':'verified',
          'change_reason':'配置任务按已核对的执行回执收尾',
          'execution_result':{'result_status':'completed','event_id':event_id,'summary':'定时配置已完成并核对；实际周期运行单独观察。',
                              'requirements_version':expected_requirements_version,'verified_at':db.utc_now()},
          'completion_evidence':{'kind':'receipt_recorded','event_id':event_id,'event_hash':proof['event_hash'],
                                 'source_requirements_version':expected_requirements_version,
                                 'requirements_version':expected_requirements_version+1,
                                 'migrated':True,'grant_operation_id':operation_id}}
    event=_event(task,operation_id+':event','task_change','已依据原版本的配置回执完成规则迁移并收尾。',_principal(user))
    op.update(planned_task=task,planned_event=event)
    central.put('operation',operation_id,op)
    ops.bind_confirmation(op,actor=user,project_id=PROJECT_ID,target_kind='phase3_task',target_id=task_id,
        draft_hash=central.digest(request),base={'record_revision':expected_revision,'requirements_version':expected_requirements_version},input_fields=request)
    async def work():
        live=await _read_task(task_id)
        _check_expected(live,expected_revision,expected_requirements_version)
        matches=[item for item in await asyncio.to_thread(backends.get_backend().list_events) if item.get('id')==event_id]
        if len(matches)!=1 or central.digest(backends.payload(matches[0]))!=proof['event_hash']:
            _error('write_uncertain','迁移写入前原回执发生变化，未执行')
        return await _commit_task(task,event,operation_id,lambda:ops.mark_dispatched(operation_id))
    return await ops.run_write(operation_id,WRITE_LOCK,work,reconcile=lambda:_reconcile_task(operation_id))


async def refresh(actor,operation_id='',project_id=PROJECT_ID,*,task_records=None,event_records=None,registry=None):
    from server import central_maintenance
    maintenance = central_maintenance.permits(actor,'sync',project_id)
    if not maintenance:_admin(actor)
    projects.get_project(project_id)
    try:
        return await _refresh_project(actor, operation_id, project_id,task_records=task_records,event_records=event_records,registry=registry,maintenance=maintenance)
    except BaseException as exc:
        previous = local.get('state', 'refresh:' + project_id) or (local.get('state', 'refresh') if project_id == PROJECT_ID else {}) or {}
        local.put('state', 'refresh:' + project_id, {**previous, 'complete': False, 'attempted_at': db.utc_now(),
            'error': '同步未完成，保留上次已核对的进展。', 'error_code': getattr(exc, 'code', 'read_failed')})
        raise


async def _refresh_project(actor,operation_id,project_id,*,task_records=None,event_records=None,registry=None,maintenance=False):
    backend=backends.get_backend()
    discovery=getattr(backend,'refresh_registry',None)
    if discovery and registry is None:await asyncio.to_thread(discovery)
    # Complete reads before replacing any projection. No timestamp-only cursor.
    tasks=task_records if task_records is not None else await asyncio.to_thread(backend.list_tasks)
    raw_events=event_records if event_records is not None else await asyncio.to_thread(backend.list_events)
    registry=registry if registry is not None else await asyncio.to_thread(backend.registry)
    # Capability acknowledgements must precede task acceptance in the same scan.
    for system_event in raw_events:
        if isinstance(system_event,dict) and system_event.get('event_type') in ('node_registry_snapshot','peer_capability_ack'):
            try:_ingest_event(system_event,{})
            except (ValueError,KeyError,TypeError) as exc:local.quarantine(system_event,str(exc),'peer_identity')
    issues=0
    def selected_records(rows, source):
        nonlocal issues
        selected=[]
        for raw in rows:
            if isinstance(raw,dict) and raw.get('project_id')==project_id:
                selected.append(raw)
            elif isinstance(raw,dict) and projects.is_registered(raw.get('project_id')):
                continue
            else:
                local.quarantine(raw,'记录缺少可核对的已登记项目归属；原文已隔离，未作为任务或决定处理。',source,scope='workspace')
                issues+=1
        return selected
    tasks=selected_records(tasks,'unscoped_task')
    raw_events=selected_records(raw_events,'unscoped_event')
    from server import peer_tasks
    grouped={}
    for raw in tasks:
        if isinstance(raw,dict) and raw.get('task_id'):grouped.setdefault(raw['task_id'],[]).append(raw)
    selected=[]
    for group in grouped.values():
        try:selected.extend(peer_tasks.select_task_rows(group))
        except (ValueError,KeyError,TypeError) as exc:
            for raw in group:local.quarantine(raw,str(exc),'peer_task_conflict')
            issues+=1
    selected.extend(raw for raw in tasks if not isinstance(raw,dict) or not raw.get('task_id'))
    tasks=selected
    valid=[]
    for raw in tasks:
        try:
            _validate_task(raw)
            local.remember_version(raw['task_id'],raw['requirements_version'],_requirements(raw))
            valid.append(raw)
        except (ValueError,KeyError,TypeError) as exc:
            issues+=1
            local.quarantine(raw,str(exc),'task')
            previous=local.get('task_cache',raw.get('task_id','')) if isinstance(raw,dict) else None
            if previous:valid.append({**previous,'integrity_error':str(exc)})
    observed={raw.get('task_id') for raw in tasks if isinstance(raw,dict) and isinstance(raw.get('task_id'),str)}
    for head in local.items('task_head'):
        if head['task_id'] in observed:continue
        previous=local.get('task_cache',head['task_id'])
        if not previous:
            proof=local.get('task_authority',head['task_id']+'@'+str(head['record_revision'])) or {}
            previous=(central.get('operation',proof.get('operation_id','')) or {}).get('prepared_task')
        if not previous or previous.get('project_id') != project_id:continue
        valid.append({**previous,'integrity_error':'任务从主记录消失；保留上次已核对状态，未重建'})
        issues+=1
        local.quarantine({'task_id':head['task_id'],'record_revision':head['record_revision'],**environment(),
            'project_id':project_id},'任务从主记录消失，停止执行并等待核对','missing_task')
    # A fresh authoritative task can add an exact cross-project source even
    # when the old cache is absent. Read all of those bodies before replacement.
    scoped_registry=projects.resource_scope(project_id,registry,published_tasks=valid)
    documents=[]
    for document in scoped_registry.get('documents',[]):
        identifier=document.get('id') or document.get('object_id')
        documents.append(await asyncio.to_thread(backend.read_document,identifier))
    by_id={t['task_id']:t for t in valid if not t.get('integrity_error')}
    ingested=[]
    # Corrections are evaluated after their originals, independent of remote page order.
    ordered=sorted(raw_events,key=lambda e:bool(e.get('correction_of')) if isinstance(e,dict) else False)
    for raw in ordered:
        if isinstance(raw,dict) and local.get('archived_event',raw.get('id','')):
            continue
        try:ingested.append(_ingest_event(raw,by_id))
        except (ValueError,KeyError,TypeError) as exc:
            issues+=1
            local.quarantine(raw,str(exc),'event')
    refresh_record={'at':db.utc_now(),'attempted_at':db.utc_now(),'operation_id':operation_id,'complete':True,
        'error':'','mode':registry.get('mode','unknown')}
    local.replace_cache(valid,documents,refresh_record,project_id=project_id)
    local.put('state','scope',registry)
    # Valid node-generated setup proofs are completed by the service itself;
    # ordinary task actions and observations cannot grant this capability.
    registration=await peer_tasks.register_enrollments(project_id)
    issues+=registration['issue_count']
    for event in ingested:
        if event.get('event_type') not in ('submission','review','check_result'):continue
        for ref in event.get('task_refs',[]):
            task=by_id.get(ref.get('task_id'))
            if task and not event.get('late_or_inactive'):
                try:
                    await _derive_submission(event,task)
                    by_id[task['task_id']]=await _read_task(task['task_id'])
                except (ValueError,KeyError,TypeError) as exc:
                    issues+=1
                    local.quarantine(event,str(exc),'submission_validation')
    refresh_record.update(issue_count=issues,issue_summary=(str(issues)+' 项记录待核对；有效进展已同步。') if issues else '')
    local.put('state','refresh:'+project_id,refresh_record)
    if project_id==PROJECT_ID:local.put('state','refresh',refresh_record)
    return _cached_workspace(project_id,administrative=True) if maintenance else overview(actor,project_id)


def overview(actor,project_id=PROJECT_ID):
    _admin(actor)
    return _cached_workspace(project_id, administrative=True)


def _cached_workspace(project_id, *, administrative=False):
    """Read a project projection without connectors or impersonating an admin."""
    project = projects.get_project(project_id)
    scope=projects.resource_scope(project_id,local.get('state','scope') or backends.get_backend().registry())
    tasks=[task for task in local.items('task_cache') if task.get('project_id')==project_id]
    events=[event for event in local.events() if event.get('project_id')==project_id]
    for task in tasks:
        linked=[e for e in events if e.get('event_type')!='task_change' and any(r.get('task_id')==task['task_id'] for r in e.get('task_refs',[]))]
        task['execution']={'last_event':linked[-1] if linked else None,'status':'未见执行回执' if not linked else linked[-1]['event_type']}
        presentation=local.get('presentation',task['task_id']+'@'+str(task['requirements_version'])) or {}
        task['summary_stale']=('human' in task.get('dispatch_to',[]) and (
            presentation.get('requirements_version')!=task['requirements_version'] or presentation.get('status')!='verified'))
        task['input_refs_stale']=any((local.get('document_cache',r.get('document_id','')) or {}).get('content_hash',r.get('content_hash'))!=r.get('content_hash')
            or str((local.get('document_cache',r.get('document_id','')) or {}).get('revision',r.get('revision')))!=str(r.get('revision')) for r in task.get('input_refs',[]))
        expected_ack='closed_ack' if task['business_status']=='closed' else 'paused_ack' if task['business_status']=='paused' else ''
        acknowledgements=[e for e in linked if e.get('event_type')==expected_ack and any(r.get('task_id')==task['task_id'] and r.get('requirements_version')==task['requirements_version'] for r in e.get('task_refs',[]))]
        ack_nodes={e.get('claimed_node_id') for e in acknowledgements}
        task['execution']['acknowledged_nodes']=sorted(ack_nodes)
        task['execution']['pending_ack_nodes']=[n for n in task['executor_node_ids'] if expected_ack and n not in ack_nodes]
        if expected_ack and not task['execution']['pending_ack_nodes']:
            task['stop_receipt']='已收到所分配节点的'+('结束' if expected_ack=='closed_ack' else '暂停')+'回执；同账号自报，未远程终止客户端'
    task_ids={task['task_id'] for task in tasks}
    document_ids={item.get('id',item.get('object_id')) for item in scope.get('documents',[]) if isinstance(item,dict)}
    sources=[item for item in central.items('central_project_source') if item.get('project_id')==project_id]
    registered={item.get('id',item.get('object_id')):item for item in scope.get('documents',[]) if isinstance(item,dict)}
    def still_current(doc):
        entry=registered.get(doc['id'],{})
        identities={doc['id'],entry.get('object_id'),entry.get('token'),str(doc.get('source_url','')).rstrip('/').split('/')[-1]}
        observed=[item for item in sources if item['id'] in identities or item.get('source_token') in identities]
        return not any(item.get('available') is False for item in observed)
    documents=[doc for doc in local.items('document_cache') if (doc.get('project_id',PROJECT_ID)==project_id or doc['id'] in document_ids) and still_current(doc)]
    for item in sources:
        if item.get('available') and item.get('type')!='folder':
            documents.append({'id':item['id'],'project_id':project_id,'title':item['name'],
                'revision':item.get('source_revision') or item['version'],'content_hash':item['content_hash'],
                'source_url':item['source_url'],'content_status':item['content_status']})
    return {'project':{**project,'identity_mode':'role_test'},
        'nodes':[node for node in node_registry() if node['node_id'] in project['node_ids']],
        'tasks':tasks,'drafts':[draft for draft in local.items('draft') if draft.get('project_id')==project_id] if administrative else [],
        'events':events,'documents':documents,
        'operations':[o for o in central.items('operation') if str(o.get('action','')).startswith('phase3_') and o.get('project_id',PROJECT_ID)==project_id] if administrative else [],
        'scope':scope,'quarantine':[q for q in local.items('quarantine') if q.get('scope')=='workspace' or not isinstance(q.get('raw'),dict) or q['raw'].get('project_id',project_id)==project_id] if administrative else [],
        'last_refresh':local.get('state','refresh:'+project_id) or (local.get('state','refresh') if project_id==PROJECT_ID else None),
        'presentations':[item for item in local.items('presentation') if item.get('task_id') in task_ids]}


_ACTION_RESULT_SUMMARY = '已按当前要求完成执行，结果已独立核对。'


def _verified_action_result(task):
    return (rules.task_type(task)=='action' and task.get('result_status')=='completed'
            and task.get('receipt_status')=='verified')


def _verified_action_event(event):
    if event.get('event_type') not in ('submission','check_result','review') or not event.get('task_refs'):
        return False
    for ref in event['task_refs']:
        if not isinstance(ref,dict):return False
        task=local.get('task_cache',ref.get('task_id','')) or {}
        evidence=task.get('completion_evidence') or {}
        versions={task.get('requirements_version')}
        if evidence.get('migrated'):versions.add(evidence.get('source_requirements_version'))
        event_ids={task.get('submission_event_id'),(task.get('execution_result') or {}).get('event_id'),evidence.get('event_id')}
        if not _verified_action_result(task) or ref.get('requirements_version') not in versions or event.get('id') not in event_ids:
            return False
    return True


def _event_summary(event):
    """Business facts are a projection; protocol prose remains in source detail."""
    if _verified_action_event(event):
        return _ACTION_RESULT_SUMMARY
    status = {'completed':'节点报告执行完成', 'partial':'节点报告部分完成', 'blocked':'节点报告受阻'}.get(event.get('result_status'))
    label = {'read':'已收到读取回执', 'start':'节点已开始工作', 'started':'节点已开始工作',
             'submission':'已收到提交', 'review':'已收到 Agent 审阅意见', 'blocker':'已收到问题反馈',
             'question':'已收到澄清问题', 'clarification':'已收到澄清记录', 'progress':'已收到进展反馈',
             'correction':'已收到更正', 'project_feedback':'已收到项目反馈',
             'stop_ack':'已收到停止回执', 'task_change':'任务要求或状态已更新',
             'check_result':'已收到核对结果'}.get(event.get('event_type'), '已收到协作记录')
    summary = status or label
    refs = event.get('artifact_refs') or []
    if refs:
        titles = [ref.get('title') for ref in refs if isinstance(ref, dict) and ref.get('title')]
        summary += '；成果：' + ('、'.join(titles) if titles else str(len(refs)) + ' 份已提交成果')
    if event.get('late_or_inactive'):
        summary += '；作为历史记录保留'
    if event.get('result_status') in {'blocked', 'partial'}:
        summary += '；具体原因见协作原文'
    return summary + '。'


def business_event(event):
    keys=('id','event_type','task_refs','claimed_node_id','artifact_refs','recipients','created_at','correction_of',
          'result_status','late_or_inactive','operation_id','sequence','source_url')
    value={**{key:copy.deepcopy(event[key]) for key in keys if key in event},
           'summary':_event_summary(event), 'detail_ref':{'kind':'collaboration_event','id':event.get('id')}}
    if 'task_refs' in value:
        value['task_refs']=[{key:ref[key] for key in ('task_id','requirements_version') if key in ref}
                            for ref in value['task_refs'] if isinstance(ref,dict)]
    if 'artifact_refs' in value:
        value['artifact_refs']=[{key:ref[key] for key in ('document_id','id','revision','content_hash','source_url','title') if key in ref}
                                for ref in value['artifact_refs'] if isinstance(ref,dict)]
    return value


def business_task(task):
    keys=TASK_FIELDS+('id','task_id','project_id','requirements_version','requirements_hash','record_revision',
                     'business_status','result_refs','result_status','receipt_status','close_reason',
                     'close_note','manual_completion','submission_event_id','execution_result','completion_evidence',
                     'submission_evidence','review_evidence','updated_at','summary_stale','input_refs_stale',
                     'stop_receipt','resume_status','change_reason','integrity_error')
    value={key:copy.deepcopy(task[key]) for key in keys if key in task}
    if isinstance(value.get('execution_result'), dict):
        evidence = next((event for event in local.events() if event.get('id') == value['execution_result'].get('event_id')), None)
        value['execution_result']['summary'] = _event_summary(evidence or {'result_status':value['execution_result'].get('result_status')})
    value['owner_name']=(central.user(task.get('owner_id','')) or {}).get('display_name','负责人')
    value['task_type']=rules.task_type(task)
    value['completion_rule']={'kind':rules.completion_kind(task)}
    if value.get('business_status')=='pending_review':value['business_status']='active'
    value.setdefault('deliverable_audience',['admin','project_members'])
    reference_keys=('document_id','id','revision','content_hash','source_url','title')
    for field in ('input_refs','result_refs'):
        if field in value:
            value[field]=[{key:copy.deepcopy(ref[key]) for key in reference_keys if key in ref}
                          for ref in (value[field] or []) if isinstance(ref,dict)]
    nested_keys={
        'execution_result':('result_status','event_id','summary','requirements_version','verified_at'),
        'completion_evidence':('kind','event_id','event_hash','requirements_version','source_requirements_version','migrated','reviewed_ref'),
        'submission_evidence':('event_id','content_hash','event_hash','requirements_version'),
        'review_evidence':('event_id','content_hash','event_hash','requirements_version'),
        'manual_completion':('actor_id','at','source')}
    for field,fields in nested_keys.items():
        if isinstance(value.get(field),dict):
            value[field]={key:value[field][key] for key in fields if key in value[field]}
    proof=value.get('completion_evidence') or {}
    if isinstance(proof.get('reviewed_ref'),dict):
        proof['reviewed_ref']={key:proof['reviewed_ref'][key] for key in reference_keys if key in proof['reviewed_ref']}
    result=value.get('execution_result') or {}
    if result.get('event_id') and local.get('archived_event',result['event_id']):
        result['summary']={'completed':'执行已完成，回执已核对。','partial':'部分完成，详细记录已归档。',
                           'blocked':'执行受阻，详细记录已归档。'}.get(result.get('result_status'),'执行记录已归档。')
    if result and _verified_action_result(task):
        result['summary']=_ACTION_RESULT_SUMMARY
    if task.get('execution'):
        value['execution']={key:copy.deepcopy(item) for key,item in task['execution'].items() if key!='last_event'}
        last=task['execution'].get('last_event')
        archived=bool(last and local.get('archived_event',last.get('id','')))
        value['execution']['last_event']=business_event(last) if last and not archived else None
        if archived:value['execution']['status']=task.get('result_status') or '未见执行回执'
        if task.get('business_status')=='closed' and task.get('close_reason')=='completed':
            value['execution']['pending_ack_nodes']=[]
    if task.get('business_status')=='closed' and task.get('close_reason')=='completed':
        value['stop_receipt']=''
    elif value.get('stop_receipt'):
        pending=(value.get('execution') or {}).get('pending_ack_nodes')
        if pending is None:pending=task.get('executor_node_ids',[])
        value['stop_receipt']='待节点停止回执' if pending else '已收到节点停止回执'
    return value


def business_draft(draft):
    keys=('id','task_id','project_id','base_revision','base_requirements_version','change_reason','created_at','draft_hash','status','deleted_at','deleted_by')
    return {**{key:copy.deepcopy(draft[key]) for key in keys if key in draft},
            'payload':{**{key:copy.deepcopy(draft['payload'][key]) for key in TASK_FIELDS if key in draft.get('payload',{})},
                       'owner_name':(central.user(draft.get('payload',{}).get('owner_id','')) or {}).get('display_name','负责人')}}


def business_workspace(actor,project_id=PROJECT_ID):
    """Business facts; a participating member gets a read-only cache projection."""
    projects.require_read(project_id, actor)
    current = central.user(actor['id'])
    administrative = 'admin' in current.get('roles', [])
    data=_cached_workspace(project_id, administrative=administrative)
    document_keys=('id','title','project_id','revision','content_hash','source_url','folder_id','zone')
    archived={item['id'] for item in local.items('archived_event')}
    registered={item.get('id',item.get('object_id')) for item in data['scope'].get('documents',[])
                if isinstance(item,dict) and not item.get('retired')}
    result = {'project':{key:data['project'][key] for key in ('id','name','owner_id','description','status','is_default','revision','node_ids','folder_source','resolved_folder_url') if key in data['project']},
            'nodes':[{key:node[key] for key in ('node_id','public_id','label','display_name','member_id') if key in node}
                     for node in data['nodes']],
            'tasks':[business_task(task) for task in data['tasks']],
            'drafts':[business_draft(draft) for draft in data['drafts'] if draft.get('status')!='deleted'],
            'deleted_drafts':[business_draft(draft) for draft in data['drafts'] if draft.get('status')=='deleted'],
            'events':[business_event(event) for event in data['events'] if event['id'] not in archived
                      and event.get('event_type') not in {'node_registry_snapshot','peer_capability_ack','peer_enrollment_request'}],
            'documents':[{key:doc[key] for key in document_keys if key in doc} for doc in data['documents']
                         if doc['id'] in registered or any(item['id']==doc['id'] and item.get('available')
                             for item in central.items('central_project_source') if item.get('project_id')==project_id)],
            'scope':{**{key:data['scope'][key] for key in ('task_table_url','event_table_url') if key in data['scope']},
                     'zones':{name:{key:zone[key] for key in ('folder_id','source_url','name') if key in zone}
                              for name,zone in data['scope'].get('zones',{}).items() if isinstance(zone,dict)}},
            'last_refresh':{key:(data['last_refresh'] or {}).get(key) for key in ('at','complete','attempted_at','error','error_code','issue_count','issue_summary')}}
    if not administrative:
        result['read_only'] = True
        # Members see business outcomes, not the administrator's confirmation
        # hashes, draft contents, or shared-table management entry points.
        for task in result['tasks']:
            for key in ('decision','completion_evidence','submission_evidence','review_evidence'):
                task.pop(key, None)
        result['scope'] = {'zones': {'project': result['scope']['zones'].get('project', {})}}
    return result


def readable_task(task_id, actor):
    task = local.get('task_cache', task_id)
    if not task:
        _error('not_found', '请先同步当前任务')
    projects.require_read(task.get('project_id'), actor)
    return task


def list_tasks(actor,project_id=PROJECT_ID):return overview(actor,project_id)['tasks']
def get_task(task_id,actor):
    _admin(actor)
    value=local.get('task_cache',task_id)
    if not value:_error('not_found','请先同步当前任务')
    return value

def task_history(task_id,actor):
    task=get_task(task_id,actor)
    return {'task':task,'versions':local.history(task_id),'events':[e for e in local.events() if any(r.get('task_id')==task_id for r in e.get('task_refs',[]))]}

def list_events(actor,cursor=0,limit=1000):
    _admin(actor)
    return local.events(int(cursor or 0),limit)

def get_document(document_id,actor):
    _admin(actor)
    value=local.get('document_cache',document_id)
    if not value:_error('not_found','文档尚未完整读取')
    return value

def current_project_document(document_id,project_id):
    """Current synchronized project bodies, scoped before any content is returned."""
    sources=[item for item in central.items('central_project_source') if item.get('project_id')==project_id]
    registry=projects.resource_scope(project_id,backends.get_backend().registry())
    entry=next((item for item in registry.get('documents',[]) if item.get('id')==document_id),{})
    tokens={document_id,entry.get('object_id'),entry.get('token'),str(entry.get('source_url','')).rstrip('/').split('/')[-1]}
    item=next((item for item in sources if item['id'] in tokens or item.get('source_token') in tokens),None)
    if not item:return None
    if not item.get('available'):_error('source_removed','资料已移出项目当前目录；历史任务引用仍然保留')
    if item.get('content_status')!='full_text':_error('body_unavailable','该来源仅有附件信息，正文未解析')
    record=central.get('central_project_content',central.digest([item['id'],item['version']]))
    if not record:_error('incomplete_source','资料正文缓存未完整保存')
    return {'id':item['id'],'project_id':project_id,'title':item['name'],'content':record['content'],
        'revision':item.get('source_revision') or item['version'],'content_hash':item['content_hash'],
        'source_url':item['source_url'],'observed_at':item.get('synced_at'),'source':'complete_project_snapshot'}


async def read_document(document_id,actor,project_id=None):
    _admin(actor)
    if project_id:
        projects.require_read(project_id,actor)
        cached=current_project_document(document_id,project_id)
        if cached:return cached
    value=await asyncio.to_thread(backends.get_backend().read_document,document_id)
    local.put('document_cache',value['id'],value)
    return value

def public_onboard(node_id):
    if node_id not in NODE_IDS:_error('not_found','测试节点未登记')
    registry=local.get('state','scope') or backends.get_backend().registry()
    links={}
    for name,key in (('任务登记','task_table_url'),('协作记录','event_table_url')):
        url=registry.get(key,'')
        parsed=urlparse(url)
        if parsed.scheme=='https' and parsed.hostname and parsed.hostname.endswith('.feishu.cn'):links[name]=url
    documents=registry.get('documents',[])
    current_guide=next((doc for doc in documents if isinstance(doc,dict) and 'Agent执行说明 v2 统一协作记录' in str(doc.get('title',''))),None)
    if current_guide:
        links['当前 Agent 执行说明 v2']=current_guide['source_url']
    for doc in documents:
        if isinstance(doc,dict):
            if current_guide and 'Agent执行说明' in str(doc.get('title','')):
                continue
            url=doc.get('source_url','');parsed=urlparse(url)
            if parsed.scheme=='https' and parsed.hostname and parsed.hostname.endswith('.feishu.cn'):links[doc.get('title') or doc['id']]=url
    return {'node_id':node_id,'identity_mode':'role_test','links':links,'rules':[
        '仅通过本轮登记的飞书新对象读取任务、资料和提交协作记录；不得使用 localhost 或共享本机路径作为通信通道。',
        '只执行分配给本节点的当前有效要求；关键操作前和提交前重读任务版本、暂停与关闭状态。',
        '两个节点所有进展、问题、Review、更正和执行回执的完整正文只追加到同一张协作记录表；长篇制作成果可建文档，在表内引用确切版本并独立读回。',
        '共用飞书账号，本节点归属是测试自报；不得修改任务目标、状态或批准字段，不把自报人工同意当作真实确认。',
        '旧版、暂停或关闭后的结果保留原版本并说明情况，不自动重新执行；不得启动 Runner 或访问正式资源。只有 Panel 已开始、归属本节点且当前有效的任务明确要求配置时，才可在该任务范围内设置本实验客户端原生定时任务；首次接入本身不授权设置。']}


workspace=overview
history=task_history
task_action=apply_task_action
sync=refresh


async def migrate_legacy_quality_task(task_id, actor, operation_id, expected_revision, expected_requirements_version):
    """Explicit, versioned business migration; does not infer professional approval."""
    from server.central_service import WRITE_LOCK
    user=_admin(actor)
    request={'task_id':task_id,'actor_id':user['id'],'expected_revision':expected_revision,
             'expected_requirements_version':expected_requirements_version}
    op=central.operation(operation_id,'phase3_task:migrate_completion',request)
    if op.get('authorization'):
        async def replay():
            return await _commit_task(op['planned_task'],op['planned_event'],operation_id,
                                      lambda:ops.mark_dispatched(operation_id))
        return await ops.run_write(operation_id,WRITE_LOCK,replay,reconcile=lambda:_reconcile_task(operation_id))
    current=await _read_task(task_id)
    _check_expected(current,expected_revision,expected_requirements_version)
    legacy_kind=current.get('completion_rule',{}).get('kind')
    if (legacy_kind not in ('human','human_review') and current.get('task_type')!='review'
            and current.get('business_status')!='pending_review'):
        _error('invalid_transition','任务已采用当前完成规则，无需迁移')
    if current['business_status']=='closed':
        _error('task_closed','已结束任务保留原始历史，不迁移')
    fields=_normalize_fields({'task_type':rules.task_type(current),
                             'completion_rule':{'kind':rules.completion_kind(current)}},current,current['project_id'])
    task={**current,**fields,'requirements_version':expected_requirements_version+1,
          'record_revision':expected_revision+1,'requirements_hash':central.digest(fields),
          'business_status':'paused' if current['business_status']=='paused' else 'active',
          'grant_operation_id':operation_id,'change_reason':'移除质量审阅门槛；工作目标、责任、旧成果和回执保持不变',
          'updated_at':db.utc_now(),'workflow_version':2}
    for field in ('quality_status','decision','review_evidence'):
        task.pop(field,None)
    if task.get('resume_status')=='pending_review':task['resume_status']='active'
    proof=current.get('submission_evidence') or current.get('review_evidence') or {}
    source_id=proof.get('event_id') or (current.get('execution_result') or {}).get('event_id')
    if source_id:
        matches=[e for e in await asyncio.to_thread(backends.get_backend().list_events) if e.get('id')==source_id]
        if len(matches)==1:
            event=matches[0]
            valid=(event.get('result_status')=='completed' and event.get('claimed_node_id') in current['executor_node_ids']
                   and any(ref.get('task_id')==task_id and ref.get('requirements_version')==expected_requirements_version for ref in event.get('task_refs',[]))
                   and (not proof.get('event_hash') or proof['event_hash']==central.digest(backends.payload(event))))
            if valid:
                await _verify_refs(event.get('artifact_refs',[]),current,True,event['claimed_node_id'])
                task.update(result_status='completed',receipt_status='verified',
                    completion_evidence={'kind':'receipt_recorded','event_id':source_id,
                        'event_hash':central.digest(backends.payload(event)),'migrated':True,
                        'source_requirements_version':expected_requirements_version,
                        'requirements_version':task['requirements_version'],'grant_operation_id':operation_id})
                if not task.get('periodicity'):
                    task.update(business_status='closed',close_reason='completed')
    event=_event(task,operation_id+':event','task_change',task['change_reason'],_principal(user))
    op.update(planned_task=task,planned_event=event)
    central.put('operation',operation_id,op)
    ops.bind_confirmation(op,actor=user,project_id=current['project_id'],target_kind='phase3_task',target_id=task_id,
        draft_hash=central.digest(request),base={'record_revision':expected_revision,'requirements_version':expected_requirements_version},input_fields=request)
    async def work():
        live=await _read_task(task_id)
        _check_expected(live,expected_revision,expected_requirements_version)
        local.immutable('task_workflow_history',task_id+'@'+str(expected_revision),
                        {'task':current,'migration_operation_id':operation_id,'at':db.utc_now()})
        return await _commit_task(task,event,operation_id,lambda:ops.mark_dispatched(operation_id))
    return await ops.run_write(operation_id,WRITE_LOCK,work,reconcile=lambda:_reconcile_task(operation_id))
