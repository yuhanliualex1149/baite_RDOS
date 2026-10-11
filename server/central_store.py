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


PROJECT_ID = "rdos-self"
ONBOARDING_IDS = ("A1FrdvwproG7axxh6jtcOnk5nPd", "H1TmdAL4Eo3O1zxx1kXctet2nsd", "NmEtd50lcopox4xuRG3c3Ovenxd")
FEEDBACK_ID = os.environ.get("BAITE_CENTRAL_FEEDBACK_DOC", "RoxidRoZXocXSnxR0tlc4aCMnof")
BASE_ID = os.environ.get("BAITE_CENTRAL_BASE_TOKEN", "E34dbYDEXaCRnDsMQDBcMvyMnjb")
TABLE_ID = os.environ.get("BAITE_CENTRAL_TABLE_ID", "tblvJU3CwPt6eAaw")
FEISHU_HOST = "https://scn33386s7ui.feishu.cn"


def digest(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def identifier(prefix: str) -> str:
    return prefix + "_" + secrets.token_urlsafe(12)


def init() -> None:
    with db.connection() as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='central_users'").fetchone()
    if not exists and db.db_path() == db.DEFAULT_DB_PATH:
        destination = db.db_path().parent / "backups" / ("pre-central-v1-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".db")
        backup_database(db.db_path(), destination)
    with db.connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS central_users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL, roles_json TEXT NOT NULL,
                password_hash TEXT NOT NULL, identity_mode TEXT NOT NULL DEFAULT 'actual',
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
        old = conn.execute("SELECT username,password_hash FROM admin_account WHERE id=1").fetchone()
        if old and not conn.execute("SELECT 1 FROM central_users WHERE username=?", (old["username"],)).fetchone():
            conn.execute("INSERT INTO central_users VALUES(?,?,?,?,?,?,1,?)", (
                identifier("mem"), old["username"], os.environ.get("BAITE_ADMIN_DISPLAY_NAME", "管理员"),
                '["admin"]', old["password_hash"], "actual", db.utc_now()))
    if not get("project", PROJECT_ID):
        put("project", PROJECT_ID, {"id": PROJECT_ID, "name": "RDOS 自身改造", "description": "通过实际研发协作与反馈改进百特 AI 时代的工作方法。"})
    if not get("state", "runtime"):
        put("state", "runtime", {"paused": False, "next_due": future(300), "schedule_verified": False})
    for number, token in enumerate(ONBOARDING_IDS, 1):
        if not get("source", token):
            put("source", token, {"id": token, "name": "Agent 接入阶段" + str(number), "kind": "docx", "source_url": FEISHU_HOST + "/docx/" + token})
    for token, name, kind, path in (
        (FEEDBACK_ID, "百特工作反馈记录", "docx", "docx/"),
        (os.environ.get("BAITE_SELECTED_RAG_FOLDER_TOKEN", "KQjyf6KrxlZ24MdkHirc9TP0n2d"), "Selected RAG", "folder", "drive/folder/"),
        (os.environ.get("BAITE_CENTRAL_SKILL_FOLDER_TOKEN", "ImWqfzWcvl5LWUdFmxKcLIfdntp"), "组织共享 Skill", "folder", "drive/folder/"),
    ):
        if not get("source", token):
            put("source", token, {"id": token, "name": name, "kind": kind, "source_url": FEISHU_HOST + "/" + path + token})
    for run in items("run"):
        if run["status"] in ("queued", "running"):
            run.update(status="interrupted", error="服务已重启，上次运行中断；可重试，写入将先核对远端。", completed_at=db.utc_now())
            put("run", run["id"], run)
    state = get("state", "runtime")
    if state.get("next_due") and state["next_due"] < db.utc_now():
        state.update(missed="服务未运行期间未执行，最近计划时间：" + state["next_due"], next_due=future(300))
        put("state", "runtime", state)


def future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def put(kind: str, record_id: str, value: dict) -> dict:
    with db.connection() as conn:
        conn.execute("INSERT INTO central_records VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at", (kind, record_id, json.dumps(value, ensure_ascii=False), db.utc_now()))
    return value


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


def create_user(username: str, display_name: str, roles: list, password: str = "", identity_mode: str = "actual", member_id: str = "") -> dict:
    if not username.strip() or len(username) > 80 or not display_name.strip() or len(display_name) > 80:
        raise ValueError("请填写有效账号和显示名")
    if identity_mode not in ("actual", "role_test"):
        raise ValueError("身份性质应为 actual 或 role_test")
    if member_id and not re.fullmatch(r"mem_[A-Za-z0-9_-]{8,80}", member_id):
        raise ValueError("成员标识格式无效")
    roles = validate_roles(roles)
    actual_password = password or secrets.token_urlsafe(18)
    # An admin may preserve an existing Feishu member marker when provisioning
    # an empty server. Normal UI creation still generates an opaque ID.
    record_id = member_id or identifier("mem")
    with db.connection() as conn:
        conn.execute("INSERT INTO central_users VALUES(?,?,?,?,?,?,1,?)", (record_id, username.strip(), display_name.strip(), json.dumps(roles), db.hash_password(actual_password), identity_mode, db.utc_now()))
    return {"user": user(record_id), "password": actual_password}


def update_user(record_id: str, fields: dict) -> dict:
    old = user(record_id)
    if not old:
        raise ValueError("成员不存在")
    roles = validate_roles(fields.get("roles", old["roles"]))
    enabled = bool(fields.get("enabled", old["enabled"]))
    if "admin" in old["roles"] and ("admin" not in roles or not enabled) and sum("admin" in item["roles"] and item["enabled"] for item in users()) == 1:
        raise ValueError("需要保留至少一位管理员")
    name = fields.get("display_name", old["display_name"]).strip()
    username = fields.get("username", old["username"]).strip()
    mode = fields.get("identity_mode", old["identity_mode"])
    if not username or len(username) > 80 or not name or len(name) > 80 or mode not in ("actual", "role_test"):
        raise ValueError("成员信息无效")
    with db.connection() as conn:
        conn.execute("UPDATE central_users SET username=?,display_name=?,roles_json=?,identity_mode=?,enabled=? WHERE id=?", (username, name, json.dumps(roles), mode, int(enabled), record_id))
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
    item.update(status=status, result=result, updated_at=db.utc_now())
    put("operation", item["id"], item)
    return result
