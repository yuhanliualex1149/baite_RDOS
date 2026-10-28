"""Confirmed local-test writes, with one cancellation-safe write owner.

Only authenticated server confirmation handlers may call ``bind_confirmation``.
Tool arguments are never confirmation records. ``run_write`` callbacks must not
take WRITE_LOCK again, and must call ``mark_dispatched`` before any side effect.
The work callback returns a verified result; uncertain attempts only reconcile.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
from collections.abc import Awaitable, Callable

from server import central_store as store, db


class OperationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


# Strong references outlive cancellation of the HTTP request or parent run.
ACTIVE_WRITES: dict[str, asyncio.Task] = {}
_CURRENT_OPERATION = contextvars.ContextVar("phase1_current_operation", default=None)
_SHUTTING_DOWN = False


def begin_shutdown() -> None:
    """Close admission before cancelling runs or waiting for existing writes."""
    global _SHUTTING_DOWN
    _SHUTTING_DOWN = True


def _accepting_writes() -> None:
    if _SHUTTING_DOWN:
        raise OperationError("server_stopping", "服务正在停止，不再接受新的写入或确认")


def _environment() -> tuple[str, str]:
    if os.environ.get('RDOS_RUNTIME_MODE') == 'production':
        from server.lab_panel import environment
        current = environment()  # requires the validated formal launcher
        return current['env_id'], current['generation']
    from lab.guard import require_active
    require_active()
    env = os.environ.get("RDOS_LAB_ENV_ID", "")
    generation = os.environ.get("RDOS_LAB_GENERATION", "")
    if not env or not generation:
        raise OperationError("authorization_failed", "缺少实验环境与代次标识")
    return env, generation


def _update(operation_id: str, change: Callable[[dict], None]) -> dict:
    """Update the existing record atomically without replacing service fields."""
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT payload FROM central_records WHERE kind='operation' AND id=?", (operation_id,)).fetchone()
        if not row:
            raise OperationError("invalid_operation", "写入操作不存在")
        item = json.loads(row[0])
        change(item)
        item["updated_at"] = db.utc_now()
        conn.execute("UPDATE central_records SET payload=?,updated_at=? WHERE kind='operation' AND id=?",
                     (json.dumps(item, ensure_ascii=False), item["updated_at"], operation_id))
    return item


def _actor(actor_id: str, project_id: str) -> dict:
    actor = store.user(actor_id)
    if not actor or not actor.get("enabled") or "admin" not in actor.get("roles", []):
        raise OperationError("authorization_failed", "确认者当前没有有效管理员权限")
    from server.central_projects import project
    try:
        project(project_id, actor)
    except (ValueError, PermissionError) as exc:
        raise OperationError("authorization_failed", "确认的项目当前不可访问") from exc
    return actor


def _audit_check(operation_id: str, allowed: bool, code: str) -> None:
    if store.get("operation", operation_id):
        _update(operation_id, lambda item: item.setdefault("authorization_checks", []).append(
            {"at": db.utc_now(), "allowed": allowed, "code": code}))


def bind_confirmation(op: dict, *, actor: dict, project_id: str, target_kind: str,
                      target_id: str, draft_hash: str, base: dict,
                      input_fields: dict | None = None, run_id: str = "") -> dict:
    return _bind_authorization(op, actor=actor, project_id=project_id, target_kind=target_kind,
        target_id=target_id, draft_hash=draft_hash, base=base, input_fields=input_fields, run_id=run_id)


def bind_user_request(op: dict, *, request_authorization, actor: dict, project_id: str,
                      target_kind: str, target_id: str, draft_hash: str, base: dict,
                      input_fields: dict | None = None, run_id: str = "") -> dict:
    from server.central_requests import authorization_details
    details = authorization_details(request_authorization, op)
    return _bind_authorization(op, actor=actor, project_id=project_id, target_kind=target_kind,
        target_id=target_id, draft_hash=draft_hash, base=base, input_fields=input_fields,
        run_id=details['source_run_id'], source='authenticated_user_request', request_binding=details)


def _bind_authorization(op: dict, *, actor: dict, project_id: str, target_kind: str,
                      target_id: str, draft_hash: str, base: dict,
                      input_fields: dict | None = None, run_id: str = "",
                      source: str = 'authenticated_confirmation', request_binding: dict | None = None) -> dict:
    """Bind an already-reviewed draft to an existing operation, never to text.

The caller must compare the submitted draft hash with the server's current
draft before this call. Existing approval scope cannot be replaced on retry.
"""
    _accepting_writes()
    env, generation = _environment()
    actor_id = str(actor.get("id") or "")
    _actor(actor_id, project_id)
    if not all(isinstance(value, str) and value for value in (project_id, target_kind, target_id, draft_hash)) or not isinstance(base, dict):
        raise OperationError("authorization_failed", "确认必须绑定项目、目标、草稿与版本基线")
    saved = store.get("operation", op.get("id", ""))
    if not saved or saved.get("payload_hash") != op.get("payload_hash") or saved.get("action") != op.get("action"):
        raise OperationError("operation_conflict", "确认与原操作不匹配")
    binding = {"env_id": env, "generation": generation, "project_id": project_id,
               "target_kind": target_kind, "target_id": target_id, "draft_hash": draft_hash,
               "base": base, "actor_id": actor_id, "actor_kind": "human", "run_id": run_id,
               "action": saved["action"], "payload_hash": saved["payload_hash"],
               "input_hash": store.digest(input_fields) if input_fields is not None else None,
               "source": source, **(request_binding or {})}
    fingerprint = store.digest(binding)

    def bind(item: dict) -> None:
        if item.get("authorization"):
            if item.get("authorization_hash") != fingerprint or store.digest(item["authorization"]) != fingerprint:
                raise OperationError("operation_conflict", "同一操作不能替换已确认的范围或内容")
            return
        if item.get("dispatched_at") or item.get("prepared_fields") or item.get("status") not in ("pending", "failed"):
            raise OperationError("authorization_failed", "历史写入不能补记为事前授权，请先核对原结果")
        item.update(authorization=binding, authorization_hash=fingerprint,
                    confirmed_at=db.utc_now(), env_id=env, generation=generation,
                    project_id=project_id, target_kind=target_kind, target_id=target_id,
                    run_id=run_id, dispatch_protocol=1)

    return _update(saved["id"], bind)


def require_confirmation(operation_id: str, *, actor_id: str, project_id: str,
                         target_kind: str, target_id: str,
                         input_fields: dict | None = None, draft_hash: str | None = None,
                         base: dict | None = None) -> dict:
    """Recheck trusted scope before ensure-fields, transport, or local mutation."""
    try:
        env, generation = _environment()
        item = store.get("operation", operation_id)
        binding = (item or {}).get("authorization")
        if not isinstance(binding, dict) or item.get("authorization_hash") != store.digest(binding):
            raise OperationError("authorization_failed", "此写入没有有效的人工确认")
        expected = {"env_id": env, "generation": generation, "actor_id": actor_id,
                    "project_id": project_id, "target_kind": target_kind, "target_id": target_id,
                    "action": item.get("action"), "payload_hash": item.get("payload_hash"),
                    "actor_kind": "human"}
        if any(binding.get(key) != value for key, value in expected.items()):
            raise OperationError("authorization_failed", "写入超出已确认的环境、目标或身份范围")
        if binding.get('source') == 'authenticated_user_request':
            from server.central_requests import recheck_binding
            try:
                recheck_binding(binding, item)
            except (ValueError, PermissionError) as exc:
                raise OperationError(getattr(exc, 'code', 'authorization_failed'), str(exc)) from exc
        elif binding.get('source') != 'authenticated_confirmation':
            raise OperationError('authorization_failed', '写入没有有效的用户授权来源')
        if input_fields is not None and binding.get("input_hash") != store.digest(input_fields):
            raise OperationError("operation_conflict", "写入内容与已确认内容不同")
        if draft_hash is not None and binding.get("draft_hash") != draft_hash:
            raise OperationError("operation_conflict", "草稿版本与已确认版本不同")
        if base is not None and binding.get("base") != base:
            raise OperationError("operation_conflict", "写入基线与已确认基线不同")
        _actor(actor_id, project_id)
    except OperationError as exc:
        _audit_check(operation_id, False, exc.code)
        raise
    _audit_check(operation_id, True, "authorized")
    return store.get("operation", operation_id)


def _check_saved_confirmation(operation_id: str) -> dict:
    item = store.get("operation", operation_id) or {}
    binding = item.get("authorization") or {}
    return require_confirmation(operation_id, actor_id=binding.get("actor_id", ""),
                                project_id=binding.get("project_id", ""),
                                target_kind=binding.get("target_kind", ""),
                                target_id=binding.get("target_id", ""))


def mark_dispatched(operation_id: str) -> None:
    """Persist the send boundary before entering a possibly blocking adapter."""
    owner = _CURRENT_OPERATION.get()
    if not owner or owner["id"] != operation_id:
        raise OperationError("invalid_operation", "写入必须由当前受保护的操作执行")
    _check_saved_confirmation(operation_id)
    dispatched_at = db.utc_now()

    def mark(item: dict) -> None:
        item.setdefault("dispatched_at", dispatched_at)
        if item.get("attempts"):
            item["attempts"][-1]["dispatched_at"] = dispatched_at

    _update(operation_id, mark)
    owner["dispatched_at"] = dispatched_at


def _end_attempt(operation_id: str, status: str, code: str = "") -> None:
    def end(item: dict) -> None:
        if item.get("attempts"):
            item["attempts"][-1].update(status=status, completed_at=db.utc_now(), error_code=code)
        item.pop("active_attempt", None)
    _update(operation_id, end)


def _uncertain(operation_id: str, code: str, dispatched_at: str | None = None) -> None:
    def mark(item: dict) -> None:
        if item.get("status") != "succeeded":
            item.update(status="uncertain", error_code=code, needs_reconciliation=True)
            if dispatched_at:
                item.setdefault("dispatched_at", dispatched_at)
    _update(operation_id, mark)


async def run_write(operation_id: str, write_lock: asyncio.Lock,
                    work: Callable[[], Awaitable[dict]], *,
                    reconcile: Callable[[], Awaitable[dict]] | None = None) -> dict:
    """Own the lock beyond caller cancellation; never queue or resend uncertainty.

``reconcile`` returns {status: verified, result: {...}}, {status: conflict}, or
{status: unknown}. Only verified completes the operation. A successful ``work``
must return a result whose readback was already verified by the service.
"""
    _accepting_writes()
    item = _check_saved_confirmation(operation_id)
    if operation_id in ACTIVE_WRITES and not ACTIVE_WRITES[operation_id].done():
        raise OperationError("write_in_progress", "原写入仍在执行，请查看原操作结果")
    if item.get("status") == "succeeded":
        return item.get("result", {})
    if item.get("status") == "conflict":
        raise OperationError("operation_conflict", "原操作与当前目标冲突，请人工核对")
    if write_lock.locked() or any(not task.done() for task in ACTIVE_WRITES.values()):
        raise OperationError("write_busy", "另一项写入正在完成或核对，请稍后继续")

    async def owned() -> dict:
        # An unrelated short confirmation section may have taken the lock since
        # task creation. Reject instead of adding an unbounded lock waiter.
        if write_lock.locked():
            raise OperationError("write_busy", "写入通道当前忙，请稍后继续")
        async with write_lock:
            owner = {"id": operation_id, "dispatched_at": None}
            initial = _check_saved_confirmation(operation_id)
            needs_reconcile = bool(initial.get("dispatched_at") or initial.get("status") == "uncertain")

            def begin(current: dict) -> None:
                # Another process may retire or start the operation after the
                # first read. Begin and retirement must serialize in SQLite.
                if current.get("status") in ("conflict", "rejected", "succeeded"):
                    raise OperationError("operation_conflict", "原操作已完成或退休，不能再次派发")
                if current.get("active_attempt") or current.get("status") in ("running", "in_progress"):
                    raise OperationError("write_busy", "原操作正在其他进程执行或核对")
                if (current.get("authorization_hash") != initial.get("authorization_hash")
                    or current.get("dispatched_at") != initial.get("dispatched_at")):
                    raise OperationError("operation_conflict", "原操作授权或派发边界已变化，请重新核对")
                number = len(current.setdefault("attempts", [])) + 1
                current["attempts"].append({"number": number, "mode": "reconcile" if needs_reconcile else "write",
                                            "status": "running", "started_at": db.utc_now()})
                current.update(active_attempt=number, status="uncertain" if needs_reconcile else "running")

            _update(operation_id, begin)
            token = _CURRENT_OPERATION.set(owner)
            try:
                if needs_reconcile:
                    receipt = await reconcile() if reconcile else {"status": "unknown"}
                    if receipt.get("status") == "verified" and isinstance(receipt.get("result"), dict):
                        result = receipt["result"]
                    elif receipt.get("status") == "conflict":
                        _update(operation_id, lambda current: current.update(status="conflict", error_code="operation_conflict", needs_reconciliation=True))
                        raise OperationError("operation_conflict", "原写入与当前内容冲突，未再次发送")
                    else:
                        raise OperationError("write_uncertain", "原写入结果尚不能确认，未再次发送")
                else:
                    result = await work()
                    if not isinstance(result, dict):
                        raise OperationError("write_unverified", "写入没有返回可核对的结果")
                latest = store.get("operation", operation_id)
                if latest.get("status") != "succeeded":
                    store.finish_operation(latest, result)
                def verified(current: dict) -> None:
                    current.update(needs_reconciliation=False, verified_at=db.utc_now(), error_code="")
                    if owner["dispatched_at"]:
                        current.setdefault("dispatched_at", owner["dispatched_at"])
                _update(operation_id, verified)
                _end_attempt(operation_id, "succeeded")
                return store.get("operation", operation_id).get("result", result)
            except BaseException as exc:
                latest = store.get("operation", operation_id)
                code = getattr(exc, "code", "interrupted" if isinstance(exc, asyncio.CancelledError) else "write_failed")
                if latest.get("status") != "succeeded":
                    if latest.get("status") == "conflict":
                        pass
                    elif owner["dispatched_at"] or latest.get("dispatched_at") or needs_reconcile:
                        _uncertain(operation_id, code, owner["dispatched_at"])
                    else:
                        _update(operation_id, lambda current: current.update(status="failed", error_code=code))
                _end_attempt(operation_id, store.get("operation", operation_id)["status"], code)
                raise
            finally:
                _CURRENT_OPERATION.reset(token)

    task = asyncio.create_task(owned(), name="phase1-write-" + operation_id)
    ACTIVE_WRITES[operation_id] = task

    def finished(completed: asyncio.Task) -> None:
        if ACTIVE_WRITES.get(operation_id) is completed:
            ACTIVE_WRITES.pop(operation_id, None)
        # The parent may already have timed out; retrieving the exception does
        # not change what an awaiting caller receives, and avoids lost warnings.
        if not completed.cancelled():
            completed.exception()

    task.add_done_callback(finished)
    return await asyncio.shield(task)


async def drain_writes(timeout: float = 2.0) -> list[str]:
    """Bounded shutdown wait. Never cancel threads or their owning write tasks."""
    pending = {operation_id: task for operation_id, task in ACTIVE_WRITES.items() if not task.done()}
    if pending:
        await asyncio.wait(list(pending.values()), timeout=max(0.0, timeout))
    remaining = [operation_id for operation_id, task in pending.items() if not task.done()]
    for operation_id in remaining:
        _uncertain(operation_id, "shutdown_pending_write")
    return remaining


def recover_interrupted() -> dict:
    """Startup-only classification; no adapter calls, automatic retries or replay."""
    global _SHUTTING_DOWN
    _environment()
    if any(not task.done() for task in ACTIVE_WRITES.values()):
        raise OperationError("write_in_progress", "存在本进程写入，不能执行启动恢复")
    result = {"interrupted_runs": 0, "uncertain_operations": 0, "interrupted_operations": 0}
    for run in store.items("run"):
        if run.get("status") in ("queued", "running", "cancelling"):
            run.update(status="interrupted", completed_at=db.utc_now(), error_code="interrupted",
                       error="上次进程未完成本次运行；已保留记录，未自动重放。")
            store.put("run", run["id"], run)
            result["interrupted_runs"] += 1
    for op in store.items("operation"):
        if op.get("status") in ("succeeded", "conflict", "rejected"):
            continue
        unsafe = bool(op.get("dispatched_at") or op.get("prepared_fields") or op.get("status") == "uncertain"
                      or (not op.get("dispatch_protocol") and op.get("status") in ("pending", "running", "in_progress")))
        if unsafe:
            if op.get("status") != "uncertain" or op.get("active_attempt"):
                _uncertain(op["id"], "interrupted_write")
                _end_attempt(op["id"], "interrupted", "interrupted_write")
            result["uncertain_operations"] += 1
        elif op.get("status") in ("running", "in_progress"):
            _update(op["id"], lambda current: current.update(status="failed", error_code="interrupted_before_write"))
            _end_attempt(op["id"], "interrupted", "interrupted_before_write")
            result["interrupted_operations"] += 1
    _SHUTTING_DOWN = False
    return result
