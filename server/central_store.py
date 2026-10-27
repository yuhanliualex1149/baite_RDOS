"""Small local state for the central Agent; shared bodies remain in Feishu."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from server import db
from server.backup import backup_database


PROJECT_ID = "project_rdos" if os.environ.get("RDOS_RUNTIME_MODE") == "production" else "lab_project_phase0"
ONBOARDING_IDS = ()
FEEDBACK_ID = ""
BASE_ID = "lab_task_store"
TABLE_ID = "lab_task_table"
FEISHU_HOST = os.environ.get("BAITE_PUBLIC_URL", "")


def digest(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def identifier(prefix: str) -> str:
    return prefix + "_" + secrets.token_urlsafe(12)


def init() -> None:
    """Seed synthetic Panel records only; never restore, migrate or replay work."""
    if os.environ.get("RDOS_RUNTIME_MODE") == "production":
        return init_formal()
    from lab.guard import require_active
    from server.lab_panel import runtime_status
    require_active()
    with db.connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS central_users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL, roles_json TEXT NOT NULL,
                password_hash TEXT NOT NULL, identity_mode TEXT NOT NULL DEFAULT 'role_test',
                enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS central_sessions (
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES central_users(id),
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS central_records (
                kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
                updated_at TEXT NOT NULL, PRIMARY KEY(kind,id)
            );
        """)
        admin = conn.execute("SELECT username,password_hash FROM admin_account WHERE id=1").fetchone()
        if not admin:
            raise RuntimeError("实验管理员尚未初始化")
        conn.execute("INSERT OR IGNORE INTO central_users VALUES(?,?,?,?,?,?,1,?)", (
            "mem_lab_admin_0001", admin["username"], "实验管理员",
            '["admin"]', admin["password_hash"], "role_test", db.utc_now()))
        for member_id, username, name in (
            ("mem_lab_codex_0001", "lab_codex", "Codex 虚构测试成员"),
            ("mem_lab_workbuddy_0001", "lab_workbuddy", "WorkBuddy 虚构测试成员"),
        ):
            if not conn.execute("SELECT 1 FROM central_users WHERE id=?", (member_id,)).fetchone():
                conn.execute("INSERT INTO central_users VALUES(?,?,?,?,?,?,1,?)", (
                    member_id, username, name, '["researcher"]',
                    db.hash_password(secrets.token_urlsafe(24)), "role_test", db.utc_now()))
    if not get("project", PROJECT_ID):
        put("project", PROJECT_ID, {"id": PROJECT_ID, "name": "RDOS 2.0 阶段 0 实验项目",
            "description": "仅供 Panel 浏览的合成项目。两个客户端读取模拟材料、写反馈并读回，使用独立模拟服务分别验收；本页不代表真实飞书联调。",
            "status": "active", "member_ids": ["mem_lab_codex_0001", "mem_lab_workbuddy_0001"],
            "feedback_id": "", "feedback_url": "", "folder_url": "", "identity_mode": "role_test"})
    if not get("state", "runtime"):
        put("state", "runtime", runtime_status())
    if not get("source", "lab_doc_intro"):
        put("source", "lab_doc_intro", {"id": "lab_doc_intro", "name": "阶段 0 合成介绍材料",
            "kind": "local_mock", "source_url": "", "project_id": PROJECT_ID,
            "description": "从各节点工作区提供的唯一模拟服务入口读取。"})


def init_formal() -> None:
    """Version only the central store; retain every existing real account."""
    from server.production_runtime import require
    require()
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='central_users'").fetchone():
            raise RuntimeError("正式成员账号尚未迁移，拒绝初始化")
        if not conn.execute("SELECT 1 FROM central_users WHERE enabled=1 LIMIT 1").fetchone():
            raise RuntimeError("正式成员账号为空，拒绝初始化")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS central_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES central_users(id),
                expires_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS central_records (
                kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
                updated_at TEXT NOT NULL, PRIMARY KEY(kind,id)
            )
        """)


def seed_test_tasks() -> None:
    """Explicit fixture for isolated legacy tests; never called by the live app."""
    for node in ("codex", "workbuddy"):
        task_id = "lab_task_" + node
        if not get("task", task_id):
            put("task", task_id, {"id": task_id, "task_id": task_id, "project_id": PROJECT_ID,
                "member_ids": ["mem_lab_" + node + "_0001"], "version": "1", "status": "已发布",
                "nature": "一次性", "task_kind": "work", "source_url": "",
                "instructions": "纯合成隔离测试任务。"})


def future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def put(kind: str, record_id: str, value: dict) -> dict:
    with db.connection() as conn:
        if kind == 'task_version':
            prior = conn.execute('SELECT payload FROM central_records WHERE kind=? AND id=?', (kind, record_id)).fetchone()
            if prior:
                old = json.loads(prior[0])
                if _task_version_content(old) != _task_version_content(value):
                    raise ValueError('同一任务要求版本不能覆盖为不同内容')
                return old
        conn.execute("INSERT INTO central_records VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at", (kind, record_id, json.dumps(value, ensure_ascii=False), db.utc_now()))
    return value


def _task_version_content(value: dict) -> str:
    fields = ('task_id', 'project_id', 'version', 'status', 'instructions', 'member_ids',
              'task_kind', 'stage', 'nature', 'related_task', 'owner_id', 'execution_nodes',
              'goal', 'inputs', 'deliverables', 'completion_rule', 'dispatch_to', 'return_to')
    return digest({key: value.get(key, [] if key == 'member_ids' else '') for key in fields})


def get(kind: str, record_id: str) -> Any:
    with db.connection() as conn:
        row = conn.execute("SELECT payload FROM central_records WHERE kind=? AND id=?", (kind, record_id)).fetchone()
    return json.loads(row[0]) if row else None


def items(kind: str) -> list:
    with db.connection() as conn:
        rows = conn.execute("SELECT payload FROM central_records WHERE kind=? ORDER BY updated_at DESC", (kind,)).fetchall()
    return [json.loads(row[0]) for row in rows]


def remove(kind: str, record_id: str) -> None:
    with db.connection() as conn:
        conn.execute("DELETE FROM central_records WHERE kind=? AND id=?", (kind, record_id))


def public_user(row: Any) -> dict:
    return {"id": row["id"], "username": row["username"], "display_name": row["display_name"], "roles": json.loads(row["roles_json"]), "identity_mode": row["identity_mode"], "enabled": bool(row["enabled"])}


def users() -> list:
    with db.connection() as conn:
        rows = conn.execute("SELECT * FROM central_users ORDER BY created_at").fetchall()
    return [public_user(row) for row in rows]


def user(record_id: str) -> Any:
    return next((item for item in users() if item["id"] == record_id), None)


def validate_roles(roles: list) -> list:
    if not roles or not isinstance(roles, list) or any(role not in ("admin", "researcher") for role in roles):
        raise ValueError("只支持管理员、研究员及双角色")
    return sorted(set(roles))


def new_password_hash(password: str) -> str:
    """The account create/change/reset paths share the same password bounds."""
    if not isinstance(password, str) or not 10 <= len(password) <= 500:
        raise ValueError("新密码需要 10 至 500 个字符。")
    return db.hash_password(password)


def _member_record(conn, kind: str, record_id: str, value: dict, now: str) -> None:
    conn.execute("INSERT INTO central_records VALUES(?,?,?,?) ON CONFLICT(kind,id) "
                 "DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                 (kind, record_id, json.dumps(value, ensure_ascii=False), now))


def _member_actor(conn, actor_id: str | None) -> None:
    """Recheck HTTP authority under the lock held for the account mutation."""
    if actor_id is None:
        return
    actor = conn.execute("SELECT roles_json,enabled FROM central_users WHERE id=?", (actor_id,)).fetchone()
    if not actor or not actor['enabled'] or 'admin' not in json.loads(actor['roles_json']):
        raise PermissionError("账号管理权限已变化，请重新登录。")


def _ensure_member_node(conn, member_id: str, display_name: str, roles: list) -> dict | None:
    """Registration only: no client install, assignment, transport or scheduling."""
    if "researcher" not in roles:
        return None
    from server import rdos_projects
    # Freeze the old Default participants before the registry grows. The same
    # connection makes account, registry and membership changes atomic.
    rdos_projects.freeze_default_nodes(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS phase3_local (
        kind TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,updated_at TEXT NOT NULL,
        PRIMARY KEY(kind,id))""")
    for row in conn.execute("SELECT payload FROM phase3_local WHERE kind='node'"):
        node = json.loads(row[0])
        if node.get("member_id") == member_id:
            return node
    node_id = identifier("node")
    now = db.utc_now()
    node = {"node_id": node_id, "public_id": node_id, "member_id": member_id,
            "label": display_name + "的 Agent", "display_name": display_name + "的 Agent",
            "enabled": True, "created_at": now, "registration_source": "member_account"}
    conn.execute("INSERT INTO phase3_local VALUES('node',?,?,?)",
                 (node_id, json.dumps(node, ensure_ascii=False), now))
    return node


def create_user(username: str, display_name: str, roles: list, password: str = "", identity_mode: str = "actual", member_id: str = "", *, actor_id: str | None = None) -> dict:
    if (not isinstance(username, str) or not isinstance(display_name, str)
            or not username.strip() or len(username) > 80 or not display_name.strip() or len(display_name) > 80):
        raise ValueError("请填写有效账号和显示名")
    if identity_mode not in ("actual", "role_test"):
        raise ValueError("身份性质应为 actual 或 role_test")
    if member_id and not re.fullmatch(r"mem_[A-Za-z0-9_-]{8,80}", member_id):
        raise ValueError("成员标识格式无效")
    roles = validate_roles(roles)
    actual_password = password or secrets.token_urlsafe(18)
    encoded = new_password_hash(actual_password)
    # An admin may preserve an existing Feishu member marker when provisioning
    # an empty server. Normal UI creation still generates an opaque ID.
    record_id = member_id or identifier("mem")
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _member_actor(conn, actor_id)
        if conn.execute("SELECT 1 FROM central_users WHERE username=? OR id=?", (username.strip(), record_id)).fetchone():
            raise ValueError("登录名或成员标识已存在，请查看现有账号。")
        conn.execute("INSERT INTO central_users VALUES(?,?,?,?,?,?,1,?)", (record_id, username.strip(), display_name.strip(), json.dumps(roles), encoded, identity_mode, db.utc_now()))
        node = _ensure_member_node(conn, record_id, display_name.strip(), roles)
    result = {"user": user(record_id), "password": actual_password}
    if node:
        result['node'] = {'node_id': node.get('public_id') or node['node_id'],
                          'display_name': node.get('display_name') or node.get('label'),
                          'member_id': record_id}
    return result


def update_user(record_id: str, fields: dict, *, actor_id: str | None = None) -> dict:
    allowed = {"username", "display_name", "roles", "identity_mode", "enabled"}
    if not isinstance(fields, dict) or set(fields) - allowed:
        raise ValueError("成员信息包含未支持字段")
    refresh_note = False
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _member_actor(conn, actor_id)
        row = conn.execute("SELECT * FROM central_users WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise ValueError("成员不存在")
        old = public_user(row)
        roles = validate_roles(fields.get("roles", old["roles"]))
        enabled = fields.get("enabled", old["enabled"])
        if type(enabled) is not bool:
            raise ValueError("账号启用状态无效")
        name = fields.get("display_name", old["display_name"])
        username = fields.get("username", old["username"])
        mode = fields.get("identity_mode", old["identity_mode"])
        if (not isinstance(username, str) or not isinstance(name, str)
                or not username.strip() or len(username) > 80 or not name.strip() or len(name) > 80
                or mode not in ("actual", "role_test")):
            raise ValueError("成员信息无效")
        username, name = username.strip(), name.strip()
        if "admin" in old["roles"] and old["enabled"] and ("admin" not in roles or not enabled):
            others = conn.execute("SELECT roles_json FROM central_users WHERE enabled=1 AND id<>?", (record_id,)).fetchall()
            if not any("admin" in json.loads(other["roles_json"]) for other in others):
                raise ValueError("需要保留至少一位启用的管理员")
        if conn.execute("SELECT 1 FROM central_users WHERE username=? AND id<>?", (username, record_id)).fetchone():
            raise ValueError("登录名已存在，请使用其他登录名。")
        conn.execute("UPDATE central_users SET username=?,display_name=?,roles_json=?,identity_mode=?,enabled=? WHERE id=?", (username, name, json.dumps(roles), mode, int(enabled), record_id))
        _ensure_member_node(conn, record_id, name, roles)
        revoke = username != old["username"] or roles != old["roles"] or enabled != old["enabled"]
        if revoke:
            conn.execute("DELETE FROM central_sessions WHERE user_id=?", (record_id,))
            from server import central_accounts
            target = conn.execute("SELECT * FROM central_users WHERE id=?", (record_id,)).fetchone()
            central_accounts.sync_legacy_account(conn, target)
            refresh_note = record_id == central_accounts.SEED_ADMIN_ID and username != old["username"]
        now = db.utc_now()
        audit_id = identifier("member_audit")
        _member_record(conn, "member_audit", audit_id, {
            "id": audit_id, "target_id": record_id, "action": "account_updated", "created_at": now,
            **({'actor_id': actor_id} if actor_id is not None else {}),
            "changed_fields": sorted(key for key in fields if fields[key] != old.get(key)),
        }, now)
    if refresh_note:
        central_accounts.refresh_initial_access_note(record_id)
    return user(record_id)


def login(username: str, password: str) -> Any:
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM central_users WHERE username=? AND enabled=1", (username,)).fetchone()
        if not row or not db.verify_password(password, row["password_hash"]):
            return None
        token = secrets.token_urlsafe(32)
        conn.execute("DELETE FROM central_sessions WHERE expires_at<=?", (db.utc_now(),))
        conn.execute("INSERT INTO central_sessions VALUES(?,?,?)", (db.hash_token(token), row["id"], future(12 * 3600)))
    return {"token": token, "user": public_user(row)}


def session(token: str) -> Any:
    if not token:
        return None
    with db.connection() as conn:
        row = conn.execute("SELECT u.* FROM central_users u JOIN central_sessions s ON s.user_id=u.id WHERE s.token_hash=? AND s.expires_at>? AND u.enabled=1", (db.hash_token(token), db.utc_now())).fetchone()
    return public_user(row) if row else None


def logout(token: str) -> None:
    with db.connection() as conn:
        conn.execute("DELETE FROM central_sessions WHERE token_hash=?", (db.hash_token(token),))


def operation(operation_id: str, action: str, payload: dict) -> dict:
    if not operation_id or len(operation_id) > 150:
        raise ValueError("缺少有效写回标识")
    payload_hash = digest({"action": action, "payload": payload})
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT payload FROM central_records WHERE kind='operation' AND id=?", (operation_id,)).fetchone()
        if row:
            item = json.loads(row[0])
            if item["payload_hash"] != payload_hash:
                raise ValueError("同一写回标识不能用于不同内容")
            return item
        item = {"id": operation_id, "action": action, "payload_hash": payload_hash, "status": "pending", "created_at": db.utc_now()}
        conn.execute("INSERT INTO central_records VALUES('operation',?,?,?)", (operation_id, json.dumps(item, ensure_ascii=False), db.utc_now()))
    return item


def finish_operation(item: dict, result: dict, status: str = "succeeded") -> dict:
    latest = get("operation", item["id"]) or item
    latest.update(status=status, result=result, updated_at=db.utc_now())
    put("operation", item["id"], latest)
    return result


def put_many(records: list[tuple[str, str, dict]]) -> None:
    """Atomically publish a complete local read snapshot or related facts."""
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        for kind, record_id, value in records:
            if kind == 'task_version':
                prior = conn.execute('SELECT payload FROM central_records WHERE kind=? AND id=?', (kind, record_id)).fetchone()
                if prior:
                    if _task_version_content(json.loads(prior[0])) != _task_version_content(value):
                        raise ValueError('同一任务要求版本不能覆盖为不同内容')
                    continue
            conn.execute('INSERT INTO central_records VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at',
                         (kind, record_id, json.dumps(value, ensure_ascii=False), db.utc_now()))
