from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo

from rdos_protocol import content_hash, seal_snapshot
from server.backup import backup_database

from server.workflow_reference import (
    WORKFLOW_SOURCE_HASH,
    WORKFLOW_SOURCE_TOKEN,
    WORKFLOW_SOURCE_URL,
    WORKFLOW_VERSION,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "baite.db"
PASSWORD_ITERATIONS = 310_000
_SNAPSHOT_CONNECTION: ContextVar = ContextVar("snapshot_connection", default=None)
DEFAULT_RULE = """# 全局工作规则

- 优先使用当前已同步的 Selected RAG；
- Shared Skill 是机构推荐版本，Local Agent 可以保留个性化版本；
- Local Skill 的稳定改进应通过 Proposal 提交给系统管理员；
- 重大风险、对外发布和不可逆操作必须升级给系统管理员；
- 日常执行步骤、Skill 选择和本地 Workflow 由 Local Agent 自主决定。
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_path() -> Path:
    configured = os.environ.get("BAITE_DB_PATH")
    return Path(configured).expanduser().resolve() if configured else DEFAULT_DB_PATH


def _secret_key() -> bytes:
    value = os.environ.get("BAITE_SESSION_SECRET", "")
    if len(value) < 24:
        raise RuntimeError("BAITE_SESSION_SECRET 至少需要 24 个字符")
    return value.encode("utf-8")


def hash_token(token: str) -> str:
    return hmac.new(_secret_key(), token.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    if len(password) < 10:
        raise ValueError("管理员密码至少需要 10 个字符")
    actual_salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), actual_salt, PASSWORD_ITERATIONS
    )
    return "$".join(
        (
            "pbkdf2_sha256",
            str(PASSWORD_ITERATIONS),
            base64.urlsafe_b64encode(actual_salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_text, expected_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(expected_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    existing = _SNAPSHOT_CONNECTION.get()
    if existing is not None:
        yield existing
        return
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def consistent_snapshot() -> Iterator[None]:
    with connection() as conn:
        conn.execute("BEGIN")
        token = _SNAPSHOT_CONNECTION.set(conn)
        try:
            yield
        finally:
            _SNAPSHOT_CONNECTION.reset(token)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
    )


def _setting(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def _set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, now),
    )


def _bump_revision(conn: sqlite3.Connection) -> int:
    current = int(_setting(conn, "shared_revision") or "0") + 1
    _set_setting(conn, "shared_revision", str(current))
    return current


def _backup_legacy_database() -> None:
    path = db_path()
    if path != DEFAULT_DB_PATH or not path.exists():
        return
    with closing(sqlite3.connect(path)) as conn:
        legacy = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agents'"
        ).fetchone()
        migrated = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runner_nodes'"
        ).fetchone()
    if not legacy or migrated:
        return
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f"pre-admin-workbench-{path.stat().st_mtime_ns}.db"
    if not destination.exists():
        backup_database(path, destination)


def _backup_database_for_schema(target_version: int) -> None:
    path = db_path()
    if not path.exists():
        return
    try:
        with closing(sqlite3.connect(path)) as conn:
            has_settings = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
            ).fetchone()
            if not has_settings:
                return
            row = conn.execute(
                "SELECT value FROM settings WHERE key='schema_version'"
            ).fetchone()
            current = int(row[0]) if row else 0
    except (sqlite3.DatabaseError, TypeError, ValueError):
        return
    if current >= target_version:
        return
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f"pre-schema-v{target_version}-{path.stat().st_mtime_ns}.db"
    if not destination.exists():
        backup_database(path, destination)


def _rename_legacy_tables(conn: sqlite3.Connection) -> None:
    mappings = (
        ("agents", "legacy_agents"),
        ("collaboration_requests", "legacy_collaboration_requests"),
        ("selected_rag", "legacy_selected_rag"),
    )
    for source, destination in mappings:
        if _table_exists(conn, source) and not _table_exists(conn, destination):
            conn.execute(f"ALTER TABLE {source} RENAME TO {destination}")


def _next_monday_nine() -> str:
    zone_name = os.environ.get("BAITE_TIMEZONE", "Asia/Shanghai")
    zone = ZoneInfo(zone_name)
    now = datetime.now(zone)
    days = (7 - now.weekday()) % 7
    candidate = (now + timedelta(days=days)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate.astimezone(timezone.utc).isoformat()


def next_workday_nine() -> str:
    zone_name = os.environ.get("BAITE_TIMEZONE", "Asia/Shanghai")
    zone = ZoneInfo(zone_name)
    now = datetime.now(zone)
    candidate = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc).isoformat()


def init_db() -> None:
    _backup_legacy_database()
    _backup_database_for_schema(5)
    with connection() as conn:
        if _table_exists(conn, "settings"):
            _rename_legacy_tables(conn)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admin_account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admin_sessions (
                token_hash TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runner_nodes (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                workspace_key TEXT NOT NULL UNIQUE,
                workspace_path TEXT NOT NULL DEFAULT '',
                token_hash TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_seen_at TEXT,
                current_focus TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'idle',
                progress_summary TEXT NOT NULL DEFAULT '',
                needs_collaboration TEXT NOT NULL DEFAULT '',
                progress_updated_at TEXT,
                last_synced_revision INTEGER NOT NULL DEFAULT -1,
                sync_health TEXT NOT NULL DEFAULT 'unknown',
                sync_failure_count INTEGER NOT NULL DEFAULT 0,
                last_sync_success_at TEXT,
                last_sync_error TEXT NOT NULL DEFAULT '',
                retry_nonce INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS shared_skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                version TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                file_path TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS skill_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                source_runner_id TEXT NOT NULL REFERENCES runner_nodes(id),
                skill_name TEXT NOT NULL,
                base_version TEXT NOT NULL DEFAULT '',
                proposed_version TEXT NOT NULL,
                summary TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'published', 'returned')),
                feedback TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runner_activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                runner_id TEXT NOT NULL REFERENCES runner_nodes(id),
                kind TEXT NOT NULL CHECK (kind IN ('skill', 'workflow', 'method')),
                name TEXT NOT NULL,
                version TEXT NOT NULL DEFAULT '',
                purpose TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS collaborations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                from_runner_id TEXT NOT NULL REFERENCES runner_nodes(id),
                to_runner_id TEXT NOT NULL REFERENCES runner_nodes(id),
                category TEXT NOT NULL CHECK (
                    category IN ('ordinary', 'risk', 'external_release', 'irreversible')
                ),
                topic TEXT NOT NULL,
                summary TEXT NOT NULL,
                reference TEXT NOT NULL DEFAULT '',
                requires_admin INTEGER NOT NULL CHECK (requires_admin IN (0, 1)),
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'confirmed', 'returned', 'escalated')
                ),
                feedback TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (from_runner_id != to_runner_id)
            );

            CREATE TABLE IF NOT EXISTS event_receipts (
                event_id TEXT PRIMARY KEY,
                runner_id TEXT NOT NULL REFERENCES runner_nodes(id),
                event_type TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                response_json TEXT NOT NULL,
                received_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rag_snapshots (
                id TEXT PRIMARY KEY,
                folder_token TEXT NOT NULL,
                manifest_hash TEXT NOT NULL UNIQUE,
                manifest_json TEXT NOT NULL,
                unsupported_json TEXT NOT NULL DEFAULT '[]',
                synced_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rag_snapshot_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id TEXT NOT NULL REFERENCES rag_snapshots(id),
                source_token TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_name TEXT NOT NULL,
                runtime_name TEXT NOT NULL,
                source_url TEXT NOT NULL DEFAULT '',
                modified_time TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL,
                content TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                UNIQUE(snapshot_id, source_token),
                UNIQUE(snapshot_id, runtime_name)
            );

            CREATE TABLE IF NOT EXISTS rag_sync_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                folder_token TEXT NOT NULL DEFAULT '',
                active_snapshot_id TEXT REFERENCES rag_snapshots(id),
                status TEXT NOT NULL DEFAULT 'never' CHECK (
                    status IN ('never', 'healthy', 'recovering', 'needs_admin', 'legacy')
                ),
                failure_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                last_attempt_at TEXT,
                last_success_at TEXT,
                next_attempt_at TEXT,
                next_scheduled_at TEXT NOT NULL,
                ignored_items_json TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                feishu_folder_token TEXT NOT NULL,
                feishu_folder_url TEXT NOT NULL,
                workflow_version TEXT NOT NULL,
                development_mode TEXT NOT NULL,
                product_types_json TEXT NOT NULL DEFAULT '[]',
                delivery_scales_json TEXT NOT NULL DEFAULT '[]',
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                daily_cutoff TEXT NOT NULL DEFAULT '18:00',
                timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                status TEXT NOT NULL DEFAULT 'active' CHECK (
                    status IN ('active', 'paused', 'completed')
                ),
                revision INTEGER NOT NULL DEFAULT 1,
                active_snapshot_id TEXT,
                source_sync_status TEXT NOT NULL DEFAULT 'never' CHECK (
                    source_sync_status IN ('never', 'healthy', 'recovering', 'needs_admin')
                ),
                source_failure_count INTEGER NOT NULL DEFAULT 0,
                source_last_error TEXT NOT NULL DEFAULT '',
                source_last_attempt_at TEXT,
                source_last_success_at TEXT,
                source_next_sync_at TEXT,
                progress_folder_token TEXT NOT NULL DEFAULT '',
                progress_folder_url TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS project_members (
                project_id TEXT NOT NULL REFERENCES projects(id),
                runner_id TEXT NOT NULL REFERENCES runner_nodes(id),
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                joined_at TEXT NOT NULL,
                left_at TEXT,
                PRIMARY KEY(project_id, runner_id)
            );

            CREATE TABLE IF NOT EXISTS project_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                project_id TEXT NOT NULL REFERENCES projects(id),
                runner_id TEXT NOT NULL REFERENCES runner_nodes(id),
                report_date TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
                workflow_nodes_json TEXT NOT NULL DEFAULT '[]',
                issues_json TEXT NOT NULL DEFAULT '[]',
                file_manifest_json TEXT NOT NULL DEFAULT '[]',
                file_changes_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS project_workflow_states (
                project_id TEXT NOT NULL REFERENCES projects(id),
                node_code TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('not_started', 'in_progress', 'complete', 'blocked', 'not_applicable')
                ),
                evidence_json TEXT NOT NULL DEFAULT '[]',
                source_report_id INTEGER REFERENCES project_reports(id),
                updated_at TEXT NOT NULL,
                PRIMARY KEY(project_id, node_code)
            );

            CREATE TABLE IF NOT EXISTS project_todos (
                project_id TEXT NOT NULL REFERENCES projects(id),
                todo_id TEXT NOT NULL,
                title TEXT NOT NULL,
                owner TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK (status IN ('open', 'in_progress', 'blocked', 'done')),
                due_date TEXT,
                source_runner_id TEXT REFERENCES runner_nodes(id),
                source_report_id INTEGER REFERENCES project_reports(id),
                updated_at TEXT NOT NULL,
                PRIMARY KEY(project_id, todo_id)
            );

            CREATE TABLE IF NOT EXISTS project_gate_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL REFERENCES projects(id),
                runner_id TEXT NOT NULL REFERENCES runner_nodes(id),
                source_report_id INTEGER REFERENCES project_reports(id),
                gate_code TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '[]',
                note TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK (
                    status IN ('reported_passed', 'confirmed', 'returned')
                ),
                feedback TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS project_snapshots (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                manifest_hash TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                ignored_json TEXT NOT NULL DEFAULT '[]',
                synced_at TEXT NOT NULL,
                UNIQUE(project_id, manifest_hash)
            );

            CREATE TABLE IF NOT EXISTS project_snapshot_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id TEXT NOT NULL REFERENCES project_snapshots(id),
                source_token TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_name TEXT NOT NULL,
                runtime_name TEXT NOT NULL,
                source_url TEXT NOT NULL DEFAULT '',
                modified_time TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL,
                content TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                UNIQUE(snapshot_id, source_token),
                UNIQUE(snapshot_id, runtime_name)
            );

            CREATE TABLE IF NOT EXISTS project_export_state (
                project_id TEXT NOT NULL REFERENCES projects(id),
                runner_id TEXT NOT NULL REFERENCES runner_nodes(id),
                status TEXT NOT NULL DEFAULT 'pending' CHECK (
                    status IN ('pending', 'healthy', 'recovering', 'needs_admin')
                ),
                dirty INTEGER NOT NULL DEFAULT 1 CHECK (dirty IN (0, 1)),
                failure_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                last_attempt_at TEXT,
                last_success_at TEXT,
                PRIMARY KEY(project_id, runner_id)
            );

            CREATE TABLE IF NOT EXISTS project_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL REFERENCES projects(id),
                kind TEXT NOT NULL,
                message TEXT NOT NULL,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS workflow_reference_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version TEXT NOT NULL,
                source_token TEXT NOT NULL,
                source_url TEXT NOT NULL,
                expected_hash TEXT NOT NULL,
                observed_hash TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'unchecked' CHECK (
                    status IN ('unchecked', 'healthy', 'changed', 'recovering', 'needs_admin')
                ),
                failure_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                last_checked_at TEXT,
                next_check_at TEXT NOT NULL
            );
            """
        )

        runner_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(runner_nodes)").fetchall()
        }
        if "workspace_path" not in runner_columns:
            conn.execute(
                "ALTER TABLE runner_nodes ADD COLUMN workspace_path TEXT NOT NULL DEFAULT ''"
            )
        for column in ("platform", "runner_version", "actual_workspace"):
            if column not in runner_columns:
                conn.execute(f"ALTER TABLE runner_nodes ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")

        now = utc_now()
        receipt_columns = {row["name"] for row in conn.execute("PRAGMA table_info(event_receipts)")}
        if "payload_hash" not in receipt_columns:
            # Historical receipts cannot safely acknowledge an unknown payload.
            conn.execute("ALTER TABLE event_receipts ADD COLUMN payload_hash TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES ('current_rule', ?, ?)",
            (DEFAULT_RULE, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES ('shared_revision', '1', ?)",
            (now,),
        )

        admin = conn.execute("SELECT 1 FROM admin_account WHERE id = 1").fetchone()
        if not admin:
            username = os.environ.get("BAITE_ADMIN_USERNAME", "").strip()
            password = os.environ.get("BAITE_ADMIN_PASSWORD", "")
            if not username or not password:
                raise RuntimeError(
                    "首次启动需要设置 BAITE_ADMIN_USERNAME 和 BAITE_ADMIN_PASSWORD"
                )
            conn.execute(
                "INSERT INTO admin_account(id, username, password_hash, updated_at) VALUES (1, ?, ?, ?)",
                (username, hash_password(password), now),
            )

        folder_token = os.environ.get("BAITE_SELECTED_RAG_FOLDER_TOKEN", "").strip()
        conn.execute(
            """
            INSERT OR IGNORE INTO rag_sync_state(
                id, folder_token, next_scheduled_at
            ) VALUES (1, ?, ?)
            """,
            (folder_token, _next_monday_nine()),
        )
        if folder_token:
            conn.execute(
                "UPDATE rag_sync_state SET folder_token = ? WHERE id = 1",
                (folder_token,),
            )

        _import_legacy_rag(conn)
        workflow_token = os.environ.get(
            "BAITE_RD_WORKFLOW_FILE_TOKEN", WORKFLOW_SOURCE_TOKEN
        ).strip()
        workflow_url = os.environ.get(
            "BAITE_RD_WORKFLOW_SOURCE_URL", WORKFLOW_SOURCE_URL
        ).strip()
        workflow_hash = os.environ.get(
            "BAITE_RD_WORKFLOW_EXPECTED_HASH", WORKFLOW_SOURCE_HASH
        ).strip()
        conn.execute(
            """
            INSERT OR IGNORE INTO workflow_reference_state(
                id, version, source_token, source_url, expected_hash, next_check_at
            ) VALUES (1, ?, ?, ?, ?, ?)
            """,
            (
                WORKFLOW_VERSION,
                workflow_token,
                workflow_url,
                workflow_hash,
                next_workday_nine(),
            ),
        )
        conn.execute(
            """
            UPDATE workflow_reference_state
            SET source_token = ?, source_url = ?, expected_hash = ?
            WHERE id = 1
            """,
            (workflow_token, workflow_url, workflow_hash),
        )
        _set_setting(conn, "schema_version", "5")


def _import_legacy_rag(conn: sqlite3.Connection) -> None:
    state = conn.execute("SELECT active_snapshot_id FROM rag_sync_state WHERE id = 1").fetchone()
    if state and state["active_snapshot_id"]:
        return
    if not _table_exists(conn, "legacy_selected_rag"):
        return
    rows = conn.execute(
        "SELECT id, name, content, updated_at FROM legacy_selected_rag ORDER BY name"
    ).fetchall()
    if not rows:
        return
    files = []
    for row in rows:
        content = str(row["content"])
        files.append(
            {
                "source_token": f"legacy:{row['id']}",
                "source_type": "legacy",
                "source_name": row["name"],
                "runtime_name": row["name"],
                "source_url": "",
                "modified_time": row["updated_at"],
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "content": content,
                "size_bytes": len(content.encode("utf-8")),
            }
        )
    manifest = [
        {key: item[key] for key in item if key not in ("content",)} for item in files
    ]
    manifest_text = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    manifest_hash = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()
    snapshot_id = f"legacy-{manifest_hash[:16]}"
    now = utc_now()
    conn.execute(
        """
        INSERT OR IGNORE INTO rag_snapshots(
            id, folder_token, manifest_hash, manifest_json, unsupported_json, synced_at
        ) VALUES (?, 'legacy', ?, ?, '[]', ?)
        """,
        (snapshot_id, manifest_hash, manifest_text, now),
    )
    for item in files:
        conn.execute(
            """
            INSERT OR IGNORE INTO rag_snapshot_files(
                snapshot_id, source_token, source_type, source_name, runtime_name,
                source_url, modified_time, content_hash, content, size_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                item["source_token"],
                item["source_type"],
                item["source_name"],
                item["runtime_name"],
                item["source_url"],
                item["modified_time"],
                item["content_hash"],
                item["content"],
                item["size_bytes"],
            ),
        )
    conn.execute(
        """
        UPDATE rag_sync_state
        SET active_snapshot_id = ?, status = 'legacy', last_success_at = ?
        WHERE id = 1
        """,
        (snapshot_id, now),
    )


def authenticate_admin(username: str, password: str) -> bool:
    with connection() as conn:
        row = conn.execute(
            "SELECT username, password_hash FROM admin_account WHERE id = 1"
        ).fetchone()
    return bool(row and row["username"] == username and verify_password(password, row["password_hash"]))


def create_admin_session(ttl_hours: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=ttl_hours)
    with connection() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (now.isoformat(),))
        conn.execute(
            "INSERT INTO admin_sessions(token_hash, created_at, expires_at) VALUES (?, ?, ?)",
            (hash_token(token), now.isoformat(), expires.isoformat()),
        )
    return token


def admin_session_valid(token: str) -> bool:
    now = utc_now()
    with connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM admin_sessions WHERE token_hash = ? AND expires_at > ?",
            (hash_token(token), now),
        ).fetchone()
    return bool(row)


def delete_admin_session(token: str) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (hash_token(token),))


def admin_profile() -> Dict[str, Any]:
    with connection() as conn:
        row = conn.execute(
            "SELECT username, updated_at FROM admin_account WHERE id = 1"
        ).fetchone()
    return dict(row)


def change_admin_password(current_password: str, new_password: str) -> None:
    profile = admin_profile()
    if not authenticate_admin(profile["username"], current_password):
        raise ValueError("current_password_invalid")
    encoded = hash_password(new_password)
    with connection() as conn:
        conn.execute(
            "UPDATE admin_account SET password_hash = ?, updated_at = ? WHERE id = 1",
            (encoded, utc_now()),
        )
        conn.execute("DELETE FROM admin_sessions")


def _workspace_key(display_name: str) -> str:
    cleaned = "".join(
        char.lower() if char.isalnum() else "-" for char in display_name.strip()
    ).strip("-")
    return cleaned[:80] or f"runner-{secrets.token_hex(4)}"


def create_runner(display_name: str, workspace_path: str, public_url: str) -> Dict[str, Any]:
    runner_id = f"rnr_{secrets.token_urlsafe(12)}"
    token = f"brt_{secrets.token_urlsafe(32)}"
    base_key = _workspace_key(display_name)
    now = utc_now()
    with connection() as conn:
        workspace_key = base_key
        index = 2
        while conn.execute(
            "SELECT 1 FROM runner_nodes WHERE workspace_key = ?", (workspace_key,)
        ).fetchone():
            workspace_key = f"{base_key}-{index}"
            index += 1
        conn.execute(
            """
            INSERT INTO runner_nodes(
                id, display_name, workspace_key, workspace_path, token_hash,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                runner_id,
                display_name,
                workspace_key,
                workspace_path,
                hash_token(token),
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM runner_nodes WHERE id = ?", (runner_id,)).fetchone()
    item = dict(row)
    item.pop("token_hash", None)
    item["runner_token"] = token
    item["config"] = {
        "control_url": public_url.rstrip("/"),
        "runner_id": runner_id,
        "runner_token": token,
        "workspace": workspace_path,
    }
    return item


def list_runners() -> List[Dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT id, display_name, workspace_key, workspace_path, enabled,
                   created_at, updated_at,
                   last_seen_at, current_focus, status, progress_summary,
                   needs_collaboration, progress_updated_at, last_synced_revision,
                   sync_health, sync_failure_count, last_sync_success_at,
                   last_sync_error, retry_nonce, platform, runner_version, actual_workspace
            FROM runner_nodes ORDER BY created_at, display_name
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_runner(runner_id: str) -> Optional[Dict[str, Any]]:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM runner_nodes WHERE id = ?", (runner_id,)
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    item.pop("token_hash", None)
    return item


def authenticate_runner(token: str) -> Optional[Dict[str, Any]]:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM runner_nodes WHERE token_hash = ? AND enabled = 1",
            (hash_token(token),),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    item.pop("token_hash", None)
    return item


def update_runner(runner_id: str, display_name: Optional[str], enabled: Optional[bool]) -> Dict[str, Any]:
    updates = []
    values: List[Any] = []
    if display_name is not None:
        updates.append("display_name = ?")
        values.append(display_name)
    if enabled is not None:
        updates.append("enabled = ?")
        values.append(int(enabled))
    if not updates:
        item = get_runner(runner_id)
        if not item:
            raise LookupError("runner_not_found")
        return item
    updates.append("updated_at = ?")
    values.append(utc_now())
    values.append(runner_id)
    with connection() as conn:
        cursor = conn.execute(
            f"UPDATE runner_nodes SET {', '.join(updates)} WHERE id = ?", values
        )
        if cursor.rowcount != 1:
            raise LookupError("runner_not_found")
    return get_runner(runner_id) or {}


def rotate_runner_token(runner_id: str) -> str:
    token = f"brt_{secrets.token_urlsafe(32)}"
    with connection() as conn:
        cursor = conn.execute(
            "UPDATE runner_nodes SET token_hash = ?, updated_at = ? WHERE id = ?",
            (hash_token(token), utc_now(), runner_id),
        )
        if cursor.rowcount != 1:
            raise LookupError("runner_not_found")
    return token


def retry_runner_sync(runner_id: str) -> Dict[str, Any]:
    with connection() as conn:
        cursor = conn.execute(
            """
            UPDATE runner_nodes
            SET retry_nonce = retry_nonce + 1, sync_failure_count = 0,
                sync_health = 'recovering', last_sync_error = '', updated_at = ?
            WHERE id = ?
            """,
            (utc_now(), runner_id),
        )
        if cursor.rowcount != 1:
            raise LookupError("runner_not_found")
    return get_runner(runner_id) or {}


def heartbeat(runner_id: str, runtime: Optional[Dict[str, str]] = None) -> None:
    with connection() as conn:
        updates = ["last_seen_at = ?"]
        values = [utc_now()]
        for field in ("platform", "runner_version", "actual_workspace"):
            if runtime and field in runtime:
                updates.append(f"{field} = ?")
                values.append(runtime[field])
        cursor = conn.execute(
            f"UPDATE runner_nodes SET {', '.join(updates)} WHERE id = ? AND enabled = 1",
            (*values, runner_id),
        )
        if cursor.rowcount != 1:
            raise LookupError("runner_not_found")


def update_runner_sync_status(
    runner_id: str,
    revision: int,
    health: str,
    failure_count: int,
    error: str,
) -> Dict[str, Any]:
    now = utc_now()
    success_at = now if health == "healthy" else None
    with connection() as conn:
        cursor = conn.execute(
            """
            UPDATE runner_nodes
            SET last_synced_revision = ?, sync_health = ?, sync_failure_count = ?,
                last_sync_error = ?,
                last_sync_success_at = COALESCE(?, last_sync_success_at),
                updated_at = ?
            WHERE id = ? AND enabled = 1
            """,
            (revision, health, failure_count, error, success_at, now, runner_id),
        )
        if cursor.rowcount != 1:
            raise LookupError("runner_not_found")
    return get_runner(runner_id) or {}


def get_rules() -> Dict[str, Any]:
    with connection() as conn:
        row = conn.execute(
            "SELECT value, updated_at FROM settings WHERE key = 'current_rule'"
        ).fetchone()
        revision = int(_setting(conn, "shared_revision") or "0")
    return {"content": row["value"], "updated_at": row["updated_at"], "revision": revision}


def update_rules(content: str) -> Dict[str, Any]:
    now = utc_now()
    with connection() as conn:
        conn.execute(
            "UPDATE settings SET value = ?, updated_at = ? WHERE key = 'current_rule'",
            (content, now),
        )
        revision = _bump_revision(conn)
    return {"content": content, "updated_at": now, "revision": revision}


def current_revision() -> int:
    with connection() as conn:
        return int(_setting(conn, "shared_revision") or "0")


def list_skills(include_content: bool = True) -> List[Dict[str, Any]]:
    columns = "id, name, version, description, updated_at"
    if include_content:
        columns += ", content"
    with connection() as conn:
        rows = conn.execute(f"SELECT {columns} FROM shared_skills ORDER BY name").fetchall()
    return [dict(row) for row in rows]


def list_skill_proposals() -> List[Dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT p.*, r.display_name AS source_runner_name,
                   s.version AS current_version, s.content AS current_content
            FROM skill_proposals p
            JOIN runner_nodes r ON r.id = p.source_runner_id
            LEFT JOIN shared_skills s ON s.name = p.skill_name
            ORDER BY CASE p.status WHEN 'pending' THEN 0 ELSE 1 END, p.updated_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def publish_skill_proposal(proposal_id: int) -> Dict[str, Any]:
    now = utc_now()
    with connection() as conn:
        proposal = conn.execute(
            "SELECT * FROM skill_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        if not proposal:
            raise LookupError("proposal_not_found")
        if proposal["status"] != "pending":
            raise ValueError("proposal_already_decided")
        conn.execute(
            """
            INSERT INTO shared_skills(name, version, description, content, file_path, updated_at)
            VALUES (?, ?, ?, ?, '', ?)
            ON CONFLICT(name) DO UPDATE SET
                version = excluded.version,
                description = excluded.description,
                content = excluded.content,
                file_path = '',
                updated_at = excluded.updated_at
            """,
            (
                proposal["skill_name"],
                proposal["proposed_version"],
                proposal["summary"],
                proposal["content"],
                now,
            ),
        )
        conn.execute(
            "UPDATE skill_proposals SET status = 'published', updated_at = ? WHERE id = ?",
            (now, proposal_id),
        )
        _bump_revision(conn)
    return next(item for item in list_skill_proposals() if item["id"] == proposal_id)


def return_skill_proposal(proposal_id: int, feedback: str) -> Dict[str, Any]:
    with connection() as conn:
        cursor = conn.execute(
            """
            UPDATE skill_proposals
            SET status = 'returned', feedback = ?, updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (feedback, utc_now(), proposal_id),
        )
        if cursor.rowcount != 1:
            exists = conn.execute(
                "SELECT 1 FROM skill_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if not exists:
                raise LookupError("proposal_not_found")
            raise ValueError("proposal_already_decided")
        _bump_revision(conn)
    return next(item for item in list_skill_proposals() if item["id"] == proposal_id)


def list_activities(limit: int = 50) -> List[Dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT a.*, r.display_name AS runner_name
            FROM runner_activities a JOIN runner_nodes r ON r.id = a.runner_id
            ORDER BY a.recorded_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_collaborations() -> List[Dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT c.*, fr.display_name AS from_name, tr.display_name AS to_name
            FROM collaborations c
            JOIN runner_nodes fr ON fr.id = c.from_runner_id
            JOIN runner_nodes tr ON tr.id = c.to_runner_id
            ORDER BY c.updated_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def decide_collaboration(collaboration_id: int, decision: str, feedback: str) -> Dict[str, Any]:
    status = "confirmed" if decision == "confirm" else "returned"
    with connection() as conn:
        item = conn.execute(
            "SELECT * FROM collaborations WHERE id = ?", (collaboration_id,)
        ).fetchone()
        if not item:
            raise LookupError("collaboration_not_found")
        if not item["requires_admin"] or item["status"] not in ("pending", "escalated"):
            raise ValueError("collaboration_not_waiting_admin")
        conn.execute(
            "UPDATE collaborations SET status = ?, feedback = ?, updated_at = ? WHERE id = ?",
            (status, feedback, utc_now(), collaboration_id),
        )
        _bump_revision(conn)
    return next(item for item in list_collaborations() if item["id"] == collaboration_id)


def process_runner_event(runner_id: str, event: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    event_id = str(event["event_id"])
    event_type = str(event["type"])
    now = utc_now()
    payload_hash = content_hash(event)
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        receipt = conn.execute(
            "SELECT runner_id, payload_hash, response_json FROM event_receipts WHERE event_id = ?", (event_id,)
        ).fetchone()
        if receipt:
            if receipt["runner_id"] != runner_id or receipt["payload_hash"] != payload_hash:
                raise ValueError("event_id_conflict")
            return json.loads(receipt["response_json"]), True

        response: Dict[str, Any]
        if event_type == "progress":
            conn.execute(
                """
                UPDATE runner_nodes
                SET current_focus = ?, status = ?, progress_summary = ?,
                    needs_collaboration = ?, progress_updated_at = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (
                    event["current_focus"],
                    event["status"],
                    event["summary"],
                    event.get("needs_collaboration", ""),
                    now,
                    now,
                    runner_id,
                ),
            )
            response = {"kind": "progress", "runner_id": runner_id}
        elif event_type == "activity_record":
            conn.execute(
                """
                INSERT INTO runner_activities(
                    event_id, runner_id, kind, name, version, purpose, recorded_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    runner_id,
                    event["kind"],
                    event["name"],
                    event.get("version", ""),
                    event["purpose"],
                    event["recorded_at"],
                    now,
                ),
            )
            response = {"kind": "activity", "event_id": event_id}
        elif event_type == "skill_change_proposal":
            cursor = conn.execute(
                """
                INSERT INTO skill_proposals(
                    event_id, source_runner_id, skill_name, base_version,
                    proposed_version, summary, content, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    event_id,
                    runner_id,
                    event["skill_name"],
                    event.get("base_version", ""),
                    event["proposed_version"],
                    event["summary"],
                    event["content"],
                    now,
                    now,
                ),
            )
            response = {"kind": "skill_proposal", "proposal_id": cursor.lastrowid}
        elif event_type == "collaboration_request":
            target = conn.execute(
                "SELECT 1 FROM runner_nodes WHERE id = ? AND enabled = 1",
                (event["to_runner_id"],),
            ).fetchone()
            if not target:
                raise ValueError("target_runner_not_found")
            if event["to_runner_id"] == runner_id:
                raise ValueError("collaboration_target_must_differ")
            requires_admin = event["category"] != "ordinary"
            cursor = conn.execute(
                """
                INSERT INTO collaborations(
                    event_id, from_runner_id, to_runner_id, category, topic,
                    summary, reference, requires_admin, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    event_id,
                    runner_id,
                    event["to_runner_id"],
                    event["category"],
                    event["topic"],
                    event["summary"],
                    event.get("reference", ""),
                    int(requires_admin),
                    now,
                    now,
                ),
            )
            _bump_revision(conn)
            response = {"kind": "collaboration", "collaboration_id": cursor.lastrowid}
        elif event_type == "collaboration_response":
            item = conn.execute(
                "SELECT * FROM collaborations WHERE id = ?", (event["collaboration_id"],)
            ).fetchone()
            if not item:
                raise ValueError("collaboration_not_found")
            if item["to_runner_id"] != runner_id:
                raise ValueError("collaboration_response_not_allowed")
            if item["status"] != "pending" or item["requires_admin"]:
                raise ValueError("collaboration_not_waiting_runner")
            action = event["action"]
            status = {"confirm": "confirmed", "return": "returned", "escalate": "escalated"}[action]
            requires_admin = 1 if action == "escalate" else 0
            conn.execute(
                """
                UPDATE collaborations
                SET status = ?, requires_admin = ?, feedback = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, requires_admin, event.get("note", ""), now, item["id"]),
            )
            _bump_revision(conn)
            response = {"kind": "collaboration_response", "collaboration_id": item["id"]}
        elif event_type == "project_update":
            response = _process_project_update(conn, runner_id, event, now)
        else:
            raise ValueError("unsupported_event_type")

        conn.execute(
            """
            INSERT INTO event_receipts(event_id, runner_id, event_type, payload_hash, response_json, received_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_id, runner_id, event_type, payload_hash, json.dumps(response), now),
        )
    return response, False


def rag_state() -> Dict[str, Any]:
    with connection() as conn:
        row = conn.execute("SELECT * FROM rag_sync_state WHERE id = 1").fetchone()
    item = dict(row)
    item["ignored_items"] = json.loads(item.pop("ignored_items_json") or "[]")
    return item


def active_rag_files(include_content: bool = False) -> List[Dict[str, Any]]:
    columns = (
        "f.source_token, f.source_type, f.source_name, f.runtime_name, f.source_url, "
        "f.modified_time, f.content_hash, f.size_bytes, s.synced_at, s.id AS snapshot_id"
    )
    if include_content:
        columns += ", f.content"
    with connection() as conn:
        rows = conn.execute(
            f"""
            SELECT {columns}
            FROM rag_sync_state state
            JOIN rag_snapshots s ON s.id = state.active_snapshot_id
            JOIN rag_snapshot_files f ON f.snapshot_id = s.id
            WHERE state.id = 1
            ORDER BY f.runtime_name
            """
        ).fetchall()
    return [dict(row) for row in rows]


def commit_rag_snapshot(
    folder_token: str,
    manifest_hash: str,
    manifest: List[Dict[str, Any]],
    files: List[Dict[str, Any]],
    ignored_items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    now = utc_now()
    snapshot_id = f"rag_{manifest_hash[:24]}"
    changed = False
    with connection() as conn:
        state = conn.execute("SELECT * FROM rag_sync_state WHERE id = 1").fetchone()
        active_hash = None
        if state["active_snapshot_id"]:
            active = conn.execute(
                "SELECT manifest_hash FROM rag_snapshots WHERE id = ?",
                (state["active_snapshot_id"],),
            ).fetchone()
            active_hash = active["manifest_hash"] if active else None
        if active_hash != manifest_hash:
            changed = True
            conn.execute(
                """
                INSERT OR IGNORE INTO rag_snapshots(
                    id, folder_token, manifest_hash, manifest_json, unsupported_json, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    folder_token,
                    manifest_hash,
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                    json.dumps(ignored_items, ensure_ascii=False),
                    now,
                ),
            )
            for item in files:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO rag_snapshot_files(
                        snapshot_id, source_token, source_type, source_name, runtime_name,
                        source_url, modified_time, content_hash, content, size_bytes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        item["source_token"],
                        item["source_type"],
                        item["source_name"],
                        item["runtime_name"],
                        item.get("source_url", ""),
                        item.get("modified_time", ""),
                        item["content_hash"],
                        item["content"],
                        item["size_bytes"],
                    ),
                )
            _bump_revision(conn)
        else:
            snapshot_id = state["active_snapshot_id"]
        has_unsupported = any(
            item.get("status") == "unsupported" for item in ignored_items
        )
        status = "needs_admin" if has_unsupported else "healthy"
        conn.execute(
            """
            UPDATE rag_sync_state
            SET folder_token = ?, active_snapshot_id = ?, status = ?, failure_count = 0,
                last_error = ?, last_attempt_at = ?, last_success_at = ?,
                next_attempt_at = NULL, next_scheduled_at = ?, ignored_items_json = ?
            WHERE id = 1
            """,
            (
                folder_token,
                snapshot_id,
                status,
                "存在不支持的文件类型" if has_unsupported else "",
                now,
                now,
                _next_monday_nine(),
                json.dumps(ignored_items, ensure_ascii=False),
            ),
        )
    return {"changed": changed, "snapshot_id": snapshot_id, "state": rag_state()}


def record_rag_failure(error: str, retry_seconds: int = 60) -> Dict[str, Any]:
    now_dt = datetime.now(timezone.utc)
    with connection() as conn:
        state = conn.execute("SELECT failure_count FROM rag_sync_state WHERE id = 1").fetchone()
        failures = int(state["failure_count"]) + 1
        status = "needs_admin" if failures >= 10 else "recovering"
        next_attempt = None
        if status == "recovering":
            next_attempt = (now_dt + timedelta(seconds=retry_seconds)).isoformat()
        conn.execute(
            """
            UPDATE rag_sync_state
            SET status = ?, failure_count = ?, last_error = ?, last_attempt_at = ?,
                next_attempt_at = ? WHERE id = 1
            """,
            (status, failures, error[:5000], now_dt.isoformat(), next_attempt),
        )
    return rag_state()


def reset_rag_retry() -> Dict[str, Any]:
    with connection() as conn:
        conn.execute(
            """
            UPDATE rag_sync_state
            SET status = 'recovering', failure_count = 0, last_error = '',
                next_attempt_at = ? WHERE id = 1
            """,
            (utc_now(),),
        )
    return rag_state()


def rag_sync_due() -> bool:
    state = rag_state()
    now = datetime.now(timezone.utc)
    if state["status"] == "needs_admin":
        return False
    if state.get("next_attempt_at"):
        return datetime.fromisoformat(state["next_attempt_at"]) <= now
    return datetime.fromisoformat(state["next_scheduled_at"]) <= now


def shared_snapshot(runner_id: str) -> Dict[str, Any]:
    with consistent_snapshot():
        return seal_snapshot(_shared_snapshot(runner_id))


def _shared_snapshot(runner_id: str) -> Dict[str, Any]:
    runner = get_runner(runner_id)
    if not runner or not runner["enabled"]:
        raise LookupError("runner_not_found")
    rag = active_rag_files(include_content=True)
    state = rag_state()
    return {
        "revision": current_revision(),
        "global_rules": get_rules(),
        "shared_skills": list_skills(include_content=True),
        "skill_proposals": [
            {
                "event_id": item["event_id"],
                "skill_name": item["skill_name"],
                "base_version": item["base_version"],
                "proposed_version": item["proposed_version"],
                "summary": item["summary"],
                "status": item["status"],
                "feedback": item["feedback"],
                "updated_at": item["updated_at"],
            }
            for item in list_skill_proposals()
            if item["source_runner_id"] == runner_id
        ],
        "selected_rag": rag,
        "rag_snapshot": {
            "snapshot_id": state.get("active_snapshot_id"),
            "status": state["status"],
            "folder_token": state["folder_token"],
        },
        "collaborations": [
            item
            for item in list_collaborations()
            if item["from_runner_id"] == runner_id or item["to_runner_id"] == runner_id
        ],
        "retry_nonce": runner["retry_nonce"],
    }


def work_queue() -> Dict[str, Any]:
    proposals = [item for item in list_skill_proposals() if item["status"] == "pending"]
    judgments = []
    for item in list_collaborations():
        if item["requires_admin"] and item["status"] in ("pending", "escalated"):
            item["kind"] = "collaboration"
            judgments.append(item)
    with connection() as conn:
        gate_rows = conn.execute(
            """
            SELECT g.id, g.project_id, g.gate_code, g.note, g.created_at,
                   p.name AS project_name, r.display_name AS runner_name
            FROM project_gate_records g
            JOIN projects p ON p.id = g.project_id
            JOIN runner_nodes r ON r.id = g.runner_id
            WHERE g.status = 'reported_passed'
            ORDER BY g.created_at
            """
        ).fetchall()
    for row in gate_rows:
        item = dict(row)
        item["kind"] = "project_gate"
        judgments.append(item)
    state = rag_state()
    incidents: List[Dict[str, Any]] = []
    if state["status"] == "needs_admin":
        incidents.append(
            {
                "kind": "selected_rag",
                "title": "Selected RAG 同步需要处理",
                "message": state["last_error"],
                "failure_count": state["failure_count"],
            }
        )
    for runner in list_runners():
        if runner["sync_health"] == "needs_admin":
            incidents.append(
                {
                    "kind": "runner",
                    "runner_id": runner["id"],
                    "title": f"{runner['display_name']} 同步需要处理",
                    "message": runner["last_sync_error"],
                    "failure_count": runner["sync_failure_count"],
                }
            )
    for project in list_projects():
        if project["source_sync_status"] == "needs_admin":
            incidents.append(
                {
                    "kind": "project_source",
                    "project_id": project["id"],
                    "title": f"{project['name']} 资料同步需要处理",
                    "message": project["source_last_error"],
                    "failure_count": project["source_failure_count"],
                }
            )
        with connection() as conn:
            export_rows = conn.execute(
                "SELECT * FROM project_export_state WHERE project_id = ?",
                (project["id"],),
            ).fetchall()
        for export in (dict(row) for row in export_rows):
            if export["status"] == "needs_admin":
                incidents.append(
                    {
                        "kind": "project_export",
                        "project_id": project["id"],
                        "runner_id": export["runner_id"],
                        "title": f"{project['name']} 进度写回需要处理",
                        "message": export["last_error"],
                        "failure_count": export["failure_count"],
                    }
                )
    reference = workflow_reference_state()
    if reference["status"] in ("changed", "needs_admin"):
        incidents.append(
            {
                "kind": "workflow_reference",
                "title": "R&D Workflow v0.3 来源需要核对",
                "message": reference["last_error"],
                "failure_count": reference["failure_count"],
            }
        )
    return {"publish": proposals, "judgments": judgments, "incidents": incidents}


def overview() -> Dict[str, Any]:
    return {
        "runners": list_runners(),
        "activities": list_activities(30),
        "shared_skills": list_skills(include_content=False),
        "selected_rag": active_rag_files(include_content=False),
        "rag_sync": rag_state(),
        "work_queue": work_queue(),
        "shared_revision": current_revision(),
        "projects": list_projects(),
    }


# Project management intentionally remains separate from the organization-wide
# shared revision. A daily project report must not force unrelated runners to
# rebuild Rules, Skills, and Selected RAG.


def _json_value(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def extract_feishu_folder_token(value: str) -> str:
    text = value.strip()
    match = re.search(r"/drive/folder/([A-Za-z0-9_-]+)", text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{8,}", text):
        return text
    raise ValueError("invalid_feishu_folder")


def _project_members(conn: sqlite3.Connection, project_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT m.runner_id, m.active, m.joined_at, m.left_at,
               r.display_name, r.enabled
        FROM project_members m
        JOIN runner_nodes r ON r.id = m.runner_id
        WHERE m.project_id = ?
        ORDER BY m.active DESC, r.display_name
        """,
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _project_dict(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item["product_types"] = _json_value(item.pop("product_types_json"), [])
    item["delivery_scales"] = _json_value(item.pop("delivery_scales_json"), [])
    return item


def _local_now(zone_name: str) -> datetime:
    try:
        zone = ZoneInfo(zone_name)
    except Exception:
        zone = ZoneInfo("Asia/Shanghai")
    return datetime.now(zone)


def _reporting_state(
    project: Dict[str, Any], members: List[Dict[str, Any]], conn: sqlite3.Connection
) -> Dict[str, Any]:
    now = _local_now(project["timezone"])
    today = now.date().isoformat()
    in_period = project["start_date"] <= today <= project["end_date"]
    workday = now.weekday() < 5
    required = project["status"] == "active" and in_period and workday
    cutoff_hour, cutoff_minute = (int(part) for part in project["daily_cutoff"].split(":"))
    after_cutoff = (now.hour, now.minute) >= (cutoff_hour, cutoff_minute)
    statuses: List[Dict[str, Any]] = []
    for member in (item for item in members if item["active"]):
        report = conn.execute(
            """
            SELECT id, report_date, summary, complete, created_at
            FROM project_reports
            WHERE project_id = ? AND runner_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (project["id"], member["runner_id"]),
        ).fetchone()
        status = "not_required"
        if required:
            if report and report["report_date"] == today:
                status = "reported" if report["complete"] else "incomplete"
            else:
                status = "overdue" if after_cutoff else "due_today"
        statuses.append(
            {
                "runner_id": member["runner_id"],
                "display_name": member["display_name"],
                "status": status,
                "latest_report_at": report["created_at"] if report else None,
                "latest_summary": report["summary"] if report else "",
            }
        )

    pending_gate = conn.execute(
        """
        SELECT COUNT(*) AS count FROM project_gate_records
        WHERE project_id = ? AND status = 'reported_passed'
        """,
        (project["id"],),
    ).fetchone()["count"]
    states = {item["status"] for item in statuses}
    if project["status"] == "paused":
        computed = "paused"
    elif project["status"] == "completed":
        computed = "completed"
    elif today < project["start_date"]:
        computed = "not_started"
    elif today > project["end_date"]:
        computed = "cycle_ended"
    elif pending_gate:
        computed = "gate_verification"
    elif "overdue" in states or "incomplete" in states:
        computed = "overdue"
    elif "due_today" in states:
        computed = "due_today"
    else:
        computed = "on_track"
    return {
        "computed_status": computed,
        "today": today,
        "reporting_required": required,
        "runner_reports": statuses,
        "pending_gate_count": int(pending_gate),
    }


def _audit(
    conn: sqlite3.Connection, project_id: str, kind: str, message: str, actor: str
) -> None:
    conn.execute(
        """
        INSERT INTO project_audit(project_id, kind, message, actor, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (project_id, kind, message, actor, utc_now()),
    )


def create_project(payload: Dict[str, Any]) -> Dict[str, Any]:
    token = extract_feishu_folder_token(payload["feishu_folder_url"])
    project_id = f"prj_{secrets.token_urlsafe(10)}"
    member_ids = list(dict.fromkeys(payload["participant_runner_ids"]))
    if not member_ids:
        raise ValueError("participants_required")
    if payload["start_date"] > payload["end_date"]:
        raise ValueError("invalid_project_dates")
    now = utc_now()
    with connection() as conn:
        placeholders = ",".join("?" for _ in member_ids)
        enabled = conn.execute(
            f"SELECT id FROM runner_nodes WHERE enabled = 1 AND id IN ({placeholders})",
            member_ids,
        ).fetchall()
        if len(enabled) != len(member_ids):
            raise ValueError("participant_runner_not_found")
        duplicate = conn.execute(
            """
            SELECT 1 FROM projects
            WHERE feishu_folder_token = ? AND status != 'completed'
            """,
            (token,),
        ).fetchone()
        if duplicate:
            raise ValueError("folder_already_in_use")
        conn.execute(
            """
            INSERT INTO projects(
                id, name, description, feishu_folder_token, feishu_folder_url,
                workflow_version, development_mode, product_types_json,
                delivery_scales_json, start_date, end_date, daily_cutoff,
                timezone, status, source_next_sync_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                payload["name"],
                payload.get("description", ""),
                token,
                payload["feishu_folder_url"],
                WORKFLOW_VERSION,
                payload["development_mode"],
                json.dumps(payload.get("product_types", []), ensure_ascii=False),
                json.dumps(payload.get("delivery_scales", []), ensure_ascii=False),
                payload["start_date"],
                payload["end_date"],
                payload.get("daily_cutoff", "18:00"),
                payload.get("timezone", "Asia/Shanghai"),
                payload.get("status", "active"),
                now,
                now,
                now,
            ),
        )
        for runner_id in member_ids:
            conn.execute(
                """
                INSERT INTO project_members(project_id, runner_id, joined_at)
                VALUES (?, ?, ?)
                """,
                (project_id, runner_id, now),
            )
            conn.execute(
                """
                INSERT INTO project_export_state(project_id, runner_id)
                VALUES (?, ?)
                """,
                (project_id, runner_id),
            )
        _audit(conn, project_id, "project_created", "项目已立项", "system_admin")
    return get_project(project_id, include_history=True) or {}


def list_projects() -> List[Dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()
        result = []
        for row in rows:
            item = _project_dict(row)
            members = _project_members(conn, item["id"])
            item["members"] = [member for member in members if member["active"]]
            item.update(_reporting_state(item, members, conn))
            result.append(item)
    return result


def _active_project_files(
    conn: sqlite3.Connection, project_id: str, include_content: bool
) -> List[Dict[str, Any]]:
    columns = (
        "source_token, source_type, source_name, runtime_name, source_url, "
        "modified_time, content_hash, size_bytes"
    )
    if include_content:
        columns += ", content"
    rows = conn.execute(
        f"""
        SELECT {columns} FROM project_snapshot_files
        WHERE snapshot_id = (SELECT active_snapshot_id FROM projects WHERE id = ?)
        ORDER BY runtime_name
        """,
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_project(project_id: str, include_history: bool = False) -> Optional[Dict[str, Any]]:
    with connection() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not row:
            return None
        item = _project_dict(row)
        members = _project_members(conn, project_id)
        item["members"] = members
        item.update(_reporting_state(item, members, conn))
        item["workflow_nodes"] = [
            {
                **dict(node),
                "evidence": _json_value(node["evidence_json"], []),
            }
            for node in conn.execute(
                """
                SELECT node_code, status, evidence_json, source_report_id, updated_at
                FROM project_workflow_states WHERE project_id = ? ORDER BY node_code
                """,
                (project_id,),
            ).fetchall()
        ]
        for node in item["workflow_nodes"]:
            node.pop("evidence_json", None)
        item["todos"] = [
            dict(todo)
            for todo in conn.execute(
                """
                SELECT todo_id, title, owner, status, due_date, source_runner_id, updated_at
                FROM project_todos WHERE project_id = ?
                ORDER BY CASE status WHEN 'blocked' THEN 0 WHEN 'in_progress' THEN 1
                         WHEN 'open' THEN 2 ELSE 3 END, updated_at DESC
                """,
                (project_id,),
            ).fetchall()
        ]
        gates = conn.execute(
            """
            SELECT g.*, r.display_name AS runner_name
            FROM project_gate_records g
            JOIN runner_nodes r ON r.id = g.runner_id
            WHERE g.project_id = ? ORDER BY g.created_at DESC
            """,
            (project_id,),
        ).fetchall()
        item["gate_records"] = []
        for gate in gates:
            gate_item = dict(gate)
            gate_item["evidence"] = _json_value(gate_item.pop("evidence_json"), [])
            item["gate_records"].append(gate_item)
        item["content_files"] = _active_project_files(conn, project_id, False)
        snapshot = None
        if item.get("active_snapshot_id"):
            snapshot = conn.execute(
                "SELECT * FROM project_snapshots WHERE id = ?",
                (item["active_snapshot_id"],),
            ).fetchone()
        item["content_snapshot"] = dict(snapshot) if snapshot else None
        if item["content_snapshot"]:
            item["content_snapshot"]["ignored"] = _json_value(
                item["content_snapshot"].pop("ignored_json"), []
            )
            item["content_snapshot"].pop("manifest_json", None)
        item["export_states"] = [
            dict(export)
            for export in conn.execute(
                """
                SELECT e.*, r.display_name AS runner_name
                FROM project_export_state e JOIN runner_nodes r ON r.id = e.runner_id
                WHERE e.project_id = ? ORDER BY r.display_name
                """,
                (project_id,),
            ).fetchall()
        ]
        if include_history:
            reports = conn.execute(
                """
                SELECT p.*, r.display_name AS runner_name
                FROM project_reports p JOIN runner_nodes r ON r.id = p.runner_id
                WHERE p.project_id = ? ORDER BY p.id DESC LIMIT 100
                """,
                (project_id,),
            ).fetchall()
            item["reports"] = []
            for report in reports:
                report_item = dict(report)
                for source, target in (
                    ("workflow_nodes_json", "workflow_nodes"),
                    ("issues_json", "issues"),
                    ("file_manifest_json", "file_manifest"),
                    ("file_changes_json", "file_changes"),
                ):
                    report_item[target] = _json_value(report_item.pop(source), [])
                item["reports"].append(report_item)
            item["audit"] = [
                dict(audit)
                for audit in conn.execute(
                    """
                    SELECT * FROM project_audit WHERE project_id = ?
                    ORDER BY id DESC LIMIT 100
                    """,
                    (project_id,),
                ).fetchall()
            ]
    return item


def update_project(project_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    now = utc_now()
    with connection() as conn:
        current_row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not current_row:
            raise LookupError("project_not_found")
        current = _project_dict(current_row)
        merged = {**current, **{key: value for key, value in payload.items() if value is not None}}
        if merged["start_date"] > merged["end_date"]:
            raise ValueError("invalid_project_dates")
        folder_url = merged["feishu_folder_url"]
        folder_token = extract_feishu_folder_token(folder_url)
        folder_changed = folder_token != current["feishu_folder_token"]
        duplicate = conn.execute(
            """
            SELECT 1 FROM projects
            WHERE id != ? AND feishu_folder_token = ? AND status != 'completed'
            """,
            (project_id, folder_token),
        ).fetchone()
        if duplicate and merged["status"] != "completed":
            raise ValueError("folder_already_in_use")
        conn.execute(
            """
            UPDATE projects SET name = ?, description = ?, feishu_folder_token = ?,
                feishu_folder_url = ?, development_mode = ?, product_types_json = ?,
                delivery_scales_json = ?, start_date = ?, end_date = ?,
                daily_cutoff = ?, timezone = ?, status = ?, revision = revision + 1,
                source_next_sync_at = CASE WHEN feishu_folder_token != ? THEN ?
                                           ELSE source_next_sync_at END,
                updated_at = ? WHERE id = ?
            """,
            (
                merged["name"],
                merged.get("description", ""),
                folder_token,
                folder_url,
                merged["development_mode"],
                json.dumps(merged.get("product_types", []), ensure_ascii=False),
                json.dumps(merged.get("delivery_scales", []), ensure_ascii=False),
                merged["start_date"],
                merged["end_date"],
                merged["daily_cutoff"],
                merged["timezone"],
                merged["status"],
                folder_token,
                now,
                now,
                project_id,
            ),
        )
        if folder_changed:
            conn.execute(
                """
                UPDATE projects SET active_snapshot_id = NULL,
                    source_sync_status = 'never', source_failure_count = 0,
                    source_last_error = '', source_last_success_at = NULL,
                    source_next_sync_at = ?, progress_folder_token = '',
                    progress_folder_url = '' WHERE id = ?
                """,
                (now, project_id),
            )
            conn.execute(
                """
                UPDATE project_export_state SET status = 'pending', dirty = 1,
                    failure_count = 0, last_error = '' WHERE project_id = ?
                """,
                (project_id,),
            )
        if "participant_runner_ids" in payload and payload["participant_runner_ids"] is not None:
            requested = set(payload["participant_runner_ids"])
            if not requested:
                raise ValueError("participants_required")
            placeholders = ",".join("?" for _ in requested)
            valid = conn.execute(
                f"SELECT id FROM runner_nodes WHERE enabled = 1 AND id IN ({placeholders})",
                list(requested),
            ).fetchall()
            if len(valid) != len(requested):
                raise ValueError("participant_runner_not_found")
            existing_rows = conn.execute(
                "SELECT runner_id, active FROM project_members WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            existing = {row["runner_id"]: bool(row["active"]) for row in existing_rows}
            for runner_id in requested:
                conn.execute(
                    """
                    INSERT INTO project_members(project_id, runner_id, active, joined_at, left_at)
                    VALUES (?, ?, 1, ?, NULL)
                    ON CONFLICT(project_id, runner_id) DO UPDATE SET
                        active = 1, left_at = NULL
                    """,
                    (project_id, runner_id, now),
                )
                conn.execute(
                    """
                    INSERT INTO project_export_state(project_id, runner_id, status, dirty)
                    VALUES (?, ?, 'pending', 1)
                    ON CONFLICT(project_id, runner_id) DO UPDATE SET
                        status = 'pending', dirty = 1
                    """,
                    (project_id, runner_id),
                )
            for runner_id, active in existing.items():
                if active and runner_id not in requested:
                    conn.execute(
                        """
                        UPDATE project_members SET active = 0, left_at = ?
                        WHERE project_id = ? AND runner_id = ?
                        """,
                        (now, project_id, runner_id),
                    )
        _audit(conn, project_id, "project_updated", "项目设置已更新", "system_admin")
    return get_project(project_id, include_history=True) or {}


def update_project_todo(
    project_id: str, todo_id: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    now = utc_now()
    with connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not exists:
            raise LookupError("project_not_found")
        current = conn.execute(
            "SELECT * FROM project_todos WHERE project_id = ? AND todo_id = ?",
            (project_id, todo_id),
        ).fetchone()
        title = payload.get("title") or (current["title"] if current else "")
        if not title:
            raise ValueError("todo_title_required")
        owner = payload.get("owner", current["owner"] if current else "")
        status_value = payload.get("status", current["status"] if current else "open")
        due_date = payload.get("due_date", current["due_date"] if current else None)
        conn.execute(
            """
            INSERT INTO project_todos(
                project_id, todo_id, title, owner, status, due_date, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, todo_id) DO UPDATE SET
                title = excluded.title, owner = excluded.owner,
                status = excluded.status, due_date = excluded.due_date,
                updated_at = excluded.updated_at
            """,
            (project_id, todo_id, title, owner, status_value, due_date, now),
        )
        conn.execute(
            "UPDATE projects SET revision = revision + 1, updated_at = ? WHERE id = ?",
            (now, project_id),
        )
        _audit(conn, project_id, "todo_updated", f"TODO {todo_id} 已更新", "system_admin")
        row = conn.execute(
            "SELECT * FROM project_todos WHERE project_id = ? AND todo_id = ?",
            (project_id, todo_id),
        ).fetchone()
    return dict(row)


def decide_project_gate(
    project_id: str, record_id: int, decision: str, feedback: str
) -> Dict[str, Any]:
    target = "confirmed" if decision == "confirm" else "returned"
    now = utc_now()
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM project_gate_records WHERE id = ? AND project_id = ?",
            (record_id, project_id),
        ).fetchone()
        if not row:
            raise LookupError("gate_record_not_found")
        if row["status"] != "reported_passed":
            raise ValueError("gate_already_decided")
        conn.execute(
            """
            UPDATE project_gate_records SET status = ?, feedback = ?, updated_at = ?
            WHERE id = ?
            """,
            (target, feedback, now, record_id),
        )
        conn.execute(
            """
            UPDATE projects SET revision = revision + 1, updated_at = ? WHERE id = ?
            """,
            (now, project_id),
        )
        conn.execute(
            """
            UPDATE project_export_state SET dirty = 1, status = 'pending'
            WHERE project_id = ? AND runner_id = ?
            """,
            (project_id, row["runner_id"]),
        )
        _audit(
            conn,
            project_id,
            "gate_decision",
            f"{row['gate_code']} 记录已{('确认' if target == 'confirmed' else '退回核实')}",
            "system_admin",
        )
        result = conn.execute(
            "SELECT * FROM project_gate_records WHERE id = ?", (record_id,)
        ).fetchone()
    item = dict(result)
    item["evidence"] = _json_value(item.pop("evidence_json"), [])
    return item


def _manifest_changes(previous: List[Dict[str, Any]], current: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    old = {item["path"]: item for item in previous}
    new = {item["path"]: item for item in current}
    changes: List[Dict[str, Any]] = []
    for path in sorted(new.keys() - old.keys()):
        changes.append({"path": path, "change": "added"})
    for path in sorted(old.keys() - new.keys()):
        changes.append({"path": path, "change": "removed"})
    for path in sorted(old.keys() & new.keys()):
        if old[path].get("sha256") != new[path].get("sha256") or old[path].get(
            "size_bytes"
        ) != new[path].get("size_bytes"):
            changes.append({"path": path, "change": "modified"})
    return changes


def _process_project_update(
    conn: sqlite3.Connection, runner_id: str, event: Dict[str, Any], now: str
) -> Dict[str, Any]:
    project = conn.execute(
        "SELECT * FROM projects WHERE id = ?", (event["project_id"],)
    ).fetchone()
    if not project:
        raise ValueError("project_not_found")
    member = conn.execute(
        """
        SELECT 1 FROM project_members
        WHERE project_id = ? AND runner_id = ? AND active = 1
        """,
        (event["project_id"], runner_id),
    ).fetchone()
    if not member:
        raise ValueError("project_membership_required")
    local_date = datetime.fromisoformat(now).astimezone(
        ZoneInfo(project["timezone"])
    ).date().isoformat()
    previous = conn.execute(
        """
        SELECT file_manifest_json FROM project_reports
        WHERE project_id = ? AND runner_id = ? ORDER BY id DESC LIMIT 1
        """,
        (event["project_id"], runner_id),
    ).fetchone()
    previous_manifest = _json_value(previous["file_manifest_json"], []) if previous else []
    manifest = event.get("file_manifest", [])
    changes = _manifest_changes(previous_manifest, manifest)
    cursor = conn.execute(
        """
        INSERT INTO project_reports(
            event_id, project_id, runner_id, report_date, summary, complete,
            workflow_nodes_json, issues_json, file_manifest_json,
            file_changes_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event["event_id"],
            event["project_id"],
            runner_id,
            local_date,
            event.get("summary", ""),
            int(event.get("complete", False)),
            json.dumps(event.get("workflow_nodes", []), ensure_ascii=False),
            json.dumps(event.get("issues", []), ensure_ascii=False),
            json.dumps(manifest, ensure_ascii=False),
            json.dumps(changes, ensure_ascii=False),
            now,
        ),
    )
    report_id = int(cursor.lastrowid)
    for node in event.get("workflow_nodes", []):
        conn.execute(
            """
            INSERT INTO project_workflow_states(
                project_id, node_code, status, evidence_json, source_report_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, node_code) DO UPDATE SET
                status = excluded.status, evidence_json = excluded.evidence_json,
                source_report_id = excluded.source_report_id, updated_at = excluded.updated_at
            """,
            (
                event["project_id"],
                node["code"],
                node["status"],
                json.dumps(node.get("evidence", []), ensure_ascii=False),
                report_id,
                now,
            ),
        )
    for todo in event.get("todos", []):
        conn.execute(
            """
            INSERT INTO project_todos(
                project_id, todo_id, title, owner, status, due_date,
                source_runner_id, source_report_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, todo_id) DO UPDATE SET
                title = excluded.title, owner = excluded.owner,
                status = excluded.status, due_date = excluded.due_date,
                source_runner_id = excluded.source_runner_id,
                source_report_id = excluded.source_report_id,
                updated_at = excluded.updated_at
            """,
            (
                event["project_id"],
                todo["todo_id"],
                todo["title"],
                todo.get("owner", ""),
                todo["status"],
                todo.get("due_date"),
                runner_id,
                report_id,
                now,
            ),
        )
    gate_id = None
    gate = event.get("gate_claim")
    if gate and gate.get("claimed_passed"):
        evidence_json = json.dumps(gate.get("evidence", []), ensure_ascii=False)
        prior_gate = conn.execute(
            """
            SELECT id FROM project_gate_records
            WHERE project_id = ? AND runner_id = ? AND gate_code = ?
              AND evidence_json = ? AND note = ?
            ORDER BY id DESC LIMIT 1
            """,
            (
                event["project_id"],
                runner_id,
                gate["code"],
                evidence_json,
                gate.get("note", ""),
            ),
        ).fetchone()
        if prior_gate:
            gate_id = int(prior_gate["id"])
        else:
            gate_cursor = conn.execute(
                """
                INSERT INTO project_gate_records(
                    project_id, runner_id, source_report_id, gate_code,
                    evidence_json, note, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'reported_passed', ?, ?)
                """,
                (
                    event["project_id"],
                    runner_id,
                    report_id,
                    gate["code"],
                    evidence_json,
                    gate.get("note", ""),
                    now,
                    now,
                ),
            )
            gate_id = int(gate_cursor.lastrowid)
    conn.execute(
        """
        UPDATE projects SET revision = revision + 1, updated_at = ? WHERE id = ?
        """,
        (now, event["project_id"]),
    )
    conn.execute(
        """
        INSERT INTO project_export_state(project_id, runner_id, status, dirty)
        VALUES (?, ?, 'pending', 1)
        ON CONFLICT(project_id, runner_id) DO UPDATE SET dirty = 1, status = 'pending'
        """,
        (event["project_id"], runner_id),
    )
    _audit(
        conn,
        event["project_id"],
        "project_report",
        "Runner 已回传完整项目状态" if event.get("complete") else "Runner 已回传不完整项目状态",
        runner_id,
    )
    return {
        "kind": "project_update",
        "project_id": event["project_id"],
        "report_id": report_id,
        "gate_record_id": gate_id,
        "file_changes": changes,
    }


def runner_project_snapshot(runner_id: str) -> Dict[str, Any]:
    with consistent_snapshot():
        return seal_snapshot(_runner_project_snapshot(runner_id))


def _runner_project_snapshot(runner_id: str) -> Dict[str, Any]:
    with connection() as conn:
        ids = [
            row["id"]
            for row in conn.execute(
                """
                SELECT p.id FROM projects p
                JOIN project_members m ON m.project_id = p.id
                WHERE m.runner_id = ? AND m.active = 1 ORDER BY p.created_at
                """,
                (runner_id,),
            ).fetchall()
        ]
        items = []
        for project_id in ids:
            project = get_project(project_id, include_history=False)
            if not project:
                continue
            project["content_files"] = _active_project_files(conn, project_id, True)
            project["runner_today_reported"] = any(
                item["runner_id"] == runner_id
                and item["status"] in ("reported", "incomplete")
                for item in project["runner_reports"]
            )
            project.pop("reports", None)
            project.pop("audit", None)
            items.append(seal_snapshot(project))
    # Include export health, membership and date-derived state as well as revision.
    token = content_hash([item["snapshot_hash"] for item in items])
    return {"sync_token": token, "projects": items}


def commit_project_snapshot(
    project_id: str,
    manifest_hash: str,
    manifest: List[Dict[str, Any]],
    files: List[Dict[str, Any]],
    ignored: List[Dict[str, Any]],
) -> Dict[str, Any]:
    now = utc_now()
    snapshot_id = f"pjs_{hashlib.sha256((project_id + manifest_hash).encode()).hexdigest()[:24]}"
    with connection() as conn:
        project = conn.execute(
            "SELECT active_snapshot_id FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not project:
            raise LookupError("project_not_found")
        current_hash = None
        if project["active_snapshot_id"]:
            current = conn.execute(
                "SELECT manifest_hash FROM project_snapshots WHERE id = ?",
                (project["active_snapshot_id"],),
            ).fetchone()
            current_hash = current["manifest_hash"] if current else None
        changed = current_hash != manifest_hash
        if changed:
            conn.execute(
                """
                INSERT OR IGNORE INTO project_snapshots(
                    id, project_id, manifest_hash, manifest_json, ignored_json, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    project_id,
                    manifest_hash,
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                    json.dumps(ignored, ensure_ascii=False),
                    now,
                ),
            )
            for item in files:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO project_snapshot_files(
                        snapshot_id, source_token, source_type, source_name, runtime_name,
                        source_url, modified_time, content_hash, content, size_bytes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        item["source_token"],
                        item["source_type"],
                        item["source_name"],
                        item["runtime_name"],
                        item.get("source_url", ""),
                        item.get("modified_time", ""),
                        item["content_hash"],
                        item["content"],
                        item["size_bytes"],
                    ),
                )
            conn.execute(
                """
                UPDATE projects SET active_snapshot_id = ?, revision = revision + 1,
                    updated_at = ? WHERE id = ?
                """,
                (snapshot_id, now, project_id),
            )
        else:
            snapshot_id = project["active_snapshot_id"]
        conn.execute(
            """
            UPDATE projects SET source_sync_status = 'healthy', source_failure_count = 0,
                source_last_error = '', source_last_attempt_at = ?, source_last_success_at = ?,
                source_next_sync_at = ? WHERE id = ?
            """,
            (now, now, next_workday_nine(), project_id),
        )
        _audit(
            conn,
            project_id,
            "content_sync",
            "项目资料已更新" if changed else "项目资料无变化",
            "control_server",
        )
    return {"changed": changed, "snapshot_id": snapshot_id}


def record_project_sync_failure(project_id: str, error: str) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    with connection() as conn:
        row = conn.execute(
            "SELECT source_failure_count FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not row:
            raise LookupError("project_not_found")
        failures = int(row["source_failure_count"]) + 1
        sync_status = "needs_admin" if failures >= 10 else "recovering"
        next_attempt = None if failures >= 10 else (now + timedelta(seconds=60)).isoformat()
        conn.execute(
            """
            UPDATE projects SET source_sync_status = ?, source_failure_count = ?,
                source_last_error = ?, source_last_attempt_at = ?, source_next_sync_at = ?
            WHERE id = ?
            """,
            (sync_status, failures, error[:5000], now.isoformat(), next_attempt, project_id),
        )
    return get_project(project_id) or {}


def due_project_ids() -> List[str]:
    now = utc_now()
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT id FROM projects
            WHERE status = 'active' AND source_sync_status != 'needs_admin'
              AND source_next_sync_at IS NOT NULL AND source_next_sync_at <= ?
            """,
            (now,),
        ).fetchall()
    return [row["id"] for row in rows]


def retry_project_sync(project_id: str) -> Dict[str, Any]:
    with connection() as conn:
        cursor = conn.execute(
            """
            UPDATE projects SET source_sync_status = 'recovering', source_failure_count = 0,
                source_last_error = '', source_next_sync_at = ? WHERE id = ?
            """,
            (utc_now(), project_id),
        )
        if cursor.rowcount != 1:
            raise LookupError("project_not_found")
    return get_project(project_id) or {}


def pending_project_exports() -> List[Dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT e.*, p.name AS project_name, p.feishu_folder_token,
                   p.progress_folder_token, r.display_name AS runner_name
            FROM project_export_state e
            JOIN projects p ON p.id = e.project_id
            JOIN runner_nodes r ON r.id = e.runner_id
            WHERE e.dirty = 1 AND e.status != 'needs_admin'
            ORDER BY e.last_attempt_at IS NOT NULL, e.last_attempt_at
            """
        ).fetchall()
    return [dict(row) for row in rows]


def set_project_progress_folder(project_id: str, token: str, url: str = "") -> None:
    with connection() as conn:
        conn.execute(
            """
            UPDATE projects SET progress_folder_token = ?, progress_folder_url = ?,
                updated_at = ? WHERE id = ?
            """,
            (token, url, utc_now(), project_id),
        )


def project_progress_markdown(project_id: str, runner_id: str) -> str:
    with connection() as conn:
        project = conn.execute(
            "SELECT name FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        runner = conn.execute(
            "SELECT display_name FROM runner_nodes WHERE id = ?", (runner_id,)
        ).fetchone()
        if not project or not runner:
            raise LookupError("project_or_runner_not_found")
        reports = conn.execute(
            """
            SELECT * FROM project_reports
            WHERE project_id = ? AND runner_id = ? ORDER BY id
            """,
            (project_id, runner_id),
        ).fetchall()
        gates = conn.execute(
            """
            SELECT * FROM project_gate_records
            WHERE project_id = ? AND runner_id = ? ORDER BY id
            """,
            (project_id, runner_id),
        ).fetchall()
    lines = [
        f"# {project['name']}｜{runner['display_name']} 项目进展",
        "",
        f"Runner ID：`{runner_id}`",
        "",
        "> 此文件由 RDOS Control Server 根据结构化日报生成。项目正文不在此处修改。",
        "",
    ]
    if not reports:
        lines.append("暂无日报。")
    for report in reports:
        lines.extend(
            [
                f"## {report['report_date']}｜{report['created_at']}",
                "",
                f"完整性：{'完整' if report['complete'] else '不完整'}",
                "",
                report["summary"] or "（没有 Agent 进展摘要，仅记录文件技术快照。）",
                "",
            ]
        )
        nodes = _json_value(report["workflow_nodes_json"], [])
        if nodes:
            lines.extend(["### 工作流节点", ""])
            for node in nodes:
                evidence = "、".join(node.get("evidence", [])) or "未提供"
                lines.append(f"- {node['code']}：{node['status']}；证据：{evidence}")
            lines.append("")
        changes = _json_value(report["file_changes_json"], [])
        if changes:
            lines.extend(["### 文件变化", ""])
            for change in changes:
                lines.append(f"- {change['change']}：`{change['path']}`")
            lines.append("")
        issues = _json_value(report["issues_json"], [])
        if issues:
            lines.extend(["### 问题 / 风险", ""])
            lines.extend(f"- {issue}" for issue in issues)
            lines.append("")
    if gates:
        lines.extend(["## Gate 记录", ""])
        for gate in gates:
            lines.append(
                f"- {gate['gate_code']}：{gate['status']}"
                + (f"；反馈：{gate['feedback']}" if gate["feedback"] else "")
            )
    return "\n".join(lines).rstrip() + "\n"


def record_project_export_success(project_id: str, runner_id: str) -> None:
    now = utc_now()
    with connection() as conn:
        conn.execute(
            """
            UPDATE project_export_state SET status = 'healthy', dirty = 0,
                failure_count = 0, last_error = '', last_attempt_at = ?,
                last_success_at = ? WHERE project_id = ? AND runner_id = ?
            """,
            (now, now, project_id, runner_id),
        )


def record_project_export_failure(project_id: str, runner_id: str, error: str) -> None:
    now = utc_now()
    with connection() as conn:
        row = conn.execute(
            """
            SELECT failure_count FROM project_export_state
            WHERE project_id = ? AND runner_id = ?
            """,
            (project_id, runner_id),
        ).fetchone()
        if not row:
            return
        failures = int(row["failure_count"]) + 1
        export_status = "needs_admin" if failures >= 10 else "recovering"
        conn.execute(
            """
            UPDATE project_export_state SET status = ?, dirty = 1,
                failure_count = ?, last_error = ?, last_attempt_at = ?
            WHERE project_id = ? AND runner_id = ?
            """,
            (export_status, failures, error[:5000], now, project_id, runner_id),
        )


def retry_project_export(project_id: str, runner_id: Optional[str] = None) -> None:
    with connection() as conn:
        if not conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone():
            raise LookupError("project_not_found")
        if runner_id:
            cursor = conn.execute(
                """
                UPDATE project_export_state SET status = 'pending', dirty = 1,
                    failure_count = 0, last_error = ''
                WHERE project_id = ? AND runner_id = ?
                """,
                (project_id, runner_id),
            )
        else:
            cursor = conn.execute(
                """
                UPDATE project_export_state SET status = 'pending', dirty = 1,
                    failure_count = 0, last_error = '' WHERE project_id = ?
                """,
                (project_id,),
            )
        if cursor.rowcount == 0:
            raise LookupError("project_export_not_found")


def workflow_reference_state() -> Dict[str, Any]:
    with connection() as conn:
        row = conn.execute("SELECT * FROM workflow_reference_state WHERE id = 1").fetchone()
    return dict(row)


def workflow_check_due() -> bool:
    state = workflow_reference_state()
    return state["status"] != "needs_admin" and state["next_check_at"] <= utc_now()


def record_workflow_check(observed_hash: str) -> Dict[str, Any]:
    now = utc_now()
    with connection() as conn:
        state = conn.execute(
            "SELECT expected_hash FROM workflow_reference_state WHERE id = 1"
        ).fetchone()
        changed = not hmac.compare_digest(state["expected_hash"], observed_hash)
        conn.execute(
            """
            UPDATE workflow_reference_state SET observed_hash = ?, status = ?,
                failure_count = 0, last_error = ?, last_checked_at = ?, next_check_at = ?
            WHERE id = 1
            """,
            (
                observed_hash,
                "changed" if changed else "healthy",
                "飞书工作流原文已变化；当前项目仍继续使用冻结的 v0.3。" if changed else "",
                now,
                next_workday_nine(),
            ),
        )
    return workflow_reference_state()


def record_workflow_check_failure(error: str) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    with connection() as conn:
        row = conn.execute(
            "SELECT failure_count FROM workflow_reference_state WHERE id = 1"
        ).fetchone()
        failures = int(row["failure_count"]) + 1
        check_status = "needs_admin" if failures >= 10 else "recovering"
        next_check = (
            next_workday_nine()
            if failures >= 10
            else (now + timedelta(seconds=60)).isoformat()
        )
        conn.execute(
            """
            UPDATE workflow_reference_state SET status = ?, failure_count = ?,
                last_error = ?, last_checked_at = ?, next_check_at = ? WHERE id = 1
            """,
            (check_status, failures, error[:5000], now.isoformat(), next_check),
        )
    return workflow_reference_state()
