"""Password management for the account system used by the current Panel.

Only salted password hashes and metadata-only audit facts are persisted. The
legacy single-admin account is a compatibility mirror of the original Panel
administrator, never an independent password source.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
import sqlite3
import tempfile
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from server import central_store as store, db, lab_panel
from server.lab_routes import SanitizedLabRoute


router = APIRouter(prefix="/api/central", tags=["panel-accounts"], route_class=SanitizedLabRoute)
COOKIE = lab_panel.cookie_name("central")
SEED_ADMIN_ID = "mem_p6BbcYJrwAcv_M5Y" if os.environ.get("RDOS_RUNTIME_MODE") == "production" else "mem_lab_admin_0001"


class AccountError(ValueError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class PasswordInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    current_password: str = Field(min_length=1, max_length=500, repr=False)
    new_password: str = Field(min_length=10, max_length=500, repr=False)
    confirm_password: str = Field(min_length=10, max_length=500, repr=False)


def _current_user(token: Optional[str] = Cookie(default=None, alias=COOKIE),
                  actor: Optional[str] = Header(default=None, alias="X-RDOS-Actor")) -> dict:
    # Import lazily so this router can be registered after central_api.
    from server.central_api import current_user
    return current_user(token, actor)


def _new_hash(new_password: str, confirm_password: str) -> str:
    if not isinstance(new_password, str) or not isinstance(confirm_password, str):
        raise AccountError(422, "invalid_password", "密码格式无效。")
    if new_password != confirm_password:
        raise AccountError(409, "password_confirmation_mismatch", "两次输入的新密码不一致。")
    try:
        return store.new_password_hash(new_password)
    except ValueError as exc:
        raise AccountError(422, "invalid_password", "新密码需要 10 至 500 个字符。") from exc


def _enabled_user(conn, user_id: str):
    row = conn.execute("SELECT * FROM central_users WHERE id=? AND enabled=1", (user_id,)).fetchone()
    if row is None:
        raise AccountError(404, "account_not_found", "账号不存在或已停用。")
    return row


def _record(conn, kind: str, record_id: str, value: dict, now: str) -> None:
    conn.execute("INSERT INTO central_records VALUES(?,?,?,?) ON CONFLICT(kind,id) "
                 "DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                 (kind, record_id, json.dumps(value, ensure_ascii=False), now))


def sync_legacy_account(conn, target) -> None:
    """A disabled/demoted original admin must not retain legacy admin login."""
    if target["id"] != SEED_ADMIN_ID:
        return
    allowed = bool(target["enabled"]) and "admin" in json.loads(target["roles_json"])
    encoded = target["password_hash"] if allowed else db.hash_password(secrets.token_urlsafe(32))
    conn.execute("UPDATE admin_account SET username=?,password_hash=?,updated_at=? WHERE id=1",
                 (target["username"], encoded, db.utc_now()))
    conn.execute("DELETE FROM admin_sessions")


def _update(conn, target, encoded: str, actor_id: str, action: str) -> None:
    """Hash, all affected session revocations and audit commit together."""
    now = db.utc_now()
    conn.execute("UPDATE central_users SET password_hash=? WHERE id=?", (encoded, target["id"]))
    conn.execute("DELETE FROM central_sessions WHERE user_id=?", (target["id"],))
    if target["id"] == SEED_ADMIN_ID:
        updated = conn.execute("SELECT * FROM central_users WHERE id=?", (target["id"],)).fetchone()
        sync_legacy_account(conn, updated)
        _record(conn, "state", "bootstrap_password_status", {
            "invalidated": True, "user_id": target["id"], "changed_at": now,
            "message": "初始密码已失效，请使用本人设置的密码。",
        }, now)
    record_id = store.identifier("account_audit")
    _record(conn, "account_audit", record_id, {
        "id": record_id, "action": action, "actor_id": actor_id,
        "target_id": target["id"], "created_at": now,
    }, now)


def refresh_initial_access_note(user_id: str) -> bool:
    """Replace the obsolete bootstrap note, never with a current password.

    Password changes are already committed. A failed note write must not make
    callers think the password transaction failed and repeat it blindly.
    """
    if user_id != SEED_ADMIN_ID or os.environ.get('RDOS_RUNTIME_MODE') == 'production':
        return True
    updated = False
    temporary = None
    try:
        runtime = Path(os.environ["RDOS_LAB_RUNTIME"])
        target = runtime / "PANEL_ACCESS.txt"
        if (not runtime.is_absolute() or runtime.is_symlink() or db.db_path().parent != runtime.resolve()
            or target.is_symlink() or (target.exists() and target.stat().st_nlink != 1)):
            raise ValueError("invalid access note path")
        user = store.user(user_id)
        address = os.environ.get("BAITE_PUBLIC_URL", "")
        parsed = urlsplit(address)
        if parsed.scheme not in ("http", "https") or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password:
            raise ValueError("invalid panel address")
        status = store.get('state', 'bootstrap_password_status') or {}
        password_note = ("初始密码已失效，请使用本人设置的密码。" if status.get('invalidated')
                         else "请使用当前密码。")
        text = ("RDOS Panel 登录\n地址：" + address + "\n账号：" + user["username"]
            + "\n" + password_note + "本文件不保存当前密码。\n"
            + "忘记密码时，先停止 Panel，再在本机运行 labctl.py reset-password。\n")
        descriptor, name = tempfile.mkstemp(prefix=".panel-access-", dir=runtime)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(text)
        temporary.chmod(0o600)
        os.replace(temporary, target)
        updated = True
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        pass
    finally:
        if temporary is not None:
            try: temporary.unlink(missing_ok=True)
            except OSError: pass
    try:
        with db.connection() as conn:
            row = conn.execute("SELECT payload FROM central_records WHERE kind='state' AND id='bootstrap_password_status'").fetchone()
            if row:
                status = json.loads(row[0])
                status["access_note_updated"] = updated
                _record(conn, "state", "bootstrap_password_status", status, db.utc_now())
    except (OSError, ValueError, sqlite3.Error):
        pass
    return updated


def change_password(user_id: str, current_password: str, new_password: str,
                    confirm_password: str) -> dict:
    encoded = _new_hash(new_password, confirm_password)
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _enabled_user(conn, user_id)
        if not db.verify_password(current_password, actor["password_hash"]):
            raise AccountError(403, "current_password_invalid", "当前密码不正确。")
        _update(conn, actor, encoded, actor["id"], "password_changed")
    refresh_initial_access_note(user_id)
    return {"ok": True, "relogin_required": True}


def reset_password(actor_id: str, target_id: str, current_password: str,
                   new_password: str, confirm_password: str) -> dict:
    encoded = _new_hash(new_password, confirm_password)
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _enabled_user(conn, actor_id)
        if "admin" not in json.loads(actor["roles_json"]):
            raise AccountError(403, "admin_required", "仅管理员可以重置其他账号的密码。")
        if not db.verify_password(current_password, actor["password_hash"]):
            raise AccountError(403, "current_password_invalid", "管理员当前密码不正确。")
        if target_id == actor_id:
            raise AccountError(409, "use_change_password", "修改本人密码请使用账号菜单中的“修改我的密码”。")
        target = _enabled_user(conn, target_id)
        _update(conn, target, encoded, actor_id, "password_reset")
    refresh_initial_access_note(target_id)
    return {"ok": True, "relogin_required": False}


def recover_password(username: str, new_password: str, confirm_password: str) -> dict:
    """Local operator recovery; deliberately has no HTTP route.

    The supported labctl entrypoint validates the local environment and reads
    both secrets with getpass before invoking this helper under the guard.
    """
    encoded = _new_hash(new_password, confirm_password)
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM central_users WHERE username=? AND enabled=1", (username,)).fetchone()
        if row is None or "admin" not in json.loads(row["roles_json"]):
            raise AccountError(404, "admin_not_found", "未找到启用的管理员账号。")
        _update(conn, row, encoded, "local:operator", "password_recovered")
        target_id = row["id"]
    refresh_initial_access_note(target_id)
    return {"ok": True, "relogin_required": True}


def _failure(exc: AccountError) -> HTTPException:
    return HTTPException(exc.status, {"code": exc.code, "message": exc.message})


@router.post("/auth/change-password")
def change(payload: PasswordInput, request: Request, response: Response,
           user: dict = Depends(_current_user)):
    from server.central_api import _check_login_limit, _cookie_path
    _check_login_limit(request)
    try:
        result = change_password(user["id"], payload.current_password, payload.new_password,
                                 payload.confirm_password)
    except AccountError as exc:
        raise _failure(exc) from exc
    response.delete_cookie(COOKIE, path=_cookie_path())
    if user["id"] == SEED_ADMIN_ID:
        response.delete_cookie(lab_panel.cookie_name("admin"), path=_cookie_path())
    return result


@router.post("/users/{user_id}/reset-password")
def reset(user_id: str, payload: PasswordInput, request: Request,
          user: dict = Depends(_current_user)):
    from server.central_api import _check_login_limit
    _check_login_limit(request)
    try:
        return reset_password(user["id"], user_id, payload.current_password,
                              payload.new_password, payload.confirm_password)
    except AccountError as exc:
        raise _failure(exc) from exc
