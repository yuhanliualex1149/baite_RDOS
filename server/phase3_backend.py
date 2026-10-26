"""Authoritative phase-3 mock transport and explicit real-adapter selection.

The mock authority has its own tables. Cache writes never alter its business
records; the service must read through this contract even in local tests.
"""
from __future__ import annotations

import copy
import importlib
import json
import os
import threading
from server import db, central_store as central, phase3_store as local

_LOCK = threading.RLock()


class BackendError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def payload(record: dict) -> dict:
    return {k: v for k, v in record.items() if k not in
            ('record_id', 'source_url', 'readback_verified', 'source_environment', 'remote_actor') and not k.startswith('_')}


class LocalBackend:
    def __init__(self):
        self.faults = {}

    def registry(self) -> dict:
        return {'mode': 'local_mock', 'project_id': local.PROJECT_ID,
            'zones': {'project': {'folder_id': 'lab_phase3_project_root', 'source_url': ''},
                      'agent': {'folder_id': 'lab_phase3_agent_root', 'source_url': ''}},
            'documents': [self._document(d) for d in self._items('document')],
            'task_table_url': '', 'event_table_url': '', 'identity_mode': 'role_test',
            'permission_boundary': '本地模拟；未证明真实成员隔离'}

    def fault(self, point: str, count: int = 1) -> None:
        self.faults[point] = count

    def _fault(self, point: str) -> None:
        if self.faults.get(point, 0):
            self.faults[point] -= 1
            raise BackendError('write_uncertain' if 'commit' in point else 'read_failed', '合成故障：' + point)

    def _get(self, kind, record_id):
        with db.connection() as conn:
            row = conn.execute('SELECT payload FROM phase3_mock WHERE kind=? AND id=?', (kind, record_id)).fetchone()
        return json.loads(row[0]) if row else None

    def _items(self, kind):
        with db.connection() as conn:
            rows = conn.execute('SELECT payload FROM phase3_mock WHERE kind=? ORDER BY id', (kind,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _save(self, kind, record_id, value, conn=None):
        if conn is None:
            with db.connection() as connection:
                self._save(kind, record_id, value, connection)
            return
        conn.execute('INSERT INTO phase3_mock VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload',
                     (kind, record_id, json.dumps(value, ensure_ascii=False)))

    def _apply(self, opid, action, inputs, mutation, after):
        digest = central.digest({'action': action, 'input': inputs})
        with _LOCK, db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT payload FROM phase3_mock WHERE kind='operation' AND id=?", (opid,)).fetchone()
            if row:
                old = json.loads(row[0])
                if old['hash'] != digest:
                    raise BackendError('operation_conflict', '同一远端操作标识不能用于不同内容')
                return copy.deepcopy(old['result'])
            result = mutation(conn)
            self._save('operation', opid, {'id': opid, 'hash': digest, 'action': action, 'result': result}, conn)
        self._fault(after)
        return result

    def list_tasks(self) -> list:
        self._fault('read_tasks')
        return self._items('task')

    def read_task(self, taskid: str):
        self._fault('read_task')
        return self._get('task', taskid)

    def write_task(self, task: dict, opid: str) -> dict:
        value = {**payload(task), 'operation_id': opid}
        def mutation(conn):
            row = conn.execute("SELECT payload FROM phase3_mock WHERE kind='task' AND id=?", (value['task_id'],)).fetchone()
            old = json.loads(row[0]) if row else None
            if old and old.get('project_id') != value.get('project_id'):
                raise BackendError('out_of_scope', '已有任务不能改派到其他项目')
            if value['record_revision'] != (old['record_revision'] + 1 if old else 1):
                raise BackendError('version_conflict', '任务记录已变化')
            result = {**value, 'record_id': (old or {}).get('record_id', 'mock_' + value['task_id'])}
            self._save('task', value['task_id'], result, conn)
            return result
        self._apply(opid, 'task', value, mutation, 'task_after_commit')
        self._fault('task_readback')
        result = self.read_task(value['task_id'])
        if central.digest(payload(result)) != central.digest(value):
            raise BackendError('readback_mismatch', '任务正文读回不一致')
        return result

    def list_events(self) -> list:
        self._fault('read_events')
        return self._items('event')

    def append_event(self, event: dict, opid: str) -> dict:
        value = {**payload(event), 'operation_id': opid}
        def mutation(conn):
            row = conn.execute("SELECT payload FROM phase3_mock WHERE kind='event' AND id=?", (value['id'],)).fetchone()
            if row:
                old = json.loads(row[0])
                if payload(old) != value:
                    raise BackendError('operation_conflict', '相同协作事件 ID 对应不同内容')
                return old
            result = {**value, 'record_id': 'mock_' + value['id']}
            self._save('event', value['id'], result, conn)
            return result
        self._apply(opid, 'event', value, mutation, 'event_after_commit')
        self._fault('event_readback')
        result = self._get('event', value['id'])
        if payload(result) != value:
            raise BackendError('readback_mismatch', '协作事件读回不一致')
        return result

    @staticmethod
    def _document(item):
        return {**item, 'object_id': item['id'], 'content_hash': central.digest(item['content']), 'source_url': ''}

    def read_document(self, identifier: str) -> dict:
        self._fault('read_document')
        item = self._get('document', identifier)
        from server import rdos_projects
        if not item or not rdos_projects.is_registered(item['project_id']):
            raise BackendError('out_of_scope', '文档未登记在本轮实验范围')
        return self._document(item)

    def create_document(self, zone: str, title: str, content: str, opid: str, *, project_id=local.PROJECT_ID) -> dict:
        if zone not in ('project', 'agent'):
            raise BackendError('out_of_scope', '文档不在两个实验资料根下')
        identifier = 'lab_phase3_doc_' + central.digest(opid)[:20]
        from server import rdos_projects
        registry = rdos_projects.resource_scope(project_id, self.registry())
        def mutation(conn):
            item = {'id': identifier, 'project_id': project_id, 'title': title, 'content': content,
                    'revision': 1, 'folder_id': registry['zones'][zone]['folder_id'], 'zone': zone, 'operation_id': opid}
            self._save('document', identifier, item, conn)
            return self._document(item)
        inputs = [zone, title, content] if project_id == local.PROJECT_ID else [project_id, zone, title, content]
        self._apply(opid, 'document_create', inputs, mutation, 'document_after_commit')
        self._fault('document_readback')
        return self.read_document(identifier)

    def find_created_document(self, opid: str):
        operation = self._get('operation', opid)
        if not operation or operation.get('action') != 'document_create':
            return None
        return self.read_document(operation['result']['id'])

    def patch_document(self, identifier: str, base_revision, base_hash: str, old_text: str, new_text: str, opid: str) -> dict:
        def mutation(conn):
            row = conn.execute("SELECT payload FROM phase3_mock WHERE kind='document' AND id=?", (identifier,)).fetchone()
            if not row:
                raise BackendError('out_of_scope', '文档不属于本轮实验')
            item = json.loads(row[0])
            if str(item['revision']) != str(base_revision) or central.digest(item['content']) != base_hash:
                raise BackendError('version_conflict', '文档版本已变化，保留当前正文')
            if not old_text or item['content'].count(old_text) != 1:
                raise BackendError('patch_conflict', '局部修改必须唯一匹配原文')
            item.update(content=item['content'].replace(old_text, new_text, 1), revision=item['revision'] + 1, operation_id=opid)
            self._save('document', identifier, item, conn)
            return self._document(item)
        self._apply(opid, 'document_patch', [identifier, base_revision, base_hash, old_text, new_text], mutation, 'document_after_commit')
        self._fault('document_readback')
        return self.read_document(identifier)


_LOCAL = LocalBackend()


def get_backend():
    try:
        module = importlib.import_module('server.phase3_feishu')
    except ModuleNotFoundError as exc:
        if exc.name != 'server.phase3_feishu':
            raise
    else:
        backend = module.get_backend()
        if backend is not None:
            return backend
    if os.environ.get('RDOS_RUNTIME_MODE') == 'production':
        raise BackendError('not_configured', '正式飞书适配器未就绪，拒绝使用模拟业务数据')
    return _LOCAL
