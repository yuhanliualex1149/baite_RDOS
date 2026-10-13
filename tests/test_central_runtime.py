from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from server import central_service as service, central_store as store, db
from server.central_agent import AgentError, CentralAgent
from server.feishu_tools import FeishuError, FeishuTools
from tests.test_central_feishu import FakeCli


class CentralRuntimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "BAITE_DB_PATH": str(Path(self.temp.name) / "runtime.db"),
            "BAITE_ADMIN_USERNAME": "runtime-admin",
            "BAITE_ADMIN_PASSWORD": "RuntimeTestPassword123!",
        })
        self.environment.start()
        db.init_db()
        store.init()
        self.user = store.users()[0]
        self.previous_lock = service.RUN_LOCK
        service.RUN_LOCK = asyncio.Lock()

    async def asyncTearDown(self):
        await service.shutdown()
        service.RUN_LOCK = self.previous_lock
        self.environment.stop()
        self.temp.cleanup()

    def run_record(self, operation="runtime-test", kind="chat", trigger="manual"):
        return service.new_run(self.user, "读取反馈并检查依据", operation, kind, trigger)

    async def test_organization_chat_requires_project_for_project_tools(self):
        run = service.new_run(self.user, "团队有什么进展？", "org-runtime", project_id="")
        self.assertEqual(run["project_id"], "")
        for name, arguments in (
            ("list_tasks", {}), ("read_feedback", {}),
            ("prepare_task", {"instructions": "安排工作", "member_ids": [self.user["id"]]}),
            ("save_task", {"task_id": "FOLLOW-UP", "related_task": "ORIGINAL", "instructions": "补充", "member_ids": [self.user["id"]]}),
            ("propose_change", {"target_id": "doc", "target_kind": "docx", "summary": "建议", "old_text": "a", "new_text": "b", "evidence": "反馈"}),
        ):
            with self.subTest(tool=name), patch.object(service, "feishu") as remote:
                result = await service.execute_tool(name, arguments, run, self.user)
                self.assertFalse(result["ok"])
                self.assertIn("明确", result["display_error"])
                remote.assert_not_called()
        project = {"id": "another-project", "name": "另一个项目", "member_ids": [self.user["id"]],
                   "feedback_id": "another-feedback", "status": "active"}
        store.put("project", project["id"], project)
        bound = {"project_id": store.PROJECT_ID, "operation_id": "bound", "identity_mode": "role_test"}
        denied = await service.execute_tool("read_feedback", {"project_id": project["id"]}, bound, self.user)
        self.assertFalse(denied["ok"])
        self.assertIn("另一个项目", denied["display_error"])
        overview = await service.execute_tool("list_projects", {}, run, self.user)
        self.assertTrue(overview["ok"])
        self.assertIn(project["id"], [item["id"] for item in overview["projects"]])
        materials = {"feedback": [], "changes": [], "feedback_source": "https://example.test/docx/another-feedback"}
        with patch.object(service, "collect_materials", AsyncMock(return_value=materials)) as read:
            allowed = await service.execute_tool("read_feedback", {"project_id": project["id"]}, run, self.user)
            self.assertTrue(allowed["ok"])
            read.assert_awaited_once_with(project["id"])

    async def test_organization_chat_runs_with_team_context_and_no_default_project(self):
        store.put("project", "team-project", {"id": "team-project", "name": "团队工作",
            "member_ids": [self.user["id"]], "feedback_id": "team-feedback", "status": "active"})
        run = service.new_run(self.user, "哪些工作需要介入？", "team-context", project_id="")

        async def answer(agent, messages, tools, execute_tool, **kwargs):
            context = json.loads(messages[0]["content"].split("\n", 1)[1])
            self.assertIsNone(context["project"])
            self.assertIn("team-project", [item["id"] for item in context["projects"]])
            self.assertEqual(context["tasks"], [])
            self.assertIn("哪些工作需要介入", messages[-1]["content"])
            self.assertIn("project_id", next(item for item in tools if item["function"]["name"] == "read_feedback")["function"]["parameters"]["properties"])
            return {"content": "目前已登记团队工作；尚未读取具体反馈。请指定项目后继续。", "model": "test", "usage": {"model_calls": 1}}

        with patch.object(CentralAgent, "run", answer):
            await service.run_agent(run["id"])
        stored = store.get("run", run["id"])
        self.assertEqual(stored["status"], "succeeded")
        self.assertEqual(stored["project_id"], "")
        self.assertEqual(store.get("message", run["id"] + ":assistant")["project_id"], "")

    async def test_organization_chat_sees_saved_scheduled_check_without_tool_bodies(self):
        state = store.get("state", "runtime")
        state.update(schedule_verified=True, last_scheduled={
            "id": "scheduled-latest", "at": "2026-10-12T00:40:00+00:00", "status": "succeeded"})
        store.put("state", "runtime", state)
        store.put("run", "scheduled-older", {"id": "scheduled-older", "operation_id": "scheduled-older", "trigger": "scheduled",
            "project_id": store.PROJECT_ID, "status": "failed", "created_at": "2026-10-11T00:00:00+00:00",
            "completed_at": "2026-10-11T00:02:00+00:00", "error": "读取反馈失败"})
        store.put("run", "scheduled-latest", {"id": "scheduled-latest", "operation_id": "scheduled-latest", "trigger": "scheduled",
            "project_id": store.PROJECT_ID, "status": "succeeded", "created_at": "2026-10-12T00:37:00+00:00",
            "completed_at": "2026-10-12T00:40:00+00:00", "result": "已读回反馈，暂无新内容。",
            "steps": [{"type": "tool_result", "name": "read_feedback", "ok": True,
                       "arguments": {"secret": "do-not-share"}, "result": "do-not-share…（完整材料通过来源读取）"},
                      {"type": "tool_result", "name": "list_tasks", "result": {"ok": False, "error": "not-shared"}}]})
        store.put("run", "manual-other", {"id": "manual-other", "operation_id": "manual-other", "trigger": "manual",
            "status": "succeeded", "created_at": "2026-10-12T00:45:00+00:00"})
        run = service.new_run(self.user, "最近一次中央定时检查运行了吗？", "scheduled-history", project_id="")

        async def answer(agent, messages, tools, execute_tool, **kwargs):
            context = json.loads(messages[0]["content"].split("\n", 1)[1])
            checks = context["central_scheduled_checks"]
            self.assertTrue(checks["schedule_verified"])
            self.assertEqual(checks["last_scheduled"], state["last_scheduled"])
            self.assertEqual([item["id"] for item in checks["recent_runs"]], ["scheduled-latest", "scheduled-older"])
            self.assertEqual(checks["recent_runs"][0]["tools"],
                             [{"name": "read_feedback", "ok": True}, {"name": "list_tasks", "ok": False}])
            self.assertEqual(checks["recent_runs"][0]["summary"], "已读回反馈，暂无新内容。")
            self.assertNotIn("do-not-share", messages[0]["content"])
            return {"content": "最近一次中央定时检查已成功，反馈已读回。", "model": "test", "usage": {}}

        with patch.object(CentralAgent, "run", answer):
            await service.run_agent(run["id"])
        self.assertEqual(store.get("run", run["id"])["status"], "succeeded")

    async def test_same_organization_request_keeps_project_task_drafts_distinct(self):
        for project_id in ("team-one", "team-two"):
            store.put("project", project_id, {"id": project_id, "name": project_id,
                "member_ids": [self.user["id"]], "feedback_id": project_id + "-feedback", "status": "active"})
        run = service.new_run(self.user, "给两个项目准备工作", "two-projects", project_id="")
        fields = {"instructions": "整理本周进展", "member_ids": [self.user["id"]], "task_kind": "work"}
        first = service.prepare_task(fields, {**run, "project_id": "team-one"}, self.user)
        second = service.prepare_task(fields, {**run, "project_id": "team-two"}, self.user)
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["task_id"], second["task_id"])
        self.assertEqual((first["project_id"], second["project_id"]), ("team-one", "team-two"))

    def materials(self):
        feedback = {"id": "feedback-one", "content_hash": "feedback-hash", "processed_hash": "", "content": "实际反馈"}
        change = {"id": "change-one", "object_id": "watched", "old_hash": "before", "new_hash": "after", "diff": "-旧内容\n+新内容", "processed": False}
        store.put("feedback", feedback["id"], feedback)
        store.put("material_change", change["id"], change)
        return {"pending_feedback": [feedback], "feedback": [feedback], "changes": [change]}

    async def test_source_changes_remain_pending_after_baseline_read_again(self):
        source_id = store.ONBOARDING_IDS[0]
        source = store.get("source", source_id)
        source["watch"] = True
        store.put("source", source_id, source)
        before = {"content": "旧内容", "content_hash": "before", "source_url": source["source_url"]}
        after = {"content": "新内容", "content_hash": "after", "source_url": source["source_url"]}
        store.put("material", source_id, before)

        class ReadOnlyFeishu:
            def list_tasks(self):
                return {"items": []}

            def read_document(self, document_id):
                return {"content": "", "source_url": "https://feishu.test/feedback"}

        with patch.object(service, "feishu", return_value=ReadOnlyFeishu()), patch.object(service, "source_read", return_value=after):
            first = await service.collect_materials()
            second = await service.collect_materials()
        self.assertEqual(len(first["changes"]), 1)
        self.assertEqual(first["changes"][0]["id"], second["changes"][0]["id"])
        self.assertFalse(second["changes"][0]["processed"])
        self.assertEqual(store.get("material", source_id)["content_hash"], "after")
        self.assertEqual(len(store.items("material_change")), 1)

    async def test_fresh_watched_read_records_change_before_replacing_baseline(self):
        source_id = store.ONBOARDING_IDS[0]
        source = store.get("source", source_id)
        source["watch"] = True
        store.put("source", source_id, source)
        store.put("material", source_id, {"content": "old body", "content_hash": "old", "source_url": source["source_url"]})
        latest = {"ok": True, "object_id": source_id, "content": "new body", "content_hash": "new", "source_url": source["source_url"]}
        with patch.object(service, "source_read", return_value=latest):
            result = await service.execute_tool("read_material", {"object_id": source_id, "kind": "docx"}, {"operation_id": "watched-read"}, self.user)
        self.assertTrue(result["ok"])
        self.assertEqual(store.get("material", source_id)["content_hash"], "new")
        changes = store.items("material_change")
        self.assertEqual(len(changes), 1)
        self.assertFalse(changes[0]["processed"])
        self.assertEqual(changes[0]["old_hash"], "old")
        self.assertEqual(changes[0]["new_hash"], "new")

    async def test_failed_analysis_preserves_pending_feedback_and_source_changes(self):
        materials = self.materials()

        async def fail(*args, **kwargs):
            raise AgentError("model_http", "真实模型请求失败（HTTP 503）")

        run = self.run_record(kind="sync")
        with patch.object(service, "collect_materials", AsyncMock(return_value=materials)), patch.object(CentralAgent, "run", fail):
            await service.run_agent(run["id"])
        self.assertEqual(store.get("run", run["id"])["status"], "failed")
        self.assertEqual(store.get("feedback", "feedback-one")["processed_hash"], "")
        self.assertFalse(store.get("material_change", "change-one")["processed"])

        async def success(*args, **kwargs):
            return {"content": "已根据实际反馈分析", "model": "test", "usage": {"model_calls": 1}}

        retry = self.run_record("retry-success", "sync")
        with patch.object(service, "collect_materials", AsyncMock(return_value=materials)), patch.object(CentralAgent, "run", success):
            await service.run_agent(retry["id"])
        self.assertEqual(store.get("feedback", "feedback-one")["processed_hash"], "feedback-hash")
        self.assertTrue(store.get("material_change", "change-one")["processed"])

    async def test_chat_read_feedback_is_processed_only_after_model_success(self):
        materials = self.materials()

        async def read_then_finish(agent, messages, tools, execute_tool, **kwargs):
            result = await execute_tool("read_feedback", {})
            self.assertTrue(result["ok"])
            self.assertEqual(store.get("feedback", "feedback-one")["processed_hash"], "")
            self.assertFalse(store.get("material_change", "change-one")["processed"])
            return {"content": "根据实际读取给出分析", "model": "test", "usage": {"model_calls": 2}}

        run = self.run_record()
        with patch.object(service, "execute_tool", AsyncMock(return_value={"ok": True, **materials})), patch.object(CentralAgent, "run", read_then_finish):
            await service.run_agent(run["id"])
        self.assertEqual(store.get("run", run["id"])["status"], "succeeded")
        self.assertEqual(store.get("feedback", "feedback-one")["processed_hash"], "feedback-hash")
        self.assertTrue(store.get("material_change", "change-one")["processed"])

    async def test_chat_failure_and_newer_feedback_do_not_mark_unanalyzed_hash(self):
        materials = self.materials()

        async def read_then_fail(agent, messages, tools, execute_tool, **kwargs):
            await execute_tool("read_feedback", {})
            raise AgentError("model_network", "模型连接失败")

        run = self.run_record()
        with patch.object(service, "execute_tool", AsyncMock(return_value={"ok": True, **materials})), patch.object(CentralAgent, "run", read_then_fail):
            await service.run_agent(run["id"])
        self.assertEqual(store.get("feedback", "feedback-one")["processed_hash"], "")
        self.assertFalse(store.get("material_change", "change-one")["processed"])

        async def read_then_changed(agent, messages, tools, execute_tool, **kwargs):
            await execute_tool("read_feedback", {})
            updated = store.get("feedback", "feedback-one")
            updated["content_hash"] = "newer-feedback-hash"
            store.put("feedback", updated["id"], updated)
            return {"content": "已分析旧正文", "model": "test", "usage": {}}

        run = self.run_record("newer-feedback")
        with patch.object(service, "execute_tool", AsyncMock(return_value={"ok": True, **materials})), patch.object(CentralAgent, "run", read_then_changed):
            await service.run_agent(run["id"])
        self.assertEqual(store.get("feedback", "feedback-one")["processed_hash"], "")

    async def test_total_time_limit_includes_pre_model_feishu_reads(self):
        async def slow_collect(*args):
            await asyncio.sleep(0.1)
            return {"pending_feedback": [], "changes": []}

        model = AsyncMock()
        run = self.run_record(kind="sync")
        with patch.object(service, "collect_materials", slow_collect), patch.object(CentralAgent, "max_seconds", 0.01), patch.object(CentralAgent, "run", model):
            await service.run_agent(run["id"])
        self.assertEqual(store.get("run", run["id"])["status"], "failed")
        self.assertIn("上限", store.get("run", run["id"])["error"])
        model.assert_not_awaited()

    async def test_duplicate_launch_runs_one_model_analysis(self):
        calls = []

        async def model(*args, **kwargs):
            calls.append(True)
            await asyncio.sleep(0.01)
            return {"content": "一次分析", "model": "test", "usage": {}}

        run = self.run_record()
        with patch.object(CentralAgent, "run", model):
            service.launch(run)
            service.launch(run)
            await asyncio.gather(*list(service.ACTIVE_TASKS))
        self.assertEqual(len(calls), 1)
        self.assertEqual(store.get("run", run["id"])["status"], "succeeded")
        self.assertEqual(sum(item["role"] == "assistant" for item in store.items("message")), 1)

    async def test_pause_prevents_reads_and_shutdown_records_interruption(self):
        state = store.get("state", "runtime")
        state["paused"] = True
        store.put("state", "runtime", state)
        run = self.run_record(kind="sync")
        collect = AsyncMock()
        with patch.object(service, "collect_materials", collect):
            await service.run_agent(run["id"])
        self.assertEqual(store.get("run", run["id"])["status"], "paused")
        collect.assert_not_awaited()
        state["paused"] = False
        store.put("state", "runtime", state)
        started = asyncio.Event()

        async def slow_model(*args, **kwargs):
            started.set()
            await asyncio.sleep(10)

        run = self.run_record("shutdown")
        with patch.object(CentralAgent, "run", slow_model):
            service.launch(run)
            await started.wait()
            await service.shutdown()
        self.assertEqual(store.get("run", run["id"])["status"], "interrupted")

    async def test_actual_scheduler_trigger_is_separate_from_manual(self):
        state = store.get("state", "runtime")
        state["next_due"] = "2000-01-01T00:00:00+00:00"
        store.put("state", "runtime", state)
        stop = asyncio.Event()

        async def read_no_changes(*args):
            stop.set()
            return {"pending_feedback": [], "changes": []}

        with patch.object(service, "collect_materials", read_no_changes):
            await service.scheduler(stop)
            await asyncio.gather(*list(service.ACTIVE_TASKS))
        runtime = store.get("state", "runtime")
        self.assertTrue(runtime["schedule_verified"])
        self.assertEqual(runtime["last_scheduled"]["status"], "succeeded")
        self.assertNotIn("last_manual", runtime)
        self.assertTrue(store.items("run")[0]["no_analysis"])

    async def test_restart_interrupts_queued_run_but_preserves_pending_evidence(self):
        self.materials()
        run = self.run_record()
        store.init()
        self.assertEqual(store.get("run", run["id"])["status"], "interrupted")
        self.assertEqual(store.get("feedback", "feedback-one")["processed_hash"], "")
        self.assertFalse(store.get("material_change", "change-one")["processed"])

    async def test_agent_task_requires_id_and_replay_does_not_create_new_version(self):
        class TaskRemote:
            def __init__(self):
                self.tasks = []
                self.writes = 0

            def ensure_task_fields(self):
                return {"ok": True}

            def list_tasks(self):
                return {"items": self.tasks}

            def save_task(self, fields, operation_id, record_id=None):
                self.writes += 1
                item = {**fields, "record_id": record_id or "remote-one", "source_url": "https://feishu.test/tasks"}
                self.tasks = [task for task in self.tasks if task["task_id"] != item["task_id"]] + [item]
                return item

        remote = TaskRemote()
        run = self.run_record()
        remote.tasks = [{"task_id": "ORIGINAL", "project_id": store.PROJECT_ID, "member_ids": [self.user["id"]], "status": "已发布", "version": "1", "task_kind": "work", "record_id": "rec-original"}]
        fields = {"instructions": "读取实际材料后反馈", "member_ids": [self.user["id"]], "related_task": "ORIGINAL"}
        spec = next(item for item in service.tool_specs() if item["function"]["name"] == "save_task")
        self.assertIn("task_id", spec["function"]["parameters"]["required"])
        with patch.object(service, "feishu", return_value=remote):
            missing = await service.execute_tool("save_task", fields, run, self.user)
            self.assertEqual(missing["code"], "task_id_required")
            self.assertEqual(remote.writes, 0)
            fields["task_id"] = "RDOS-LOGICAL-ONE"
            first = await service.execute_tool("save_task", fields, run, self.user)
            replay = await service.execute_tool("save_task", fields, run, self.user)
            changed = await service.execute_tool("save_task", {**fields, "instructions": "改写同一任务的措辞"}, run, self.user)
            self.assertEqual(first["item"], replay["item"])
            self.assertEqual(changed["code"], "validation")
            self.assertFalse(changed["ok"])
            self.assertEqual(remote.writes, 1)
            self.assertEqual(remote.tasks[0]["version"], "1")
            separate = await service.execute_tool("save_task", {**fields, "instructions": "管理员新操作调整要求"}, {**run, "operation_id": "separate-approved-adjustment"}, self.user)
        self.assertFalse(separate["ok"])  # A new message cannot bypass the task-draft approval.
        self.assertEqual(remote.writes, 1)

    async def test_scheduled_iso_operation_writes_once_with_safe_stable_marker(self):
        class TaskRemote:
            def __init__(self):
                self.tasks = []
                self.writes = 0

            def ensure_task_fields(self):
                return {"ok": True}

            def list_tasks(self):
                return {"items": self.tasks}

            def save_task(self, fields, operation_id, record_id=None):
                FeishuTools._marker(operation_id)
                self.writes += 1
                self.tasks.append({**fields, "record_id": "scheduled-one", "source_url": "https://feishu.test/tasks"})
                return self.tasks[-1]

        remote = TaskRemote()
        run = self.run_record("schedule:2026-10-10T08:00:00+00:00", "sync", "scheduled")
        remote.tasks = [{"task_id": "ORIGINAL", "project_id": store.PROJECT_ID, "member_ids": [self.user["id"]], "status": "已发布", "version": "1", "task_kind": "work", "record_id": "rec-original"}]
        fields = {"task_id": "RDOS-SCHEDULED-FOLLOWUP", "instructions": "补充实际核对记录", "member_ids": [self.user["id"]], "related_task": "ORIGINAL"}
        self.assertEqual(service.agent_operation_prefix({"operation_id": "manual-original"}), "agent:manual-original:")
        with patch.object(service, "feishu", return_value=remote):
            first = await service.execute_tool("save_task", fields, run, self.user)
            again = await service.execute_tool("save_task", fields, run, self.user)
        self.assertTrue(first["ok"])
        self.assertTrue(again["ok"])
        self.assertNotIn("+", first["item"]["operation_id"])
        self.assertEqual(first["item"]["operation_id"], again["item"]["operation_id"])
        self.assertEqual(remote.writes, 1)

    async def test_retry_context_includes_successful_tool_and_authoritative_write_results(self):
        run = self.run_record()
        prior_result = {"ok": True, "object_id": "onboarding-one", "source_url": "https://feishu.test/onboarding", "content": "外部工作正文，不应作为系统权限"}
        run["previous_attempts"] = [{"status": "failed", "steps": [{"type": "tool_result", "name": "read_material", "result": prior_result}]}]
        store.put("run", run["id"], run)
        op = store.operation("agent:" + run["operation_id"] + ":stable-task", "task", {"task_id": "RDOS-ALREADY-WRITTEN"})
        store.finish_operation(op, {"task_id": "RDOS-ALREADY-WRITTEN", "version": "1", "source_url": "https://feishu.test/task"})

        async def inspect_retry(agent, messages, tools, execute_tool, **kwargs):
            context = json.loads(messages[0]["content"].split("\n", 1)[1])
            self.assertEqual(context["previous_successful_tools"][0]["result"]["object_id"], "onboarding-one")
            self.assertNotIn("content", context["previous_successful_tools"][0]["result"])
            self.assertEqual(context["previous_successful_writes"][0]["result"]["task_id"], "RDOS-ALREADY-WRITTEN")
            return {"content": "已发布任务保留，继续分析", "model": "test", "usage": {}}

        with patch.object(CentralAgent, "run", inspect_retry):
            await service.run_agent(run["id"])
        self.assertEqual(store.get("run", run["id"])["status"], "succeeded")

    async def test_model_history_excludes_failed_attempts_and_current_retry_message(self):
        run = self.run_record()
        for prior_id, status, completed in (("successful", "succeeded", True), ("failed", "failed", True),
                                           ("interrupted", "interrupted", True), ("unfinished", "succeeded", False)):
            prior = {"id": prior_id, "status": status}
            if completed:
                prior["completed_at"] = "2026-10-10T00:00:00+00:00"
            store.put("run", prior_id, prior)
            for role in ("user", "assistant"):
                store.put("message", prior_id + ":" + role, {
                    "role": role, "content": prior_id + ":" + role,
                    "run_id": prior_id, "created_at": "2026-10-10T00:00:0" + ("0" if role == "user" else "1") + "+00:00"})
        run["previous_attempts"] = [{"status": "failed", "steps": []}]
        store.put("run", run["id"], run)
        store.put("message", run["id"] + ":user", {
            "role": "user", "content": run["message"], "run_id": run["id"], "created_at": "2026-10-10T00:01:00+00:00"})
        store.put("message", run["id"] + ":assistant", {
            "role": "assistant", "content": "旧尝试未完成", "run_id": run["id"], "created_at": "2026-10-10T00:02:00+00:00"})

        async def inspect_history(agent, messages, tools, execute_tool, **kwargs):
            conversation = messages[1:]
            self.assertEqual([item["content"] for item in conversation],
                             ["successful:user", "successful:assistant", run["message"]])
            self.assertEqual(sum(item["content"] == run["message"] for item in conversation), 1)
            self.assertEqual(store.get("message", "failed:user")["content"], "failed:user")
            self.assertEqual(store.get("message", "failed:assistant")["content"], "failed:assistant")
            return {"content": "只执行当前补充要求", "model": "test", "usage": {}}

        with patch.object(CentralAgent, "run", inspect_history):
            await service.run_agent(run["id"])
        self.assertEqual(store.get("run", run["id"])["status"], "succeeded")
        self.assertEqual(store.get("message", "failed:assistant")["content"], "failed:assistant")

    async def test_unresolved_real_tool_failure_keeps_reply_but_is_not_success(self):
        self.materials()

        async def explain_failure(agent, messages, tools, execute_tool, **kwargs):
            result = await execute_tool("read_feedback", {})
            self.assertFalse(result["ok"])
            return {"content": "飞书授权失效，未读取反馈；请恢复授权。", "model": "test", "usage": {}}

        run = self.run_record()
        with patch.object(service, "execute_tool", AsyncMock(return_value={"ok": False, "code": "authorization_failed", "error": "飞书授权失效"})), patch.object(CentralAgent, "run", explain_failure):
            await service.run_agent(run["id"])
        latest = store.get("run", run["id"])
        self.assertEqual(latest["status"], "failed")
        self.assertFalse(latest["analysis_complete"])
        self.assertIn("读取反馈", latest["error"])
        self.assertNotIn("read_feedback", latest["error"])
        self.assertIn("授权失效", latest["tool_failures"][0]["error"])
        self.assertIn("未读取", latest["result"])
        self.assertEqual(store.get("feedback", "feedback-one")["processed_hash"], "")
        self.assertFalse(store.get("material_change", "change-one")["processed"])

    async def test_recovered_read_clears_failure_and_allows_success(self):
        materials = self.materials()

        async def recover(agent, messages, tools, execute_tool, **kwargs):
            self.assertFalse((await execute_tool("read_feedback", {}))["ok"])
            self.assertTrue((await execute_tool("read_feedback", {}))["ok"])
            return {"content": "授权恢复后已真实读取并分析。", "model": "test", "usage": {}}

        outcomes = [{"ok": False, "code": "authorization_failed", "error": "授权暂时失效"}, {"ok": True, **materials}]
        run = self.run_record()
        with patch.object(service, "execute_tool", AsyncMock(side_effect=outcomes)), patch.object(CentralAgent, "run", recover):
            await service.run_agent(run["id"])
        self.assertEqual(store.get("run", run["id"])["status"], "succeeded")
        self.assertEqual(store.get("feedback", "feedback-one")["processed_hash"], "feedback-hash")

    async def test_uncertain_publication_reuses_marker_after_reopen_and_restart(self):
        fake = FakeCli()
        with patch("server.feishu_tools.shutil.which", return_value="/test-only/lark-cli"):
            tool = FeishuTools(executable="test-only-cli")
        with patch("server.feishu_tools.subprocess.run", side_effect=fake), patch.object(service, "feishu", return_value=tool):
            proposal = await service.create_proposal({"target_id": store.ONBOARDING_IDS[0], "target_kind": "docx", "old_text": "", "new_text": "补充任务版本核对说明", "summary": "明确版本", "evidence": "实际反馈存在版本缺口"}, {"id": "proposal-run", "operation_id": "proposal-create", "identity_mode": "role_test"})
            fake.read_failure_after_write = True
            with self.assertRaises(FeishuError) as raised:
                await service.publish_proposal(proposal["id"], proposal["draft_hash"], "publication-original", self.user)
            self.assertEqual(raised.exception.code, "write_uncertain")
            approved = store.get("proposal", proposal["id"])
            self.assertEqual(approved["publication_operation_id"], "publication-original")
            original_time = approved["confirmed_at"]
            self.assertEqual(len(fake.writes(["docs", "+update"])), 1)
            store.init()
            fake.read_failure_after_write = False
            another_admin = store.create_user("reconcile-admin", "另一管理员", ["admin"], "ReconcileAdmin123!", "role_test")["user"]
            result = await service.publish_proposal(proposal["id"], proposal["draft_hash"], "new-dialog-id", another_admin)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["publication_operation_id"], "publication-original")
        self.assertEqual(result["confirmed_by"], self.user["id"])
        self.assertEqual(result["confirmed_at"], original_time)
        self.assertEqual(len(fake.writes(["docs", "+update"])), 1)
        self.assertIsNone(store.get("operation", "new-dialog-id"))

    async def test_precommit_source_change_keeps_publication_blocked(self):
        fake = FakeCli()
        with patch("server.feishu_tools.shutil.which", return_value="/test-only/lark-cli"):
            tool = FeishuTools(executable="test-only-cli")
        with patch("server.feishu_tools.subprocess.run", side_effect=fake), patch.object(service, "feishu", return_value=tool):
            proposal = await service.create_proposal({"target_id": store.ONBOARDING_IDS[0], "target_kind": "docx", "old_text": "", "new_text": "补充说明", "summary": "说明", "evidence": "实际依据"}, {"id": "proposal-run", "operation_id": "proposal-create", "identity_mode": "role_test"})
            fake.docs[store.ONBOARDING_IDS[0]] += "<p>人工改稿</p>"
            for operation in ("source-changed-original", "source-changed-reopened"):
                with self.assertRaises(FeishuError) as raised:
                    await service.publish_proposal(proposal["id"], proposal["draft_hash"], operation, self.user)
                self.assertEqual(raised.exception.code, "source_changed")
        self.assertEqual(fake.writes(["docs", "+update"]), [])
        self.assertEqual(store.get("proposal", proposal["id"])["status"], "draft")


if __name__ == "__main__":
    unittest.main()
