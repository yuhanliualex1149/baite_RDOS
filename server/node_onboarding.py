"""Prepare initial client setup as part of the authenticated onboarding action.

The node still receives and completes an ordinary Feishu task. No client files,
timers or task rows are modified outside their existing business services.
"""
from __future__ import annotations

import os
import json
from server import central_store as central, phase3_store as local
from server import rdos_projects as projects, phase3_service as service
from server import db, phase1_operations as operations

_ACTIVE = set()


def _candidate_project(node_id, user):
    candidates = [p for p in projects.list_projects(user) if p['status'] == 'active'
                  and node_id in p['node_ids']]
    if not candidates:
        raise projects.ProjectError('node_has_no_project', '请先在项目管理中为节点分配项目，再复制接入指令', 409)
    return next((p for p in candidates if p.get('is_default')), candidates[0])


def _setup_payload(task_id, node_id):
    return {'task_id': task_id, 'title': '完成 RDOS 工作接入',
        'goal': '按本节点当前接入说明校验并持久安装 RDOS 执行 Skill，确认后续会话可调用。'
            '复用飞书连接并读取有效任务，运行 enroll-peer 为本任务提交节点互派申请并独立读回；私钥只留本节点。'
            '公钥与持钥证明写回后，中央随既有同步自动核验并完成登记，不需要人工审核；节点后续检查自动核对中央签名并回执。'
            '登记与互派就绪单独跟踪，申请步骤完成后不必等待它们才能提交本次配置完成。'
            '使用客户端原生定时能力配置每日 09:00 检查任务及已安装机构 Shared Skills 更新、周五 16:30 按所属项目有效任务汇总真实工作周报，时区 Asia/Shanghai。'
            'Shared Skills 有更新先在本节点对话询问用户，同意具体版本后再更新；未同意不安装，同版不反复询问。'
            '两条安排都调用已安装的当前 Skill，并绑定当前会话和获准工区；已有对应 RDOS 安排只原位更新，保留 ID、时间、时区、启停状态及绑定，不新建平行安排。'
            '未具备原生定时能力或需要本人授权时如实报告受阻，不用外部后台服务替代。'
            '完成后核对持久安装和两条安排，将简短配置结果写入统一协作记录表并独立读回。'
            '无实际工作时不发布空周报，不扩大资料、项目或读者范围。',
        'executor_node_ids': [node_id], 'task_type': 'action',
        'deliverable': 'Skill 持久启用，节点互派申请已提交并读回，两条定时安排已保存并核对，接入配置回执已独立读回。',
        'output_zone': 'agent', 'dispatch_to': ['agent'], 'return_to': ['agent'],
        'deliverable_audience': ['researcher'], 'completion_rule': {'kind': 'receipt_recorded'}}


async def _new_plan(node_id, user, base_operation, task_id, attempt):
    project = _candidate_project(node_id, user)
    operation = base_operation if attempt == 0 else base_operation + ':attempt:' + str(attempt)
    draft = await service.prepare_task(_setup_payload(task_id, node_id), user, operation + ':draft', project['id'])
    return {'node_id': node_id, 'task_id': task_id, 'project_id': project['id'],
        'actor_id': user['id'], 'operation_id': operation, 'attempt': attempt,
        'draft_id': draft['id'], 'draft_hash': draft['draft_hash'], 'source': 'panel_node_onboarding'}


def _replace_unpublished_plan(node_id, old, replacement):
    """Retire only an operation that has no dispatched write and no live owner."""
    if old['operation_id'] in operations.ACTIVE_WRITES:
        raise projects.ProjectError('write_busy', '原接入发布仍在执行，请等待原操作', 409)
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("SELECT payload FROM phase3_local WHERE kind='node_onboarding' AND id=?", (node_id,)).fetchone()
        if not row or json.loads(row['payload']) != old:
            raise projects.ProjectError('write_busy', '接入计划已变化，请重新读取', 409)
        operation = conn.execute("SELECT payload FROM central_records WHERE kind='operation' AND id=?",
            (old['operation_id'],)).fetchone()
        if operation:
            original = json.loads(operation['payload'])
            if (original.get('dispatched_at') or original.get('active_attempt')
                or original.get('status') in ('running', 'in_progress', 'uncertain', 'succeeded')):
                raise projects.ProjectError('write_uncertain', '原接入发布结果尚不能确认，未另建任务', 409)
            original.update(status='conflict', error_code='onboarding_superseded',
                superseded_by=replacement['operation_id'], updated_at=db.utc_now())
            conn.execute("UPDATE central_records SET payload=?,updated_at=? WHERE kind='operation' AND id=?",
                (json.dumps(original, ensure_ascii=False), original['updated_at'], old['operation_id']))
        draft = conn.execute("SELECT payload FROM phase3_local WHERE kind='draft' AND id=?", (old['draft_id'],)).fetchone()
        if draft:
            value = json.loads(draft['payload'])
            if value.get('status') == 'draft' and value.get('draft_hash') == old['draft_hash']:
                value.update(status='superseded', superseded_by=replacement['draft_id'])
                conn.execute("UPDATE phase3_local SET payload=?,updated_at=? WHERE kind='draft' AND id=?",
                    (json.dumps(value, ensure_ascii=False), db.utc_now(), old['draft_id']))
        history = {**old, 'superseded_by': replacement['operation_id'], 'superseded_at': db.utc_now()}
        conn.execute("INSERT INTO phase3_local VALUES('node_onboarding_history',?,?,?)",
            (old['operation_id'], json.dumps(history, ensure_ascii=False), history['superseded_at']))
        conn.execute("UPDATE phase3_local SET payload=?,updated_at=? WHERE kind='node_onboarding' AND id=?",
            (json.dumps(replacement, ensure_ascii=False), db.utc_now(), node_id))


def _instruction(node, task=None):
    public = node.get('public_id') or node['node_id']
    url = os.environ['BAITE_PUBLIC_URL'].rstrip('/') + '/nodes/' + public + '/start'
    text = f'请按 RDOS 接入说明持久安装并启用执行 Skill，再检查当前飞书任务：{url}\n本节点：{public}。'
    if task:
        text += f'接入配置任务：{task["task_id"]}，包含节点登记、每日检查、每周反馈及接入回执；已有定时安排原位更新，不重复创建。'
    return text


async def prepare(node_id, actor):
    """One deterministic setup task per new node; copy/retry never redispatches."""
    user = projects._admin(actor)
    node = next((n for n in local.node_registry() if node_id in (n['node_id'], n.get('public_id'))), None)
    member = central.user(node.get('member_id', '')) if node else None
    if not node or not member or not member.get('enabled'):
        raise projects.ProjectError('invalid_node', '节点关联账号未启用', 409)
    # Existing migrated nodes keep their current upgrade decisions and timers.
    # Copying an entry must not publish a held legacy upgrade implicitly.
    if node.get('registration_source') != 'member_account':
        return {'instruction': _instruction(node), 'task': None}
    if 'researcher' not in member.get('roles', []):
        raise projects.ProjectError('invalid_node', '节点关联账号没有研发成员身份', 409)
    key = node['node_id']
    if key in _ACTIVE:
        raise projects.ProjectError('write_busy', '接入配置正在准备，请稍后继续原操作', 409)
    _ACTIVE.add(key)
    try:
        plan = local.get('node_onboarding', key)
        base_operation = 'node-onboard:' + central.digest([service.environment(), key])[:32]
        task_id = 'RDOS-ONBOARD-' + central.digest(base_operation)[:16].upper()
        if not plan:
            plan = local.immutable('node_onboarding', key,
                await _new_plan(key, user, base_operation, task_id, 0))
        else:
            # Read the authoritative row before deciding whether the old plan
            # can be replaced. A missing or incomplete read never proves absence.
            try:
                published = await service._read_task(plan['task_id'], False)
            except service.Phase3Error as exc:
                saved = central.get('operation', plan['operation_id']) or {}
                original = central.user(plan['actor_id'])
                # The transport may have committed before its response was
                # lost. No authority index is published until readback; resume
                # this exact authorized operation before judging the row.
                if (exc.code != 'task_tampered' or not saved.get('dispatched_at')
                    or saved.get('status') != 'uncertain' or not original
                    or not original.get('enabled') or 'admin' not in original.get('roles', [])):
                    raise
                await service.apply_task_action(plan['task_id'], 'start',
                    {'draft_id': plan['draft_id'], 'expected_hash': plan['draft_hash']}, original,
                    plan['operation_id'], 0, 0, project_id=plan['project_id'])
                published = await service._read_task(plan['task_id'])
            if published:
                if published['project_id'] != plan['project_id'] or published['executor_node_ids'] != [key]:
                    raise projects.ProjectError('operation_conflict', '接入任务归属发生变化，请核对原任务', 409)
                saved = central.get('operation', plan['operation_id']) or {}
                original = central.user(plan['actor_id'])
                if ((saved.get('dispatched_at') or saved.get('status') == 'uncertain')
                    and saved.get('status') != 'succeeded' and original and original.get('enabled')
                    and 'admin' in original.get('roles', [])):
                    # Reconcile the original operation and its event, never
                    # issue a new administrator authorization for this row.
                    await service.apply_task_action(plan['task_id'], 'start',
                        {'draft_id': plan['draft_id'], 'expected_hash': plan['draft_hash']}, original,
                        plan['operation_id'], 0, 0, project_id=plan['project_id'])
                    published = await service._read_task(plan['task_id'])
                return {'instruction': _instruction(node, published), 'task': published}
            original = central.get('operation', plan['operation_id']) or {}
            if original.get('dispatched_at') or original.get('status') in ('uncertain', 'succeeded'):
                raise projects.ProjectError('write_uncertain', '原接入发布结果尚不能确认，未另建任务', 409)
            prior = central.user(plan['actor_id'])
            valid_actor = prior and prior.get('enabled') and 'admin' in prior.get('roles', [])
            valid_project = (projects.is_registered(plan['project_id'])
                and projects.get_project(plan['project_id']).get('status') == 'active'
                and key in projects.project_node_ids(plan['project_id']))
            if not valid_actor or not valid_project:
                replacement = await _new_plan(key, user, base_operation, plan['task_id'],
                    int(plan.get('attempt', 0)) + 1)
                _replace_unpublished_plan(key, plan, replacement)
                plan = replacement
        op = central.get('operation', plan['operation_id']) or {}
        if op.get('status') != 'succeeded':
            # Resume the original, still-valid administrator action. A later
            # copy cannot replace its actor, content, project or operation ID.
            original = central.user(plan['actor_id'])
            await service.apply_task_action(plan['task_id'], 'start',
                {'draft_id': plan['draft_id'], 'expected_hash': plan['draft_hash']}, original,
                plan['operation_id'], 0, 0, project_id=plan['project_id'])
        task = await service._read_task(plan['task_id'])
        if task['project_id'] != plan['project_id'] or task['executor_node_ids'] != [key]:
            raise projects.ProjectError('operation_conflict', '接入任务归属发生变化，请核对原任务', 409)
        return {'instruction': _instruction(node, task), 'task': task}
    finally:
        _ACTIVE.discard(key)
