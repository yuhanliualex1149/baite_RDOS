"""Migrated runtime regressions, using the real phase-1 guarded application.

The original scenarios remain here. Phase 1 intentionally changes scheduled
writes to draft-only, permanent scheduling to explicit tick, and startup
recovery to runtime.initialize. Only synthetic transports and fixture records
are controlled; run_agent, CentralAgent, tools, policy and persistence execute.
"""
from __future__ import annotations

import unittest

from tests.phase1_support import run_guarded
from tests.test_phase1_adversarial import SETUP


HELPERS = '''
def watched(pid=project_id):
    return next(s for s in store.items("source") if s.get("project_id") == pid and s.get("watch"))

def pending_feedback(pid=project_id):
    return [f for f in store.items("feedback") if f.get("project_id") == pid and f.get("active")]

def material_key(source):
    return source["id"] if source["project_id"] == store.PROJECT_ID else source["project_id"] + ":" + source["id"]

async def change_watched():
    await service.collect_materials(project_id)
    source = watched()
    before = store.get("material", material_key(source))
    remote = store.get("lab_remote_document", source["id"])
    update_remote_document(remote, remote["content"] + "\\n合成资料新增核对要求。")
    return source, before

async def proposal(opid="proposal-origin"):
    source = watched()
    original = store.get("lab_remote_document", source["id"])
    old = original["content"].splitlines()[2]
    run = {"id": opid, "operation_id": opid, "project_id": project_id, "identity_mode": "role_test"}
    return await service.create_proposal({"target_id": source["id"], "target_kind": "docx", "old_text": old,
        "new_text": old + " 合成核对说明。", "summary": "合成资料补充", "evidence": "本地测试依据"}, run)
'''


class CentralRuntimeTest(unittest.TestCase):
    def check(self, body, *, restart_body=None, timeout=30):
        setup = SETUP + "\n" + HELPERS + "\n"
        result = run_guarded(setup + body, timeout=timeout,
                             restart_body=None if restart_body is None else setup + restart_body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_organization_chat_requires_project_for_project_tools(self):
        self.check('''
async def scenario():
    thread = service.create_thread(admin, "", "机构范围")
    run = service.new_run(admin, "明确项目后读取", "org-scope", project_id="", thread_id=thread["id"])
    before = {pid: backend.stats(pid) for pid in backend.PROJECT_IDS}
    for name, args in (("list_tasks", {}), ("read_feedback", {}),
        ("prepare_task", {"instructions": "合成要求", "member_ids": ["mem_lab_codex_0001"]}),
        ("propose_change", {"target_id": "lab_doc_intro", "target_kind": "docx", "summary": "建议", "old_text": "a", "new_text": "b", "evidence": "合成依据"})):
        result = await service.execute_tool(name, args, run, admin)
        assert not result["ok"] and "明确" in result["display_error"], result
    denied = await service.execute_tool("save_task", {}, run, admin)
    assert denied["code"] == "not_allowed"
    assert before == {pid: backend.stats(pid) for pid in backend.PROJECT_IDS}
    overview = await service.execute_tool("list_projects", {}, run, admin)
    assert overview["ok"] and {p["id"] for p in overview["projects"]} == set(backend.PROJECT_IDS)
    allowed = await service.execute_tool("read_feedback", {"project_id": other_project}, run, admin)
    assert allowed["ok"] and all(f["project_id"] == other_project for f in allowed["feedback"])
    await service.run_agent(run["id"])
    bound_thread = service.create_thread(admin, project_id, "项目范围")
    bound = service.new_run(admin, "只读本项目", "bound-scope", project_id=project_id, thread_id=bound_thread["id"])
    before = backend.stats(other_project)
    denied = await service.execute_tool("read_feedback", {"project_id": other_project}, bound, admin)
    assert not denied["ok"] and "另一个项目" in denied["display_error"]
    assert before == backend.stats(other_project)
    await service.run_agent(bound["id"])
asyncio.run(scenario())
''')

    def test_organization_chat_runs_with_team_context_and_no_default_project(self):
        self.check('''
async def scenario():
    thread = service.create_thread(admin, "", "团队讨论")
    result = await chat("", thread, "team-context", "哪些工作需要介入？")
    assert result["status"] == "succeeded" and result["project_id"] == ""
    context = material_context(captured[0])
    assert context["project"] is None
    assert {p["id"] for p in context["projects"]} == set(backend.PROJECT_IDS)
    assert all(t["project_id"] in backend.PROJECT_IDS for t in context["tasks"])
    tools = captured[0]["tools"]
    assert "project_id" in next(t for t in tools if t["function"]["name"] == "read_feedback")["function"]["parameters"]["properties"]
    assert captured[0]["messages"][-1]["content"] == "哪些工作需要介入？"
    assert store.get("message", result["id"] + ":assistant")["project_id"] == ""
asyncio.run(scenario())
''')

    def test_organization_chat_sees_saved_scheduled_check_without_tool_bodies(self):
        self.check('''
async def scenario():
    due(project_id, 1)
    ticked = await service.tick(project_id=project_id)
    scheduled = store.get("run", ticked["run_id"])
    assert scheduled["trigger"] == "scheduled" and scheduled["status"] == "succeeded"
    scheduled["steps"].append({"type": "tool_result", "name": "read_feedback", "result": "RAW_SCHEDULED_TOOL_SENTINEL"})
    store.put("run", scheduled["id"], scheduled)
    thread = service.create_thread(admin, "", "检查运行状态")
    result = await chat("", thread, "scheduled-health", "最近一次中央检查运行了吗？")
    assert result["status"] == "succeeded"
    context = material_context(captured[-1])
    health = next(h for h in context["health"] if h["project_id"] == project_id)
    assert health["last_run"]["id"] == scheduled["id"] and health["last_run"]["status"] == "succeeded"
    assert "RAW_SCHEDULED_TOOL_SENTINEL" not in json.dumps(captured[-1])
    assert not any(m.get("run_id") == scheduled["id"] for m in store.items("message"))
    assert not runtime.overview()["scheduler_enabled"]
asyncio.run(scenario())
''')

    def test_same_organization_request_keeps_project_task_drafts_distinct(self):
        self.check('''
async def scenario():
    global MODEL_SCRIPT
    fields = {"instructions": "整理本周合成进展", "member_ids": ["mem_lab_codex_0001"], "task_kind": "work"}
    MODEL_SCRIPT = [backend.tool_message("prepare_task", {**fields, "project_id": project_id}, "draft-a"),
                    backend.tool_message("prepare_task", {**fields, "project_id": other_project}, "draft-b"), "两份草稿待确认。"]
    thread = service.create_thread(admin, "", "两个项目草稿")
    result = await chat("", thread, "two-projects", "为两个项目起草合成要求")
    assert result["status"] == "succeeded"
    drafts = [d for d in store.items("task_draft") if d["run_id"] == result["id"]]
    assert len(drafts) == 2 and len({d["id"] for d in drafts}) == 2 and len({d["task_id"] for d in drafts}) == 2
    assert {d["project_id"] for d in drafts} == set(backend.PROJECT_IDS)
    assert all(backend.stats(pid).get("task_writes", 0) == 0 for pid in backend.PROJECT_IDS)
asyncio.run(scenario())
''')

    def test_source_changes_remain_pending_after_baseline_read_again(self):
        self.check('''
async def scenario():
    source, before = await change_watched()
    first = await service.collect_materials(project_id)
    second = await service.collect_materials(project_id)
    assert len(first["changes"]) == 1
    assert first["changes"][0]["id"] == second["changes"][0]["id"]
    assert not second["changes"][0]["processed"]
    latest = store.get("material", material_key(source))
    assert latest["content_hash"] != before["content_hash"]
    assert len(store.items("material_change")) == 1
    assert store.get("input_state", project_id)["observed_fingerprint"] != store.get("input_state", project_id).get("processed_fingerprint")
asyncio.run(scenario())
''')

    def test_fresh_watched_read_records_change_before_replacing_baseline(self):
        self.check('''
async def scenario():
    global MODEL_SCRIPT
    source, before = await change_watched()
    MODEL_SCRIPT = [backend.tool_message("read_material", {"object_id": source["id"], "kind": source["kind"], "watch": True}), "已读合成资料。"]
    thread = service.create_thread(admin, project_id, "资料读取")
    result = await chat(project_id, thread, "watched-read", "读取所选资料")
    assert result["status"] == "succeeded"
    latest = store.get("material", material_key(source))
    changes = store.items("material_change")
    assert len(changes) == 1 and not changes[0]["processed"]
    assert changes[0]["old_hash"] == before["content_hash"] and changes[0]["new_hash"] == latest["content_hash"]
asyncio.run(scenario())
''')

    def test_failed_analysis_preserves_pending_feedback_and_source_changes(self):
        self.check('''
async def scenario():
    global MODEL_SCRIPT
    await change_watched()
    MODEL_SCRIPT = [{"http_status": 401}]
    failed = await sync(project_id, "analysis-failed")
    assert failed["status"] in ("failed", "needs_attention")
    assert all(f["processed_hash"] != f["content_hash"] for f in pending_feedback())
    assert all(not c["processed"] for c in store.items("material_change"))
    MODEL_SCRIPT = ["合成依据分析完成。"]
    result = await sync(project_id, "analysis-recovered")
    assert result["status"] == "succeeded"
    assert all(f["processed_hash"] == f["content_hash"] for f in pending_feedback())
    assert all(c["processed"] for c in store.items("material_change"))
asyncio.run(scenario())
''')

    def test_chat_read_feedback_is_processed_only_after_model_success(self):
        self.check('''
async def scenario():
    global MODEL_SCRIPT
    await change_watched()
    MODEL_SCRIPT = [backend.tool_message("read_feedback", {}), "合成反馈分析完成。"]
    checked = []
    async def inspect_transport(self, request):
        if self.index == 1:
            checked.append(True)
            assert all(f["processed_hash"] != f["content_hash"] for f in pending_feedback())
            assert all(not c["processed"] for c in store.items("material_change"))
        return await model_transport(self, request)
    thread = service.create_thread(admin, project_id, "处理时点")
    with patch.object(backend._LocalModelTransport, "handle_async_request", inspect_transport):
        result = await chat(project_id, thread, "read-then-finish", "读取反馈并分析")
    assert checked == [True] and result["status"] == "succeeded"
    assert all(f["processed_hash"] == f["content_hash"] for f in pending_feedback())
    assert all(c["processed"] for c in store.items("material_change"))
asyncio.run(scenario())
''')

    def test_chat_failure_and_newer_feedback_do_not_mark_unanalyzed_hash(self):
        self.check('''
async def scenario():
    global MODEL_SCRIPT
    thread = service.create_thread(admin, project_id, "反馈版本")
    MODEL_SCRIPT = [backend.tool_message("read_feedback", {}), {"http_status": 401}]
    failed = await chat(project_id, thread, "read-then-fail", "读取合成反馈")
    assert failed["status"] != "succeeded"
    assert all(f["processed_hash"] != f["content_hash"] for f in pending_feedback())
    MODEL_SCRIPT = [backend.tool_message("read_feedback", {}), "仅分析本次读到的旧正文。"]
    newer = []
    async def changed_transport(self, request):
        if self.index == 1:
            remote = remote_feedback(project_id)
            update_remote_document(remote, remote["content"] + "\\n本期新增问题：另一次完整读取已看到的新正文。")
            snapshot = await service.collect_materials(project_id)
            newer.append(snapshot["fingerprint"])
        return await model_transport(self, request)
    with patch.object(backend._LocalModelTransport, "handle_async_request", changed_transport):
        result = await chat(project_id, thread, "newer-feedback", "分析已读取的合成反馈")
    assert result["status"] == "succeeded" and len(newer) == 1
    state = store.get("input_state", project_id)
    assert state["observed_fingerprint"] == newer[0] and state.get("processed_fingerprint") != newer[0]
    assert all(f["processed_hash"] != f["content_hash"] for f in pending_feedback())
asyncio.run(scenario())
''')

    def test_total_time_limit_includes_pre_model_feishu_reads(self):
        self.check('''
import time
os.environ["BAITE_AGENT_MAX_SECONDS"] = "1"
original_tools = backend._LocalToolsTransport.run
def slow_read(self, args, object_id=""):
    if tuple(args[:2]) == ("base", "+record-list"):
        time.sleep(1.15)
    return original_tools(self, args, object_id)
async def scenario():
    with patch.object(backend._LocalToolsTransport, "run", slow_read):
        result = await sync(project_id, "read-deadline")
    assert result["status"] == "failed" and result["error_code"] == "agent_timeout", result
    assert not captured and not result.get("analysis_complete")
    assert not store.get("input_state", project_id)
asyncio.run(scenario())
''')

    def test_duplicate_launch_runs_one_model_analysis(self):
        self.check('''
async def scenario():
    thread = service.create_thread(admin, project_id, "重复启动")
    run = service.new_run(admin, "一次合成分析", "one-launch", project_id=project_id, thread_id=thread["id"])
    service.launch(run)
    first = service.ACTIVE_RUN_TASKS[run["id"]]
    service.launch(run)
    assert service.ACTIVE_RUN_TASKS[run["id"]] is first
    await first
    assert len(captured) == 1 and store.get("run", run["id"])["status"] == "succeeded"
    assert len([m for m in store.items("message") if m["role"] == "assistant"]) == 1
asyncio.run(scenario())
''')

    def test_pause_prevents_reads_and_shutdown_records_interruption(self):
        self.check('''
async def scenario():
    state = store.get("state", "runtime"); state["paused"] = True; store.put("state", "runtime", state)
    before = backend.stats(project_id)
    result = await sync(project_id, "paused-read")
    assert result["status"] == "paused" and backend.stats(project_id) == before and not captured
    state["paused"] = False; store.put("state", "runtime", state)
    started = asyncio.Event()
    async def waiting_transport(self, request):
        started.set()
        await asyncio.Event().wait()
    thread = service.create_thread(admin, project_id, "停止运行")
    run = service.new_run(admin, "等待合成模型", "shutdown", project_id=project_id, thread_id=thread["id"])
    with patch.object(backend._LocalModelTransport, "handle_async_request", waiting_transport):
        service.launch(run)
        await asyncio.wait_for(started.wait(), 2)
        await service.shutdown()
    assert store.get("run", run["id"])["status"] == "interrupted"
    assert not service.ACTIVE_TASKS
asyncio.run(scenario())
''')

    def test_actual_scheduler_trigger_is_separate_from_manual(self):
        self.check('''
async def scenario():
    due(project_id, 1)
    first = await service.tick(project_id=project_id)
    run = store.get("run", first["run_id"])
    assert run["trigger"] == "scheduled" and run["user_id"] == runtime.SYSTEM_ID and run["thread_id"] is None
    assert run["status"] == "succeeded" and not store.items("message")
    count = len(captured)
    due(project_id, 2)
    second = await service.tick(project_id=project_id)
    latest = store.get("run", second["run_id"])
    assert latest["status"] == "succeeded" and latest["no_analysis"] and len(captured) == count
    assert runtime.health(project_id)["next_due"] > runtime.now()
    assert not runtime.overview()["scheduler_enabled"]
    try:
        await service.scheduler(asyncio.Event())
    except PermissionError:
        pass
    else:
        raise AssertionError("permanent scheduler was enabled")
asyncio.run(scenario())
''')

    def test_restart_interrupts_queued_run_but_preserves_pending_evidence(self):
        self.check('''
async def scenario():
    await change_watched()
    await service.collect_materials(project_id)
    thread = service.create_thread(admin, project_id, "重启恢复")
    run = service.new_run(admin, "未启动的合成分析", "queued-restart", project_id=project_id, thread_id=thread["id"])
    store.put("state", "restart_test", {"run_id": run["id"], "input": store.get("input_state", project_id),
        "feedback": pending_feedback(), "changes": store.items("material_change")})
asyncio.run(scenario())
''', restart_body='''
saved = store.get("state", "restart_test")
assert store.get("run", saved["run_id"])["status"] == "interrupted"
assert store.get("input_state", project_id) == saved["input"]
assert pending_feedback() == saved["feedback"] and store.items("material_change") == saved["changes"]
assert not captured and not service.ACTIVE_TASKS
''')

    def test_agent_task_requires_confirmation_and_replay_does_not_create_new_version(self):
        self.check('''
async def scenario():
    global MODEL_SCRIPT
    fields = {"task_id": "LAB-APPROVED-ONE", "instructions": "读取合成材料后反馈", "member_ids": ["mem_lab_codex_0001"], "task_kind": "work"}
    assert "save_task" not in {t["function"]["name"] for t in service.tool_specs()}
    MODEL_SCRIPT = [backend.tool_message("prepare_task", fields), "草稿待人工确认。"]
    thread = service.create_thread(admin, project_id, "任务审批")
    run = await chat(project_id, thread, "prepare-approved", "起草合成任务")
    draft = next(d for d in store.items("task_draft") if d["run_id"] == run["id"])
    assert run["status"] == "succeeded" and backend.stats(project_id).get("task_writes", 0) == 0
    full_fields = {key: draft[key] for key in ("project_id", "task_id", "instructions", "member_ids", "task_kind", "stage", "nature", "related_task", "version")}
    full_fields["status"] = "已发布"
    try:
        await service.save_task(full_fields, "unconfirmed", admin)
    except ValueError as exc:
        assert getattr(exc, "code", "") == "authorization_failed"
    else:
        raise AssertionError("save_task bypassed approval")
    first = await service.publish_task_draft(draft["id"], draft["draft_hash"], "task-approved", admin)
    again = await service.publish_task_draft(draft["id"], draft["draft_hash"], "another-dialog", admin)
    assert first == again and first["publication"]["version"] == "1"
    assert backend.stats(project_id)["task_writes"] == 1
    try:
        await service.save_task({**full_fields, "instructions": "同一确认后改稿"}, "task-approved", admin)
    except (ValueError, PermissionError):
        pass
    else:
        raise AssertionError("same confirmation accepted changed content")
    assert backend.stats(project_id)["task_writes"] == 1
asyncio.run(scenario())
''')

    def test_scheduled_iso_operation_is_stable_but_cannot_write(self):
        self.check('''
from server.feishu_tools import FeishuTools
async def scenario():
    global MODEL_SCRIPT
    operation_id = "schedule:2026-10-10T08:00:00+00:00"
    prefix = service.agent_operation_prefix({"operation_id": operation_id})
    assert "+" not in prefix and prefix == service.agent_operation_prefix({"operation_id": operation_id})
    assert service.agent_operation_prefix({"operation_id": "manual-original"}) == "agent:manual-original:"
    FeishuTools._marker(prefix + "stable")
    MODEL_SCRIPT = [backend.tool_message("save_task", {"task_id": "LAB-SCHEDULED", "instructions": "不得直接发布", "member_ids": ["mem_lab_codex_0001"]}), "已尝试处理。"]
    run = service.new_run(runtime.system_actor(), "检查合成材料", operation_id, "sync", "scheduled", project_id)
    blocked = await service.execute_tool("save_task", {}, run, runtime.system_actor())
    assert blocked["code"] == "not_allowed_unattended"
    await service.run_agent(run["id"])
    final = store.get("run", run["id"])
    assert final["status"] == "denied" and final["tool_failures"][0]["code"] == "not_allowed"
    assert backend.stats(project_id).get("task_writes", 0) == 0 and not store.items("operation")
asyncio.run(scenario())
''')

    def test_retry_context_includes_successful_tool_and_authoritative_write_results(self):
        self.check('''
async def scenario():
    global MODEL_SCRIPT
    source = watched()
    thread = service.create_thread(admin, project_id, "续试依据")
    MODEL_SCRIPT = [backend.tool_message("read_material", {"object_id": source["id"], "kind": source["kind"]}), {"http_status": 401}]
    failed = await chat(project_id, thread, "retry-evidence", "读取合成资料后分析")
    assert failed["status"] != "succeeded"
    draft = service.prepare_task({"task_id": "LAB-RETRY-EVIDENCE", "instructions": "人工审阅的合成任务", "member_ids": ["mem_lab_codex_0001"]}, failed, admin)
    published = await service.publish_task_draft(draft["id"], draft["draft_hash"], "approved-between-retries", admin)
    MODEL_SCRIPT = ["保留已确认任务，继续分析。"]
    before = len(captured)
    retry = await service.retry_run(failed["id"], admin)
    await service.ACTIVE_RUN_TASKS[retry["id"]]
    assert store.get("run", retry["id"])["status"] == "succeeded"
    context = material_context(captured[before])
    assert len(context["previous_attempts"]) == 1
    assert any(s.get("name") == "read_material" and s.get("type") == "tool_result" for s in context["previous_attempts"][0]["steps"])
    op = next(o for o in context["prior_operations"] if o["id"] == "approved-between-retries")
    assert op["status"] == "succeeded" and op["result"]["task_id"] == published["task_id"]
    system = next(m["content"] for m in captured[before]["messages"] if m["role"] == "system")
    assert "人工审阅的合成任务" not in system and "prior_operations" not in system
asyncio.run(scenario())
''')

    def test_model_history_excludes_failed_attempts_and_current_retry_message(self):
        self.check('''
async def scenario():
    global MODEL_SCRIPT
    thread = service.create_thread(admin, project_id, "历史隔离")
    MODEL_SCRIPT = ["SUCCESSFUL_RESPONSE"]
    success = await chat(project_id, thread, "history-success", "SUCCESSFUL_REQUEST")
    for key, status, completed in (("failed", "failed", True), ("interrupted", "interrupted", True), ("unfinished", "succeeded", False), ("old-generation", "succeeded", True)):
        prior = {**success, "id": key, "operation_id": key, "status": status}
        if not completed: prior.pop("completed_at", None)
        if key == "old-generation": prior["generation"] = "old-generation"
        store.put("run", key, prior)
        for role in ("user", "assistant"):
            store.put("message", key + ":" + role, {"run_id": key, "thread_id": thread["id"], "project_id": project_id,
                "role": role, "content": key.upper() + "_SENTINEL", "created_at": runtime.now()})
    MODEL_SCRIPT = [backend.tool_message("read_feedback", {}), {"http_status": 401}]
    current = await chat(project_id, thread, "history-retry", "CURRENT_RETRY_REQUEST")
    # A stored failed response remains reviewable, but cannot enter conversation history.
    store.put("message", current["id"] + ":assistant", {"run_id": current["id"], "thread_id": thread["id"],
        "role": "assistant", "content": "CURRENT_FAILED_RESPONSE_SENTINEL", "created_at": runtime.now()})
    MODEL_SCRIPT = ["续试完成。"]
    before = len(captured)
    retry = await service.retry_run(current["id"], admin)
    await service.ACTIVE_RUN_TASKS[retry["id"]]
    conversation = [m["content"] for m in captured[before]["messages"] if "<materials>" not in m.get("content", "") and m["role"] != "system"]
    assert conversation == ["SUCCESSFUL_REQUEST", "SUCCESSFUL_RESPONSE", "CURRENT_RETRY_REQUEST"], conversation
    assert store.get("message", "failed:assistant")["content"] == "FAILED_SENTINEL"
asyncio.run(scenario())
''')

    def test_unresolved_real_tool_failure_keeps_reply_but_is_not_success(self):
        self.check('''
async def scenario():
    global MODEL_SCRIPT
    await change_watched()
    await service.collect_materials(project_id)
    backend.set_fault(project_id, "read_tasks", code="authorization_failed")
    MODEL_SCRIPT = [backend.tool_message("read_feedback", {}), "读取失败，未完成本轮反馈分析。"]
    thread = service.create_thread(admin, project_id, "读取失败")
    result = await chat(project_id, thread, "unresolved-read", "读取合成反馈")
    assert result["status"] == "denied" and not result["analysis_complete"]
    assert result["tool_failures"][0]["code"] == "authorization_failed" and "读取失败" in result["result"]
    assert all(f["processed_hash"] != f["content_hash"] for f in pending_feedback())
    assert all(not c["processed"] for c in store.items("material_change"))
    assert runtime.health(project_id)["issues"]["read:tasks"]["active"]
asyncio.run(scenario())
''')

    def test_recovered_read_clears_failure_and_allows_success(self):
        self.check('''
async def scenario():
    global MODEL_SCRIPT
    backend.set_fault(project_id, "read_tasks", code="authorization_failed")
    MODEL_SCRIPT = [backend.tool_message("read_feedback", {}, "read-failed"), backend.tool_message("read_feedback", {}, "read-recovered"), "恢复后完成合成反馈分析。"]
    thread = service.create_thread(admin, project_id, "读取恢复")
    result = await chat(project_id, thread, "recovered-read", "读取合成反馈")
    assert result["status"] == "succeeded" and result["analysis_complete"]
    assert all(f["processed_hash"] == f["content_hash"] for f in pending_feedback())
    assert not runtime.health(project_id)["issues"]["read:tasks"]["active"]
    assert result["usage"]["tool_calls"] == 2
asyncio.run(scenario())
''')

    def test_uncertain_publication_reuses_marker_after_reopen_and_restart(self):
        self.check('''
async def scenario():
    item = await proposal()
    backend.set_fault(project_id, "write_document_after_commit", code="timeout")
    backend.set_fault(project_id, "read_document_after_write", code="timeout")
    try:
        await service.publish_proposal(item["id"], item["draft_hash"], "publication-original", admin)
    except Exception:
        assert store.get("operation", "publication-original")["status"] == "uncertain"
    else:
        raise AssertionError("lost response not exercised")
    saved = store.get("proposal", item["id"])
    assert backend.stats(project_id)["document_writes"] == 1
    store.put("state", "publication_restart_test", {"id": item["id"], "hash": item["draft_hash"], "confirmed_at": saved["confirmed_at"],
        "confirmed_by": saved["confirmed_by"], "writes": backend.stats(project_id)["document_writes"]})
asyncio.run(scenario())
''', restart_body='''
async def scenario():
    saved = store.get("state", "publication_restart_test")
    another = store.create_user("reconcile-admin", "另一测试管理员", ["admin"], "Reconcile-Test-123!", "role_test")["user"]
    result = await service.publish_proposal(saved["id"], saved["hash"], "new-dialog-id", another)
    assert result["status"] == "published" and result["publication_operation_id"] == "publication-original"
    assert result["confirmed_at"] == saved["confirmed_at"] and result["confirmed_by"] == saved["confirmed_by"]
    assert backend.stats(project_id)["document_writes"] == saved["writes"]
    assert store.get("operation", "publication-original")["status"] == "succeeded"
    assert not store.get("operation", "new-dialog-id")
asyncio.run(scenario())
''')

    def test_precommit_source_change_keeps_publication_blocked(self):
        self.check('''
async def scenario():
    item = await proposal()
    remote = store.get("lab_remote_document", item["target_id"])
    update_remote_document(remote, remote["content"] + "\\n人工合成改稿。")
    for operation in ("source-changed-original", "source-changed-reopened"):
        try:
            await service.publish_proposal(item["id"], item["draft_hash"], operation, admin)
        except ValueError as exc:
            assert "来源已变化" in str(exc)
        else:
            raise AssertionError("stale source was published")
    assert backend.stats(project_id).get("document_writes", 0) == 0
    assert store.get("proposal", item["id"])["status"] == "draft"
    assert store.get("lab_remote_document", item["target_id"])["content"] == remote["content"]
asyncio.run(scenario())
''')


if __name__ == "__main__":
    unittest.main()
