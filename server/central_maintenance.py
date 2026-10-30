"""Bounded, durable maintenance owned by the Panel process.

The maintenance principal is not an administrator account and cannot acquire
ordinary business tools. A capability exists only in this module's live job.
The refresh stage may process existing valid receipts; inspection thereafter
observes records and never repairs, creates work, or edits memory.
"""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import hashlib
import re
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from server import db, central_store as store, phase3_store as local
from server import rdos_projects as projects

# Current Beijing schedule is fixed UTC+08; avoid reading host tzdata under guard.
ZONE = timezone(timedelta(hours=8), 'Asia/Shanghai')
_MAX_SECONDS = 30 * 60
_CAPABILITY = object()
_ACTIVE: dict[str, asyncio.Task] = {}
_HEARTBEAT = None
_STOPPING = False
_PERMANENT = {'authorization_failed', 'out_of_scope', 'source_permission_denied',
              'source_authentication_failed', 'knowledge_not_configured', 'project_source_unavailable',
              'configuration_required', 'account_changed'}


class MaintenanceError(ValueError):
    def __init__(self, code, message, status=409):
        self.code, self.status = code, status
        super().__init__(message)


def _fail(code, message, status=409):
    raise MaintenanceError(code, message, status)


def _time(value=None):
    value = datetime.now(timezone.utc) if value is None else value
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        _fail('invalid_time', '维护时间必须包含时区', 422)
    return value.astimezone(timezone.utc)


def _iso(value=None):
    return _time(value).isoformat()


def _next(kind, now):
    current = _time(now).astimezone(ZONE)
    if kind == 'hourly_refresh':
        return (current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).astimezone(timezone.utc)
    due = current.replace(hour=23, minute=0, second=0, microsecond=0)
    due += timedelta(days=(4 - due.weekday()) % 7)
    if due <= current:
        due += timedelta(days=7)
    return due.astimezone(timezone.utc)


def _latest(kind, now):
    current = _time(now).astimezone(ZONE)
    if kind == 'hourly_refresh':
        return current.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    due = current.replace(hour=23, minute=0, second=0, microsecond=0)
    due -= timedelta(days=(due.weekday() - 4) % 7)
    if due > current:
        due -= timedelta(days=7)
    return due.astimezone(timezone.utc)


def _environment():
    from server.phase1_runtime import environment
    return environment()


def _actor(project_ids):
    return {'id': 'system:maintenance', 'identity_mode': 'system_maintenance',
            'project_ids': tuple(project_ids), '_capability': _CAPABILITY, **_environment()}


def permits(actor, action, project_id=None):
    if (not isinstance(actor, dict) or actor.get('_capability') is not _CAPABILITY
            or actor.get('id') != 'system:maintenance' or action not in {'sync', 'knowledge'}):
        return False
    if any(actor.get(k) != v for k, v in _environment().items()):
        return False
    scope = actor.get('project_ids')
    if not isinstance(scope, tuple) or any(not projects.is_registered(pid) for pid in scope):
        return False
    return project_id is None if action == 'knowledge' else project_id in scope


def _admin(actor):
    user = store.user(actor.get('id', '')) if isinstance(actor, dict) else None
    if not user or not user.get('enabled') or 'admin' not in user.get('roles', []):
        _fail('authorization_failed', '此操作需要当前管理员身份', 403)
    return user


def initialize(now=None, *, enabled=True):
    """Idempotent opt-in at application startup, never backfill before enable."""
    now = _time(now)
    for name in ('hourly_refresh', 'weekly_inspection'):
        if store.get('maintenance_schedule', name):
            continue
        store.put('maintenance_schedule', name, {'id': name, 'enabled': bool(enabled),
            'revision': 1, 'timezone': 'Asia/Shanghai', 'enabled_at': _iso(now),
            'next_due_at': _iso(_next(name, now)), 'last_run_id': None,
            'description': '每小时整点' if name == 'hourly_refresh' else '每周五23:00'})
    for run in store.items('maintenance_run'):
        if run['status'] in {'queued', 'running', 'draining'} and run['id'] not in _ACTIVE:
            run.update(status='interrupted', completed_at=_iso(now),
                       summary='上次维护在服务停止时中断；已保存结果保留，未自动重放写入。')
            store.put('maintenance_run', run['id'], run)


def state(actor=None):
    if actor is not None:
        _admin(actor)
    schedules = store.items('maintenance_schedule')
    runs = sorted(store.items('maintenance_run'), key=lambda row: row.get('created_at', ''), reverse=True)
    inspection = next((row for row in runs if row['kind'] == 'inspection' and row.get('inspection')), None)
    return {'schedules': schedules, 'active_run': next((row for row in runs if row['id'] in _ACTIVE), None),
            'latest_inspection': inspection, 'paused': bool((store.get('state', 'runtime') or {}).get('paused')),
            'heartbeat_running': _HEARTBEAT is not None and not _HEARTBEAT.done(),
            'execution_boundary': '刷新处理有效回执；诊断阶段只读。服务停止期间不执行，恢复只补最近一期。'}


def set_schedule(schedule_id, enabled, expected_revision, actor, *, now=None):
    _admin(actor)
    if schedule_id not in {'hourly_refresh', 'weekly_inspection'} or type(enabled) is not bool:
        _fail('invalid_schedule', '维护安排无效', 422)
    now = _time(now)
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("SELECT payload FROM central_records WHERE kind='maintenance_schedule' AND id=?", (schedule_id,)).fetchone()
        if not row:
            _fail('not_found', '维护安排尚未初始化', 404)
        item = json.loads(row[0])
        if type(expected_revision) is not int or item['revision'] != expected_revision:
            _fail('version_conflict', '维护安排已变化，请刷新后再保存')
        item.update(enabled=enabled, revision=item['revision'] + 1, updated_by=actor['id'], updated_at=_iso(now))
        if enabled:
            item.update(enabled_at=_iso(now), next_due_at=_iso(_next(schedule_id, now)))
        conn.execute("UPDATE central_records SET payload=?,updated_at=? WHERE kind='maintenance_schedule' AND id=?",
                     (json.dumps(item, ensure_ascii=False), _iso(now), schedule_id))
    return item


def _scope(project_ids):
    registered = sorted(item['id'] for item in store.items('project') if projects.is_registered(item.get('id')))
    if project_ids is None or project_ids == []:
        return registered
    if not isinstance(project_ids, list) or any(not isinstance(pid, str) or pid not in registered for pid in project_ids):
        _fail('out_of_scope', '维护只接受已登记的项目', 403)
    return sorted(set(project_ids))


def _save(run, **changes):
    run.update(changes)
    store.put('maintenance_run', run['id'], run)


def _failure(exc):
    code = getattr(exc, 'code', 'source_read_failed')
    if not isinstance(code, str) or not re.fullmatch(r'[a-zA-Z0-9_:.-]{1,80}', code):
        code = 'source_read_failed'
    return {'status': 'failed', 'error_code': code,
            'message': '该来源本次未完整处理，保留上次已核对内容。'}


def _backoff(key, result, now):
    prior = store.get('maintenance_source', key) or {'id': key, 'consecutive_failures': 0}
    if result['status'] == 'succeeded':
        prior.update(consecutive_failures=0, status='succeeded', last_success_at=_iso(now), next_retry_at=None,
                     error_code='', needs_attention=False)
    else:
        count = prior['consecutive_failures'] + 1
        code = result.get('error_code', 'source_read_failed')
        permanent = code in _PERMANENT
        minutes = min(360, 5 * 2 ** min(count - 1, 7))
        prior.update(consecutive_failures=count, status='needs_attention' if permanent else 'failed',
                     error_code=code, next_retry_at=None if permanent else _iso(_time(now) + timedelta(minutes=minutes)),
                     needs_attention=permanent or count >= 3)
    prior['last_attempt_at'] = _iso(now)
    store.put('maintenance_source', key, prior)
    return prior


async def _source(run, key, work, *, manual, now, deadline):
    if _STOPPING:
        return {'source': key, 'status': 'skipped', 'error_code': 'stopping', 'message': '服务正在停止，未开始新的维护步骤。'}
    if (store.get('state', 'runtime') or {}).get('paused'):
        return {'source': key, 'status': 'skipped', 'error_code': 'paused', 'message': '中央已暂停，未启动新的维护步骤。'}
    if time.monotonic() >= deadline:
        return {'source': key, 'status': 'skipped', 'error_code': 'deadline', 'message': '本轮达到30分钟限额，未启动新的来源。'}
    prior = store.get('maintenance_source', key) or {}
    if not manual and (prior.get('status') == 'needs_attention' or prior.get('next_retry_at') and _time(prior['next_retry_at']) > _time(now)):
        return {'source': key, 'status': 'skipped', 'error_code': 'backoff', 'next_retry_at': prior.get('next_retry_at'),
                'message': '该来源等待恢复检查，其他来源继续。'}
    _save(run, stage=key)
    try:
        worker = asyncio.create_task(work())
        try:
            result = await asyncio.wait_for(asyncio.shield(worker), max(0.001, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            # The owner stays registered until the in-flight worker drains. CLI
            # calls also receive this deadline; a timed-out write is reconciled
            # by the existing operation core, never resent by maintenance.
            _save(run, status='draining', stage=key, summary='本轮达到30分钟限额，正在收束已发出的操作。')
            try:
                await asyncio.shield(worker)
            except Exception:
                pass
            result = {'status': 'failed', 'error_code': 'maintenance_deadline',
                      'message': '本轮达到30分钟限额，未启动后续来源。'}
        if not isinstance(result, dict):
            raise ValueError('incomplete_result')
        result = {**result, 'source': key}
        if result.get('status') not in {'succeeded', 'partial', 'failed'}:
            result['status'] = 'succeeded'
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        result = {**_failure(exc), 'source': key}
    retry = _backoff(key, result, now if run.get('controlled_clock') else _time())
    result.update(consecutive_failures=retry['consecutive_failures'], next_retry_at=retry.get('next_retry_at'),
                  needs_attention=retry.get('needs_attention', False))
    return result


async def _refresh(run, actor, *, manual, now, deadline):
    from server import phase3_backend, phase3_service, shared_knowledge
    backend = phase3_backend.get_backend()
    results = []
    snapshot = {}
    async def tables():
        # Both complete reads precede all project cache replacement.
        tasks = await asyncio.to_thread(backend.list_tasks)
        events = await asyncio.to_thread(backend.list_events)
        if not isinstance(tasks, list) or not isinstance(events, list):
            _fail('incomplete_source', '业务表没有完整返回')
        discovery = getattr(backend, 'refresh_registry', None)
        if discovery:
            await asyncio.to_thread(discovery)
        snapshot.update(tasks=tasks, events=events, registry=await asyncio.to_thread(backend.registry))
        return {'status': 'succeeded', 'task_count': len(tasks), 'event_count': len(events)}
    tables_result = await _source(run, 'business_tables', tables, manual=manual, now=now, deadline=deadline)
    results.append(tables_result)
    for pid in actor['project_ids']:
        if tables_result['status'] == 'succeeded':
            async def project(pid=pid):
                workspace = await phase3_service.refresh(actor, run['id'] + ':' + pid, pid,
                    task_records=copy.deepcopy(snapshot['tasks']), event_records=copy.deepcopy(snapshot['events']),
                    registry=copy.deepcopy(snapshot['registry']))
                last = workspace.get('last_refresh') or {}
                return {'status': 'partial' if last.get('issue_count') else 'succeeded', 'project_id': pid,
                        'task_count': len(workspace.get('tasks', [])), 'event_count': len(workspace.get('events', [])),
                        'issue_count': last.get('issue_count', 0)}
            results.append(await _source(run, 'project_records:' + pid, project, manual=manual, now=now, deadline=deadline))
        else:
            results.append({'source': 'project_records:' + pid, 'status': 'skipped', 'error_code': 'table_read_incomplete'})
        async def files(pid=pid):
            return await asyncio.to_thread(shared_knowledge.sync_project_files, backend, pid, actor, run['id'] + ':files:' + pid)
        results.append(await _source(run, 'project_files:' + pid, files, manual=manual, now=now, deadline=deadline))
    for category in ('selected_rag', 'shared_skills'):
        async def knowledge(category=category):
            if backend.registry().get('mode') == 'local_mock' and not hasattr(backend, 'read_knowledge'):
                return {'status': 'failed', 'error_code': 'knowledge_not_configured', 'message': '当前模拟后端未设置共享知识来源。'}
            response = await asyncio.to_thread(shared_knowledge.sync, category, actor, run['id'] + ':' + category, backend=backend)
            result = response['sync_result']
            projected = {key: result[key] for key in ('status', 'item_count', 'results', 'error_code', 'message') if key in result}
            if result['status'] != 'succeeded' and not projected.get('error_code'):
                failures = [part for part in result.get('results', []) if part.get('status') in {'partial', 'failed'}]
                failures.extend(unit for part in result.get('results', []) for unit in part.get('units', []) if unit.get('status') == 'failed')
                chosen = next((part for part in failures if part.get('error_code') in _PERMANENT), None)
                chosen = chosen or next((part for part in failures if part.get('error_code')), None)
                if chosen:
                    projected['error_code'] = chosen['error_code']
            return projected
        results.append(await _source(run, category, knowledge, manual=manual, now=now, deadline=deadline))
    async def node_registry():
        from server import peer_tasks
        outcome = await peer_tasks.renew_registry_if_due()
        return {'status': 'succeeded', 'registry': outcome}
    results.append(await _source(run, 'node_registry', node_registry, manual=manual, now=now, deadline=deadline))
    return results


def inspect_evidence(project_ids, *, now=None):
    """Deterministic evidence projection; no model, connectors or state writes."""
    from server.phase1_runtime import health
    observations = []
    for pid in project_ids:
        workspace = __import__('server.phase3_service', fromlist=['_cached_workspace'])._cached_workspace(pid, administrative=True)
        tasks = workspace.get('tasks', [])
        records = workspace.get('last_refresh') or {}
        observations.append({'project_id': pid, 'name': workspace['project']['name'], 'status': workspace['project'].get('status'),
            'task_count': len(tasks), 'active_count': sum(t.get('business_status') == 'active' for t in tasks),
            'pending_result_count': sum(t.get('business_status') == 'pending_review' for t in tasks),
            'integrity_issue_count': sum(bool(t.get('integrity_error')) for t in tasks),
            'verified_receipt_count': sum(t.get('receipt_status') == 'verified' for t in tasks),
            'nodes': [{'node_id': node.get('node_id'),
                       'last_observed_record_at': max((event.get('created_at', '') for event in workspace.get('events', [])
                           if event.get('claimed_node_id') == node.get('node_id')), default=None),
                       'evidence_kind': 'service_records_only'} for node in workspace.get('nodes', [])],
            'last_refresh': {key: records[key] for key in ('at', 'complete', 'error_code', 'issue_count') if key in records},
            'health': {key: health(pid).get(key) for key in ('status', 'consecutive_failures', 'next_due', 'uncertain_operations')},
            'evidence_boundary': '已知回执及缓存不证明节点本次在线。'})
    pending = [row for row in store.items('operation') if row.get('status') in {'uncertain', 'pending_reconciliation', 'running', 'interrupted'}
               and row.get('project_id') in project_ids]
    runs = [row for row in store.items('run') if row.get('project_id') in project_ids or not row.get('project_id')]
    users = store.users()
    enabled_admins = sum(user.get('enabled', False) and 'admin' in user.get('roles', []) for user in users)
    from server.lab_panel import runtime_status
    runtime = runtime_status()
    from lab import guard
    runtime_state = store.get('state', 'runtime') or {}
    boundary = {'process_guard_active': bool(guard._active),
                'business_scheduler_enabled': bool(runtime_state.get('scheduler_enabled')),
                'maintenance_principal': 'system:maintenance',
                'diagnosis_can_mutate_business': False,
                'scope': 'registered_projects_and_sources',
                'evidence_kind': 'current_process_configuration_not_penetration_test'}
    latest_runs = sorted(runs, key=lambda row: row.get('created_at', ''), reverse=True)[:30]
    model = {'model': os.environ.get('BAITE_LLM_MODEL', runtime['model']) if runtime['model_mode'] == 'agent_plan' else 'mock',
             'mode': runtime['model_mode'], 'configured': runtime['model_configured'],
             'thinking': os.environ.get('BAITE_LLM_THINKING', '') if runtime['model_mode'] == 'agent_plan' else '',
             'reasoning_effort': os.environ.get('BAITE_LLM_REASONING_EFFORT', '') if runtime['model_mode'] == 'agent_plan' else '',
             'observed_run_count': len(runs), 'recent_failures': sum(row.get('status') == 'failed' for row in latest_runs),
             'evidence': '当前有效非密钥配置与既有运行记录；本次未额外调用模型，不证明当前连通性。'}
    cli = {'status': 'not_applicable', 'reason': '当前项目后端未使用飞书CLI。'}
    from server import phase3_backend
    adapter = phase3_backend.get_backend()
    config = getattr(adapter, 'cfg', {}).get('phase3', {})
    if adapter.registry().get('mode') == 'feishu_test':
        expected = config.get('cli_sha256')
        executor = getattr(adapter, 'executor', None)
        path = Path(config.get('cli_path', ''))
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_absolute() and not path.is_symlink() else ''
            matches = bool(expected and expected == actual and getattr(executor, 'binary_hash', None) == expected)
            cli = {'status': 'matched' if matches else 'needs_attention', 'configured_sha256': expected,
                   'observed_sha256': actual, 'path_is_fixed_absolute': path.is_absolute(),
                   'evidence': '固定CLI文件哈希；未执行任意CLI探测，不覆盖其内部网络行为。'}
        except (OSError, PermissionError):
            cli = {'status': 'unavailable', 'configured_sha256': expected,
                   'reason': '未能读取固定CLI文件，不能据此宣称出口验证通过。'}
    code_root = Path(__file__).resolve().parent
    code_versions = {}
    for relative in ('central_maintenance.py', 'central_service.py', 'phase3_service.py', 'central_agent.py', 'peer_tasks.py', '../lab/guard.py'):
        path = code_root / relative
        try:
            code_versions['lab/guard.py' if relative.startswith('../') else 'server/' + relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, PermissionError):
            code_versions[relative] = 'not_read'
    boundary['cli'] = cli
    sources = [{key: row.get(key) for key in ('id', 'status', 'consecutive_failures', 'error_code', 'needs_attention', 'last_success_at')}
               for row in store.items('maintenance_source')
               if not row['id'].startswith(('project_records:', 'project_files:')) or row['id'].split(':', 1)[1] in project_ids]
    issues = []
    for source in sources:
        if source['status'] != 'succeeded':
            issues.append({'kind': 'source', 'source': source['id'], 'code': source['error_code'], 'summary': '来源读取或处理需要核对。'})
    for project in observations:
        if project['integrity_issue_count'] or project['last_refresh'].get('issue_count') or project['last_refresh'].get('complete') is False:
            issues.append({'kind': 'project', 'project_id': project['project_id'], 'summary': '项目记录或最近完整读取仍有待核对项。'})
    if cli['status'] in {'needs_attention', 'unavailable'}:
        issues.append({'kind': 'boundary', 'summary': '固定CLI文件或配置尚未核对通过。'})
    if pending:
        issues.append({'kind': 'operation', 'count': len(pending), 'summary': '仍有操作需要核对，未自动重放。'})
    if not boundary['process_guard_active']:
        issues.append({'kind': 'boundary', 'summary': '当前进程防护未启用。'})
    if boundary['business_scheduler_enabled']:
        issues.append({'kind': 'scheduler', 'summary': '无人值守业务编排处于开启状态，需要管理员核对。'})
    if not enabled_admins:
        issues.append({'kind': 'account', 'summary': '没有启用的管理员账号。'})
    return {'observed_at': _iso(now), 'project_ids': list(project_ids), 'projects': observations,
            'sources': sources, 'pending_operation_ids': [row['id'] for row in pending],
            'accounts': {'enabled_admin_count': enabled_admins}, 'model_evidence': model, 'tool_boundary': boundary, 'issues': issues,
            'read_only': True, 'changes': [],
            'code_evidence': {'files': code_versions, 'kind': 'current_file_hashes_not_test_execution'},
            'not_checked': ['本轮未对每个出口执行负向探测。', '未检查Local Agent私有状态、系统级隔离或定时真实触发。',
                            '未额外调用真实模型验证当前连通性。'],
            'boundary': '检查登记范围的实际证据；未执行任意命令、修复、通知、任务派发或记忆整理。'}


async def _work(run, *, manual, now):
    actor = _actor(run['project_ids'])
    deadline = time.monotonic() + _MAX_SECONDS
    from server.maintenance_limits import DEADLINE
    deadline_token = DEADLINE.set(deadline)
    _save(run, status='running', started_at=_iso(now), stage='refresh')
    try:
        results = await _refresh(run, actor, manual=manual, now=now, deadline=deadline)
        _save(run, results=results)
        if run['kind'] == 'inspection':
            # This second stage calls no synchronization/receipt processing.
            _save(run, stage='inspection')
            report = inspect_evidence(run['project_ids'], now=now)
            _save(run, inspection=report)
        failed = any(row['status'] != 'succeeded' for row in results)
        success = any(row['status'] == 'succeeded' for row in results)
        status = 'partial' if failed and success else 'failed' if failed else 'succeeded'
        report = run.get('inspection') or {}
        count = len(report.get('issues', []))
        summary = ('已完成同步与只读检查，' + (str(count) + ' 项问题需关注。' if count else '本次未发现需要处理的问题。')) if run['kind'] == 'inspection' else '已完成全量来源检查。'
        if failed:
            summary = '部分来源未完成；已保留成功结果和缺口。' + ('只读检查结果已保留。' if run['kind'] == 'inspection' else '')
        _save(run, status=status, stage='finished', completed_at=_iso(), summary=summary)
    except asyncio.CancelledError:
        _save(run, status='interrupted', completed_at=_iso(), summary='维护中断；已有写入仅保留核对证据，不自动重放。')
        raise
    except Exception as exc:
        _save(run, status='failed', completed_at=_iso(), error_code=_failure(exc)['error_code'], summary='维护未完成，请查看已保存的来源结果。')
    finally:
        DEADLINE.reset(deadline_token)
        _ACTIVE.pop(run['id'], None)


async def request_run(kind, actor, operation_id, project_ids=None, *, now=None, system=False, missed=False, schedule_ids=None):
    if kind not in {'refresh', 'inspection'}:
        _fail('invalid_kind', '维护类型无效', 422)
    if not system:
        _admin(actor)
    elif actor is not _CAPABILITY:
        _fail('authorization_failed', '不能伪造系统维护身份', 403)
    if not isinstance(operation_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,180}', operation_id):
        _fail('invalid_operation', '维护操作标识无效', 422)
    if _STOPPING:
        _fail('stopping', '服务正在停止，未接受新的维护', 409)
    if (store.get('state', 'runtime') or {}).get('paused'):
        _fail('paused', '中央已暂停，未开始维护', 409)
    scope = _scope(project_ids)
    key = 'maintenance_' + store.digest(operation_id)[:28]
    old = store.get('maintenance_run', key)
    if old:
        if old['kind'] != kind or old['project_ids'] != scope:
            _fail('operation_conflict', '同一操作标识不能用于不同维护')
        return old
    active = next((store.get('maintenance_run', rid) for rid in _ACTIVE), None)
    if active:
        covered = set(scope).issubset(active['project_ids']) and (kind == active['kind'] or active['kind'] == 'inspection')
        if covered:
            return {**active, 'coalesced': True, 'requested_kind': kind,
                    'message': '已有覆盖相同范围的维护正在执行，请查看现有运行。'}
        return {**active, 'status': 'busy', 'coalesced': False, 'requested_kind': kind,
                'request_not_started': True, 'summary': '其他范围的维护正在执行，本次请求未启动；不会排队或扩大现有运行范围。'}
    run = {'id': key, 'operation_id': operation_id, 'kind': kind, 'status': 'queued', 'project_ids': scope,
           'created_at': _iso(now), 'identity': 'system:maintenance', 'requested_by': 'system:maintenance' if system else actor['id'],
           'missed_while_offline': bool(missed), 'controlled_clock': now is not None,
           'schedule_ids': schedule_ids or [], 'results': []}
    store.put('maintenance_run', key, run)
    _ACTIVE[key] = asyncio.create_task(_work(run, manual=not system, now=_time(now)))
    return run


async def wait(run_id):
    task = _ACTIVE.get(run_id)
    if task:
        await asyncio.shield(task)
    return store.get('maintenance_run', run_id)


async def tick(now=None):
    controlled_clock = now is not None
    now = _time(now)
    if _STOPPING or (store.get('state', 'runtime') or {}).get('paused'):
        return {'status': 'paused'}
    due = [row for row in store.items('maintenance_schedule') if row['enabled'] and _time(row['next_due_at']) <= now]
    if not due:
        return {'status': 'not_due'}
    if _ACTIVE:
        return {'status': 'busy', 'run_id': next(iter(_ACTIVE))}
    # A simultaneous Friday/hourly occurrence is one refresh plus inspection.
    kind = 'inspection' if any(row['id'] == 'weekly_inspection' for row in due) else 'refresh'
    latest = max(_latest(row['id'], now) for row in due)
    op = 'heartbeat:' + kind + ':' + latest.strftime('%Y%m%dT%H%M%SZ')
    missed = any(now - _time(row['next_due_at']) >= timedelta(minutes=1) for row in due)
    run = await request_run(kind, _CAPABILITY, op, now=now if controlled_clock else None, system=True, missed=missed, schedule_ids=[r['id'] for r in due])
    for row in due:
        row.update(next_due_at=_iso(_next(row['id'], now)), last_due_at=_iso(_latest(row['id'], now)),
                   last_run_id=run['id'], last_started_at=_iso(now))
        store.put('maintenance_schedule', row['id'], row)
    return {'status': 'started', 'run_id': run['id'], 'missed_while_offline': missed}


async def _loop():
    while not _STOPPING:
        try:
            await tick()
        except Exception:
            # Do not log exception bodies from connectors or credentials.
            store.put('maintenance_health', 'heartbeat', {'status': 'failed', 'at': _iso(), 'message': '维护心跳未完成本次检查。'})
        await asyncio.sleep(15)


async def start(*, enabled=True):
    global _HEARTBEAT, _STOPPING
    _STOPPING = False
    initialize(enabled=enabled)
    if _HEARTBEAT is None or _HEARTBEAT.done():
        _HEARTBEAT = asyncio.create_task(_loop())


async def stop():
    global _HEARTBEAT, _STOPPING
    _STOPPING = True
    if _HEARTBEAT:
        _HEARTBEAT.cancel()
        await asyncio.gather(_HEARTBEAT, return_exceptions=True)
        _HEARTBEAT = None
    # CLI operations have their own hard timeout. On shutdown stop scheduling
    # immediately and allow the in-flight core operation a bounded drain.
    pending = list(_ACTIVE.values())
    if pending:
        _, pending = await asyncio.wait(pending, timeout=20)
        if pending:
            # Do not cancel a to_thread owner then release its identity while
            # its CLI may still commit. The process shutdown bounds termination;
            # surviving same-process calls keep their active owner until drain.
            for rid, task in list(_ACTIVE.items()):
                if task in pending:
                    run = store.get('maintenance_run', rid)
                    _save(run, status='draining', summary='服务停止中；已发出的操作仍由原运行负责核对。')


from server.central_api import current_user
from server.lab_routes import SanitizedLabRoute
router = APIRouter(prefix='/api/central/maintenance', tags=['central-maintenance'], route_class=SanitizedLabRoute)


class RunInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    operation_id: str = Field(min_length=1, max_length=180, pattern=r'^[A-Za-z0-9_.:-]+$')
    project_ids: list[str] | None = None


class ScheduleInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    schedule_id: Literal['hourly_refresh', 'weekly_inspection']
    enabled: bool
    expected_revision: int = Field(ge=1)


def _http(exc):
    return HTTPException(getattr(exc, 'status', 409), detail={'code': getattr(exc, 'code', 'maintenance_failed'), 'message': str(exc)})


@router.get('/state')
def route_state(actor: dict = Depends(current_user)):
    try:
        return state(actor)
    except MaintenanceError as exc:
        raise _http(exc) from None


@router.patch('/schedule')
def route_schedule(payload: ScheduleInput, actor: dict = Depends(current_user)):
    try:
        return set_schedule(payload.schedule_id, payload.enabled, payload.expected_revision, actor)
    except MaintenanceError as exc:
        raise _http(exc) from None


@router.post('/refresh')
async def route_refresh(payload: RunInput, actor: dict = Depends(current_user)):
    try:
        from server.rdos_contract import internal_input
        return await request_run('refresh', actor, payload.operation_id, internal_input(payload.project_ids))
    except MaintenanceError as exc:
        raise _http(exc) from None


@router.post('/inspect')
async def route_inspect(payload: RunInput, actor: dict = Depends(current_user)):
    try:
        from server.rdos_contract import internal_input
        return await request_run('inspection', actor, payload.operation_id, internal_input(payload.project_ids))
    except MaintenanceError as exc:
        raise _http(exc) from None


@router.get('/runs')
def route_runs(limit: int = 20, actor: dict = Depends(current_user)):
    try:
        _admin(actor)
        if not 1 <= limit <= 100:
            _fail('invalid_limit', '记录数量须为1至100', 422)
        rows = sorted(store.items('maintenance_run'), key=lambda row: row.get('created_at', ''), reverse=True)
        return {'items': rows[:limit], 'total': len(rows)}
    except MaintenanceError as exc:
        raise _http(exc) from None


def get_run(run_id, actor):
    _admin(actor)
    run = store.get('maintenance_run', run_id)
    if not run:
        _fail('not_found', '维护运行不存在', 404)
    return run


@router.get('/runs/{run_id}')
def route_run(run_id: str, actor: dict = Depends(current_user)):
    try:
        return get_run(run_id, actor)
    except MaintenanceError as exc:
        raise _http(exc) from None
