"""Shared Central memory. SQLite is authoritative; Markdown is a view.

Only the persisted interpretation of the current authenticated request may
mutate memory. Business records and chat history are never imported at startup.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import threading
import unicodedata

from server import central_store as store, db, phase1_runtime as runtime

_SUMMARY_LOCK = threading.Lock()
_SCOPES = {'account', 'project', 'organization'}
_KINDS = {'remember', 'correct', 'replace', 'invalidate', 'forget'}
_MEMORY = 'central_memory'
_HISTORY = 'central_memory_history'
_TOMBSTONE = 'central_memory_suppression'
_OPERATION = 'central_memory_operation'
_LIMIT = 8


class MemoryError(ValueError):
    def __init__(self, code, message, status=409, **details):
        self.code, self.status = code, status
        self.details = details
        super().__init__(message)


def _fail(code, message, status=409):
    raise MemoryError(code, message, status)


def _get(conn, kind, key):
    row = conn.execute('SELECT payload FROM central_records WHERE kind=? AND id=?', (kind, key)).fetchone()
    return json.loads(row[0]) if row else None


def _all(conn, kind):
    return [json.loads(row[0]) for row in conn.execute('SELECT payload FROM central_records WHERE kind=? ORDER BY id', (kind,))]


def _put(conn, kind, key, value):
    conn.execute('INSERT INTO central_records VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at',
                 (kind, key, json.dumps(value, ensure_ascii=False), db.utc_now()))


def _actor(actor, conn=None, *, write=False):
    if not isinstance(actor, dict):
        _fail('authorization_failed', '当前账号不能访问中央记忆', 403)
    if conn is None:
        current = store.user(actor.get('id', ''))
    else:
        row = conn.execute('SELECT * FROM central_users WHERE id=?', (actor.get('id', ''),)).fetchone()
        current = store.public_user(row) if row else None
    if (not current or not current.get('enabled') or not set(current.get('roles', [])) & {'admin', 'researcher'}
            or write and 'admin' not in current['roles']):
        _fail('authorization_failed', '当前账号不能访问中央记忆', 403)
    return current


def _readable_projects(actor):
    from server import central_projects, rdos_projects
    # Registered projects have the newer node-based participation model.
    result = set(rdos_projects.readable_project_ids(actor))
    for item in store.items('project'):
        if item.get('rdos_project_schema') == 1:
            continue
        try:
            central_projects.project(item['id'], actor)
        except (PermissionError, central_projects.ProjectNotFoundError):
            continue
        result.add(item['id'])
    return result


def _visible(item, actor, projects):
    if item.get('scope') == 'account':
        # Legacy account entries now belong to the shared Central space. The
        # researcher interface must not gain former account-only material.
        return 'admin' in actor.get('roles', [])
    if item.get('scope') == 'project':
        return item.get('project_id') in projects
    return (item.get('scope') == 'organization'
            and (item.get('audience') != 'central_admins' or 'admin' in actor.get('roles', [])))


def _identity(item):
    """Accept v1 IDs and tombstones without a second per-account memory space."""
    if item.get('scope') == 'account':
        return 'organization', '', ''
    return item.get('scope'), '', item.get('project_id', '') if item.get('scope') == 'project' else ''


def _text(value, name, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or not empty and not value.strip():
        _fail('invalid_memory', name + '不完整或过长', 422)
    if '\x00' in value:
        _fail('invalid_memory', name + '含无效字符', 422)
    return value.strip()


def _topic(value):
    return ' '.join(unicodedata.normalize('NFKC', _text(value, '记忆主题', 160)).casefold().split())


def _refs(values):
    if not isinstance(values, list):
        return set()
    return {v if isinstance(v, str) else v.get('id', v.get('memory_id', ''))
            for v in values if isinstance(v, (str, dict))}


def _trusted(conn, run, actor, changes):
    user = _actor(actor, conn, write=True)
    if not isinstance(run, dict):
        _fail('authorization_failed', '记忆变更缺少当前请求', 403)
    saved = _get(conn, 'run', run.get('id', ''))
    keys = ('id', 'user_id', 'project_id', 'thread_id', 'message_hash', 'message', 'operation_id',
            'allowed_projects', 'env_id', 'generation', 'request_plan', 'purpose')
    if (not saved or saved.get('status') not in {'queued', 'running'}
            or saved.get('user_id') != user['id'] or any(saved.get(k) != run.get(k) for k in keys)
            or saved.get('kind') != 'chat' or saved.get('trigger') != 'manual'
            or any(saved.get(k) != v for k, v in runtime.environment().items())):
        _fail('authorization_failed', '记忆变更不属于当前认证请求', 403)
    if (saved.get('purpose') in {'diagnose', 'diagnostic'} or saved.get('read_only')
            or saved.get('diagnostic_only') or saved.get('mode') in {'diagnose', 'diagnostic'}):
        _fail('read_only_diagnostic', '只读自检不整理或修改记忆', 403)
    plan = _get(conn, 'central_request_plan', saved.get('request_plan', ''))
    if (not plan or any(plan.get(k) != saved.get(k) for k in
            ('user_id', 'project_id', 'thread_id', 'message_hash', 'allowed_projects', 'env_id', 'generation'))
            or plan.get('run_id') != saved['id'] or plan.get('memory_changes') != changes
            or plan.get('purpose') in {'diagnose', 'diagnostic'}):
        _fail('authorization_failed', '记忆变更须与当前请求解析结果一致', 403)
    if saved.get('thread_id'):
        thread = _get(conn, 'thread', saved['thread_id'])
        if (not thread or thread.get('user_id') != user['id'] or thread.get('project_id') != saved.get('project_id')
                or thread.get('readonly') or any(thread.get(k) != saved.get(k) for k in ('env_id', 'generation'))):
            _fail('authorization_failed', '记忆变更的对话范围已变化', 403)
    return user, saved


def _scope(change, run, actor, readable):
    scope = change.get('scope', 'account')
    if scope not in _SCOPES:
        _fail('invalid_memory', '记忆范围应为账号、项目或机构', 422)
    # `account` remains accepted from older parsers, but no longer creates a
    # private store. Source ownership remains in each immutable source record.
    if change.get('account_id') not in (None, '', actor['id']):
        _fail('authorization_failed', '记忆来源账号不能由模型冒充', 403)
    scope = 'organization' if scope == 'account' else scope
    account_id = ''
    project_id = change.get('project_id') or run.get('project_id', '') if scope == 'project' else ''
    if scope == 'project':
        allowed = set(run.get('allowed_projects', [])) & set(readable)
        if isinstance(project_id, str) and project_id not in allowed:
            # The Feishu task table uses the registered public ID, while Panel
            # and long-term memory use the internal project ID. Resolve only a
            # protocol alias that is already in this run's scope. This pure
            # mapping works before the phase-3 backend has been initialized.
            from server import rdos_contract
            project_id = rdos_contract.internal_input(project_id)
        if not project_id or project_id not in allowed:
            _fail('authorization_failed', '记忆项目不在当前请求范围', 403)
    return scope, account_id, project_id


def _history(conn, item):
    _put(conn, _HISTORY, store.digest([item['id'], item['version']]), item)


def _origin(run, quote):
    message = run['message']
    start = message.find(quote)
    if start < 0:
        _fail('invalid_memory_source', '记忆依据必须来自当前用户请求', 422)
    spans, offset = [], 0
    while (found := message.find(quote, offset)) >= 0:
        spans.append([found, found + len(quote)])
        offset = found + len(quote)
    return {'run_id': run['id'], 'message_id': run['id'] + ':user',
            'message_hash': run.get('message_hash'), 'source_digest': store.digest(message),
            'start': start, 'end': start + len(quote), 'spans': spans, 'quote_hash': store.digest(quote),
            'quote': quote, 'user_id': run['user_id']}


def _source_suppressed(conn, source, scope, account_id, project_id):
    for old in _all(conn, _TOMBSTONE):
        if _identity(old) != (scope, account_id, project_id):
            continue
        for prior in old.get('sources', []):
            if ((source.get('run_id') and prior.get('run_id') == source.get('run_id')
                 or source.get('source_id') and prior.get('source_id') == source.get('source_id'))
                    and prior.get('quote_hash') == source.get('quote_hash')):
                return True
    return False


def _resolve(conn, change, scope, account_id, project_id):
    requested = change.get('memory_id')
    if requested:
        current = _get(conn, _MEMORY, requested)
        if not current or _identity(current) != (scope, account_id, project_id):
            _fail('memory_not_found', '当前范围内找不到这条记忆', 404)
        if current.get('status') == 'superseded' and current.get('replaced_by'):
            replacement = _get(conn, _MEMORY, current['replaced_by'])
            if replacement and _identity(replacement) == (scope, account_id, project_id):
                current = replacement
        if change.get('topic') and _topic(change['topic']) != current['topic_key']:
            _fail('memory_conflict', '记忆标识与主题不一致')
        return current, current['id'], current['topic'], current['topic_key']
    topic = _text(change.get('topic'), '记忆主题', 160)
    normalized = _topic(topic)
    key = 'memory_' + store.digest([scope, account_id, project_id, normalized])[:32]
    same = [item for item in _all(conn, _MEMORY)
            if (*_identity(item), item.get('topic_key'))
            == (scope, account_id, project_id, normalized)]
    priorities = {'active': 3, 'candidate': 2, 'inactive': 1}
    usable = [item for item in same if item.get('status') in priorities]
    active = [item for item in usable if item.get('status') == 'active']
    if len({item.get('content') for item in active}) > 1:
        _fail('memory_conflict', '这个主题有多条不同的既有约定，请先指定需要更正或替代的记忆')
    if usable:
        usable.sort(key=lambda item: (priorities[item['status']], item.get('updated_at', '')), reverse=True)
        current = usable[0]
        return current, current['id'], topic, normalized
    fallback = _get(conn, _MEMORY, key)
    if fallback and fallback.get('status') == 'superseded' and fallback.get('replaced_by'):
        replacement = _get(conn, _MEMORY, fallback['replaced_by'])
        if replacement and _identity(replacement) == (scope, account_id, project_id):
            fallback, key = replacement, replacement['id']
    return fallback, key, topic, normalized


def _deictic(conn, change, run, actor, readable):
    if change.get('kind') not in {'correct', 'replace', 'invalidate', 'forget'} or change.get('memory_id'):
        return change
    topic = str(change.get('topic') or '').strip()
    placeholders = {'', '刚才', '刚才那条', '刚才的约定', '上一条', '前一条', '这条', '这一条', '这项约定'}
    if topic not in placeholders or not re.search(r'刚才|上一条|前一条|这(?:一?条|项约定)', change.get('evidence_quote', '')):
        return change
    events = []
    for operation in _all(conn, _OPERATION):
        if operation.get('run_id') == run['id']:
            continue
        origin = _get(conn, 'run', operation.get('run_id', ''))
        if not origin or origin.get('thread_id') != run.get('thread_id'):
            continue
        targets = {item['id']: item.get('version') for item in operation.get('changes', []) if item.get('id')}
        if targets:
            events.append((operation['at'], targets))
    for origin in _all(conn, 'run'):
        if (origin.get('thread_id') == run.get('thread_id')
                and origin.get('id') != run['id'] and origin.get('memory_displayed_at')):
            targets = {key: (origin.get('displayed_memory_versions') or {}).get(key)
                       for key in origin.get('displayed_memory_ids', [])}
            if targets:
                events.append((origin['memory_displayed_at'], targets))
    if not events:
        _fail('memory_ambiguous', '这段对话中还不能确定你指的是哪条记忆，请说明主题')
    _, targets = max(events, key=lambda event: event[0])
    if len(targets) != 1:
        _fail('memory_ambiguous', '刚才涉及多条记忆，请说明要修改的主题')
    key, version = next(iter(targets.items()))
    item = _get(conn, _MEMORY, key)
    if not item or not _visible(item, actor, readable):
        _fail('memory_not_found', '刚才那条记忆已不在当前可读范围', 404)
    if version is not None and item.get('version') != version:
        _fail('memory_conflict', '刚才那条记忆已发生变化，请先核对当前内容')
    return {**change, 'memory_id': key, 'topic': item['topic'], 'scope': item['scope'],
            'project_id': item.get('project_id', '')}


def _forget(conn, current, source, now):
    if current is None:
        _fail('memory_not_found', '当前范围内找不到这条记忆', 404)
    # A forgotten generation must not accidentally erase a later, freshly
    # declared entry sharing its topic. Candidate variants belong to this ID.
    variants = [current]
    candidate = _get(conn, _MEMORY, current['id'] + ':candidate')
    if candidate:
        variants.append(candidate)
    # Migrated duplicate IDs are retained as aliases. Forgetting the shared
    # current entry must suppress their old history and sources as well.
    known = {item['id'] for item in variants}
    for item in _all(conn, _MEMORY):
        if item.get('replaced_by') in known and item['id'] not in known:
            variants.append(item)
            known.add(item['id'])
    for item in variants:
        sources = []
        versions = [old for old in _all(conn, _HISTORY) if old.get('id') == item['id']] + [item]
        for old in versions:
            for origin in old.get('sources', []):
                minimal = {key: value for key, value in origin.items() if key not in {'quote', 'user_id'}}
                if minimal not in sources:
                    sources.append(minimal)
        sources.append({key: value for key, value in source.items() if key not in {'quote', 'user_id'}})
        prior = _get(conn, _TOMBSTONE, item['id']) or {}
        sources += [s for s in prior.get('sources', []) if s not in sources]
        tombstone = {key: item.get(key, '') for key in ('id', 'scope', 'account_id', 'project_id')}
        tombstone.update(sources=sources, forgotten_at=now)
        _put(conn, _TOMBSTONE, item['id'], tombstone)
        if item.get('status') != 'forgotten':
            _history(conn, item)
            item = {**item, 'version': item['version'] + 1, 'status': 'forgotten', 'content': '',
                    'sources': [], 'updated_at': now, 'forgotten_at': now, 'updated_by': source['user_id']}
            _put(conn, _MEMORY, item['id'], item)
            _history(conn, item)
    saved = _get(conn, _MEMORY, current['id'])
    return {'kind': 'forget', 'id': current['id'], 'version': saved['version'], 'status': 'forgotten'}


def _invalidate(conn, current, source, now):
    if current is None:
        _fail('memory_not_found', '当前范围内找不到待失效的记忆', 404)
    if current.get('status') == 'forgotten':
        return {'kind': 'invalidate', 'id': current['id'], 'version': current['version'],
                'status': 'forgotten', 'suppressed': True}
    if current.get('status') != 'inactive':
        _history(conn, current)
        current = {**current, 'version': current['version'] + 1, 'status': 'inactive',
                   'updated_at': now, 'invalidated_at': now, 'updated_by': source['user_id'],
                   'sources': [*current.get('sources', []), source], 'change_kind': 'invalidate'}
        _put(conn, _MEMORY, current['id'], current)
        _history(conn, current)
    return {'kind': 'invalidate', 'id': current['id'], 'version': current['version'], 'status': 'inactive'}


def apply_changes(run, actor, changes):
    """Apply only the complete, persisted request plan; no free-form tool writes."""
    if not isinstance(changes, list) or len(changes) > _LIMIT or any(not isinstance(c, dict) for c in changes):
        _fail('invalid_memory', '单次记忆变更最多八项', 422)
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        user, saved = _trusted(conn, run, actor, changes)
        if not changes:
            return {'ok': True, 'changes': [], 'summary_updated': False}
        readable = _readable_projects(user)
        operation_id = store.digest([saved['id'], changes])
        previous = _get(conn, _OPERATION, operation_id)
        if previous:
            results = copy.deepcopy(previous['changes'])
            for result in results:
                current = _get(conn, _MEMORY, result.get('id', ''))
                if current and current.get('status') in {'forgotten', 'inactive'}:
                    result.update(status=current['status'], suppressed=current['status'] == 'forgotten')
                result['reused'] = True
        else:
            results = []
            for change in changes:
                kind = change.get('kind')
                if kind not in _KINDS:
                    _fail('invalid_memory', '不支持这项记忆操作', 422)
                quote = _text(change.get('evidence_quote'), '记忆依据', 4000)
                source = _origin(saved, quote)
                change = _deictic(conn, change, saved, user, readable)
                scope, account_id, project_id = _scope(change, saved, user, readable)
                if _source_suppressed(conn, source, scope, account_id, project_id) and kind != 'forget':
                    results.append({'kind': kind, 'status': 'suppressed', 'suppressed': True})
                    continue
                current, key, topic, normalized = _resolve(conn, change, scope, account_id, project_id)
                if kind == 'forget':
                    results.append(_forget(conn, current, source, db.utc_now()))
                    continue
                if kind == 'invalidate':
                    results.append(_invalidate(conn, current, source, db.utc_now()))
                    continue
                content = _text(change.get('content'), '记忆内容', 2000)
                evidence = change.get('evidence_kind', change.get('evidence', 'explicit'))
                if evidence not in {'explicit', 'inferred', 'member_report'}:
                    _fail('invalid_memory', '记忆依据性质无效', 422)
                status = 'active' if evidence == 'explicit' else 'candidate'
                if kind in {'correct', 'replace'} and current is None:
                    _fail('memory_not_found', '当前范围内找不到待更正的记忆', 404)
                if status == 'active' and key.endswith(':candidate'):
                    parent = _get(conn, _MEMORY, key[:-len(':candidate')])
                    if parent:
                        current, key = parent, parent['id']
                if current and current.get('status') == 'forgotten':
                    if status != 'active':
                        results.append({'kind': kind, 'status': 'suppressed', 'suppressed': True})
                        continue
                    # A fresh explicit declaration is a new entry, not a
                    # resurrection of the ID whose old sources were forgotten.
                    key = 'memory_' + store.digest([scope, account_id, project_id, normalized, saved['id']])[:32]
                    current = None
                if status == 'candidate' and current and current.get('status') in {'active', 'inactive'}:
                    key += ':candidate'
                    current = _get(conn, _MEMORY, key)
                if (kind == 'remember' and status == 'active' and current and current.get('status') == 'active'
                        and current.get('content') != content and current.get('updated_by') != user['id']):
                    _fail('memory_conflict', '这项约定与其他管理员的有效记忆不同，请先明确是否替代')
                now = db.utc_now()
                if current and current.get('content') == content and current.get('status') == status:
                    sources = current.get('sources', [])
                    if source not in sources:
                        current['sources'] = [*sources, source]
                        current['updated_at'] = now
                        _put(conn, _MEMORY, key, current)
                        _history(conn, current)
                    result = {'kind': kind, 'id': key, 'version': current['version'], 'status': status, 'reused': True}
                else:
                    if current:
                        _history(conn, current)
                    item = {'id': key, 'scope': scope, 'account_id': account_id, 'project_id': project_id,
                            'topic': topic, 'topic_key': normalized, 'content': content,
                            'status': status, 'evidence_kind': evidence,
                            'change_kind': kind,
                            'version': current['version'] + 1 if current else 1,
                            'sources': [source], 'created_at': current.get('created_at', now) if current else now,
                            'updated_at': now, 'effective_at': _text(change.get('effective_at', now), '生效时间', 80),
                            'updated_by': user['id'], 'replaces_version': current['version'] if current else None}
                    item['audience'] = ((current or {}).get('audience')
                                        or ('central_admins' if change.get('scope', 'account') == 'account' else 'permitted_members'))
                    item['applicability'] = copy.deepcopy((current or {}).get('applicability') or (
                        {'kind': 'account', 'account_id': user['id']} if change.get('scope', 'account') == 'account'
                        else {'kind': 'project', 'project_id': project_id} if scope == 'project'
                        else {'kind': 'organization'}))
                    for legacy_key in ('legacy_scope', 'legacy_account_id', 'shared_migrated_at'):
                        if current and legacy_key in current:
                            item[legacy_key] = current[legacy_key]
                    _put(conn, _MEMORY, key, item)
                    _history(conn, item)
                    result = {'kind': kind, 'id': key, 'version': item['version'], 'status': status, 'reused': False}
                results.append(result)
                if status == 'active':
                    for candidate in _all(conn, _MEMORY):
                        if (candidate['id'] != key and candidate.get('status') == 'candidate'
                                and (*_identity(candidate), candidate.get('topic_key'))
                                    == (scope, account_id, project_id, normalized)):
                            _history(conn, candidate)
                            candidate.update(status='superseded', version=candidate['version'] + 1,
                                             updated_at=now, replaced_by=key)
                            _put(conn, _MEMORY, candidate['id'], candidate)
                            _history(conn, candidate)
            _put(conn, _OPERATION, operation_id, {'id': operation_id, 'run_id': saved['id'],
                 'user_id': user['id'], 'changes': results, 'at': db.utc_now()})
    outcome = {'ok': True, 'changes': results, 'summary_updated': False}
    if changes:
        try:
            _write_summary()
            outcome['summary_updated'] = True
        except Exception:
            # The database commit is the success criterion; the derived file can
            # be rebuilt later and must never undo an already completed task.
            outcome.update(_summary_failure('记忆已保存'))
    return outcome


def _eligible(actor, project_ids=None):
    current = _actor(actor)
    readable = _readable_projects(current)
    if project_ids is not None:
        if not isinstance(project_ids, (list, set, tuple)) or any(not isinstance(p, str) for p in project_ids):
            _fail('invalid_memory', '记忆项目范围无效', 422)
        requested = set(project_ids)
        if not requested <= readable:
            _fail('authorization_failed', '记忆项目不在当前账号范围', 403)
        readable &= requested
    return current, readable, [item for item in store.items(_MEMORY) if _visible(item, current, readable)]


def _score(item, query, projects):
    terms = [term for term in re.split(r'\s+', query.casefold()) if term][:32]
    for segment in re.findall(r'[\u4e00-\u9fff]{2,}', query):
        terms.extend(segment[index:index + 2] for index in range(min(len(segment) - 1, 32)))
    terms = list(dict.fromkeys(terms))[:64]
    text = (item.get('topic', '') + ' ' + item.get('content', '')).casefold()
    match = sum(5 if term in item.get('topic', '').casefold() else 1 for term in terms if term in text)
    return (match, item.get('scope') == 'project' and item.get('project_id') in projects,
            item.get('scope') == 'account', item.get('updated_at', ''))


def _public(item, actor, *, history=False, model=False):
    result = {key: copy.deepcopy(item.get(key)) for key in
              ('id', 'scope', 'project_id', 'topic', 'content', 'version', 'status', 'evidence_kind',
               'effective_at', 'created_at', 'updated_at', 'replaces_version', 'applicability')}
    if not result.get('applicability'):
        result['applicability'] = ({'kind': 'account', 'account_id': item.get('account_id', '')}
                                   if item.get('scope') == 'account'
                                   else {'kind': 'project', 'project_id': item.get('project_id', '')}
                                   if item.get('scope') == 'project' else {'kind': 'organization'})
    result['sources'] = []
    for source in item.get('sources', [])[-2:]:
        # Shared memory is usable across accounts without exposing the author's
        # original conversation. Source ownership never becomes execution authority.
        own = source.get('user_id') == actor['id']
        keys = ('run_id', 'message_id', 'user_id') if model else ('run_id', 'message_id', 'quote', 'user_id')
        projected = {key: source.get(key) for key in (keys if own else ('user_id',)) if key in source}
        if source.get('source_kind'):
            projected['source_kind'] = source['source_kind']
        if 'quote' in projected:
            projected['quote'] = str(projected['quote'])[:500]
        result['sources'].append(projected)
    if item.get('status') == 'forgotten':
        result.update(content='', topic='', sources=[])
    return result


def settings_items(actor, *, include_inactive=True):
    """Administrator settings projection; never includes foreign raw messages."""
    user, _, values = _eligible(_actor(actor, write=True))
    values = [item for item in values if item.get('status') != 'superseded'
              and (include_inactive or item.get('status') in {'active', 'candidate'})]
    values.sort(key=lambda item: (item.get('topic_key', ''), item['id']))
    return {'items': [_public(item, user) for item in values], 'has_more': False}


def apply_settings(actor, change, operation_id, expected_version):
    """A real Panel settings action, independent from authenticated chat plans.

    This deliberately records no chat/run ID. Authentication, optimistic version
    checks, immutable operation input and memory history commit in one transaction.
    """
    operation_id = _text(operation_id, '操作标识', 120)
    if not re.fullmatch(r'[A-Za-z0-9_.:-]+', operation_id) or type(expected_version) is not int or expected_version < 0:
        _fail('invalid_memory', '记忆操作标识或预期版本无效', 422)
    if not isinstance(change, dict) or change.get('kind') not in _KINDS:
        _fail('invalid_memory', '记忆操作无效', 422)
    fingerprint = store.digest({'change': change, 'expected_version': expected_version})
    operation_key = 'settings:' + store.digest(operation_id)
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        user = _actor(actor, conn, write=True)
        previous = _get(conn, _OPERATION, operation_key)
        if previous:
            if previous.get('user_id') != user['id'] or previous.get('input_hash') != fingerprint:
                _fail('operation_conflict', '同一操作标识不能用于不同的记忆修改')
            results = copy.deepcopy(previous['changes'])
            for result in results:
                latest = _get(conn, _MEMORY, result['id'])
                if latest and latest.get('status') in {'forgotten', 'inactive'}:
                    result.update(status=latest['status'], suppressed=latest['status'] == 'forgotten')
                result['reused'] = True
        else:
            kind = change['kind']
            readable = _readable_projects(user)
            scope, account_id, project_id = _scope(change, {'allowed_projects': list(readable)}, user, readable)
            current, key, topic, normalized = _resolve(conn, change, scope, account_id, project_id)
            actual = current['version'] if current else 0
            if actual != expected_version:
                raise MemoryError('memory_conflict', '记忆已发生变化，请核对当前内容后再保存',
                                  current_version=actual,
                                  current=_public(current, user) if current else None)
            if kind != 'remember' and not current:
                _fail('memory_not_found', '找不到要修改的记忆', 404)
            now = db.utc_now()
            source = {'source_kind': 'panel_settings', 'source_id': operation_key,
                      'user_id': user['id'], 'quote_hash': fingerprint, 'at': now}
            if kind == 'forget':
                results = [_forget(conn, current, source, now)]
            elif kind == 'invalidate':
                results = [_invalidate(conn, current, source, now)]
            else:
                content = _text(change.get('content'), '记忆内容', 2000)
                if current and current.get('status') == 'candidate':
                    confirmed = [item for item in _all(conn, _MEMORY)
                                 if item['id'] != key and item.get('status') == 'active'
                                 and (*_identity(item), item.get('topic_key'))
                                 == (scope, account_id, project_id, normalized)]
                    if confirmed:
                        raise MemoryError('memory_conflict', '这个主题已有明确约定，请核对并修改该条约定',
                                          current_version=confirmed[0]['version'],
                                          current=_public(confirmed[0], user))
                if current and current.get('status') == 'forgotten':
                    if kind != 'remember':
                        _fail('memory_forgotten', '这条记忆已忘记；新的明确约定请新建')
                    key = 'memory_' + store.digest([scope, project_id, normalized, operation_key])[:32]
                    current = None
                if current and current.get('status') == 'active' and current.get('content') == content:
                    results = [{'kind': kind, 'id': key, 'version': current['version'],
                                'status': 'active', 'reused': True}]
                else:
                    if current:
                        _history(conn, current)
                    applicability = copy.deepcopy((current or {}).get('applicability') or (
                        {'kind': 'account', 'account_id': user['id']} if change.get('scope') == 'account'
                        else {'kind': 'project', 'project_id': project_id} if scope == 'project'
                        else {'kind': 'organization'}))
                    item = {'id': key, 'scope': scope, 'account_id': account_id, 'project_id': project_id,
                            'topic': topic, 'topic_key': normalized, 'content': content,
                            'status': 'active', 'evidence_kind': 'explicit', 'change_kind': kind,
                            'version': current['version'] + 1 if current else 1, 'sources': [source],
                            'created_at': (current or {}).get('created_at', now), 'updated_at': now,
                            'effective_at': now, 'updated_by': user['id'], 'applicability': applicability,
                            'replaces_version': (current or {}).get('version'),
                            'audience': (current or {}).get('audience') or (
                                'central_admins' if change.get('scope') == 'account' else 'permitted_members')}
                    _put(conn, _MEMORY, key, item)
                    _history(conn, item)
                    results = [{'kind': kind, 'id': key, 'version': item['version'],
                                'status': 'active', 'reused': False}]
                    for candidate in _all(conn, _MEMORY):
                        if (candidate['id'] != key and candidate.get('status') == 'candidate'
                                and (*_identity(candidate), candidate.get('topic_key'))
                                == (scope, account_id, project_id, normalized)):
                            _history(conn, candidate)
                            candidate.update(status='superseded', version=candidate['version'] + 1,
                                             updated_at=now, replaced_by=key)
                            _put(conn, _MEMORY, candidate['id'], candidate)
                            _history(conn, candidate)
            _put(conn, _OPERATION, operation_key, {'id': operation_key, 'operation_id': operation_id,
                 'source_kind': 'panel_settings', 'user_id': user['id'], 'input_hash': fingerprint,
                 'changes': results, 'at': now})
    outcome = {'ok': True, 'saved': True, 'changes': results, 'summary_updated': False}
    try:
        _write_summary()
        outcome['summary_updated'] = True
    except Exception:
        outcome.update(_summary_failure('记忆已保存'))
    return outcome


def recall(actor, project_ids, query, limit=8):
    if not isinstance(query, str) or len(query) > 10000 or type(limit) is not int or not 1 <= limit <= _LIMIT:
        _fail('invalid_memory', '记忆检索条件无效', 422)
    user, projects, values = _eligible(actor, project_ids)
    values = [item for item in values if item.get('status') == 'active']
    values.sort(key=lambda item: _score(item, query, projects), reverse=True)
    return [_public(item, user, model=True) for item in values[:limit]]


def search(actor, query='', project_id=None, include_history=False, *, allowed_projects=None):
    if not isinstance(query, str) or len(query) > 2000 or type(include_history) is not bool:
        _fail('invalid_memory', '记忆查询条件无效', 422)
    project_id = project_id or None
    requested = None if project_id is None else [project_id]
    if allowed_projects is not None:
        if not isinstance(allowed_projects, (list, set, tuple)) or any(not isinstance(p, str) for p in allowed_projects):
            _fail('invalid_memory', '记忆查询范围无效', 422)
        permitted = set(allowed_projects) & _readable_projects(_actor(actor))
        if project_id is not None and project_id not in permitted:
            _fail('authorization_failed', '记忆项目不在当前运行范围', 403)
        requested = list(permitted) if project_id is None else [project_id]
    user, projects, values = _eligible(actor, requested)
    current = {item['id']: item for item in values}
    q = query.strip().casefold()
    matches = [item for item in values if item.get('status') in {'active', 'candidate'}
               and (not q or q in (item.get('topic', '') + ' ' + item.get('content', '')).casefold() or item['id'] == query)]
    matches.sort(key=lambda item: _score(item, query, projects), reverse=True)
    result = {'items': [_public(item, user) for item in matches[:50]], 'has_more': len(matches) > 50}
    if include_history:
        old = [item for item in store.items(_HISTORY) if item['id'] in current
               and current[item['id']].get('status') != 'forgotten'
               and (not q or q in (item.get('topic', '') + ' ' + item.get('content', '')).casefold() or item['id'] == query)]
        old.sort(key=lambda item: (item.get('updated_at', ''), item['version']), reverse=True)
        result['history'] = [_public(item, user, history=True) for item in old[:50]]
        result['history_has_more'] = len(old) > 50
    return result


def inspection_metadata(actor, allowed_projects):
    _, _, values = _eligible(actor, allowed_projects)
    return [{key: item.get(key) for key in ('id', 'topic', 'scope', 'project_id', 'status', 'version', 'updated_at')}
            for item in values if item.get('status') != 'forgotten']


def context_history(history, actor):
    """Project model history, without deleting the original audit or chat UI."""
    user, projects, current = _eligible(actor)
    versions = {item['id']: item['version'] for item in current}
    tombstones = [item for item in store.items(_TOMBSTONE) if _visible(item, user, projects)]
    forgotten = {item['id'] for item in tombstones}
    sources = [source for item in tombstones for source in item.get('sources', [])]
    inactive = [item for item in store.items(_MEMORY)
                if item.get('status') in {'inactive', 'superseded'} and _visible(item, user, projects)]
    excluded = forgotten | {item['id'] for item in inactive}
    sources += [source for item in inactive for source in item.get('sources', [])]
    inactive_ids = {item['id'] for item in inactive}
    sources += [source for item in store.items(_HISTORY) if item.get('id') in inactive_ids
                for source in item.get('sources', [])]
    active = {item['id']: item for item in current if item.get('status') == 'active'}
    for old in store.items(_HISTORY):
        latest = active.get(old.get('id'))
        if latest and old['version'] < latest['version'] and old.get('content') != latest.get('content'):
            sources.extend(source for source in old.get('sources', []) if source not in latest.get('sources', []))
    result = []
    for original in history:
        message = copy.deepcopy(original)
        dependencies = _refs(message.get('memory_refs', []))
        ref_versions = dict(message.get('memory_ref_versions') or {})
        run_id = message.get('run_id')
        if run_id:
            saved = store.get('run', run_id) or {}
            if saved.get('user_id') == user['id'] or 'admin' in user.get('roles', []):
                dependencies |= _refs(saved.get('memory_refs', []))
                ref_versions.update(saved.get('memory_ref_versions') or {})
        stale = any(key in versions and type(version) is int and version < versions[key]
                    for key, version in ref_versions.items())
        if (dependencies & excluded or stale) and message.get('role') == 'assistant':
            continue
        content = message.get('content')
        if isinstance(content, str):
            spans = []
            for source in sources:
                if (source.get('source_digest') == store.digest(content)
                        and (not message.get('run_id') or message.get('run_id') == source.get('run_id'))):
                    spans.extend(tuple(span) for span in source.get('spans', [[source['start'], source['end']]]))
            merged = []
            for start, end in sorted(set(spans)):
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                else:
                    merged.append((start, end))
            for start, end in reversed(merged):
                content = content[:start] + '[这项旧约定不再适用]' + content[end:]
            message['content'] = content
        result.append(message)
    return result


def filter_conversation(messages, actor, run=None):
    staged = copy.deepcopy(messages)
    if run:
        for item in reversed(staged):
            if item.get('role') == 'user' and item.get('content') == run.get('message'):
                if not item.get('run_id'):
                    item.update(run_id=run['id'], _memory_current=True)
                break
    filtered = context_history(staged, actor)
    calls = {call.get('id') for item in filtered if item.get('role') == 'assistant'
             for call in item.get('tool_calls', []) if isinstance(call, dict)}
    filtered = [item for item in filtered if item.get('role') != 'tool' or item.get('tool_call_id') in calls]
    for item in filtered:
        if item.pop('_memory_current', False):
            item.pop('run_id', None)
    return filtered


def stamp_refs(run, ids):
    run['memory_refs'] = sorted(_refs(list(ids)))
    run['memory_ref_versions'] = {key: item['version'] for key in run['memory_refs']
                                  if (item := store.get(_MEMORY, key)) is not None}
    return run


def stamp_displayed(run, items):
    """Mark explicit search results, never automatic recall, for 'that memory'."""
    run['displayed_memory_ids'] = [item['id'] for item in items if isinstance(item, dict) and item.get('id')]
    run['displayed_memory_versions'] = {item['id']: item['version'] for item in items
                                        if isinstance(item, dict) and item.get('id') and 'version' in item}
    run['memory_displayed_at'] = db.utc_now()
    return run


def initialize():
    """Migrate existing memory entries only; never mine chats or events."""
    runtime.environment()
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        current = _get(conn, 'state', 'central_memory_schema')
        created = current is None
        if current is not None and current.get('version') not in {1, 2}:
            _fail('memory_schema_unsupported', '中央记忆记录版本不兼容，请保留数据并检查配对代码')
        migrated = []
        if current is None or current.get('version') == 1:
            now = db.utc_now()
            for item in _all(conn, _MEMORY):
                if item.get('scope') != 'account':
                    continue
                _history(conn, item)
                shared = {**item, 'scope': 'organization', 'account_id': '', 'audience': 'central_admins',
                          'legacy_scope': 'account', 'legacy_account_id': item.get('account_id', ''),
                          'applicability': {'kind': 'account', 'account_id': item.get('account_id', '')},
                          'shared_migrated_at': now, 'version': item['version'] + 1,
                          'updated_at': now, 'change_kind': 'shared_scope_migration',
                          'replaces_version': item['version']}
                _put(conn, _MEMORY, item['id'], shared)
                _history(conn, shared)
                migrated.append(item['id'])
            # Equal legacy declarations become one current entry. Preserve all
            # original IDs, source authors and versions; conflicting declarations
            # stay separate until a person explicitly resolves them.
            topics = {}
            for item in _all(conn, _MEMORY):
                if item.get('status') == 'active':
                    topics.setdefault((*_identity(item), item.get('topic_key'),
                                       json.dumps(item.get('applicability') or {}, sort_keys=True)), []).append(item)
            for variants in topics.values():
                if len(variants) < 2 or len({item.get('content') for item in variants}) != 1:
                    continue
                canonical, *duplicates = sorted(variants, key=lambda item: item['id'])
                for duplicate in duplicates:
                    for source in duplicate.get('sources', []):
                        if source not in canonical.get('sources', []):
                            canonical.setdefault('sources', []).append(source)
                    _history(conn, duplicate)
                    duplicate.update(status='superseded', version=duplicate['version'] + 1,
                                     updated_at=now, replaced_by=canonical['id'])
                    _put(conn, _MEMORY, duplicate['id'], duplicate)
                    _history(conn, duplicate)
                _put(conn, _MEMORY, canonical['id'], canonical)
                _history(conn, canonical)
            _put(conn, 'state', 'central_memory_schema', {'version': 2,
                 'initialized_at': (current or {}).get('initialized_at', now),
                 'shared_at': now, 'history_imported': False, 'storage': 'central_records',
                 'migrated_ids': migrated})
    result = {'schema_version': 2, 'created': created, 'migrated_ids': migrated,
              'history_imported': False, 'summary_updated': False}
    # This is a rollback-only compatibility patch. The established formal
    # database already has its summary; restart must preserve direct edits.
    # A missing file is rejected by the release preflight, never regenerated
    # through this startup path. Authorized memory saves retain their writer.
    if os.environ.get('RDOS_RUNTIME_MODE') == 'production' and current is not None and current.get('version') == 2:
        result['summary_preserved'] = True
    else:
        try:
            _write_summary()
            result['summary_updated'] = True
        except Exception:
            result.update(_summary_failure('中央记忆已初始化'))
    return result


def manifest_summary():
    # Counts and the Markdown view are operational metadata, not a second source.
    values = store.items(_MEMORY)
    return {'schema_version': 2, 'storage': 'central_records', 'summary_scope': 'shared',
            'active_count': sum(item.get('status') == 'active' for item in values),
            'candidate_count': sum(item.get('status') == 'candidate' for item in values),
            'summary_exists': _summary_path().is_file()}


def _summary_path():
    if os.environ.get('RDOS_RUNTIME_MODE') == 'production':
        from server.production_runtime import require
        path = require().memory_md
        if path.is_symlink() or path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
            _fail('memory_summary_path', '中央记忆摘要文件异常')
        return path
    raw = Path(os.environ['RDOS_LAB_RUNTIME'])
    if not raw.is_absolute() or raw.is_symlink() or raw.resolve() != db.db_path().resolve().parent:
        _fail('memory_summary_path', '中央记忆摘要位置不属于当前运行目录')
    directory = raw / 'central'
    if directory.is_symlink() or directory.exists() and not directory.is_dir():
        _fail('memory_summary_path', '中央记忆摘要目录异常')
    path = directory / 'memory.md'
    if path.is_symlink():
        _fail('memory_summary_path', '中央记忆摘要文件异常')
    return path


def _discard_summary():
    """Retire only our fixed derived file; never follow a replacement symlink."""
    with _SUMMARY_LOCK:
        path = _summary_path()
        existed = path.exists()
        path.unlink(missing_ok=True)
        return existed


def _summary_failure(prefix):
    try:
        removed = _discard_summary()
    except Exception:
        return {'summary_removed': False, 'summary_stale': True,
                'summary_error': prefix + '，摘要更新和旧摘要清理未完成；旧摘要可能仍含过期内容，暂不可作为依据。'}
    return {'summary_removed': removed, 'summary_stale': False,
            'summary_error': prefix + '，摘要暂不可用；旧摘要已停用，可稍后重建。'}


def _write_summary():
    with _SUMMARY_LOCK:
        path = _summary_path()
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.parent.chmod(0o700)
        values = [item for item in store.items(_MEMORY) if item.get('status') == 'active']
        values.sort(key=lambda item: (item['topic_key'], item['id']))
        lines = ['# 中央共享记忆摘要', '', '本文件由数据库自动生成，不是可独立修改的记忆来源。',
                 '管理员共用中央记忆；项目标记说明适用工作，不表示个人私有记忆。',
                 '本文件仅保存在受限运行目录，不进入公开下载或节点材料。', '']
        size = sum(len(line) for line in lines)
        for item in values:
            applicability = item.get('applicability') or {}
            label = ('[账号 ' + applicability.get('account_id', '') + ' 的约定] '
                     if applicability.get('kind') == 'account' else
                     '[项目 ' + item.get('project_id', '') + '] ' if item.get('scope') == 'project' else '[机构约定] ')
            row = '- ' + label + item['topic'].replace('\n', ' ') + '：' + item['content'].replace('\n', ' ')
            if size + len(row) > 12000:
                lines.append('- 其余有效记忆请按主题查询数据库。')
                break
            lines.append(row)
            size += len(row)
        temporary = path.with_name('memory.md.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                output.write('\n'.join(lines) + '\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)
