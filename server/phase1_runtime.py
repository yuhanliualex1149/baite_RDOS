"""Small, persisted phase-1 execution facts. No background worker is started here."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from server import central_store as store, central_projects as projects

BASE_DELAY = 300
MAX_DELAY = 21600
SYSTEM_ID = 'system:scheduler'
PERMANENT = {'authorization_failed', 'out_of_scope', 'not_configured', 'model_auth',
             'model_missing', 'model_config', 'invalid_source', 'unsupported_source'}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def later(seconds: float) -> str:
    return (datetime.fromisoformat(now()) + timedelta(seconds=seconds)).isoformat()


def environment() -> dict:
    from server.lab_panel import environment as current
    return current()


def system_actor() -> dict:
    return {'id': SYSTEM_ID, 'roles': ['scheduler'], 'identity_mode': 'role_test',
            'enabled': True, 'display_name': '实验定时检查'}


def actor(user: dict) -> dict:
    if user.get('id') == SYSTEM_ID:
        if user != system_actor():
            raise PermissionError('无效的系统身份')
        return system_actor()
    current = store.user(user.get('id', ''))
    if not current or not current.get('enabled') or 'admin' not in current.get('roles', []):
        raise PermissionError('当前账号没有中央运行权限')
    return current


def project_for_actor(project_id: str, user: dict) -> dict:
    current = actor(user)
    item = projects.project(project_id, None if current['id'] == SYSTEM_ID else current)
    from server import rdos_projects
    if os.environ.get('RDOS_RUNTIME_MODE') == 'production':
        if not rdos_projects.is_registered(project_id):
            raise PermissionError('项目不在已登记的正式范围')
        return item
    from server.lab_central_backend import PROJECT_IDS
    if project_id not in PROJECT_IDS and not rdos_projects.is_registered(project_id):
        raise PermissionError('项目不在本实验登记范围')
    return item


def create_thread(user: dict, project_id: str = '', title: str = '') -> dict:
    user = actor(user)
    if user['id'] == SYSTEM_ID:
        raise PermissionError('定时检查不创建对话')
    if project_id:
        project_for_actor(project_id, user)
    item = {'id': store.identifier('thread'), **environment(), 'project_id': project_id,
            'user_id': user['id'], 'title': title.strip()[:80] or '新对话',
            'created_at': now(), 'readonly': False}
    return store.put('thread', item['id'], item)


def get_thread(thread_id: str, user: dict, project_id: str | None = None) -> dict:
    user = actor(user)
    item = store.get('thread', thread_id)
    env = environment()
    if not item or item.get('user_id') != user['id'] or any(item.get(k) != v for k, v in env.items()):
        raise PermissionError('对话不存在或不属于当前账号和实验')
    if project_id is not None and item['project_id'] != project_id:
        raise PermissionError('对话与所选项目不一致')
    if item['project_id']:
        project_for_actor(item['project_id'], user)
    return item


def list_threads(user: dict, project_id: str = '') -> list:
    user = actor(user)
    if project_id:
        project_for_actor(project_id, user)
    env = environment()
    return sorted([t for t in store.items('thread') if t.get('user_id') == user['id']
                   and t.get('project_id') == project_id
                   and all(t.get(k) == v for k, v in env.items())],
                  key=lambda t: t['created_at'], reverse=True)


def health(project_id: str) -> dict:
    item = store.get('project_health', project_id) or {
        'project_id': project_id, 'issues': {}, 'next_due': now(),
        'last_checked_at': '', 'last_full_read_at': '', 'last_analysis_at': ''}
    pending = [o['id'] for o in store.items('operation') if o.get('project_id') == project_id
               and o.get('status') in ('uncertain', 'running')]
    active = [i for i in item.get('issues', {}).values() if i.get('active')]
    item.update(uncertain_operations=pending,
                consecutive_failures=max((i['count'] for i in active), default=0),
                needs_attention=bool(pending or any(i.get('needs_attention') for i in active)),
                error='；'.join(i['message'] for i in active),
                status='uncertain' if pending else 'needs_attention' if any(i.get('needs_attention') for i in active)
                       else 'backoff' if active else 'recovered' if item.get('last_recovered_at') else 'ready')
    return item


def save_health(item: dict) -> dict:
    return store.put('project_health', item['project_id'], item)


def checked(project_id: str) -> None:
    item = health(project_id)
    item['last_checked_at'] = now()
    save_health(item)


def _key(stage: str, source: str) -> str:
    return stage + ':' + source


def record_failure(project_id: str, stage: str, source: str, code: str, message: str,
                   attempt: str = '') -> dict:
    item = health(project_id)
    key = _key(stage, source)
    old = item.setdefault('issues', {}).get(key, {})
    if attempt and old.get('last_attempt') == attempt:
        return item
    count = old.get('count', 0) + 1 if old.get('active') else 1
    permanent = code in PERMANENT
    delay = min(BASE_DELAY * (2 ** min(count - 1, 16)), MAX_DELAY)
    issue = {'stage': stage, 'source': source, 'code': code, 'message': message[:400],
             'count': count, 'active': True, 'needs_attention': permanent or count >= 3,
             'first_failed_at': old.get('first_failed_at') if old.get('active') else now(),
             'last_failed_at': now(), 'last_attempt': attempt,
             'next_probe_at': None if permanent else later(delay)}
    item['issues'][key] = issue
    if issue['needs_attention'] and not old.get('notified'):
        issue['notified'] = True
        store.put('health_event', store.identifier('health'), {'project_id': project_id,
                  'stage': stage, 'source': source, 'kind': 'needs_attention', 'at': now(), 'message': message[:400]})
    else:
        issue['notified'] = old.get('notified', False)
    item['next_due'] = None if any(i.get('active') and i.get('next_probe_at') is None for i in item['issues'].values()) else max(
        (i['next_probe_at'] for i in item['issues'].values() if i.get('active')), default=later(BASE_DELAY))
    return save_health(item)


def record_success(project_id: str, stage: str, source: str = '') -> None:
    item = health(project_id)
    old = item.setdefault('issues', {}).get(_key(stage, source))
    if old and old.get('active'):
        old.update(active=False, recovered_at=now(), count=0, needs_attention=False)
        item['last_recovered_at'] = now()
        if old.get('notified'):
            store.put('health_event', store.identifier('health'), {'project_id': project_id,
                      'stage': stage, 'source': source, 'kind': 'recovered', 'at': now()})
    active = [i for i in item['issues'].values() if i.get('active')]
    item['next_due'] = None if any(i.get('next_probe_at') is None for i in active) else max(
        (i['next_probe_at'] for i in active), default=later(BASE_DELAY))
    if stage == 'model':
        item['last_analysis_at'] = now()
    save_health(item)


def overview() -> dict:
    items = [health(p['id']) for p in store.items('project')]
    return {'project_health': items, 'needs_attention_count': sum(i['needs_attention'] for i in items),
            'scheduler_enabled': False}


def history_limit() -> int:
    """Local operator setting; rejected if malformed, never a model argument."""
    value = (store.get('state', 'phase1_settings') or {}).get('history_messages', 12)
    if type(value) is not int or not 1 <= value <= 48:
        raise ValueError('聊天历史数量须为 1 至 48，默认 12')
    return value


def initialize() -> None:
    """Idempotent migration; recovery is classification only, never execution."""
    from server import phase1_operations
    env = environment()
    history_limit()
    version = store.get('state', 'phase1_schema') or {}
    if version.get('version', 0) < 1:
        # Preserve legacy messages without adding them to any new model context.
        for msg in store.items('message'):
            if msg.get('thread_id'):
                continue
            run = store.get('run', msg.get('run_id', '')) or {}
            owner = run.get('user_id')
            if not owner or owner == SYSTEM_ID or msg.get('kind', run.get('kind')) == 'sync':
                continue
            project_id = msg.get('project_id', run.get('project_id', store.PROJECT_ID))
            tid = 'legacy_' + store.digest([owner, project_id])[:20]
            if not store.get('thread', tid):
                store.put('thread', tid, {'id': tid, **env, 'user_id': owner, 'project_id': project_id,
                          'title': '历史对话（只读）', 'readonly': True, 'created_at': msg.get('created_at', now())})
            msg['thread_id'] = tid
            store.put('message', msg.get('id') or msg['run_id'] + ':' + msg['role'], msg)
        store.put('state', 'phase1_schema', {'version': 1, 'migrated_at': now()})
    phase1_operations.recover_interrupted()
    state = store.get('state', 'runtime') or {}
    state.setdefault('paused', False)
    if version.get('version', 0) < 2:
        # Phase 0 deliberately paused all central work. The approved Phase 1
        # migration enables manual mock orchestration once, never a scheduler.
        state['paused'] = False
        store.put('state', 'phase1_schema', {'version': 2, 'migrated_at': now(),
                  'manual_mock_enabled': True, 'reason': 'approved_phase1_transition'})
    state.update(phase=1, scheduler_enabled=False, model_mode='mock', backend='local_mock')
    state.pop('schedule_verified', None)
    store.put('state', 'runtime', state)
    for project in store.items('project'):
        save_health(health(project['id']))
