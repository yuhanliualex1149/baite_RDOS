"""Phase-3 local journals and projections; authoritative business I/O is backend-only."""
from __future__ import annotations

import json
import os
from server import db, central_store as central

_FORMAL = os.environ.get('RDOS_RUNTIME_MODE') == 'production'
PROJECT_ID = 'project_rdos' if _FORMAL else 'lab_project_phase3_collab'
OWNER_ID = 'mem_p6BbcYJrwAcv_M5Y' if _FORMAL else 'mem_lab_admin_0001'
NODE_IDS = () if _FORMAL else ('node_lab_phase3_codex', 'node_lab_phase3_workbuddy')
DEFAULT_NODES = () if _FORMAL else (
    {'node_id': NODE_IDS[0], 'public_id': 'node_codex', 'label': 'Codex', 'display_name': 'Codex',
     'member_id': 'mem_lab_codex_0001', 'enabled': True},
    {'node_id': NODE_IDS[1], 'public_id': 'node_workbuddy', 'label': 'WorkBuddy', 'display_name': 'WorkBuddy',
     'member_id': 'mem_lab_workbuddy_0001', 'enabled': True},
)


def init() -> None:
    if _FORMAL:
        return init_formal()
    from lab.guard import require_active
    require_active()
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        exists=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='phase3_local'").fetchone()
        row=conn.execute("SELECT payload FROM phase3_local WHERE kind='state' AND id='phase3_schema'").fetchone() if exists else None
        if row:
            try: marker=json.loads(row[0])
            except (ValueError,TypeError) as exc:raise RuntimeError('阶段3数据结构版本标记无效，停止初始化') from exc
            if (not isinstance(marker,dict) or type(marker.get('version')) is not int or marker['version']!=1
                    or not isinstance(marker.get('created_at'),str) or not marker['created_at']):
                raise RuntimeError('阶段3数据结构版本未知或无效，停止初始化')
        statements=('''CREATE TABLE IF NOT EXISTS phase3_local (
          kind TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,updated_at TEXT NOT NULL,
          PRIMARY KEY(kind,id))''', '''CREATE TABLE IF NOT EXISTS phase3_history (
          task_id TEXT NOT NULL,version INTEGER NOT NULL,payload_hash TEXT NOT NULL,payload TEXT NOT NULL,
          PRIMARY KEY(task_id,version))''', '''CREATE TABLE IF NOT EXISTS phase3_event_index (
          sequence INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
          payload_hash TEXT NOT NULL,payload TEXT NOT NULL)''', '''CREATE TABLE IF NOT EXISTS phase3_mock (
          kind TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(kind,id))''')
        for statement in statements:conn.execute(statement)
        now=db.utc_now()
        conn.execute("INSERT OR IGNORE INTO phase3_local VALUES('state','phase3_schema',?,?)",
                     (json.dumps({'version':1,'created_at':now}),now))
    if not central.get('project', PROJECT_ID):
        central.put('project', PROJECT_ID, {'id': PROJECT_ID, 'name': 'RDOS 工作协作',
            'member_ids': ['mem_lab_codex_0001', 'mem_lab_workbuddy_0001'], 'status': 'active',
            'identity_mode': 'role_test', 'source_environment': 'local_mock'})
    if not get('state', 'node_registry_initialized'):
        for node in DEFAULT_NODES:
            put('node', node['node_id'], node)
        put('state', 'node_registry_initialized', {'version': 1})


def init_formal() -> None:
    """Create local journals around existing users without synthetic fixtures."""
    from server.production_runtime import require
    formal = require()
    nodes = formal.resources['node_members']
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        for statement in (
            '''CREATE TABLE IF NOT EXISTS phase3_local (
              kind TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,updated_at TEXT NOT NULL,
              PRIMARY KEY(kind,id))''',
            '''CREATE TABLE IF NOT EXISTS phase3_history (
              task_id TEXT NOT NULL,version INTEGER NOT NULL,payload_hash TEXT NOT NULL,payload TEXT NOT NULL,
              PRIMARY KEY(task_id,version))''',
            '''CREATE TABLE IF NOT EXISTS phase3_event_index (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
              payload_hash TEXT NOT NULL,payload TEXT NOT NULL)''',
        ):
            conn.execute(statement)
        for node_id, member_id in nodes.items():
            user = conn.execute('SELECT display_name,enabled FROM central_users WHERE id=?', (member_id,)).fetchone()
            if not user:
                raise RuntimeError('正式节点对应成员不存在，停止初始化')
            old = conn.execute("SELECT payload FROM phase3_local WHERE kind='node' AND id=?", (node_id,)).fetchone()
            if old:
                if json.loads(old[0]).get('member_id') != member_id:
                    raise RuntimeError('正式节点成员绑定与已有记录冲突')
                continue
            value = {'node_id': node_id, 'public_id': node_id, 'member_id': member_id,
                     'label': user['display_name'], 'display_name': user['display_name'],
                     'enabled': bool(user['enabled']), 'registration_source': 'formal_migration'}
            conn.execute('INSERT INTO phase3_local VALUES(?,?,?,?)',
                         ('node', node_id, json.dumps(value, ensure_ascii=False), db.utc_now()))
        now = db.utc_now()
        conn.execute("INSERT OR IGNORE INTO phase3_local VALUES('state','phase3_schema',?,?)",
                     (json.dumps({'version': 1, 'created_at': now}), now))
        conn.execute("INSERT OR IGNORE INTO phase3_local VALUES('state','business_workflow',?,?)",
                     (json.dumps({'version': 1, 'source': 'formal_migration'}), now))
    if not central.get('project', PROJECT_ID):
        central.put('project', PROJECT_ID, {'id': PROJECT_ID, 'name': 'RDOS建设',
            'member_ids': list(nodes.values()), 'status': 'active',
            'identity_mode': 'actual', 'source_environment': 'feishu_production'})


def node_registry() -> list:
    """Registered nodes, never a product assumption about the number of nodes."""
    return [node for node in items('node') if node.get('enabled', True)]


def get(kind: str, record_id: str):
    with db.connection() as conn:
        row = conn.execute('SELECT payload FROM phase3_local WHERE kind=? AND id=?', (kind, record_id)).fetchone()
    return json.loads(row[0]) if row else None


def items(kind: str) -> list:
    with db.connection() as conn:
        rows = conn.execute('SELECT payload FROM phase3_local WHERE kind=? ORDER BY updated_at,id', (kind,)).fetchall()
    return [json.loads(row[0]) for row in rows]


def put(kind: str, record_id: str, value: dict) -> dict:
    with db.connection() as conn:
        conn.execute('INSERT INTO phase3_local VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at',
                     (kind, record_id, json.dumps(value, ensure_ascii=False), db.utc_now()))
    return value


def immutable(kind: str, record_id: str, value: dict) -> dict:
    """First insert or byte-semantic replay; never silently rewrite a business fact."""
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT payload FROM phase3_local WHERE kind=? AND id=?', (kind, record_id)).fetchone()
        if row:
            old = json.loads(row[0])
            if central.digest(old) != central.digest(value):
                raise ValueError('同一记录标识不能用于不同内容')
            return old
        conn.execute('INSERT INTO phase3_local VALUES(?,?,?,?)', (kind, record_id, json.dumps(value, ensure_ascii=False), db.utc_now()))
    return value


def confirm_task_head(task: dict, content_hash: str) -> None:
    """Advance only after an independent authoritative task readback, never on planning."""
    value = {key: task[key] for key in ('task_id', 'record_revision', 'requirements_version', 'env_id', 'generation')}
    value['content_hash'] = content_hash
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("SELECT payload FROM phase3_local WHERE kind='task_head' AND id=?", (task['task_id'],)).fetchone()
        if row:
            old = json.loads(row[0])
            if old['record_revision'] > value['record_revision'] or (old['record_revision'] == value['record_revision'] and old != value):
                raise ValueError('已核对任务最新版本不能回退或替换')
        conn.execute("INSERT INTO phase3_local VALUES('task_head',?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                     (task['task_id'], json.dumps(value, ensure_ascii=False), db.utc_now()))


class RefreshRace(ValueError):
    code = 'refresh_race'


def task_heads() -> dict:
    """Snapshot confirmed heads before a remote refresh starts reading."""
    with db.connection() as conn:
        rows = conn.execute("SELECT id,payload FROM phase3_local WHERE kind='task_head'").fetchall()
    return {row['id']: json.loads(row['payload']) for row in rows}


def remember_version(task_id: str, version: int, requirements: dict) -> None:
    digest = central.digest(requirements)
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old = conn.execute('SELECT payload_hash FROM phase3_history WHERE task_id=? AND version=?', (task_id, version)).fetchone()
        if old and old[0] != digest:
            raise ValueError('相同任务要求版本出现不同内容，原历史已保留')
        conn.execute('INSERT OR IGNORE INTO phase3_history VALUES(?,?,?,?)',
                     (task_id, version, digest, json.dumps(requirements, ensure_ascii=False)))


def history(task_id: str) -> list:
    with db.connection() as conn:
        rows = conn.execute('SELECT version,payload FROM phase3_history WHERE task_id=? ORDER BY version', (task_id,)).fetchall()
    return [{'requirements_version': row[0], **json.loads(row[1])} for row in rows]


def index_event(event: dict, content: dict) -> dict:
    digest = central.digest(content)
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT payload_hash,payload,sequence FROM phase3_event_index WHERE id=?', (event['id'],)).fetchone()
        if row:
            if row[0] != digest:
                raise ValueError('相同事件 ID 出现不同原始内容')
            # Original-content hash stays immutable; current-version/late flags
            # are projections and must follow later task state changes.
            conn.execute('UPDATE phase3_event_index SET payload=? WHERE id=?',
                         (json.dumps(event, ensure_ascii=False), event['id']))
            return {**event, 'sequence': row[2]}
        cursor = conn.execute('INSERT INTO phase3_event_index(id,payload_hash,payload) VALUES(?,?,?)',
                              (event['id'], digest, json.dumps(event, ensure_ascii=False)))
        return {**event, 'sequence': cursor.lastrowid}


def events(after: int = 0, limit: int = 1000) -> list:
    with db.connection() as conn:
        rows = conn.execute('SELECT sequence,payload FROM phase3_event_index WHERE sequence>? ORDER BY sequence LIMIT ?',
                            (after, max(1, min(limit, 1000)))).fetchall()
    return [{**json.loads(row[1]), 'sequence': row[0]} for row in rows]


def event_fingerprint(event_id: str):
    """Immutable original-source fingerprint, separate from display projections."""
    with db.connection() as conn:
        row = conn.execute('SELECT payload_hash FROM phase3_event_index WHERE id=?', (event_id,)).fetchone()
    return row[0] if row else None


def replace_cache(tasks: list, documents: list, refresh: dict, project_id: str | None = None,
                  *, expected_heads: dict | None = None) -> None:
    """Call only after a complete backend read; failure keeps the previous projection."""
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if expected_heads is not None:
            rows = conn.execute("SELECT id,payload FROM phase3_local WHERE kind='task_head'").fetchall()
            current = {row['id']: json.loads(row['payload']) for row in rows}
            if current != expected_heads:
                raise RefreshRace('任务在同步读取期间已更新；保留原投影并重新读取')
        if project_id is None:
            conn.execute("DELETE FROM phase3_local WHERE kind='task_cache'")
        else:
            conn.execute("DELETE FROM phase3_local WHERE kind='task_cache' AND json_extract(payload,'$.project_id')=?", (project_id,))
        for task in tasks:
            conn.execute('INSERT INTO phase3_local VALUES(?,?,?,?)', ('task_cache', task['task_id'], json.dumps(task, ensure_ascii=False), db.utc_now()))
        for document in documents:
            conn.execute('INSERT INTO phase3_local VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at',
                         ('document_cache', document['id'], json.dumps(document, ensure_ascii=False), db.utc_now()))
        conn.execute('INSERT INTO phase3_local VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at',
                     ('state', 'refresh' if project_id is None else 'refresh:' + project_id, json.dumps(refresh, ensure_ascii=False), db.utc_now()))
        if project_id == PROJECT_ID:
            conn.execute('INSERT INTO phase3_local VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at',
                         ('state', 'refresh', json.dumps(refresh, ensure_ascii=False), db.utc_now()))


def quarantine(raw, reason: str, source: str, *, scope: str | None = None) -> None:
    identifier = central.digest([source, raw])
    if get('quarantine', identifier):
        return
    put('quarantine', identifier, {'id': identifier, 'source': source, 'raw': raw,
        **({'scope': scope} if scope else {}),
        'reason': str(reason)[:500], 'observed_at': db.utc_now()})
