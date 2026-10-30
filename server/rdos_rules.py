"""RDOS business rules shared by the central service and the execution Skill.

Pure standard-library code: importing this module never opens files, connects to
services or grants authority. Inputs must be authoritative task snapshots.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

RULES_VERSION = '1.1.0'
TASK_TYPES = ('action', 'deliverable')
RESULT_STATUSES = ('completed', 'partial', 'blocked')
AUDIENCES = ('admin', 'project_members', 'researcher')
COMPLETION_KINDS = ('receipt_recorded', 'artifact_present', 'source_versions_match')
LEGACY_TASK_FIELDS = ('title', 'goal', 'owner_id', 'executor_node_ids', 'input_refs', 'deliverable',
                     'output_zone', 'dispatch_to', 'return_to', 'completion_rule', 'due_at', 'editable_document_ids')
BUSINESS_TASK_FIELDS = ('task_type', 'deliverable_audience', 'periodicity')


def period_key(task, now=None):
    """Return the Beijing business week for an explicitly periodic task."""
    spec = task.get('periodicity')
    if spec is None:
        return None
    if (not isinstance(spec, dict) or set(spec) != {'unit', 'timezone', 'deadline_weekday', 'deadline_time'}
            or spec['unit'] != 'week' or spec['timezone'] != 'Asia/Shanghai'
            or type(spec['deadline_weekday']) is not int or spec['deadline_weekday'] not in range(7)
            or not isinstance(spec['deadline_time'], str) or len(spec['deadline_time']) != 5
            or spec['deadline_time'][2] != ':' or not spec['deadline_time'][:2].isdigit()
            or not spec['deadline_time'][3:].isdigit()
            or int(spec['deadline_time'][:2]) > 23 or int(spec['deadline_time'][3:]) > 59):
        raise RuleViolation('invalid_periodicity', '周期任务须指定北京时间、星期与截止时刻')
    current = now or datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise RuleViolation('invalid_periodicity', '周期计算需要带时区的观察时间')
    current = current.astimezone(timezone(timedelta(hours=8)))
    iso = current.isocalendar()
    return f'{iso.year}-W{iso.week:02d}'


def valid_result_period(event, task, now=None):
    """Late delivery may settle the previous week, never an arbitrary period."""
    current = period_key(task, now)
    if current is None:
        if 'period_key' in event:
            raise RuleViolation('invalid_periodicity', '一次性任务不能冒充周期结果')
        return None
    previous = period_key(task, (now or datetime.now(timezone.utc)) - timedelta(days=7))
    if event.get('period_key') not in {current, previous}:
        raise RuleViolation('invalid_periodicity', '周期回执与当前或上一工作周不匹配')
    return event['period_key']


class RuleViolation(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def digest(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


def requirements(task):
    """Do not change the fingerprint of records written before business v1."""
    fields = LEGACY_TASK_FIELDS + tuple(key for key in BUSINESS_TASK_FIELDS if key in task)
    return {key: task.get(key) for key in fields}


def task_type(task):
    # Historical Review records remain readable as ordinary work.
    value = task.get('task_type') or 'action'
    return 'action' if value == 'review' else value


def completion_kind(task):
    rule=task.get('completion_rule')
    value = rule.get('kind') if isinstance(rule,dict) else None
    return 'receipt_recorded' if value in ('human', 'human_review') else value


def review_target(event,task):
    refs=task.get('input_refs') or []
    target=event.get('review_of')
    if target is None and len(refs)==1:target=refs[0]
    if target is None or target not in refs:
        raise RuleViolation('invalid_result','Review 必须明确引用任务指定的被审阅成果版本')
    return target


def validate_result(event, task):
    """Validate business completion facts; prose cannot impersonate a result."""
    status = event.get('result_status')
    if status not in RESULT_STATUSES:
        raise RuleViolation('invalid_result', '执行结果须明确为 completed、partial 或 blocked')
    if not isinstance(event.get('body'), str) or not event['body'].strip():
        raise RuleViolation('invalid_result', '执行结果必须包含实际完成内容或阻塞说明')
    kind = task_type(task)
    expected = ('submission', 'check_result', 'review') if kind == 'action' else ('submission',)
    if event.get('event_type') not in expected:
        raise RuleViolation('invalid_result', '结果类型与任务类别不匹配')
    if status == 'completed' and kind == 'deliverable' and not event.get('artifact_refs'):
        raise RuleViolation('invalid_result', '成果任务完成时必须引用确切产物版本')
    if event.get('review_of') is not None:
        review_target(event,task)
    return status


def execution_plan(task, node_id, checkpoint=None, *, now=None):
    """Describe the allowed next business step, including stop and retry paths.

    A checkpoint records this task/version's completed steps and uncertain
    operations. It never changes task authority or permits work on a stale copy.
    """
    checkpoint = checkpoint or {}
    period = period_key(task, now)
    missing = []
    for key in ('task_id', 'goal', 'owner_id', 'deliverable'):
        if not task.get(key): missing.append(key)
    if type(task.get('requirements_version')) is not int or task['requirements_version'] < 1:
        missing.append('requirements_version')
    if type(task.get('record_revision')) is not int or task['record_revision'] < 1:
        missing.append('record_revision')
    if task_type(task) not in TASK_TYPES: missing.append('task_type')
    if completion_kind(task) not in COMPLETION_KINDS: missing.append('completion_rule')
    if completion_kind(task)=='source_versions_match' and not task.get('input_refs'):missing.append('source_versions_input')
    nodes=task.get('executor_node_ids')
    if not isinstance(nodes,list):missing.append('executor_node_ids')
    assigned = isinstance(nodes,list) and node_id in nodes
    same_task = checkpoint.get('task_id') == task.get('task_id')
    same_version = (same_task and checkpoint.get('requirements_version') == task.get('requirements_version')
                    and (period is None or checkpoint.get('period_key') == period))
    changed = same_task and not same_version
    state = task.get('business_status')
    steps = checkpoint.get('completed_steps', []) if same_version else []
    uncertain = checkpoint.get('uncertain_operations', []) if same_task else []
    allowed = []
    reason = 'ready'
    if not assigned:
        reason = 'not_assigned'
    elif missing:
        reason = 'missing_conditions'
        allowed = ['report_blocker']
    elif state in ('paused', 'closed'):
        reason = 'paused' if state == 'paused' else 'closed'
        allowed = ['stop_new_work', 'report_paused_ack' if state == 'paused' else 'report_closed_ack',
                   'archive_late_result', 'correct_previous_record']
    elif state == 'pending_review':
        reason = 'awaiting_legacy_migration'
        allowed = ['read_status', 'correct_previous_record']
    elif state != 'active':
        reason = 'not_active'
    elif uncertain:
        reason = 'reconcile_before_retry'
        allowed = ['verify_existing_operation', 'report_blocker']
    elif 'submission_verified' in steps:
        reason = 'already_submitted'
        allowed = ['read_status', 'correct_previous_record']
    elif 'artifact_verified' in steps:
        reason = 'submit_existing_result'
        allowed = ['recheck_requirements', 'submit_existing_result', 'verify_receipt', 'correct_previous_record',
                   'revise_existing_result', 'report_progress', 'report_blocker']
    else:
        allowed = ['recheck_requirements', 'read_inputs', 'perform_work', 'submit_result', 'verify_receipt',
                   'report_progress', 'report_blocker', 'correct_previous_record']
    return {'rules_version': RULES_VERSION, 'task_id': task.get('task_id'),
            'period_key': period,
            'requirements_version': task.get('requirements_version'), 'record_revision': task.get('record_revision'),
            'task_type': task_type(task), 'assigned': assigned, 'reason': reason, 'allowed_actions': allowed,
            'missing_conditions': missing, 'changed_requirements': changed,
            'reusable_steps': steps, 'uncertain_operations': uncertain,
            'old_results_policy': 'retain_and_reassess_against_current_requirements' if changed else 'retain',
            'deliverable_audience': task.get('deliverable_audience', ['admin', 'project_members'])}


def evaluate_task(task, node_id, known=None):
    """Compatibility name for callers using a known business checkpoint."""
    return execution_plan(task, node_id, known)


def validate_peer_task(task, registry, issuer, *, verify_ready=True):
    """Same task-level peer scope rules run centrally and inside the Skill."""
    if (not isinstance(task, dict) or task.get('dispatch_source') != 'peer'
            or task.get('protocol_version') != 2 or task.get('workspace_id') != registry.get('workspace_id')):
        raise RuleViolation('invalid_peer_task', '互派任务工作区或协议无效')
    source = next((n for n in registry.get('nodes', []) if n.get('node_id') == issuer and n.get('enabled')), None)
    receivers = task.get('executor_node_ids')
    if not isinstance(receivers,list) or any(not isinstance(item,str) or not item for item in receivers):
        raise RuleViolation('invalid_peer_task','执行节点须为明确的节点标识列表')
    for key in ('task_id','id','operation_id','title','goal','deliverable','owner_id','peer_binding_id'):
        if not isinstance(task.get(key),str) or not task[key].strip():
            raise RuleViolation('missing_conditions','互派缺少必要业务字段')
    if task['task_id']!=task['id']:
        raise RuleViolation('invalid_peer_task','任务标识不一致')
    for key in ('dispatch_to','return_to'):
        if not isinstance(task.get(key),list) or not task[key] or any(value not in ('agent','human') for value in task[key]):
            raise RuleViolation('invalid_peer_task','下发与回传对象无效')
    audience=task.get('deliverable_audience')
    if not isinstance(audience,list) or not audience or any(item not in AUDIENCES for item in audience):
        raise RuleViolation('invalid_peer_task','交付受众无效')
    if task.get('task_type') not in TASK_TYPES or task.get('output_zone') not in ('project','agent') or not isinstance(task.get('due_at'),str):
        raise RuleViolation('invalid_peer_task','任务类别、交付位置或截止时间无效')
    if task.get('requirements_hash')!=digest(requirements(task)):
        raise RuleViolation('task_tampered','任务要求指纹不一致')
    permitted=set(LEGACY_TASK_FIELDS+BUSINESS_TASK_FIELDS+('id','task_id','project_id','protocol_version','workspace_id','requirements_hash','requirements_version','record_revision','operation_id','business_status','dispatch_source','peer_issuer_node_id','peer_binding_id','peer_task_scope'))
    if set(task)-permitted:
        raise RuleViolation('invalid_peer_task','节点不能提交中央状态或批准字段')
    if (not source or (verify_ready and not source.get('ready')) or task.get('peer_issuer_node_id') != issuer
            or not isinstance(receivers, list) or len(receivers) != 1 or issuer in receivers):
        raise RuleViolation('peer_not_ready', '发起节点或接收节点未完成互派接入')
    target = next((n for n in registry.get('nodes', []) if n.get('node_id') == receivers[0] and n.get('enabled')), None)
    if not target or (verify_ready and not target.get('ready')):
        raise RuleViolation('peer_not_ready', '接收节点尚未完成互派接入')
    if task.get('peer_binding_id') != source.get('binding_id'):
        raise RuleViolation('identity_expired', '发起节点身份已轮换')
    project = next((p for p in registry.get('projects', []) if p.get('project_id') == task.get('project_id')), None)
    if not project or project.get('status') != 'active' or issuer not in project.get('node_ids', []):
        raise RuleViolation('out_of_scope', '发起节点未参与此启用项目')
    if task.get('owner_id') != project.get('owner_id'):
        raise RuleViolation('out_of_scope', '互派不能修改项目责任归属')
    if task.get('record_revision') != 1 or task.get('requirements_version') != 1 or task.get('business_status') != 'active':
        raise RuleViolation('invalid_peer_task', '节点仅能直接新建并发布要求版本1任务')
    if completion_kind(task) != 'receipt_recorded':
        raise RuleViolation('invalid_peer_task', '直接互派首版采用有效回执收尾')
    refs=task.get('input_refs',[])
    resources={item.get('id',item.get('object_id')) for item in project.get('registered_resources',[]) if isinstance(item,dict)}
    if not isinstance(refs,list) or any(not isinstance(ref,dict) or (ref.get('document_id') or ref.get('id')) not in resources
        or not isinstance(ref.get('content_hash'),str) or not ref['content_hash'] or type(ref.get('revision')) not in (int,str)
        or (type(ref.get('revision')) is int and ref['revision']<0) or (isinstance(ref.get('revision'),str) and not ref['revision']) for ref in refs):
        raise RuleViolation('out_of_scope','互派输入必须是发起项目已登记的明确资料版本')
    if task.get('editable_document_ids'):
        raise RuleViolation('out_of_scope', '互派首版不授予修改既有文档权限')
    grant=task.get('peer_task_scope')
    expected={'input_refs':task.get('input_refs',[]), 'output_zone':task.get('output_zone'),
              'output_folder_id':project.get('zones',{}).get(task.get('output_zone'),{}).get('folder_id')}
    if not expected['output_folder_id'] or grant != expected:
        raise RuleViolation('out_of_scope', '任务输入或交付位置与登记范围不一致')
    if not all(isinstance(task.get(k),str) and task[k].strip() for k in ('title','goal','deliverable','operation_id','task_id')):
        raise RuleViolation('missing_conditions','请明确任务目标、输入和交付要求')
    plan=execution_plan(task,receivers[0])
    if plan['missing_conditions']:
        raise RuleViolation('missing_conditions','任务缺少必要业务条件')
    return project


def select_verified_task_versions(values):
    """Select a peer's latest already-verified central revision over old copies.

    Caller must verify each signature first. Two distinct creation bodies or two
    different bodies at one revision remain conflicts, never arbitrary picks.
    """
    if not values:return None
    if all(value==values[0] for value in values[1:]):return values[0]
    if any(value.get('dispatch_source')!='peer' for value in values):
        raise RuleViolation('task_conflict','同一任务有不同当前记录')
    by_revision={}
    for value in values:
        revision=value.get('record_revision')
        if revision in by_revision and by_revision[revision]!=value:
            raise RuleViolation('task_conflict','同一任务记录修订出现不同正文')
        by_revision[revision]=value
    latest=by_revision[max(by_revision)]
    if any(value.get('peer_issuer_node_id')!=latest.get('peer_issuer_node_id') or value.get('peer_binding_id')!=latest.get('peer_binding_id')
        or value.get('project_id')!=latest.get('project_id') for value in values):
        raise RuleViolation('task_conflict','互派任务来源或项目出现冲突')
    return latest
