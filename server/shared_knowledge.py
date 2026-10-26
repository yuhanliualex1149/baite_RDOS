"""Versioned read-only institution knowledge, sourced from fixed Feishu roots.

GETs use durable snapshots only. A manual RAG read commits as one snapshot;
each first-level Skill folder commits as its own complete version. No task
writes, Skill execution, connector login, or production mutation occurs.
"""
from __future__ import annotations

import asyncio
import json
import hashlib
import re
import threading
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from server import central_store as store, db, knowledge_skill_folders as skills
from server.phase3_feishu import KNOWLEDGE_ROOTS, TENANT

_NAMES = {'selected_rag': 'Selected RAG', 'shared_skills': 'Shared Skills'}
_LOCK = threading.Lock()
_ACTIVE = set()
_MAX_TEXT = 50_000_000


class KnowledgeError(ValueError):
    def __init__(self, code, message, status=409):
        self.code, self.status = code, status
        super().__init__(message)


def _error(code, message, status=409):
    raise KnowledgeError(code, message, status)


def _actor(actor, *, admin=False):
    # Internal catalog reads can omit actor; public routes always authenticate.
    if actor is None:
        if admin:
            _error('authorization_failed', '此操作需要管理员身份', 403)
        return None
    from server.central_maintenance import permits
    if permits(actor, "knowledge"):
        return actor
    value = store.user(actor.get('id', '')) if isinstance(actor, dict) else None
    if not value or not value.get('enabled') or not set(value.get('roles', [])) & {'admin', 'researcher'}:
        _error('authorization_failed', '账号当前不能读取共享知识', 403)
    if admin and 'admin' not in value['roles']:
        _error('authorization_failed', '此操作需要管理员身份', 403)
    return value


def _put(conn, kind, identifier, value):
    conn.execute('INSERT INTO central_records VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at',
        (kind, identifier, json.dumps(value, ensure_ascii=False), db.utc_now()))


def _sources():
    result = []
    for category, name in _NAMES.items():
        state = store.get('knowledge_source', category) or {}
        configured = category in KNOWLEDGE_ROOTS
        status = state.get('status', 'never' if configured else 'not_configured')
        if status == 'syncing' and state.get('operation_id') not in _ACTIVE:
            status = 'interrupted'
        result.append({'category': category, 'name': name, 'configured': configured,
            'source_url': TENANT + '/drive/folder/' + KNOWLEDGE_ROOTS[category] if configured else '',
            'status': status, 'error_code': state.get('error_code', ''), 'failed_source': state.get('failed_source'),
            'last_success_at': state.get('last_success_at'),
            'last_attempt_at': state.get('last_attempt_at'),
            'last_error': ('上次同步中断，显示最近成功内容。' if status == 'interrupted' else state.get('last_error', '')),
            'snapshot_id': state.get('snapshot_id'), 'item_count': state.get('item_count', 0),
            'text_count': state.get('text_count', 0), 'attachment_count': state.get('attachment_count', 0)})
    return result


def catalog(actor=None):
    """Cached metadata only: no connector or model calls."""
    _actor(actor)
    with db.consistent_snapshot():
        sources = _sources()
        packages = skills.items()
        items = sorted([item for item in store.items('knowledge_item') if item.get('category') == 'selected_rag'] + packages,
                       key=lambda item: (item['category'], item['path'].casefold(), item['id']))
        for source in sources:
            if source['category'] == 'shared_skills':
                active = [p for p in packages if p.get('available')]
                source.update(item_count=len(active), skill_count=len(active),
                    text_count=sum(p.get('text_count', 0) for p in active),
                    attachment_count=sum(p.get('attachment_count', 0) for p in active))
    return {'sources': sources, 'items': items}


def model_summary(actor=None):
    """Only category availability enters the initial central model context."""
    data = catalog(actor)
    sources = []
    for source in data['sources']:
        active = [item for item in data['items'] if item.get('available') and item.get('category') == source['category']]
        count = sum(item.get('text_count', 0) if item['type'] == 'skill_package' else item.get('content_status') == 'full_text' for item in active)
        sources.append({key: source[key] for key in
            ('category', 'name', 'configured', 'status', 'last_success_at')} |
            {'available_text_count': count, 'available_skill_count': sum(item['type'] == 'skill_package' for item in active)})
    return {'sources': sources, 'available_text_count': sum(s['available_text_count'] for s in sources),
            'available_skill_count': sum(s['available_skill_count'] for s in sources)}


def model_catalog(actor=None, *, category=None, query=''):
    """Explicit category/name lookup; no regex, connector calls or link following."""
    if category is not None and (not isinstance(category, str) or category not in _NAMES):
        _error('invalid_knowledge_category', '请选择有效的知识分类', 422)
    if not isinstance(query, str) or len(query) > 200:
        _error('invalid_knowledge_query', '知识查找文字须为不超过200字的文本', 422)
    data = catalog(actor)
    needle = query.strip().casefold()
    matches = []
    for item in data['items']:
        if not item.get('available'):
            continue
        if category is not None and item['category'] != category:
            continue
        matched = not needle or needle in item['name'].casefold() or needle in item['path'].casefold()
        if not matched and item['type'] == 'skill_package' and item.get('version'):
            detail = skills.read(item['id'])
            matched = any(needle in f['name'].casefold() or needle in f['relative_path'].casefold() for f in detail['files'])
        if matched:
            matches.append(item)
    return {'sources': [source for source in data['sources'] if category is None or source['category'] == category],
        'items': [{key: item[key] for key in
            ('id', 'category', 'name', 'path', 'type', 'source_url', 'version', 'source_revision',
             'content_status', 'available', 'source_status', 'file_count', 'text_count', 'entrypoint_status') if key in item} for item in matches],
        'total_matches': len(matches)}


def read_item(identifier, actor=None, version=None, *, skill_id=None, package_version=None):
    _actor(actor)
    member = None
    if identifier.startswith('knowledge_skill_'):
        return skills.read(identifier, version)
    if skill_id:
        package = skills.read(skill_id, package_version)
        member = next((f for f in package['files'] if f['id'] == identifier), None)
        if not member or (version is not None and version != member['version']):
            _error('out_of_scope', '文件不属于这个Skill版本', 403)
        version = member['version']
    item = store.get('knowledge_item', identifier)
    if not item:
        _error('knowledge_not_found', '找不到这项共享知识', 404)
    selected = version or item['version']
    key = store.digest([identifier, selected])
    record = store.get('knowledge_content', key)
    if not record:
        _error('knowledge_version_not_found', '找不到这个知识版本', 404)
    if version:
        item = {**record['item'], 'available': item.get('available', False)}
    if member is not None:
        item = {**member, 'available': item.get('available', False)}
    return {'item': item, 'content': record.get('content', ''), 'version': selected}


def _source_status(path):
    # These labels describe the source itself, never assert institutional approval.
    if any(word in path for word in ('草稿', '参考包')):
        return '来源标注为草稿'
    if any(word in path for word in ('测试版本', '测试版')):
        return '来源标注为测试版本'
    return '来源目录版本，未推定机构发布状态'


def _normalize(category, raw):
    if not isinstance(raw, list):
        _error('incomplete_knowledge', '知识来源没有返回完整文件清单')
    output, tokens, total = [], set(), 0
    for value in raw:
        if not isinstance(value, dict):
            _error('incomplete_knowledge', '知识文件记录格式不完整')
        token = value.get('token')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{4,200}', token) or token in tokens:
            _error('incomplete_knowledge', '知识对象标识缺失或重复')
        tokens.add(token)
        status = value.get('content_status')
        if status not in {'full_text', 'attachment_only', 'file_listing', 'folder'}:
            _error('incomplete_knowledge', '知识正文读取状态无效')
        body = value.get('content', '')
        if not isinstance(body, str) or (status != 'full_text' and body):
            _error('incomplete_knowledge', '知识正文读取状态与内容不一致')
        total += len(body.encode('utf-8'))
        if total > _MAX_TEXT:
            _error('incomplete_knowledge', '知识正文超过本轮完整同步范围')
        name, path, kind = value.get('name'), value.get('path'), value.get('type')
        if not all(isinstance(v, str) and v for v in (name, path, kind)):
            _error('incomplete_knowledge', '知识文件名称或目录信息缺失')
        content_hash = hashlib.sha256(body.encode('utf-8')).hexdigest() if status == 'full_text' else store.digest({k: value.get(k) for k in ('name', 'path', 'type', 'modified_time')})
        revision = str(value.get('source_revision') or '')
        # Source revision changes are metadata; identical bodies share one version.
        version = 'sha256:' + content_hash
        identifier = 'knowledge_' + store.digest([category, token])[:24]
        item = {'id': identifier, 'category': category, 'name': name, 'path': path, 'type': kind,
            'source_token': token, 'parent_token': value.get('parent_token', ''),
            'path_segments': list(value.get('path_segments', [path])),
            'source_url': TENANT + ('/drive/folder/' if kind == 'folder' else '/docx/' if kind == 'docx' else '/file/') + token,
            'version': version, 'source_revision': revision, 'content_hash': content_hash,
            'content_status': status, 'available': True, 'modified_time': value.get('modified_time', ''),
            'source_marker': value.get('source_marker', ''), 'cache_reused': bool(value.get('cache_reused')),
            'source_status': _source_status(path)}
        output.append((item, body))
    return output


def sync_project_files(backend, project_id, actor, operation_id):
    """Atomic current-directory projection; historical bodies are retained."""
    from server import rdos_projects
    from server.central_maintenance import permits
    if not permits(actor, 'sync', project_id):
        _actor(actor, admin=True)
    rdos_projects.get_project(project_id)
    category = 'project:' + project_id
    reader = getattr(backend, 'read_project_files', None)
    if reader:
        raw = reader(project_id)
    elif getattr(backend, 'registry')().get('mode') == 'local_mock':
        registry = backend.registry()
        raw = []
        for document in registry.get('documents', []):
            if document.get('project_id', rdos_projects.DEFAULT_PROJECT_ID) != project_id:
                continue
            doc = backend.read_document(document.get('id') or document.get('object_id'))
            raw.append({'token': doc['id'], 'name': doc.get('title') or doc['id'], 'path': doc.get('title') or doc['id'],
                        'type': 'docx', 'content_status': 'full_text', 'content': doc['content'],
                        'source_revision': str(doc['revision'])})
    else:
        _error('project_source_unavailable', '项目目录读取能力尚未配置', 503)
    values = _normalize(category, raw)
    now = db.utc_now()
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        seen = set()
        for item, body in values:
            item.update(project_id=project_id, synced_at=now)
            seen.add(item['id'])
            key = store.digest([item['id'], item['version']])
            if not store.get('central_project_content', key):
                _put(conn, 'central_project_content', key, {'item': item, 'content': body})
            _put(conn, 'central_project_source', item['id'], item)
        for old in store.items('central_project_source'):
            if old.get('project_id') == project_id and old['id'] not in seen:
                _put(conn, 'central_project_source', old['id'], {**old, 'available': False, 'removed_at': now})
    return {'project_id': project_id, 'status': 'succeeded', 'item_count': len(values),
            'body_reused_count': sum(bool(i.get('cache_reused')) for i, _ in values)}


def _record_attempt(categories, operation_id, actor, skill_id=None):
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old = conn.execute("SELECT payload FROM central_records WHERE kind='knowledge_sync' AND id=?", (operation_id,)).fetchone()
        if old:
            operation = json.loads(old[0])
            if operation.get('categories') != categories or operation.get('user_id') != actor['id'] or operation.get('skill_id') != skill_id:
                _error('operation_conflict', '同一操作标识不能用于不同知识同步')
            return operation
        now = db.utc_now()
        operation = {'id': operation_id, 'categories': categories, 'skill_id': skill_id, 'user_id': actor['id'], 'status': 'syncing', 'started_at': now}
        _put(conn, 'knowledge_sync', operation_id, operation)
        for category in categories:
            row = conn.execute("SELECT payload FROM central_records WHERE kind='knowledge_source' AND id=?", (category,)).fetchone()
            source = json.loads(row[0]) if row else {}
            source.update(status='syncing', last_attempt_at=now, last_error='', error_code='', failed_source=None, operation_id=operation_id)
            _put(conn, 'knowledge_source', category, source)
        return operation


def _error_info(exc):
    code = getattr(exc, 'code', 'knowledge_read_failed')
    reasons = {
        'pagination_failed': '来源目录分页未读完',
        'out_of_scope': '来源对象超出已登记目录',
        'account_changed': '飞书调用账号与登记账号不一致',
        'source_permission_denied': '飞书来源权限不足，请核对读取授权',
        'source_authentication_failed': '飞书连接认证已失效，请重新授权',
        'cli_validation_failed': '飞书CLI拒绝读取参数，需要检查连接适配',
        'source_encoding_invalid': '来源Markdown不是UTF-8文本',
        'knowledge_unavailable': '飞书知识读取服务不可用',
        'skill_not_found': '来源文件夹已不在Shared Skills根目录',
    }
    failed_source = getattr(exc, 'knowledge_source', None)
    label = ''
    if isinstance(failed_source, dict) and isinstance(failed_source.get('name'), str):
        label = '（' + re.sub(r'[\x00-\x1f\x7f]', '', failed_source['name'])[:120] + '）'
    return code, '知识同步未完成：' + reasons.get(code, '来源内容未能完整读取') + label + '。保留上次成功内容。', failed_source


def _failed_source(category, operation_id, exc):
    code, message, failed = _error_info(exc)
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        state = store.get('knowledge_source', category) or {}
        state.update(status='failed', last_attempt_at=db.utc_now(), last_error=message,
                     error_code=code, failed_source=failed, operation_id=operation_id)
        _put(conn, 'knowledge_source', category, state)
    return {'category': category, 'status': 'failed', 'error_code': code, 'message': message}


def _sync_rag(backend, actor, operation_id):
    values = _normalize('selected_rag', backend.read_knowledge('selected_rag'))
    _actor(actor, admin=True)
    now = db.utc_now()
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        seen = set()
        for item, body in values:
            item['synced_at'] = now
            seen.add(item['id'])
            if not store.get('knowledge_content', store.digest([item['id'], item['version']])):
                _put(conn, 'knowledge_content', store.digest([item['id'], item['version']]), {'item': item, 'content': body})
            _put(conn, 'knowledge_item', item['id'], item)
        for old in store.items('knowledge_item'):
            if old.get('category') == 'selected_rag' and old['id'] not in seen:
                _put(conn, 'knowledge_item', old['id'], {**old, 'available': False, 'removed_at': now})
        snapshot_id = 'snapshot_' + store.digest(['selected_rag', [(i['id'], i['version']) for i, _ in values]])
        _put(conn, 'knowledge_snapshot', snapshot_id, {'id': snapshot_id, 'category': 'selected_rag', 'synced_at': now, 'item_ids': sorted(seen)})
        _put(conn, 'knowledge_source', 'selected_rag', {'status': 'succeeded', 'operation_id': operation_id,
            'snapshot_id': snapshot_id, 'last_attempt_at': now, 'last_success_at': now, 'last_error': '',
            'item_count': len(values), 'text_count': sum(i['content_status'] == 'full_text' for i, _ in values),
            'attachment_count': sum(i['content_status'] in {'attachment_only', 'file_listing'} for i, _ in values)})
    return {'category': 'selected_rag', 'status': 'succeeded', 'item_count': len(values)}


def _sync_skills(backend, actor, operation_id, skill_id):
    if not all(hasattr(backend, name) for name in ('list_skill_folders', 'read_skill_folder')):
        _error('knowledge_unavailable', '当前飞书服务未提供Skill文件夹读取', 503)
    folders = backend.list_skill_folders()
    if not isinstance(folders, list):
        _error('incomplete_skill_folders', 'Shared Skills根目录读取不完整')
    by_id = {}
    for folder in folders:
        if (not isinstance(folder, dict) or folder.get('type') != 'folder'
                or folder.get('parent_token') != KNOWLEDGE_ROOTS['shared_skills']
                or not isinstance(folder.get('name'), str) or not folder['name']
                or not isinstance(folder.get('token'), str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{4,200}', folder['token'])):
            _error('out_of_scope', 'Skill根目录返回了未登记文件夹')
        key = skills.identifier(folder['token'])
        if key in by_id:
            _error('incomplete_skill_folders', 'Skill根目录存在重复文件夹')
        by_id[key] = folder
    if skill_id and skill_id not in by_id:
        _actor(actor, admin=True)
        with db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            skills.mark_missing(conn, skill_id, db.utc_now())
        _error('skill_not_found', '来源文件夹已不在Shared Skills根目录', 404)
    selected = [(skill_id, by_id[skill_id])] if skill_id else sorted(by_id.items())
    units = []
    for key, folder in selected:
        try:
            values = _normalize('shared_skills', backend.read_skill_folder(folder['token']))
            root = next((item for item, _ in values if item['source_token'] == folder['token']), None)
            if root is None:
                _error('incomplete_skill_folder', 'Skill未返回完整的根文件夹')
            package = skills.assemble(root, values)
            _actor(actor, admin=True)
            with db.connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                skills.commit(conn, package, values, db.utc_now())
            units.append({'skill_id': key, 'status': 'succeeded', 'version': package['item']['version']})
        except Exception as exc:
            if isinstance(exc, KnowledgeError) and exc.status == 403:
                raise
            code, message, failed = _error_info(exc)
            with db.connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                skills.failure(conn, folder, message, code, db.utc_now())
            units.append({'skill_id': key, 'status': 'failed', 'error_code': code, 'message': message})
    now = db.utc_now()
    failed = [unit for unit in units if unit['status'] == 'failed']
    status = 'partial' if failed and len(failed) < len(units) else 'failed' if failed else 'succeeded'
    outstanding = [item for item in skills.items() if item.get('available')
                   and item.get('sync_status') in {'failed', 'interrupted'}
                   and (skill_id or item['id'] in by_id)]
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if not skill_id:
            # Only the complete first-level listing proves a folder was removed.
            skills.mark_removed(conn, set(by_id), now)
        state = store.get('knowledge_source', 'shared_skills') or {}
        state.update(status='partial' if outstanding and skill_id else status,
                     operation_id=operation_id, last_attempt_at=now,
                     last_error='部分Skill未完成同步，已保留各自上次版本。' if outstanding else '',
                     error_code='partial_skill_sync' if outstanding else '', failed_source=None)
        if not failed and not skill_id:
            state.update(last_success_at=now, snapshot_id='skills_' + store.digest([(u['skill_id'], u['version']) for u in units]))
        _put(conn, 'knowledge_source', 'shared_skills', state)
    return {'category': 'shared_skills', 'status': status, 'units': units,
            'item_count': len([u for u in units if u['status'] == 'succeeded'])}


def sync(category, actor, operation_id, *, backend=None, skill_id=None):
    """RAG remains one snapshot; each first-level Skill folder commits alone."""
    actor = _actor(actor, admin=True)
    if category not in {*KNOWLEDGE_ROOTS, 'all'}:
        _error('knowledge_not_configured', '这个知识分类尚未设置来源', 422)
    if skill_id is not None and (category != 'shared_skills' or not isinstance(skill_id, str)
            or not re.fullmatch(r'knowledge_skill_[0-9a-f]{24}', skill_id)):
        _error('invalid_skill_sync', '请使用Shared Skills目录中的Skill标识', 422)
    if not isinstance(operation_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}', operation_id):
        _error('invalid_operation', '同步操作标识无效', 422)
    categories = sorted(KNOWLEDGE_ROOTS) if category == 'all' else [category]
    if not _LOCK.acquire(blocking=False):
        _error('knowledge_sync_busy', '共享知识正在同步，请稍后查看结果', 409)
    _ACTIVE.add(operation_id)
    try:
        operation = _record_attempt(categories, operation_id, actor, skill_id)
        if operation['status'] != 'syncing':
            return {**catalog(actor), 'sync_result': operation}
        if backend is None:
            from server import phase3_feishu
            backend = phase3_feishu.get_backend()
        results = []
        for name in categories:
            try:
                if backend is None:
                    _error('knowledge_unavailable', '当前飞书知识读取服务不可用', 503)
                result = _sync_rag(backend, actor, operation_id) if name == 'selected_rag' else _sync_skills(backend, actor, operation_id, skill_id)
            except Exception as exc:
                result = _failed_source(name, operation_id, exc)
            results.append(result)
        succeeded = any(r['status'] in {'succeeded', 'partial'} for r in results)
        failed = any(r['status'] in {'failed', 'partial'} for r in results)
        status = 'partial' if succeeded and failed else 'failed' if failed else 'succeeded'
        operation.update(status=status, finished_at=db.utc_now(), results=results,
            units=[unit for result in results for unit in result.get('units', [])],
            item_count=sum(result.get('item_count', 0) for result in results))
        failure = next((result for result in results if result['status'] == 'failed'), None)
        if failure:
            operation.update({key: failure[key] for key in ('error_code', 'message') if key in failure})
        with db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            _put(conn, 'knowledge_sync', operation_id, operation)
        return {**catalog(actor), 'sync_result': operation}
    finally:
        _ACTIVE.discard(operation_id)
        _LOCK.release()


from server.central_api import current_user
from server.lab_routes import SanitizedLabRoute

router = APIRouter(prefix='/api/central/knowledge', tags=['shared-knowledge'], route_class=SanitizedLabRoute)


class SyncInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    category: Literal['selected_rag', 'shared_skills', 'all']
    operation_id: str = Field(min_length=1, max_length=120, pattern=r'^[A-Za-z0-9_.:-]+$')
    skill_id: str | None = Field(default=None, pattern=r'^knowledge_skill_[0-9a-f]{24}$')


def _failure(exc):
    return HTTPException(getattr(exc, 'status', 409), detail={'code': getattr(exc, 'code', 'knowledge_failed'), 'message': str(exc)})


@router.get('')
def knowledge_catalog(actor: dict = Depends(current_user)):
    try:
        return catalog(actor)
    except KnowledgeError as exc:
        raise _failure(exc) from None


@router.get('/items/{identifier}')
def knowledge_item(identifier: str, version: str | None = None, skill_id: str | None = None,
                   package_version: str | None = None, actor: dict = Depends(current_user)):
    try:
        return read_item(identifier, actor, version, skill_id=skill_id, package_version=package_version)
    except KnowledgeError as exc:
        raise _failure(exc) from None


@router.post('/sync')
async def knowledge_sync(payload: SyncInput, actor: dict = Depends(current_user)):
    try:
        return await asyncio.to_thread(sync, payload.category, actor, payload.operation_id, skill_id=payload.skill_id)
    except KnowledgeError as exc:
        raise _failure(exc) from None
