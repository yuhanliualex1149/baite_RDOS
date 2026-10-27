"""Real-guard subprocess tests of confirmation and cancellation-safe writes."""
from __future__ import annotations

import unittest

from tests.phase1_support import run_guarded


SETUP = '''
from server import phase1_operations as ops

fields = {"task_id": "lab_op_task", "instructions": "合成任务", "project_id": project_id}
base = {"action": "create", "record_id": "", "version": "", "body_hash": ""}

def approved(opid="phase1-write-1", **changes):
    op = store.operation(opid, "task", {"user_id": admin["id"], "fields": fields})
    values = dict(actor=admin, project_id=project_id, target_kind="task", target_id=fields["task_id"],
                  draft_hash=store.digest(fields), base=base, input_fields=fields, run_id="run-1")
    values.update(changes)
    return ops.bind_confirmation(op, **values)

def allowed(opid="phase1-write-1", **changes):
    values = dict(actor_id=admin["id"], project_id=project_id, target_kind="task",
                  target_id=fields["task_id"], input_fields=fields, draft_hash=store.digest(fields), base=base)
    values.update(changes)
    return ops.require_confirmation(opid, **values)

def expect_error(code, fn):
    try:
        fn()
    except ops.OperationError as exc:
        assert exc.code == code, (exc.code, code)
    else:
        raise AssertionError("missing " + code)
'''


class Phase1OperationTest(unittest.TestCase):
    def check(self, body: str):
        result = run_guarded(SETUP + "\n" + body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_confirmation_scope_is_immutable_and_checked_live(self):
        self.check('''
approved()
assert allowed()["authorization"]["actor_kind"] == "human"
expect_error("authorization_failed", lambda: allowed(target_id="different-task"))
expect_error("operation_conflict", lambda: allowed(input_fields={**fields, "instructions": "改稿"}))
expect_error("operation_conflict", lambda: allowed(base={**base, "version": "2"}))
expect_error("operation_conflict", lambda: approved(draft_hash="changed"))
import os
old = os.environ["RDOS_LAB_GENERATION"]
os.environ["RDOS_LAB_GENERATION"] = "different-generation"
expect_error("authorization_failed", allowed)
os.environ["RDOS_LAB_GENERATION"] = old
with db.connection() as conn:
    conn.execute("UPDATE central_users SET enabled=0 WHERE id=?", (admin["id"],))
expect_error("authorization_failed", allowed)
assert any(not item["allowed"] for item in store.get("operation", "phase1-write-1")["authorization_checks"])
''')

    def test_unconfirmed_operation_never_enters_work(self):
        self.check('''
store.operation("unapproved", "task", {"fields": fields})
calls = []
async def work():
    calls.append("side-effect")
    return {}
async def scenario():
    try:
        await ops.run_write("unapproved", asyncio.Lock(), work)
    except ops.OperationError as exc:
        assert exc.code == "authorization_failed"
    else:
        raise AssertionError("unapproved write accepted")
asyncio.run(scenario())
assert not calls
''')

    def test_verified_write_and_duplicate_do_not_repeat(self):
        self.check('''
approved()
calls = []
async def scenario():
    lock = asyncio.Lock()
    async def work():
        ops.mark_dispatched("phase1-write-1")
        calls.append("write")
        return {"task_id": fields["task_id"], "readback_verified": True}
    first = await ops.run_write("phase1-write-1", lock, work)
    again = await ops.run_write("phase1-write-1", lock, work)
    assert first == again
    assert not lock.locked()
asyncio.run(scenario())
op = store.get("operation", "phase1-write-1")
assert calls == ["write"]
assert op["status"] == "succeeded" and op["dispatched_at"] and op["verified_at"]
assert len(op["attempts"]) == 1 and op["attempts"][0]["status"] == "succeeded"
assert not ops.ACTIVE_WRITES
''')

    def test_cancelled_caller_keeps_delayed_thread_write_locked(self):
        self.check('''
import threading
approved()
approved("other-operation")
entered = threading.Event()
release = threading.Event()
calls = []
def delayed():
    entered.set()
    assert release.wait(3), "test worker was not released"
    calls.append("write")
    return {"readback_verified": True}
async def scenario():
    lock = asyncio.Lock()
    async def work():
        ops.mark_dispatched("phase1-write-1")
        return await asyncio.to_thread(delayed)
    caller = asyncio.create_task(ops.run_write("phase1-write-1", lock, work))
    for _ in range(100):
        if entered.is_set():
            break
        await asyncio.sleep(0.005)
    assert entered.is_set()
    caller.cancel()
    try:
        await caller
    except asyncio.CancelledError:
        pass
    assert lock.locked() and "phase1-write-1" in ops.ACTIVE_WRITES
    try:
        for opid, code in (("phase1-write-1", "write_in_progress"), ("other-operation", "write_busy")):
            try:
                await ops.run_write(opid, lock, work)
            except ops.OperationError as exc:
                assert exc.code == code
            else:
                raise AssertionError("overlapping write accepted")
        assert await ops.drain_writes(0.001) == ["phase1-write-1"]
    finally:
        release.set()
    assert await ops.drain_writes(2) == []
    assert not lock.locked()
asyncio.run(scenario())
assert calls == ["write"]
assert store.get("operation", "phase1-write-1")["status"] == "succeeded"
''')

    def test_lost_response_only_reconciles_and_never_resends(self):
        self.check('''
approved()
calls = []
remote = {}
async def scenario():
    lock = asyncio.Lock()
    async def work():
        ops.mark_dispatched("phase1-write-1")
        calls.append("write")
        remote.update(task_id=fields["task_id"], readback_verified=True)
        raise TimeoutError("synthetic lost response")
    try:
        await ops.run_write("phase1-write-1", lock, work)
    except TimeoutError:
        pass
    assert store.get("operation", "phase1-write-1")["status"] == "uncertain"
    async def readback():
        calls.append("readback")
        return {"status": "verified", "result": remote}
    assert await ops.run_write("phase1-write-1", lock, work, reconcile=readback) == remote
asyncio.run(scenario())
assert calls == ["write", "readback"]
assert [a["mode"] for a in store.get("operation", "phase1-write-1")["attempts"]] == ["write", "reconcile"]
''')

    def test_uncertain_unknown_and_conflict_never_call_work(self):
        self.check('''
op = approved()
op.update(dispatched_at=db.utc_now(), status="uncertain")
store.put("operation", op["id"], op)
calls = []
async def scenario():
    lock = asyncio.Lock()
    async def work():
        calls.append("write")
        return {}
    async def unknown():
        return {"status": "unknown"}
    for _ in range(2):
        try:
            await ops.run_write(op["id"], lock, work, reconcile=unknown)
        except ops.OperationError as exc:
            assert exc.code == "write_uncertain"
        assert store.get("operation", op["id"])["status"] == "uncertain"
    async def conflict():
        return {"status": "conflict"}
    try:
        await ops.run_write(op["id"], lock, work, reconcile=conflict)
    except ops.OperationError as exc:
        assert exc.code == "operation_conflict"
    assert store.get("operation", op["id"])["status"] == "conflict"
asyncio.run(scenario())
assert not calls
''')

    def test_restart_classifies_lost_finish_and_preserves_health(self):
        self.check('''
op = approved()
op.update(status="running", dispatched_at=db.utc_now(), prepared_fields=fields,
          attempts=[{"number": 1, "status": "running"}], active_attempt=1)
store.put("operation", op["id"], op)
store.put("run", "run-1", {"id": "run-1", "status": "running", "steps": [{"tool": "read"}]})
health = {"project": project_id, "stage": "write", "consecutive_failures": 3, "next_due": "retained"}
store.put("project_health", "health-1", health)
result = ops.recover_interrupted()
assert result["interrupted_runs"] == 1 and result["uncertain_operations"] == 1
assert store.get("run", "run-1")["steps"] == [{"tool": "read"}]
assert store.get("operation", op["id"])["status"] == "uncertain"
assert store.get("project_health", "health-1") == health
async def scenario():
    async def forbidden():
        raise AssertionError("restart replayed side effect")
    async def readback():
        return {"status": "verified", "result": {"task_id": fields["task_id"], "readback_verified": True}}
    await ops.run_write(op["id"], asyncio.Lock(), forbidden, reconcile=readback)
asyncio.run(scenario())
assert store.get("operation", op["id"])["status"] == "succeeded"
assert ops.recover_interrupted()["uncertain_operations"] == 0
''')

    def test_pre_dispatch_failure_is_retryable_but_stale_save_cannot_erase_send(self):
        self.check('''
approved()
async def scenario():
    lock = asyncio.Lock()
    async def fail_before_send():
        raise ValueError("synthetic validation failure")
    try:
        await ops.run_write("phase1-write-1", lock, fail_before_send)
    except ValueError:
        pass
    assert store.get("operation", "phase1-write-1")["status"] == "failed"
    async def stale_legacy_save():
        stale = store.get("operation", "phase1-write-1")
        ops.mark_dispatched("phase1-write-1")
        store.finish_operation(stale, {"error": "synthetic"}, "failed")
        raise TimeoutError()
    try:
        await ops.run_write("phase1-write-1", lock, stale_legacy_save)
    except TimeoutError:
        pass
    op = store.get("operation", "phase1-write-1")
    assert op["status"] == "uncertain" and op["dispatched_at"]
asyncio.run(scenario())
''')

    def test_success_record_survives_later_callback_error(self):
        self.check('''
approved()
async def scenario():
    async def work():
        ops.mark_dispatched("phase1-write-1")
        store.finish_operation(store.get("operation", "phase1-write-1"), {"readback_verified": True})
        raise ValueError("synthetic post-finish error")
    try:
        await ops.run_write("phase1-write-1", asyncio.Lock(), work)
    except ValueError:
        pass
asyncio.run(scenario())
assert store.get("operation", "phase1-write-1")["status"] == "succeeded"
''')

    def test_service_confirmation_pipeline_recovers_commit_without_response(self):
        self.check('''
from server import central_service as service, phase1_runtime as runtime
runtime.initialize()
draft = service.prepare_task({"task_id": "LAB-CONFIRMED-TEST", "instructions": "合成已确认任务",
    "member_ids": ["mem_lab_codex_0001"], "task_kind": "work", "nature": "一次性"},
    {"id": "draft-run", "operation_id": "draft-run", "project_id": project_id}, admin)
backend.set_fault(project_id, "write_task_after_commit", code="timeout")
backend.set_fault(project_id, "read_tasks_after_write", code="timeout")
async def scenario():
    try:
        await service.publish_task_draft(draft["id"], draft["draft_hash"], "approved-service-write", admin)
    except Exception:
        assert store.get("operation", "approved-service-write")["status"] == "uncertain"
    else:
        raise AssertionError("fault did not interrupt response")
    assert backend.stats(project_id).get("task_writes", 0) == 1
    result = await service.publish_task_draft(draft["id"], draft["draft_hash"], "different-browser-attempt", admin)
    assert result["status"] == "published"
    assert result["publication_operation_id"] == "approved-service-write"
asyncio.run(scenario())
op = store.get("operation", "approved-service-write")
assert op["status"] == "succeeded" and op["authorization"]["draft_hash"] == draft["draft_hash"]
assert backend.stats(project_id).get("task_writes", 0) == 1
assert [a["mode"] for a in op["attempts"]] == ["write", "reconcile"]
''')

    def test_service_rejects_unapproved_before_ensure_fields(self):
        self.check('''
from server import central_service as service, phase1_runtime as runtime
runtime.initialize()
before = backend.stats(project_id)
async def scenario():
    try:
        await service.save_task(fields, "no-human-approval", admin, expected_base=base)
    except ops.OperationError as exc:
        assert exc.code == "authorization_failed"
    else:
        raise AssertionError("service accepted unapproved write")
asyncio.run(scenario())
assert backend.stats(project_id) == before
''')

    def test_shutdown_rejects_admission_but_drains_accepted_write(self):
        self.check('''
approved()
approved("next-operation")
async def scenario():
    entered, release = asyncio.Event(), asyncio.Event()
    lock = asyncio.Lock()
    calls = []
    async def work():
        ops.mark_dispatched("phase1-write-1")
        entered.set()
        await release.wait()
        calls.append("write")
        return {"readback_verified": True}
    accepted = asyncio.create_task(ops.run_write("phase1-write-1", lock, work))
    await entered.wait()
    ops.begin_shutdown()
    expect_error("server_stopping", lambda: approved("new-confirmation"))
    try:
        await ops.run_write("next-operation", lock, work)
    except ops.OperationError as exc:
        assert exc.code == "server_stopping"
    else:
        raise AssertionError("shutdown accepted another write")
    release.set()
    assert await ops.drain_writes(1) == []
    await accepted
    assert calls == ["write"]
    ops.recover_interrupted()
    async def next_work():
        ops.mark_dispatched("next-operation")
        return {"readback_verified": True}
    await ops.run_write("next-operation", lock, next_work)
asyncio.run(scenario())
assert store.get("operation", "next-operation")["status"] == "succeeded"
''')


if __name__ == "__main__":
    unittest.main()
