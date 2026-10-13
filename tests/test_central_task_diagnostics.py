"""C workbench task drafts and diagnostics use isolated SQLite and remote fixtures."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from server import central_service as service, central_store as store, db
from server.feishu_tools import FeishuError


class TaskDraftAndDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"BAITE_DB_PATH": str(Path(self.temp.name) / "central.db"),
                                         "BAITE_ADMIN_USERNAME": "test-admin", "BAITE_ADMIN_PASSWORD": "DraftTest123!",
                                         "BAITE_LLM_API_KEY": ""})
        self.env.start()
        db.init_db()
        store.init()
        self.admin = store.users()[0]
        self.researcher = store.create_user("tester", "研究员", ["researcher"], "TestMember123!", "role_test")["user"]
        store.put("project", "project-one", {"id": "project-one", "name": "测试项目", "member_ids": [self.researcher["id"]],
                                             "folder_id": "folder-one", "feedback_id": "feedback-one",
                                             "feedback_url": "https://example.test/docx/feedback-one", "status": "active"})
        self.old_lock = service.WRITE_LOCK
        service.WRITE_LOCK = asyncio.Lock()
        self.items = []
        self.remote = MagicMock()
        self.remote.list_tasks.side_effect = lambda: {"items": [dict(item) for item in self.items]}

        def write(fields, operation_id, record_id=None):
            item = {**fields, "record_id": record_id or "rec-" + fields["task_id"],
                    "operation_id": operation_id, "source_url": "https://example.test/task"}
            if record_id:
                self.items[:] = [item if old.get("record_id") == record_id else old for old in self.items]
            else:
                self.items.append(item)
            return {"task": item}

        self.remote.save_task.side_effect = write
        self.source = patch.object(service, "feishu", return_value=self.remote)
        self.source.start()

    def tearDown(self):
        self.source.stop()
        service.WRITE_LOCK = self.old_lock
        self.env.stop()
        self.temp.cleanup()

    def run_context(self, op="prepare-one"):
        return {"id": "run-one", "operation_id": op, "project_id": "project-one", "identity_mode": "role_test"}

    def original(self):
        item = {"task_id": "WORK-1", "project_id": "project-one", "version": "2", "status": "已发布",
                "instructions": "原要求", "member_ids": [self.researcher["id"]], "task_kind": "work", "stage": "",
                "nature": "一次性", "related_task": "", "record_id": "rec-work-1", "operation_id": "old-op"}
        self.items.append(item)
        return item

    async def test_create_update_withdraw_require_confirmation_and_capture_remote_baseline(self):
        created = service.prepare_task({"task_id": "NEW-1", "instructions": "完成实际盘点", "member_ids": [self.researcher["id"]],
                                        "draft_action": "create"}, self.run_context(), self.admin, existing_task={})
        self.assertEqual((created["draft_action"], created["base_task_hash"], created["version"]), ("create", "", "1"))
        self.remote.save_task.assert_not_called()
        published = await service.publish_task_draft(created["id"], created["draft_hash"], "publish-new", self.admin)
        self.assertEqual(published["status"], "published")
        self.assertEqual(self.remote.save_task.call_count, 1)
        original = self.original()
        changed = service.prepare_task({"task_id": "WORK-1", "instructions": "补充产出版本", "draft_action": "update",
                                        "reason": "原任务缺少可核对的成果版本"}, self.run_context("prepare-update"), self.admin,
                                       existing_task=original)
        self.assertEqual((changed["base_task_version"], changed["base_task_hash"]), ("2", service.task_body_hash(original)))
        self.assertEqual(changed["reason"], "原任务缺少可核对的成果版本")
        updated = await service.publish_task_draft(changed["id"], changed["draft_hash"], "publish-update", self.admin)
        self.assertEqual((updated["publication"]["version"], updated["publication"]["record_id"]), ("3", "rec-work-1"))
        stopped = service.prepare_task({"task_id": "WORK-1", "draft_action": "withdraw", "reason": "这项工作已经取消"},
                                       self.run_context("prepare-stop"), self.admin, existing_task=self.items[-1])
        self.assertEqual((stopped["target_status"], stopped["instructions"]), ("停用", "补充产出版本"))
        result = await service.publish_task_draft(stopped["id"], stopped["draft_hash"], "publish-stop", self.admin)
        self.assertEqual((result["publication"]["version"], result["publication"]["status"]), ("4", "停用"))
        await service.publish_task_draft(stopped["id"], stopped["draft_hash"], "another-confirmation", self.admin)
        self.assertEqual(self.remote.save_task.call_count, 3)

    async def test_stale_remote_task_unlocks_draft_without_writing(self):
        original = self.original()
        draft = service.prepare_task({"task_id": "WORK-1", "instructions": "修订任务", "draft_action": "update",
                                      "reason": "反馈发现要求不清楚"}, self.run_context(), self.admin, existing_task=original)
        original["instructions"] = "其他人刚修改的正文"
        with self.assertRaises(service.TaskVersionChanged) as caught:
            await service.publish_task_draft(draft["id"], draft["draft_hash"], "publish-stale", self.admin)
        self.assertEqual(caught.exception.code, "task_version_changed")
        saved = store.get("task_draft", draft["id"])
        self.assertEqual(saved["status"], "draft")
        self.assertTrue(saved["needs_review"])
        self.assertNotIn("publication_operation_id", saved)
        self.assertEqual(saved["instructions"], "修订任务")
        self.remote.save_task.assert_not_called()

    async def test_uncertain_remote_write_keeps_original_operation_for_recheck(self):
        original = self.original()
        draft = service.prepare_task({"task_id": "WORK-1", "instructions": "修订任务", "draft_action": "update",
                                      "reason": "反馈发现要求不清楚"}, self.run_context(), self.admin, existing_task=original)
        def uncertain(fields, operation_id, record_id=None):
            self.items[:] = [{**fields, "record_id": record_id, "operation_id": operation_id, "source_url": "https://example.test/task"}]
            raise FeishuError("write_uncertain", "回读超时")
        self.remote.save_task.side_effect = uncertain
        with self.assertRaises(FeishuError):
            await service.publish_task_draft(draft["id"], draft["draft_hash"], "publish-uncertain", self.admin)
        self.assertEqual(store.get("task_draft", draft["id"])["publication_operation_id"], "publish-uncertain")
        self.assertEqual(store.get("operation", "publish-uncertain")["status"], "uncertain")
        self.remote.save_task.side_effect = lambda fields, operation_id, record_id=None: {"task": self.items[0]}
        resumed = await service.publish_task_draft(draft["id"], draft["draft_hash"], "new-click-id", self.admin)
        self.assertEqual(resumed["status"], "published")
        self.assertEqual(resumed["publication"]["operation_id"], "publish-uncertain")
        self.assertEqual(self.remote.save_task.call_count, 1)  # Remote readback, not a second mutation.

    async def test_uncertain_task_not_written_retries_saved_payload_against_unchanged_base(self):
        original = self.original()
        draft = service.prepare_task({"task_id": "WORK-1", "instructions": "修订任务", "draft_action": "update",
                                      "reason": "反馈发现要求不清楚"}, self.run_context(), self.admin, existing_task=original)
        write = self.remote.save_task.side_effect
        self.remote.save_task.side_effect = FeishuError("write_uncertain", "写入请求超时，结果未确认")
        with self.assertRaises(FeishuError):
            await service.publish_task_draft(draft["id"], draft["draft_hash"], "publish-original", self.admin)
        self.assertEqual(self.items[0]["version"], "2")
        self.assertEqual(store.get("operation", "publish-original")["remote_baseline"]["body_hash"], service.task_body_hash(original))
        self.remote.save_task.side_effect = write
        result = await service.publish_task_draft(draft["id"], draft["draft_hash"], "ignored-new-click", self.admin)
        self.assertEqual((result["publication"]["version"], result["publication"]["operation_id"]),
                         ("3", "publish-original"))
        self.assertEqual(self.remote.save_task.call_count, 2)

    async def test_uncertain_task_then_newer_remote_version_never_overwrites(self):
        original = self.original()
        draft = service.prepare_task({"task_id": "WORK-1", "instructions": "修订任务", "draft_action": "update",
                                      "reason": "反馈发现要求不清楚"}, self.run_context(), self.admin, existing_task=original)
        def uncertain(fields, operation_id, record_id=None):
            self.items[:] = [{**fields, "record_id": record_id, "operation_id": operation_id,
                             "source_url": "https://example.test/task"}]
            raise FeishuError("write_uncertain", "回读超时")
        self.remote.save_task.side_effect = uncertain
        with self.assertRaises(FeishuError):
            await service.publish_task_draft(draft["id"], draft["draft_hash"], "publish-original", self.admin)
        self.items[0].update(version="4", instructions="其他人后续改动", operation_id="later-operation")
        with self.assertRaises(FeishuError) as caught:
            await service.publish_task_draft(draft["id"], draft["draft_hash"], "ignored-new-click", self.admin)
        self.assertEqual(caught.exception.code, "write_uncertain")
        self.assertIn("不会覆盖", str(caught.exception))
        self.assertEqual((self.items[0]["version"], self.items[0]["instructions"]), ("4", "其他人后续改动"))
        self.assertEqual(self.remote.save_task.call_count, 1)
        self.assertEqual(store.get("task_draft", draft["id"])["publication_operation_id"], "publish-original")

    async def test_agent_supplement_retry_reuses_original_operation_hash(self):
        self.original()
        args = {"project_id": "project-one", "task_id": "SUP-1", "related_task": "WORK-1",
                "member_ids": [self.researcher["id"]], "instructions": "补上成果位置", "reason": "反馈缺少成果链接"}
        run = self.run_context("agent-supplement")
        write = self.remote.save_task.side_effect
        self.remote.save_task.side_effect = FeishuError("write_uncertain", "写入请求超时")
        first = await service.execute_tool("save_task", args, run, self.admin)
        self.assertFalse(first["ok"])
        self.assertEqual(first["code"], "write_uncertain")
        operation_id = next(op["id"] for op in store.items("operation") if op["id"].startswith("agent:"))
        prepared = store.get("operation", operation_id)
        self.assertEqual(prepared["status"], "uncertain")
        self.assertEqual(prepared["remote_baseline"]["action"], "create")
        self.remote.save_task.side_effect = write
        resumed = await service.execute_tool("save_task", args, run, self.admin)
        self.assertTrue(resumed["ok"], resumed)
        self.assertEqual(resumed["item"]["operation_id"], operation_id)
        self.assertEqual(self.remote.save_task.call_count, 2)

    async def test_agent_supplement_rechecks_related_task_before_first_write(self):
        self.original()
        args = {"project_id": "project-one", "task_id": "SUP-1", "related_task": "WORK-1",
                "member_ids": [self.researcher["id"]], "instructions": "补上成果位置"}
        reads = 0
        def list_tasks():
            nonlocal reads
            reads += 1
            if reads == 2:
                self.items[0].update(version="3", instructions="原任务被别人更新")
            return {"items": [dict(item) for item in self.items]}
        self.remote.list_tasks.side_effect = list_tasks
        result = await service.execute_tool("save_task", args, self.run_context("first-write-race"), self.admin)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "task_version_changed")
        self.remote.save_task.assert_not_called()

    async def test_agent_uncertain_retry_stops_when_related_task_advanced(self):
        self.original()
        args = {"project_id": "project-one", "task_id": "SUP-1", "related_task": "WORK-1",
                "member_ids": [self.researcher["id"]], "instructions": "补上成果位置"}
        self.remote.save_task.side_effect = FeishuError("write_uncertain", "请求超时")
        run = self.run_context("related-advanced")
        first = await service.execute_tool("save_task", args, run, self.admin)
        self.assertEqual(first["code"], "write_uncertain")
        self.items[0].update(version="3", instructions="原任务被别人更新", operation_id="later-operation")
        second = await service.execute_tool("save_task", args, run, self.admin)
        self.assertFalse(second["ok"])
        self.assertEqual(second["code"], "write_uncertain")
        self.assertIn("引用旧版本", second["error"])
        self.assertEqual(self.remote.save_task.call_count, 1)

    async def test_agent_uncertain_written_supplement_readback_survives_related_update(self):
        self.original()
        args = {"project_id": "project-one", "task_id": "SUP-1", "related_task": "WORK-1",
                "member_ids": [self.researcher["id"]], "instructions": "补上成果位置"}
        def written_but_unconfirmed(fields, operation_id, record_id=None):
            self.items.append({**fields, "record_id": "rec-supplement", "operation_id": operation_id,
                               "source_url": "https://example.test/task"})
            raise FeishuError("write_uncertain", "回读超时")
        self.remote.save_task.side_effect = written_but_unconfirmed
        run = self.run_context("written-related-advanced")
        first = await service.execute_tool("save_task", args, run, self.admin)
        self.assertEqual(first["code"], "write_uncertain")
        self.items[0].update(version="3", instructions="原任务被别人更新", operation_id="later-operation")
        second = await service.execute_tool("save_task", args, run, self.admin)
        self.assertTrue(second["ok"], second)
        self.assertTrue(second["rechecked"])
        self.assertEqual(second["item"]["task_id"], "SUP-1")
        self.assertEqual(self.remote.save_task.call_count, 1)

    async def test_periodic_requirement_prepares_a_draft_without_remote_write(self):
        result = await service.execute_tool("prepare_task", {"project_id": "project-one", "requirement_kind": "periodic_feedback",
                                                            "member_ids": [self.researcher["id"]]}, self.run_context(), self.admin)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["requires_confirmation"])
        self.assertIn("不生成日报", result["item"]["instructions"])
        self.assertEqual(result["item"]["nature"], "周期")
        self.remote.save_task.assert_not_called()

    def test_diagnostics_is_admin_only_and_does_not_return_raw_content(self):
        store.put("feedback", "feedback-one", {"id": "feedback-one", "content": "private-example-secret", "source_readback": True,
                                               "reported_at": "2026-10-13T09:00:00+08:00", "last_read_at": "2026-10-13T10:00:00+08:00"})
        store.put("operation", "write-one", {"id": "write-one", "action": "task", "status": "uncertain", "payload_hash": "private-example-secret"})
        with self.assertRaises(PermissionError):
            service.runtime_diagnostics(self.researcher)
        result = service.runtime_diagnostics(self.admin)
        self.assertEqual(result["online"]["feishu"]["unresolved_operations"], 1)
        self.assertEqual(result["local_agents"]["feedback_readback_records"], 1)
        self.assertIn("未直接检查研究员电脑", result["scope"])
        self.assertNotIn("private-example-secret", str(result))

    async def test_template_tool_proposal_is_confirmed_before_local_version_becomes_active(self):
        from server import central_templates
        before = central_templates.snapshot("project.feedback")
        org_run = {"id": "org-run", "operation_id": "template-proposal", "project_id": "", "identity_mode": "role_test"}
        read = await service.execute_tool("read_material", {"object_id": "project.feedback", "kind": "template"},
                                          org_run, self.admin)
        self.assertTrue(read["ok"], read)
        self.assertEqual(read["version"], before["version"])
        proposed = await service.execute_tool("propose_change", {"target_id": "project.feedback", "target_kind": "template",
            "summary": "把反馈说明写得更明确", "old_text": before["content"],
            "new_text": before["content"] + "请注明共享成果的状态。", "evidence": "隔离测试反馈显示成果状态未写清"},
            org_run, self.admin)
        self.assertTrue(proposed["ok"], proposed)
        item = proposed["item"]
        self.assertEqual(item["status"], "draft")
        self.assertEqual(central_templates.snapshot("project.feedback")["version"], before["version"])
        published = await service.publish_proposal(item["id"], item["draft_hash"], "approve-template", self.admin)
        self.assertEqual(published["status"], "published")
        self.assertEqual(central_templates.snapshot("project.feedback")["version"], before["version"] + 1)
        again = await service.publish_proposal(item["id"], item["draft_hash"], "another-click", self.admin)
        self.assertEqual(again["publication"]["version"], published["publication"]["version"])
        self.remote.publish_patch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
