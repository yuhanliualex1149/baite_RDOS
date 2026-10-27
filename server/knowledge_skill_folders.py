"""Skill identity and snapshots follow first-level Feishu folders, not files."""
from __future__ import annotations

import copy
from server import central_store as store, db
from server.phase3_feishu import KNOWLEDGE_ROOTS


def _error(code, message, status=409):
    from server.shared_knowledge import KnowledgeError
    raise KnowledgeError(code, message, status)


def identifier(token):
    return 'knowledge_skill_' + store.digest(['shared_skills', token])[:24]


def _put(conn, kind, key, value):
    import json
    conn.execute('INSERT INTO central_records VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at',
                 (kind, key, json.dumps(value, ensure_ascii=False), db.utc_now()))


def assemble(folder, values):
    """Validate one complete, connected subtree and pin every member version."""
    token = folder['source_token']
    by_token = {item['source_token']: item for item, _ in values}
    root = by_token.get(token)
    if (not root or root['type'] != 'folder'
            or root.get('parent_token') != KNOWLEDGE_ROOTS['shared_skills']):
        _error('incomplete_skill_folder', 'Skill文件夹根信息不完整')
    paths, pending = {token: []}, set(by_token) - {token}
    while pending:
        ready = [t for t in pending if by_token[t].get('parent_token') in paths]
        if not ready:
            _error('out_of_scope', 'Skill包内包含范围外或循环文件')
        for current in ready:
            item = by_token[current]
            paths[current] = [*paths[item['parent_token']], item['name']]
            pending.remove(current)
    files = []
    for current, segments in paths.items():
        if current == token:
            continue
        item = copy.deepcopy(by_token[current])
        item.update(relative_segments=segments,
                    relative_path='/'.join(part.replace('/', '／') for part in segments))
        files.append(item)
    files.sort(key=lambda item: (item['relative_path'].casefold(), item['id']))
    candidates = [f for f in files if f['name'].casefold() == 'skill.md' and f['content_status'] == 'full_text']
    if candidates:
        depth = min(len(f['relative_segments']) for f in candidates)
        candidates = [f for f in candidates if len(f['relative_segments']) == depth]
    entry = candidates[0] if len(candidates) == 1 else None
    version = 'sha256:' + store.digest({'name': root['name'], 'files': [
        [f['source_token'], f['relative_segments'], f['type'], f['version'] if f['type'] != 'folder' else None]
        for f in files]})
    item = {key: copy.deepcopy(root[key]) for key in
            ('name', 'path', 'path_segments', 'source_token', 'parent_token', 'source_url', 'source_status', 'available') if key in root}
    item.update(id=identifier(token), category='shared_skills', type='skill_package', content_status='skill_package',
                version=version, file_count=sum(f['type'] != 'folder' for f in files),
                text_count=sum(f['content_status'] == 'full_text' for f in files),
                attachment_count=sum(f['content_status'] in {'attachment_only', 'file_listing'} for f in files),
                entrypoint_id=entry['id'] if entry else None,
                entrypoint_status='ready' if entry else 'ambiguous' if candidates else 'missing',
                sync_status='succeeded', last_error='', available=True)
    return {'item': item, 'files': files, 'entrypoint_id': item['entrypoint_id']}


def manifests():
    """Project the already complete legacy cache without remote calls or writes."""
    records = {item['id']: {'item': item} for item in store.items('knowledge_skill')}
    raw = [item for item in store.items('knowledge_item') if item.get('category') == 'shared_skills']
    children = {}
    for item in raw:
        children.setdefault(item.get('parent_token'), []).append(item)
    for root in children.get(KNOWLEDGE_ROOTS['shared_skills'], []):
        if root['type'] != 'folder' or identifier(root['source_token']) in records:
            continue
        values, stack, seen = [], [root], set()
        while stack:
            item = stack.pop()
            token = item['source_token']
            if token in seen:
                _error('incomplete_skill_folder', '缓存中的Skill目录存在循环')
            seen.add(token)
            values.append((item, ''))
            stack.extend(child for child in children.get(token, [])
                         if not root.get('available', True) or child.get('available', True))
        package = assemble(root, values)
        package['item']['available'] = root.get('available', True)
        package['item']['synced_at'] = root.get('synced_at')
        records[package['item']['id']] = package
    for key, record in records.items():
        if 'files' not in record and record['item'].get('version'):
            snapshot = store.get('knowledge_skill_version', store.digest([key, record['item']['version']]))
            if snapshot is not None:
                record = {**copy.deepcopy(snapshot), 'item': {**snapshot['item'], **record['item']}}
                records[key] = record
        state = store.get('knowledge_skill_state', key) or {}
        record['item'] = {**record['item'], **state}
    return records


def items():
    return [record['item'] for record in manifests().values()]


def read(key, version=None):
    current = manifests().get(key)
    if not current:
        _error('knowledge_not_found', '找不到这个Skill文件夹', 404)
    selected = version or current['item'].get('version', '')
    record = store.get('knowledge_skill_version', store.digest([key, selected])) if selected else None
    if record is None:
        if selected != current['item'].get('version', ''):
            _error('knowledge_version_not_found', '找不到这个Skill版本', 404)
        record = current
        if 'files' not in record:
            if selected:
                _error('knowledge_version_not_found', 'Skill版本清单尚未完整保存', 404)
            record = {**record, 'files': [], 'entrypoint_id': None}
    record = copy.deepcopy(record)
    item = record['item']
    item.update({k: current['item'][k] for k in ('available', 'sync_status', 'last_error', 'error_code') if k in current['item']})
    content = ''
    entry = next((f for f in record['files'] if f['id'] == record.get('entrypoint_id')), None)
    if entry:
        source = store.get('knowledge_content', store.digest([entry['id'], entry['version']]))
        if not source:
            _error('knowledge_version_not_found', 'Skill说明的对应版本尚未完整保存', 404)
        content = source.get('content', '')
    return {**record, 'item': item, 'content': content, 'version': selected}


def commit(conn, package, values, now):
    key = package['item']['id']
    previous = manifests().get(key)
    if previous and 'files' in previous:
        _put(conn, 'knowledge_skill_version', store.digest([key, previous['item']['version']]), previous)
    package = copy.deepcopy(package)
    package['item'].update(synced_at=now, sync_status='succeeded', last_error='')
    for item, body in values:
        item = {**item, 'synced_at': now}
        if not store.get('knowledge_content', store.digest([item['id'], item['version']])):
            _put(conn, 'knowledge_content', store.digest([item['id'], item['version']]), {'item': item, 'content': body})
        _put(conn, 'knowledge_item', item['id'], item)
    if not store.get('knowledge_skill_version', store.digest([key, package['item']['version']])):
        _put(conn, 'knowledge_skill_version', store.digest([key, package['item']['version']]), package)
    member_ids = {item['id'] for item, _ in values}
    if previous and 'files' in previous:
        for prior in previous['files']:
            if prior['id'] not in member_ids:
                old = store.get('knowledge_item', prior['id'])
                if old:
                    _put(conn, 'knowledge_item', prior['id'], {**old, 'available': False, 'removed_at': now})
    _put(conn, 'knowledge_skill', key, package['item'])
    _put(conn, 'knowledge_skill_state', key, {'sync_status': 'succeeded', 'last_error': '', 'error_code': '', 'last_attempt_at': now})


def failure(conn, folder, message, code, now):
    key = identifier(folder['token'])
    if key not in manifests():
        _put(conn, 'knowledge_skill', key, {
            'id': key, 'category': 'shared_skills', 'type': 'skill_package', 'content_status': 'skill_package',
            'name': folder['name'], 'path': folder['name'], 'source_token': folder['token'],
            'parent_token': KNOWLEDGE_ROOTS['shared_skills'], 'source_url': folder.get('source_url', ''),
            'version': '', 'available': True, 'file_count': 0, 'text_count': 0,
            'entrypoint_id': None, 'entrypoint_status': 'missing'})
    _put(conn, 'knowledge_skill_state', key, {'sync_status': 'failed', 'last_error': message, 'error_code': code, 'last_attempt_at': now})


def mark_removed(conn, present, now):
    for key, record in manifests().items():
        if key in present:
            continue
        item = {**record['item'], 'available': False, 'removed_at': now}
        if 'files' in record:
            _put(conn, 'knowledge_skill_version', store.digest([key, item['version']]), record)
        _put(conn, 'knowledge_skill', key, item)
        for member in record.get('files', []):
            old = store.get('knowledge_item', member['id'])
            if old:
                _put(conn, 'knowledge_item', old['id'], {**old, 'available': False, 'removed_at': now})


def mark_missing(conn, key, now):
    """A complete root listing disproves only this requested folder's presence."""
    record = manifests().get(key)
    if not record:
        return
    item = {**record['item'], 'available': False, 'removed_at': now}
    if 'files' in record and item.get('version'):
        _put(conn, 'knowledge_skill_version', store.digest([key, item['version']]), record)
    _put(conn, 'knowledge_skill', key, item)
    for member in record.get('files', []):
        old = store.get('knowledge_item', member['id'])
        if old:
            _put(conn, 'knowledge_item', old['id'], {**old, 'available': False, 'removed_at': now})
    _put(conn, 'knowledge_skill_state', key, {
        'sync_status': 'failed', 'error_code': 'skill_not_found',
        'last_error': '该Skill文件夹已不在共享根目录，保留上次版本。',
        'last_attempt_at': now})
