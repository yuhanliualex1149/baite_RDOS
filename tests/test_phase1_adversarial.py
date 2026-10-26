"""Adversarial checks through the real run/model/tool loop and local transport."""
from __future__ import annotations

import unittest

from tests.phase1_support import run_guarded


SETUP = '''
import copy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from server import central_service as service, phase1_runtime as runtime, central_templates
from server.feishu_tools import FeishuError
runtime.initialize()
other_project = next(pid for pid in backend.PROJECT_IDS if pid != project_id)
captured = []
MODEL_SCRIPT = ["local_mock：本轮分析完成。"]
original_model_request = backend._LocalModelTransport.handle_async_request
async def model_transport(self, request):
    payload = json.loads(request.content)
    captured.append(payload)
    if self.index == 0:
        self.script = copy.deepcopy(MODEL_SCRIPT)
    return await original_model_request(self, request)
# Only the fake transport is controlled: real CentralAgent.run/_request,
# execute_tool, run_agent, collect_materials, policy, locks and storage run.
patcher = patch.object(backend._LocalModelTransport, "handle_async_request", model_transport)
patcher.start()

async def sync(pid, opid):
    run = service.new_run(admin, "读取合成材料并分析", opid, "sync", "manual", pid)
    await service.run_agent(run["id"])
    return store.get("run", run["id"])

async def chat(pid, thread, opid, message):
    run = service.new_run(admin, message, opid, "chat", "manual", pid, thread["id"])
    await service.run_agent(run["id"])
    return store.get("run", run["id"])

def material_context(payload):
    value = next(m["content"] for m in payload["messages"] if m["role"] == "user" and "<materials>" in m.get("content", ""))
    return json.loads(value.split("<materials>\\n", 1)[1].split("\\n</materials>", 1)[0])

def due(pid, second):
    value = runtime.health(pid)
    value["next_due"] = "2000-01-01T00:00:%02d+00:00" % second
    runtime.save_health(value)

def remote_feedback(pid):
    return store.get("lab_remote_document", store.get("project", pid)["feedback_id"])

def update_remote_document(item, content):
    item.update(content=content, revision=item["revision"] + 1)
    store.put("lab_remote_document", item["id"], item)
'''


class Phase1AdversarialTest(unittest.TestCase):
    def check(self, body: str):
        result = run_guarded(SETUP + "\n" + body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_retry_receives_own_failed_attempt_only(self):
        self.check('''
async def scenario():
    global MODEL_SCRIPT
    own = service.create_thread(admin, project_id, "本轮")
    unrelated = service.create_thread(admin, project_id, "另一个线程")
    cross = service.create_thread(admin, other_project, "另一项目")
    MODEL_SCRIPT = ["FOREIGN_THREAD_RESPONSE"]
    assert (await chat(project_id, unrelated, "foreign-thread", "FOREIGN_THREAD_REQUEST"))["status"] == "succeeded"
    MODEL_SCRIPT = ["FOREIGN_PROJECT_RESPONSE"]
    assert (await chat(other_project, cross, "foreign-project", "FOREIGN_PROJECT_REQUEST"))["status"] == "succeeded"
    MODEL_SCRIPT = [backend.tool_message("read_feedback", {"project_id": project_id}), {"http_status": 401}]
    failed = await chat(project_id, own, "own-failed", "OWN_FAILED_REQUEST")
    assert failed["status"] in ("failed", "needs_attention") and failed["error"]
    assert any(s.get("type") == "tool_result" for s in failed["steps"])
    pending = store.get("input_state", project_id)
    assert pending["observed_fingerprint"] != pending.get("processed_fingerprint")
    failed["previous_attempts"] = [{"run_id": "other-run", "project_id": other_project,
        "thread_id": cross["id"], "status": "failed", "error": "FOREIGN_ATTEMPT_SENTINEL", "steps": []}]
    store.put("run", failed["id"], failed)
    MODEL_SCRIPT = ["OWN_RETRY_RESULT"]
    before = len(captured)
    retry = await service.retry_run(failed["id"], admin)
    await service.ACTIVE_RUN_TASKS[retry["id"]]
    final = store.get("run", retry["id"])
    assert final["status"] == "succeeded" and final["id"] == failed["id"]
    context = material_context(captured[before])
    attempts = context["previous_attempts"]
    assert len(attempts) == 1 and attempts[0]["error"]
    assert any(s.get("type") == "tool_result" for s in attempts[0]["steps"])
    text = json.dumps(captured[before], ensure_ascii=False)
    assert "OWN_FAILED_REQUEST" in text
    assert all(s not in text for s in ("FOREIGN_ATTEMPT_SENTINEL", "FOREIGN_THREAD_REQUEST", "FOREIGN_THREAD_RESPONSE",
                                      "FOREIGN_PROJECT_REQUEST", "FOREIGN_PROJECT_RESPONSE"))
asyncio.run(scenario())
''')

    def test_complete_read_quarantines_bad_record_and_processes_valid_one(self):
        self.check('''
doc = remote_feedback(project_id)
bad = "\\n\\n## 项目反馈：坏归属\\n记录类型：project_update\\n成员ID：mem_missing_member\\n反馈ID：bad_record\\n实际工作：无权归属的合成内容。"
valid = "\\n\\n## 项目反馈：有效新增\\n记录类型：project_update\\n项目ID：" + project_id + "\\n成员ID：mem_lab_workbuddy_0001\\n反馈ID：valid_record\\n本次运行标识：valid_record_run\\n实际工作：完成第二份合成材料的校对。\\n本期成果：合成校对记录，尚未由真实客户端执行。"
update_remote_document(doc, doc["content"] + bad + valid)
async def scenario():
    result = await sync(project_id, "quarantine-run")
    assert result["status"] == "succeeded", result
    state = store.get("input_state", project_id)
    assert state["observed_fingerprint"] == state["processed_fingerprint"]
    receipt = store.get("read_receipt", state["last_receipt"])
    assert receipt["complete"] and receipt["quarantine_count"] == 1
    assert set(receipt["expected_sources"]) == {c["source"] for c in receipt["coverage"]}
    assert len([q for q in store.items("feedback_quarantine") if q["project_id"] == project_id]) == 1
    feedback = [f for f in store.items("feedback") if f["project_id"] == project_id and f.get("active")]
    assert len(feedback) == 2 and all(f["processed_hash"] == f["content_hash"] for f in feedback)
    assert any("valid_record" in f["content"] for f in feedback)
    assert all("bad_record" not in f["content"] for f in feedback)
asyncio.run(scenario())
''')

    def test_failed_model_attempt_usage_is_retained_for_retry(self):
        self.check('''
async def scenario():
    global MODEL_SCRIPT
    thread = service.create_thread(admin, project_id, "失败预算")
    MODEL_SCRIPT = [backend.tool_message("read_feedback", {"project_id": project_id}), {"http_status": 401}]
    failed = await chat(project_id, thread, "failed-budget", "检查合成反馈")
    assert failed["status"] != "succeeded"
    MODEL_SCRIPT = ["续试已完成。"]
    retry = await service.retry_run(failed["id"], admin)
    await service.ACTIVE_RUN_TASKS[retry["id"]]
    final = store.get("run", retry["id"])
    assert final["status"] == "succeeded"
    previous = final["previous_attempts"][-1].get("usage") or {}
    assert previous.get("model_calls") == 2 and previous.get("tool_calls") == 1, previous
    assert final["total_usage"]["model_calls"] == 3 and final["total_usage"]["tool_calls"] == 1, final["total_usage"]
    assert len(captured) == 3
asyncio.run(scenario())
''')

    def test_partial_read_preserves_input_and_other_project_health(self):
        self.check('''
async def scenario():
    assert (await sync(project_id, "baseline-a"))["status"] == "succeeded"
    old_state = copy.deepcopy(store.get("input_state", project_id))
    old_task = copy.deepcopy(store.get("task", "lab_task_codex"))
    old_full = runtime.health(project_id)["last_full_read_at"]
    remote = store.get("lab_remote_task", "lab_task_codex")
    remote["fields"]["任务说明"] += " 新的合成要求。"
    # This is a valid new requirement, unlike the separate same-version
    # tampering regression: recovery must retain it until the full read succeeds.
    remote["fields"]["版本"] = "2"
    store.put("lab_remote_task", remote["id"], remote)
    watched = next(s["id"] for s in store.items("source") if s.get("project_id") == project_id and s.get("watch"))
    original = backend._LocalToolsTransport.run
    def interrupted_read(self, args, object_id=""):
        if self.project_id == project_id and tuple(args[:2]) == ("docs", "+fetch") and object_id == watched:
            raise FeishuError("timeout", "合成资料读取超时", watched)
        return original(self, args, object_id)
    with patch.object(backend._LocalToolsTransport, "run", interrupted_read):
        failed = await sync(project_id, "partial-a")
    assert failed["status"] == "failed"
    assert store.get("input_state", project_id) == old_state
    assert store.get("task", "lab_task_codex") == old_task
    assert runtime.health(project_id)["last_full_read_at"] == old_full
    receipt = next(r for r in store.items("read_receipt") if r["run_id"] == failed["id"])
    assert not receipt["complete"] and receipt["missing_sources"] == [watched]
    assert len(receipt["coverage"]) == 2
    a_issue = copy.deepcopy(runtime.health(project_id)["issues"]["read:" + watched])
    assert (await sync(other_project, "healthy-b"))["status"] == "succeeded"
    assert runtime.health(project_id)["issues"]["read:" + watched] == a_issue
    assert runtime.health(other_project)["consecutive_failures"] == 0
    assert (await sync(project_id, "recovery-a"))["status"] == "succeeded"
    new_state = store.get("input_state", project_id)
    assert new_state["observed_fingerprint"] != old_state["observed_fingerprint"]
    assert new_state["observed_fingerprint"] == new_state["processed_fingerprint"]
    assert not runtime.health(project_id)["issues"]["read:" + watched]["active"]
asyncio.run(scenario())
''')

    def test_partial_task_page_cannot_deactivate_absent_rows(self):
        self.check('''
async def scenario():
    assert (await sync(project_id, "page-baseline"))["status"] == "succeeded"
    before = copy.deepcopy(store.get("input_state", project_id))
    before_tasks = {t["task_id"]: t for t in store.items("task") if t["project_id"] == project_id}
    original = backend._LocalToolsTransport.run
    def partial_page(self, args, object_id=""):
        result = original(self, args, object_id)
        if self.project_id == project_id and tuple(args[:2]) == ("base", "+record-list"):
            result["data"]["items"] = result["data"]["items"][:1]
            result["data"]["has_more"] = True
        return result
    with patch.object(backend._LocalToolsTransport, "run", partial_page):
        result = await sync(project_id, "partial-task-page")
    assert result["status"] == "failed" and result["error_code"] == "read_limit"
    assert store.get("input_state", project_id) == before
    assert {t["task_id"]: t for t in store.items("task") if t["project_id"] == project_id} == before_tasks
    assert runtime.health(project_id)["issues"]["read:tasks"]["active"]
asyncio.run(scenario())
''')

    def test_no_change_tick_updates_due_and_template_version_triggers_analysis(self):
        self.check('''
async def scenario():
    due(project_id, 1)
    first = await service.tick(project_id=project_id)
    assert first["executed"] and first["status"] == "succeeded"
    before = len(captured)
    previous = store.get("input_state", project_id)["processed_fingerprint"]
    due(project_id, 2)
    no_change = await service.tick(project_id=project_id)
    run = store.get("run", no_change["run_id"])
    assert run["no_analysis"] and run["usage"]["model_calls"] == 0
    assert len(captured) == before
    assert runtime.health(project_id)["next_due"] > runtime.now()
    assert (await service.tick(project_id=project_id))["status"] == "not_due"
    template = central_templates.snapshot("project.instructions")
    # Fixture mutation models an already-approved new template version.
    store.put("instruction_template", "project.instructions", {"id": "project.instructions",
        "content": template["content"], "version": template["version"] + 1})
    due(project_id, 3)
    changed = await service.tick(project_id=project_id)
    result = store.get("run", changed["run_id"])
    assert result["status"] == "succeeded" and result["usage"]["model_calls"] > 0
    assert len(captured) > before
    state = store.get("input_state", project_id)
    assert state["processed_fingerprint"] != previous
    assert state["processed_fingerprint"] == state["observed_fingerprint"]
asyncio.run(scenario())
''')

    def test_failing_project_does_not_starve_second_project(self):
        self.check('''
async def scenario():
    due(project_id, 1)
    due(other_project, 2)
    backend.set_fault(project_id, "read_tasks", code="timeout")
    failure = await service.tick()
    assert failure["status"] == "failed"
    assert store.get("run", failure["run_id"])["project_id"] == project_id
    first_health = copy.deepcopy(runtime.health(project_id))
    success = await service.tick()
    assert success["status"] == "succeeded"
    assert store.get("run", success["run_id"])["project_id"] == other_project
    assert runtime.health(project_id)["issues"] == first_health["issues"]
    assert runtime.health(project_id)["consecutive_failures"] == 1
    assert runtime.health(other_project)["consecutive_failures"] == 0
    assert (await service.tick())["status"] == "not_due"
asyncio.run(scenario())
''')

    def test_denied_tool_cannot_be_hidden_by_success_for_another_target(self):
        self.check('''
async def scenario():
    global MODEL_SCRIPT
    good = next(s["id"] for s in store.items("source") if s.get("project_id") == project_id and s.get("watch"))
    MODEL_SCRIPT = [backend.tool_message("read_material", {"project_id": project_id, "object_id": "lab_outside_scope", "kind": "docx"}, "denied"),
                    backend.tool_message("read_material", {"project_id": project_id, "object_id": good, "kind": "docx"}, "allowed"),
                    "模型声称已经成功，服务器仍须核对工具结果。"]
    thread = service.create_thread(admin, project_id, "拒绝与成功")
    result = await chat(project_id, thread, "target-isolation", "读取两个合成目标")
    assert result["status"] != "succeeded", result
    assert not result.get("analysis_complete")
    assert result["tool_failures"]
    assert any(s.get("type") == "tool_result" and not s["result"].get("ok") for s in result["steps"])
    assert any(s.get("type") == "tool_result" and s["result"].get("ok") for s in result["steps"])
asyncio.run(scenario())
''')

    def test_unadvertised_write_call_is_denied_by_actual_model_loop(self):
        self.check('''
async def scenario():
    global MODEL_SCRIPT
    MODEL_SCRIPT = [backend.tool_message("save_task", {"project_id": project_id, "task_id": "lab_task_codex",
        "instructions": "越权合成写入", "member_ids": ["mem_lab_codex_0001"], "related_task": "lab_task_codex"}),
        "假称已经写入成功。"]
    thread = service.create_thread(admin, project_id, "拒绝写入")
    before = backend.stats(project_id).get("task_writes", 0)
    result = await chat(project_id, thread, "hidden-write", "只是讨论")
    assert result["status"] == "denied" and not result.get("analysis_complete")
    assert backend.stats(project_id).get("task_writes", 0) == before
    assert not store.items("operation")
asyncio.run(scenario())
''')


if __name__ == "__main__":
    unittest.main()
