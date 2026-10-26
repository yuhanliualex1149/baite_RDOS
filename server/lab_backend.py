"""Synthetic phase-0 node transport. Never calls Feishu or a model provider."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ID = "lab_project_phase0"
DOCUMENT_ID = "lab_doc_intro"
NODES = {
    "node_lab_codex": {"member_id": "mem_lab_codex_0001", "task_id": "lab_task_codex"},
    "node_lab_workbuddy": {"member_id": "mem_lab_workbuddy_0001", "task_id": "lab_task_workbuddy"},
}
INTRO = """# RDOS 2.0 阶段 0：纯合成实验材料

本材料完全虚构，不包含百特业务资料。本环境只验证本机节点通信。
实验物品为蓝色纸卡与橙色纸卡；蓝色纸卡编号为 LAB-BLUE-07。
请读取本人测试任务，说明你实际读到的蓝色纸卡编号，写入一次反馈，再单独读回。
本轮不连接飞书、不创建定时任务、不修改原项目、不运行机构接入流程。
反馈仅证明本地请求被接收与读回；客户端种类和新会话需由用户另行核对。
"""
_LOCK = threading.RLock()


class LabError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _settings() -> tuple[Path, Path, str, str]:
    try:
        root = Path(os.environ["RDOS_LAB_ROOT"]).resolve()
        runtime = Path(os.environ["RDOS_LAB_RUNTIME"]).resolve()
        database = Path(os.environ["BAITE_DB_PATH"]).resolve()
        env_id = os.environ["RDOS_LAB_ENV_ID"].strip()
        generation = os.environ["RDOS_LAB_GENERATION"].strip()
    except KeyError as exc:
        raise LabError(503, "lab_not_configured", "缺少实验环境配置") from exc
    if not env_id or not generation or runtime == root or root not in runtime.parents or runtime not in database.parents:
        raise LabError(503, "lab_configuration_invalid", "实验运行目录或数据库范围无效")
    if not runtime.is_dir():
        raise LabError(503, "lab_not_initialized", "实验运行目录尚未建立")
    return runtime, database, env_id, generation


def environment() -> dict:
    _, _, env_id, generation = _settings()
    return {"env_id": env_id, "generation": generation, "source_environment": "local_mock",
            "client_kind": "unverified_client"}


@contextmanager
def _connection():
    _, path, _, _ = _settings()
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init() -> None:
    """Seed only lab_* tables; leave every central/Panel table untouched."""
    runtime, _, env_id, generation = _settings()
    with _LOCK, _connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS lab_meta (id INTEGER PRIMARY KEY, env_id TEXT NOT NULL, generation TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS lab_documents (id TEXT PRIMARY KEY, content TEXT NOT NULL, revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS lab_feedback (
                id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE, node_id TEXT NOT NULL,
                member_id TEXT NOT NULL, project_id TEXT NOT NULL, task_id TEXT NOT NULL,
                content TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS lab_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL);
        """)
        old = conn.execute("SELECT * FROM lab_meta WHERE id=1").fetchone()
        if old and (old["env_id"] != env_id or old["generation"] != generation):
            raise LabError(503, "lab_database_mismatch", "实验数据库不属于当前环境或代次")
        conn.execute("INSERT OR IGNORE INTO lab_meta VALUES(1,?,?)", (env_id, generation))
        conn.execute("INSERT OR IGNORE INTO lab_documents VALUES(?,?,1)", (DOCUMENT_ID, INTRO))
    # A restart exports only missing committed events, without inventing actions.
    with _LOCK:
        _flush_events(runtime)


def _flush_events(runtime: Path) -> None:
    path = runtime / "events.jsonl"
    if path.is_symlink():
        raise LabError(503, "lab_log_invalid", "实验日志不能指向其他位置")
    last = 0
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                item = json.loads(line)
                last = max(last, int(item["sequence"]))
        except (ValueError, KeyError, TypeError) as exc:
            raise LabError(503, "lab_log_invalid", "实验日志不完整，请先核对证据") from exc
    with _connection() as conn:
        rows = conn.execute("SELECT sequence,payload FROM lab_events WHERE sequence>? ORDER BY sequence", (last,)).fetchall()
    if rows:
        with path.open("a", encoding="utf-8") as handle:
            os.chmod(path, 0o600)
            for row in rows:
                handle.write(json.dumps({**json.loads(row["payload"]), "sequence": row["sequence"]}, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _event(conn, kind: str, node_id: str = "", object_id: str = "", content_hash: str = "", **extra) -> int:
    item = {**environment(), "event_type": kind, "node_id": node_id,
            "member_id": NODES.get(node_id, {}).get("member_id", ""), "object_id": object_id,
            "at": datetime.now(timezone.utc).isoformat(), **extra}
    if content_hash:
        item["content_hash"] = content_hash
    return conn.execute("INSERT INTO lab_events(payload) VALUES(?)", (json.dumps(item, ensure_ascii=False),)).lastrowid


def _record(kind: str, node_id: str = "", object_id: str = "", content_hash: str = "", **extra) -> int:
    with _LOCK:
        with _connection() as conn:
            sequence = _event(conn, kind, node_id, object_id, content_hash, **extra)
        _flush_events(_settings()[0])
        return sequence


def _credentials() -> dict:
    runtime = _settings()[0]
    credentials_path = runtime / "credentials.json"
    if credentials_path.is_symlink():
        raise LabError(503, "lab_credentials_invalid", "实验凭据文件无效")
    try:
        credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
        tokens = credentials["node_tokens"]
        if not isinstance(tokens, dict) or any(not isinstance(tokens.get(node), str) or len(tokens[node]) < 24 for node in NODES):
            raise ValueError("invalid credentials")
        if len({tokens[node] for node in NODES}) != len(NODES):
            raise ValueError("duplicate credentials")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LabError(503, "lab_credentials_invalid", "实验节点凭据尚未准备好") from exc
    return credentials


def authenticate(authorization: str, env_id: str, generation: str) -> str:
    expected = environment()
    if env_id != expected["env_id"] or str(generation) != expected["generation"]:
        _record("request_rejected", object_id="authentication", reason="environment_mismatch")
        raise LabError(409, "environment_mismatch", "客户端环境或代次不匹配，请使用当前节点配置")
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    tokens = _credentials()["node_tokens"]
    for node_id in NODES:
        if token and secrets.compare_digest(token.encode("utf-8"), tokens[node_id].encode("utf-8")):
            return node_id
    _record("request_rejected", object_id="authentication", reason="invalid_token")
    raise LabError(401, "invalid_token", "实验节点凭据无效")


def _identity(node_id: str) -> dict:
    if node_id not in NODES:
        raise LabError(403, "unknown_node", "节点不在本次实验范围")
    return {**environment(), "node_id": node_id, **NODES[node_id], "project_id": PROJECT_ID}


def context(node_id: str) -> dict:
    result = {**_identity(node_id), "document_ids": [DOCUMENT_ID], "scope": "phase0_node_communication_only",
              "instruction": "分别读取任务与实验材料，提交本人测试反馈，再用独立请求读回；不运行旧机构接入或定时任务。"}
    result["event_sequence"] = _record("context_read", node_id, PROJECT_ID)
    return result


def tasks(node_id: str, *, record: bool = True) -> dict:
    identity = _identity(node_id)
    result = {**identity, "items": [{"task_id": identity["task_id"], "project_id": PROJECT_ID,
        "member_ids": [identity["member_id"]], "version": "1", "status": "已发布", "task_kind": "work", "stage": "",
        "instructions": "读取 lab_doc_intro，记录蓝色纸卡的编号及真实读取结果；提交一次反馈并独立读回。"}]}
    if record:
        result["event_sequence"] = _record("tasks_read", node_id, identity["task_id"])
    return result


def document(node_id: str, document_id: str, *, record: bool = True) -> dict:
    identity = _identity(node_id)
    if document_id != DOCUMENT_ID:
        _record("request_rejected", node_id, "document", reason="object_not_in_scope")
        raise LabError(404, "document_not_found", "本次实验没有这份资料")
    with _connection() as conn:
        row = conn.execute("SELECT * FROM lab_documents WHERE id=?", (document_id,)).fetchone()
    if not row:
        raise LabError(503, "lab_not_initialized", "实验资料尚未初始化")
    result = {**identity, "object_id": document_id, "content": row["content"], "revision": row["revision"],
              "content_hash": _hash(row["content"]), "title": "阶段 0 纯合成实验材料", "kind": "local_mock_document"}
    if record:
        result["event_sequence"] = _record("document_read", node_id, document_id, result["content_hash"])
    return result


def submit(node_id: str, operation_id: str, content: str, **claimed) -> dict:
    identity = _identity(node_id)
    for key, value in claimed.items():
        if value is not None and value != identity.get(key):
            _record("request_rejected", node_id, "feedback", reason="ownership_mismatch")
            raise LabError(403, "ownership_mismatch", "反馈归属与当前节点绑定不一致")
    if not operation_id or len(operation_id) > 120 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:" for c in operation_id):
        raise LabError(422, "invalid_operation_id", "操作标识须为 1–120 位字母、数字、点、冒号、下划线或短横线")
    if not isinstance(content, str) or not content.strip() or len(content) > 100000:
        raise LabError(422, "invalid_content", "反馈正文须非空且不超过 100000 字符")
    credentials = _credentials()
    sensitive = [*credentials["node_tokens"].values(), credentials.get("admin_password"), credentials.get("session_secret")]
    if any(isinstance(secret, str) and secret and (secret in content or secret in operation_id) for secret in sensitive):
        _record("request_rejected", node_id, "feedback", reason="credential_in_content")
        raise LabError(422, "credential_in_content", "反馈含实验凭据，已拒绝保存；请移除后重新提交")
    digest = _hash(content)
    with _LOCK:
        with _connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT * FROM lab_feedback WHERE operation_id=?", (operation_id,)).fetchone()
            if old and (old["node_id"] != node_id or old["content"] != content):
                raise LabError(409, "operation_conflict", "此操作标识已用于另一份反馈，请先核对原结果")
            if old:
                feedback_id, created_at, status = old["id"], old["created_at"], "duplicate"
            else:
                feedback_id = "lab_feedback_" + secrets.token_hex(12)
                created_at = datetime.now(timezone.utc).isoformat()
                status = "created"
                conn.execute("INSERT INTO lab_feedback VALUES(?,?,?,?,?,?,?,?,?)", (
                    feedback_id, operation_id, node_id, identity["member_id"], PROJECT_ID,
                    identity["task_id"], content, digest, created_at))
            sequence = _event(conn, "feedback_duplicate" if old else "feedback_appended", node_id, feedback_id, digest,
                              operation_hash=_hash(operation_id))
        _flush_events(_settings()[0])
    return {**identity, "feedback_id": feedback_id, "operation_id": operation_id, "content_hash": digest,
            "status": status, "created_at": created_at, "event_sequence": sequence, "readback_verified": False,
            "next_step": "用单独的 GET /api/lab/feedback/{feedback_id} 读取并核对正文"}


def feedback(node_id: str, feedback_id: str) -> dict:
    identity = _identity(node_id)
    with _connection() as conn:
        row = conn.execute("SELECT * FROM lab_feedback WHERE id=? AND node_id=?", (feedback_id, node_id)).fetchone()
    if not row:
        _record("request_rejected", node_id, "feedback", reason="object_not_in_scope")
        raise LabError(404, "feedback_not_found", "当前节点没有这条反馈")
    sequence = _record("feedback_readback", node_id, row["id"], row["content_hash"])
    return {**identity, "feedback_id": row["id"], "operation_id": row["operation_id"], "content": row["content"],
            "content_hash": row["content_hash"], "created_at": row["created_at"], "event_sequence": sequence,
            "readback_observed": True, "client_identity_verified": False}


class LocalMockTools:
    """Small read-only synthetic adapter; intentionally not a Feishu API clone."""
    def __init__(self, node_id: str):
        _identity(node_id)
        self.node_id = node_id

    def list_tasks(self) -> dict:
        return tasks(self.node_id, record=False)

    def read_document(self, object_id: str) -> dict:
        return document(self.node_id, object_id, record=False)


def deterministic_model(content: str) -> dict:
    return {"model": "local-deterministic-mock", "source_environment": "local_mock", "is_mock": True,
            "input_hash": _hash(content), "content": "本地模拟响应；未调用模型服务，不代表真实模型判断。"}
