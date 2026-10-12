"""Project routing and approval checks; remote tools are explicit isolated fixtures."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from server import central_projects as projects, central_service as service, central_store as store, db
from server.feishu_tools import FeishuError
from server.central_agent import CentralAgent


class CentralProjectTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"BAITE_DB_PATH": str(Path(self.temp.name) / "projects.db"),
            "BAITE_ADMIN_USERNAME": "test-admin", "BAITE_ADMIN_PASSWORD": "ProjectTest123!"})
        self.env.start()
        db.init_db()
        store.init()
        self.admin = store.users()[0]
        self.first = store.create_user("first", "同事一", ["researcher"], "FirstTest123!")["user"]
        self.other = store.create_user("other", "同事二", ["researcher"], "OtherTest123!")["user"]
        self.lock = service.WRITE_LOCK
        service.WRITE_LOCK = asyncio.Lock()
        self.run_lock = service.RUN_LOCK
        service.RUN_LOCK = asyncio.Lock()
        self.project = {"id": "project-one", "name": "项目一", "member_ids": [self.first["id"]],
                        "folder_id": "folder-one", "folder_url": store.FEISHU_HOST + "/drive/folder/folder-one",
                        "feedback_id": "feedback-one", "feedback_url": store.FEISHU_HOST + "/docx/feedback-one", "status": "active"}
        store.put("project", self.project["id"], self.project)

    def tearDown(self):
        service.WRITE_LOCK = self.lock
        service.RUN_LOCK = self.run_lock
        self.env.stop()
        self.temp.cleanup()

    def fields(self):
        return {"name": "新项目", "description": "小范围试用", "member_ids": [self.first["id"]],
                "folder_url": store.FEISHU_HOST + "/drive/folder/folder-new"}

    def test_baite_feishu_folder_domain_is_accepted_without_rewriting_link(self):
        link = projects.parse_link("https://shbebetter.feishu.cn/drive/folder/projectFolder?from=from_copylink", "folder")
        self.assertEqual(link["id"], "projectFolder")
        self.assertEqual(link["source_url"], "https://shbebetter.feishu.cn/drive/folder/projectFolder")
        with self.assertRaises(ValueError):
            projects.parse_link("https://shbebetter.feishu.cn.evil.test/drive/folder/projectFolder", "folder")

    async def test_existing_project_folder_can_also_contain_organization_materials(self):
        fields = {**self.fields(), "folder_url": "https://shbebetter.feishu.cn/drive/folder/J8fdfE94klWRLwdl4gqcyGIZn1g"}
        remote = MagicMock()
        remote.read_source.return_value = {"ok": True, "items": []}
        remote.find_project_feedback.return_value = None
        remote.ensure_project_feedback.return_value = {"ok": True, "object_id": "feedback-new",
            "source_url": "https://shbebetter.feishu.cn/docx/feedback-new"}
        with patch.object(service, "feishu", return_value=remote):
            created = await projects.save_project(fields, "existing-project-folder", self.admin)
        self.assertEqual(created["folder_url"], "https://shbebetter.feishu.cn/drive/folder/J8fdfE94klWRLwdl4gqcyGIZn1g")
        self.assertEqual(created["feedback_url"], "https://shbebetter.feishu.cn/docx/feedback-new")
        remote.ensure_project_feedback.assert_called_once_with("J8fdfE94klWRLwdl4gqcyGIZn1g", "新项目 · 工作反馈", "project:" + created["id"])

    async def test_restore_existing_project_and_feedback_without_creating_document(self):
        fields = {**self.fields(), "project_id": "prj_previous_trial_01",
                  "feedback_url": store.FEISHU_HOST + "/docx/feedback-previous"}
        remote = MagicMock()
        remote.read_source.side_effect = lambda token, kind: (
            {"ok": True, "object_id": "folder-new", "items": []}
            if kind == "folder" else {"ok": True, "object_id": "feedback-previous", "content": "原有反馈"})
        with patch.object(service, "feishu", return_value=remote):
            restored = await projects.save_project(fields, "restore-previous", self.admin)
            again = await projects.save_project(fields, "restore-previous", self.admin)
        self.assertEqual(restored, again)
        self.assertEqual(restored["id"], "prj_previous_trial_01")
        self.assertEqual(restored["feedback_id"], "feedback-previous")
        self.assertEqual(restored["status"], "active")
        self.assertEqual(remote.read_source.call_count, 2)
        remote.ensure_project_feedback.assert_not_called()
        self.assertEqual(projects.project(restored["id"], self.first)["feedback_id"], "feedback-previous")

    async def test_restore_rejects_unreadable_folder_or_document_and_other_project_id(self):
        fields = {**self.fields(), "project_id": "prj_previous_trial_02",
                  "feedback_url": store.FEISHU_HOST + "/docx/feedback-previous"}
        remote = MagicMock()
        remote.read_source.return_value = {"ok": False, "object_id": "folder-new"}
        with patch.object(service, "feishu", return_value=remote):
            with self.assertRaisesRegex(ValueError, "无法读取指定项目文件夹"):
                await projects.save_project(fields, "restore-missing", self.admin)
            self.assertEqual(store.get("project", fields["project_id"])["status"], "failed")
            remote.ensure_project_feedback.assert_not_called()
            with self.assertRaisesRegex(ValueError, "已被其他项目使用"):
                await projects.save_project(fields, "other-restore-operation", self.admin)
            remote.read_source.side_effect = lambda token, kind: (
                {"ok": True, "object_id": "folder-new", "items": []}
                if kind == "folder" else {"ok": False, "object_id": "feedback-previous"})
            with self.assertRaisesRegex(ValueError, "无法读取指定项目反馈文档"):
                await projects.save_project(fields, "restore-missing", self.admin)
            remote.read_source.side_effect = lambda token, kind: (
                {"ok": True, "object_id": "folder-new", "items": []}
                if kind == "folder" else {"ok": True, "object_id": "feedback-previous", "content": "原有反馈"})
            restored = await projects.save_project(fields, "restore-missing", self.admin)
        self.assertEqual(restored["status"], "active")
        remote.ensure_project_feedback.assert_not_called()

    async def test_restore_rejects_wrong_link_and_feedback_already_assigned(self):
        fields = {**self.fields(), "project_id": "prj_previous_trial_03",
                  "feedback_url": "https://unrelated.example/docx/feedback-previous"}
        with patch.object(service, "feishu") as remote:
            with self.assertRaises(ValueError):
                await projects.save_project(fields, "restore-wrong-host", self.admin)
            self.assertIsNone(store.get("operation", "restore-wrong-host"))
            remote.assert_not_called()
        fields["feedback_url"] = store.FEISHU_HOST + "/docx/feedback-previous"
        with patch.object(service, "feishu") as remote:
            with self.assertRaises(PermissionError):
                await projects.save_project(fields, "restore-not-admin", self.first)
            self.assertIsNone(store.get("operation", "restore-not-admin"))
            remote.assert_not_called()
        occupied = "prj_occupied_01"
        store.put("project", occupied, {"id": occupied, "name": "已有项目", "status": "active"})
        with patch.object(service, "feishu") as remote:
            with self.assertRaisesRegex(ValueError, "已被其他项目使用"):
                await projects.save_project({**fields, "project_id": occupied}, "restore-occupied", self.admin)
            remote.assert_not_called()
        self.project["feedback_id"] = "feedback-previous"
        store.put("project", self.project["id"], self.project)
        with patch.object(service, "feishu") as remote:
            with self.assertRaisesRegex(ValueError, "已关联其他项目"):
                await projects.save_project(fields, "restore-duplicate-feedback", self.admin)
            remote.assert_not_called()

    def test_project_names_only_include_existing_project_members(self):
        self.project["member_ids"] = [self.first["id"], self.admin["id"], "unknown-member"]
        store.put("project", self.project["id"], self.project)
        expected = {self.first["id"]: self.first["display_name"], self.admin["id"]: self.admin["display_name"]}
        self.assertEqual(projects.project(self.project["id"], self.first)["member_names"], expected)
        listed = next(item for item in projects.list_projects(self.first) if item["id"] == self.project["id"])
        self.assertEqual(listed["member_names"], expected)
        self.assertNotIn(self.project["id"], [item["id"] for item in projects.list_projects(self.other)])
        with self.assertRaises(PermissionError):
            projects.project(self.project["id"], self.other)
        self.assertNotIn("member_names", store.get("project", self.project["id"]))

    def test_disabled_user_is_not_an_active_project_member(self):
        shared_admin = store.create_user("shared_admin", "共享管理员", ["admin"], "SharedTest123!")["user"]
        self.project["member_ids"] = [self.first["id"], shared_admin["id"]]
        store.put("project", self.project["id"], self.project)
        store.update_user(shared_admin["id"], {"enabled": False})

        current = projects.project(self.project["id"])
        self.assertEqual(current["member_ids"], [self.first["id"]])
        self.assertEqual(current["member_names"], {self.first["id"]: self.first["display_name"]})
        listed = next(item for item in projects.list_projects(self.admin) if item["id"] == self.project["id"])
        self.assertEqual(listed["member_ids"], [self.first["id"]])
        self.assertEqual(store.get("project", self.project["id"])["member_ids"], self.project["member_ids"])

        default = store.get("project", store.PROJECT_ID)
        default.pop("member_ids", None)
        store.put("project", store.PROJECT_ID, default)
        self.assertNotIn(shared_admin["id"], projects.project(store.PROJECT_ID)["member_ids"])
        store.update_user(shared_admin["id"], {"enabled": True})
        self.assertIn(shared_admin["id"], projects.project(store.PROJECT_ID)["member_ids"])
        self.assertIn(shared_admin["id"], projects.project(self.project["id"])["member_ids"])

    async def test_project_create_readbacks_and_repeat_reuses_same_document(self):
        remote = MagicMock()
        remote.read_source.return_value = {"ok": True, "items": []}
        remote.find_project_feedback.return_value = None
        remote.ensure_project_feedback.return_value = {"ok": True, "object_id": "feedback-new", "source_url": store.FEISHU_HOST + "/docx/feedback-new"}
        with patch.object(service, "feishu", return_value=remote):
            first = await projects.save_project(self.fields(), "create-one", self.admin)
            again = await projects.save_project(self.fields(), "create-one", self.admin)
        self.assertEqual(first["feedback_id"], "feedback-new")
        self.assertEqual(first, again)
        self.assertEqual(first["status"], "active")
        remote.ensure_project_feedback.assert_called_once()
        self.assertEqual(remote.read_source.call_args.args, ("folder-new", "folder"))
        self.assertIn(first["id"], [item["id"] for item in projects.list_projects(self.first)])
        self.assertNotIn(first["id"], [item["id"] for item in projects.list_projects(self.other)])
        with self.assertRaises(PermissionError):
            projects.project(first["id"], self.other)

    async def test_uncertain_create_records_failure_and_retry_retains_remote_marker(self):
        remote = MagicMock()
        remote.read_source.return_value = {"ok": True}
        remote.find_project_feedback.return_value = None
        remote.ensure_project_feedback.side_effect = [FeishuError("write_uncertain", "文档写入尚未核对"),
            {"ok": True, "object_id": "already-created", "source_url": store.FEISHU_HOST + "/docx/already-created"}]
        with patch.object(service, "feishu", return_value=remote):
            with self.assertRaises(FeishuError):
                await projects.save_project(self.fields(), "create-retry", self.admin)
            pending = next(item for item in store.items("project") if item["name"] == "新项目")
            self.assertEqual(pending["status"], "failed")
            result = await projects.save_project(self.fields(), "create-retry", self.admin)
        self.assertEqual(result["id"], pending["id"])
        self.assertEqual(remote.ensure_project_feedback.call_args_list[0].args, remote.ensure_project_feedback.call_args_list[1].args)

    async def test_changed_folder_checks_old_creation_before_new_document(self):
        pending = {"id": "prj_pending_01", "name": "旧项目", "status": "failed",
                   "folder_id": "old-folder", "folder_url": store.FEISHU_HOST + "/drive/folder/old-folder",
                   "feedback_id": "", "member_ids": [self.first["id"]], "updated_at": "2020-01-01T00:00:00+00:00",
                   "feedback_setup": {"folder_id": "old-folder", "title": "旧项目 · 工作反馈",
                                      "operation_id": "project:prj_pending_01"}}
        store.put("project", pending["id"], pending)
        remote = MagicMock()
        remote.find_project_feedback.return_value = None
        remote.read_source.return_value = {"ok": True, "object_id": "new-folder", "items": []}
        remote.ensure_project_feedback.return_value = {"object_id": "feedback-new",
            "source_url": store.FEISHU_HOST + "/docx/feedback-new"}
        fields = {"name": "新项目", "member_ids": [self.first["id"]],
                  "folder_url": store.FEISHU_HOST + "/drive/folder/new-folder"}
        with patch.object(service, "feishu", return_value=remote):
            result = await projects.save_project(fields, "change-after-check", self.admin, pending["id"])
        self.assertEqual(result["folder_id"], "new-folder")
        self.assertEqual(result["feedback_id"], "feedback-new")
        self.assertNotIn("feedback_setup", result)
        self.assertEqual(remote.find_project_feedback.call_count, 3)
        remote.ensure_project_feedback.assert_called_once_with("new-folder", "新项目 · 工作反馈", "project:prj_pending_01")

    async def test_changed_folder_keeps_old_setup_if_old_read_fails(self):
        pending = {"id": "prj_pending_02", "name": "旧项目", "status": "failed",
                   "folder_id": "old-folder", "folder_url": store.FEISHU_HOST + "/drive/folder/old-folder",
                   "feedback_id": "", "member_ids": [self.first["id"]], "updated_at": "2020-01-01T00:00:00+00:00",
                   "feedback_setup": {"folder_id": "old-folder", "title": "旧项目 · 工作反馈",
                                      "operation_id": "project:prj_pending_02"}}
        store.put("project", pending["id"], pending)
        remote = MagicMock()
        remote.find_project_feedback.side_effect = FeishuError("remote_failed", "旧目录读取失败")
        with patch.object(service, "feishu", return_value=remote):
            with self.assertRaises(FeishuError):
                await projects.save_project({"name": "新项目", "member_ids": [self.first["id"]],
                    "folder_url": store.FEISHU_HOST + "/drive/folder/new-folder"}, "unreadable-old", self.admin, pending["id"])
        self.assertEqual(store.get("project", pending["id"])["feedback_setup"], pending["feedback_setup"])
        remote.ensure_project_feedback.assert_not_called()

    async def test_changed_folder_reuses_document_found_in_original_folder(self):
        pending = {"id": "prj_pending_03", "name": "旧项目", "status": "failed",
                   "folder_id": "old-folder", "folder_url": store.FEISHU_HOST + "/drive/folder/old-folder",
                   "feedback_id": "", "member_ids": [self.first["id"]], "updated_at": "2020-01-01T00:00:00+00:00",
                   "feedback_setup": {"folder_id": "old-folder", "title": "旧项目 · 工作反馈",
                                      "operation_id": "project:prj_pending_03"}}
        store.put("project", pending["id"], pending)
        remote = MagicMock()
        remote.find_project_feedback.return_value = {"object_id": "old-feedback", "source_url": store.FEISHU_HOST + "/docx/old-feedback"}
        remote.read_source.return_value = {"ok": True, "object_id": "new-folder", "items": []}
        fields = {"name": "新项目", "member_ids": [self.first["id"]],
                  "folder_url": store.FEISHU_HOST + "/drive/folder/new-folder"}
        with patch.object(service, "feishu", return_value=remote):
            result = await projects.save_project(fields, "change-found", self.admin, pending["id"])
        self.assertEqual(result["feedback_id"], "old-feedback")
        self.assertEqual(result["folder_id"], "new-folder")
        remote.find_project_feedback.assert_called_once_with("old-folder", "旧项目 · 工作反馈", "project:prj_pending_03")
        remote.ensure_project_feedback.assert_not_called()

    async def test_recent_uncertain_creation_does_not_switch_folders(self):
        pending = {"id": "prj_pending_recent", "name": "旧项目", "status": "failed",
                   "folder_id": "old-folder", "folder_url": store.FEISHU_HOST + "/drive/folder/old-folder",
                   "feedback_id": "", "member_ids": [self.first["id"]],
                   "feedback_setup": {"folder_id": "old-folder", "title": "旧项目 · 工作反馈",
                                      "operation_id": "project:prj_pending_recent", "started_at": db.utc_now()}}
        store.put("project", pending["id"], pending)
        with patch.object(service, "feishu") as remote:
            with self.assertRaisesRegex(ValueError, "刚尝试创建"):
                await projects.save_project({"name": "旧项目", "member_ids": [self.first["id"]],
                    "folder_url": store.FEISHU_HOST + "/drive/folder/new-folder"}, "recent-change", self.admin, pending["id"])
            remote.assert_not_called()
        self.assertEqual(store.get("project", pending["id"])["feedback_setup"], pending["feedback_setup"])

    async def test_preflight_failure_does_not_lock_new_folder(self):
        remote = MagicMock()
        remote.read_source.return_value = {"ok": True, "items": []}
        remote.find_project_feedback.side_effect = FeishuError("read_limit", "创建前目录读取失败")
        with patch.object(service, "feishu", return_value=remote):
            with self.assertRaises(FeishuError):
                await projects.save_project(self.fields(), "preflight-failed", self.admin)
        pending = next(item for item in store.items("project") if item["name"] == "新项目")
        self.assertNotIn("feedback_setup", pending)
        self.assertEqual(pending["status"], "failed")
        remote.ensure_project_feedback.assert_not_called()

    async def test_project_folder_change_preserves_existing_feedback(self):
        remote = MagicMock()
        with patch.object(service, "feishu", return_value=remote):
            result = await projects.save_project({"folder_url": store.FEISHU_HOST + "/drive/folder/another-folder"}, "folder-change", self.admin, self.project["id"])
        self.assertEqual(result["feedback_id"], "feedback-one")
        self.assertEqual(result["folder_id"], "another-folder")
        remote.ensure_project_feedback.assert_not_called()

    def document(self, task_line="", member=None, project_id="project-one", object_id="feedback-one"):
        return {"object_id": object_id, "source_url": store.FEISHU_HOST + "/docx/" + object_id,
            "content": f"## 工作反馈\n### 今日进展\n成员ID：{member or self.first['id']}\n项目ID：{project_id}\n本次运行标识：run-one\n反馈ID：feedback-entry\n{task_line}\n实际工作：完成资料核对。"}

    def test_project_feedback_without_task_and_document_identity(self):
        one = service.parse_feedback(self.document(), [], store.users(), "project-one")[0]
        self.assertEqual(one["version_status"], "项目反馈（未关联任务）")
        self.assertEqual(one["source_document_id"], "feedback-one")
        two = service.parse_feedback(self.document(object_id="another-feedback"), [], store.users(), "project-one")[0]
        self.assertNotEqual(one["id"], two["id"])
        wrong = service.parse_feedback(self.document(member=self.other["id"]), [], store.users(), "project-one")[0]
        self.assertEqual(wrong["version_status"], "归属或版本待核对")
        wrong_project = service.parse_feedback(self.document(project_id="rdos-self"), [], store.users(), "project-one")[0]
        self.assertEqual(wrong_project["version_status"], "归属或版本待核对")

    def test_connection_checks_are_not_work_results_and_onboarding_is_distinct(self):
        document = self.document()
        document["content"] += "\n记录类型：connection_check\n接入阶段：0"
        self.assertEqual(service.parse_feedback(document, [], store.users(), "project-one"), [])
        document["content"] = document["content"].replace("记录类型：connection_check", "记录类型：onboarding").replace("接入阶段：0", "接入阶段：1")
        feedback = service.parse_feedback(document, [], store.users(), "project-one")[0]
        self.assertEqual(feedback["record_type"], "onboarding")
        self.assertEqual(feedback["version_status"], "接入反馈（未关联任务）")

    def test_actual_task_reference_keeps_id_when_version_is_not_verified(self):
        task = {"task_id": "LOCAL-DEEPSEEK-ONB-01", "version": "1", "status": "已发布",
                "member_ids": [self.first["id"]], "task_kind": "onboarding"}
        for label in ("实际任务ID与版本", "实际任务 ID 与版本"):
            with self.subTest(label=label):
                document = self.document(f"{label}：LOCAL-DEEPSEEK-ONB-01（按成员指令执行，版本未能在线核实）")
                feedback = service.parse_feedback(document, [task], store.users(), "project-one")[0]
                self.assertEqual(feedback["task_id"], task["task_id"])
                self.assertEqual(feedback["task_version"], "")
                self.assertEqual(feedback["version_status"], "归属或版本待核对")
                self.assertEqual(feedback["record_type"], "onboarding")

    def test_project_template_not_applicable_stage_is_normal_project_feedback(self):
        document = self.document()
        document["content"] += "\n记录类型：project_update\n接入阶段：不适用"
        feedback = service.parse_feedback(document, [], store.users(), "project-one")[0]
        self.assertEqual(feedback["record_type"], "project_update")
        self.assertEqual(feedback["version_status"], "项目反馈（未关联任务）")
        document["content"] = document["content"].replace("记录类型：project_update", "记录类型：work")
        self.assertEqual(service.parse_feedback(document, [], store.users(), "project-one")[0]["record_type"], "work")

    async def test_project_collect_does_not_remove_other_project_records(self):
        store.put("task", "other-task", {"task_id": "other-task", "project_id": store.PROJECT_ID})
        store.put("feedback", "other-feedback", {"id": "other-feedback", "project_id": store.PROJECT_ID, "version_status": "当前版本"})
        remote = MagicMock()
        remote.list_tasks.return_value = {"items": []}
        remote.read_document.return_value = self.document()
        with patch.object(service, "feishu", return_value=remote):
            result = await service.collect_materials("project-one")
        remote.read_document.assert_called_once_with("feedback-one")
        self.assertEqual(len(result["feedback"]), 1)
        self.assertIsNotNone(store.get("task", "other-task"))
        self.assertEqual(store.get("feedback", "other-feedback")["version_status"], "当前版本")

    async def test_prepare_task_never_writes_until_exact_draft_is_confirmed(self):
        run = {"id": "run-one", "operation_id": "prepare-one", "project_id": "project-one"}
        fields = {"task_id": "WORK-ONE", "instructions": "核对现有材料", "member_ids": [self.first["id"]], "task_kind": "work", "stage": "第三阶段"}
        remote = MagicMock()
        remote.list_tasks.return_value = {"items": []}
        remote.save_task.side_effect = lambda data, operation, record: {"task": {**data, "record_id": "rec-one", "source_url": "https://example.test/task"}}
        with patch.object(service, "feishu", return_value=remote):
            draft = (await service.execute_tool("prepare_task", fields, run, self.admin))["item"]
            self.assertEqual(draft["stage"], "")
            remote.save_task.assert_not_called()
            with self.assertRaisesRegex(ValueError, "已变化"):
                await service.publish_task_draft(draft["id"], "0" * 64, "publish-old", self.admin)
            remote.save_task.assert_not_called()
            published = await service.publish_task_draft(draft["id"], draft["draft_hash"], "publish-one", self.admin)
            await service.publish_task_draft(draft["id"], draft["draft_hash"], "publish-again", self.admin)
        self.assertEqual(published["status"], "published")
        self.assertEqual(published["publication"]["project_id"], "project-one")
        remote.save_task.assert_called_once()

    async def test_agent_supplement_rejects_new_work_other_project_or_other_members(self):
        remote = MagicMock()
        original = {"task_id": "ORIGINAL", "project_id": "project-one", "member_ids": [self.first["id"]], "status": "已发布", "task_kind": "work", "version": "1"}
        remote.list_tasks.return_value = {"items": [original]}
        remote.save_task.side_effect = lambda data, operation, record: {"task": {**data, "record_id": "rec-one"}}
        run = {"operation_id": "supplement", "project_id": "project-one"}
        fields = {"task_id": "SUPPLEMENT", "instructions": "请补充实际来源链接", "member_ids": [self.first["id"]]}
        with patch.object(service, "feishu", return_value=remote):
            missing = await service.execute_tool("save_task", fields, run, self.admin)
            other = await service.execute_tool("save_task", {**fields, "related_task": "ORIGINAL", "member_ids": [self.other["id"]]}, run, self.admin)
            wrong_project = await service.execute_tool("save_task", {**fields, "related_task": "ORIGINAL"}, {**run, "project_id": "rdos-self"}, self.admin)
            self.assertFalse(missing["ok"])
            self.assertFalse(other["ok"])
            self.assertFalse(wrong_project["ok"])
            remote.save_task.assert_not_called()
            valid = await service.execute_tool("save_task", {**fields, "related_task": "ORIGINAL"}, run, self.admin)
        self.assertTrue(valid["ok"], valid)
        self.assertEqual(valid["item"]["related_task"], "ORIGINAL")
        self.assertEqual(valid["item"]["stage"], "")

    async def test_existing_task_stage_correction_inherits_fields_and_updates_original(self):
        original = {"task_id": "LOCAL-DEEPSEEK-ONB-01", "project_id": "project-one", "instructions": "完成初次接入",
            "member_ids": [self.first["id"]], "task_kind": "onboarding", "stage": "第二阶段", "nature": "每次接入执行",
            "related_task": "", "version": "1", "status": "已发布", "record_id": "original-record"}
        remote = MagicMock()
        remote.list_tasks.return_value = {"items": [original]}
        remote.save_task.side_effect = lambda data, operation, record: {"task": {**data, "record_id": record}}
        run = {"id": "correct-stage", "operation_id": "correct-stage", "project_id": "project-one"}
        with patch.object(service, "feishu", return_value=remote):
            result = await service.execute_tool("prepare_task", {"task_id": original["task_id"], "stage": "第一阶段"}, run, self.admin)
            self.assertTrue(result["ok"], result)
            draft = result["item"]
            self.assertEqual(draft["task_kind"], "onboarding")
            self.assertEqual(draft["stage"], "第一阶段")
            self.assertEqual(draft["instructions"], original["instructions"])
            self.assertEqual(draft["member_ids"], original["member_ids"])
            self.assertEqual(draft["nature"], original["nature"])
            self.assertEqual(draft["version"], "2")
            remote.save_task.assert_not_called()
            published = await service.publish_task_draft(draft["id"], draft["draft_hash"], "confirm-correction", self.admin)
        self.assertEqual(published["publication"]["record_id"], "original-record")
        self.assertEqual(published["publication"]["task_id"], original["task_id"])
        self.assertEqual(remote.save_task.call_args.args[2], "original-record")

    async def test_existing_task_description_edit_keeps_kind_stage_and_detects_stale_version(self):
        original = {"task_id": "EXISTING-ONBOARDING", "project_id": "project-one", "instructions": "原要求",
            "member_ids": [self.first["id"]], "task_kind": "onboarding", "stage": "第二阶段", "nature": "周期",
            "related_task": "", "version": "3", "status": "已发布", "record_id": "original-record"}
        remote = MagicMock()
        remote.list_tasks.return_value = {"items": [original]}
        run = {"id": "edit-body", "operation_id": "edit-body", "project_id": "project-one"}
        with patch.object(service, "feishu", return_value=remote):
            draft = (await service.execute_tool("prepare_task", {"task_id": original["task_id"], "instructions": "修正后的要求"}, run, self.admin))["item"]
            self.assertEqual((draft["task_kind"], draft["stage"], draft["nature"]), ("onboarding", "第二阶段", "周期"))
            self.assertEqual(draft["version"], "4")
            original["version"] = "4"
            with self.assertRaisesRegex(ValueError, "版本已变化"):
                await service.publish_task_draft(draft["id"], draft["draft_hash"], "confirm-stale", self.admin)
            remote.save_task.assert_not_called()

    async def test_source_link_is_only_registered_after_actual_read(self):
        remote = MagicMock()
        remote.read_source.side_effect = FeishuError("authorization_failed", "缺少资料读取权限")
        fields = {"url": store.FEISHU_HOST + "/docx/new-material", "name": "新资料", "watch": True}
        with patch.object(service, "feishu", return_value=remote):
            with self.assertRaises(FeishuError):
                await projects.register_source(fields, "new-source", self.admin)
        self.assertIsNone(store.get("source", "new-material"))
        with self.assertRaises(ValueError):
            projects.parse_link("https://evil.test/docx/new-material")

    async def test_agent_context_and_history_follow_current_project(self):
        store.put("task", "one", {"task_id": "one", "project_id": "project-one"})
        store.put("task", "other", {"task_id": "other", "project_id": store.PROJECT_ID})
        for record_id, project_id in (("prior-one", "project-one"), ("prior-other", store.PROJECT_ID)):
            store.put("run", record_id, {"id": record_id, "operation_id": record_id, "project_id": project_id, "status": "succeeded", "completed_at": db.utc_now()})
            store.put("message", record_id, {"run_id": record_id, "project_id": project_id, "role": "assistant", "content": record_id, "created_at": db.utc_now()})
        run = service.new_run(self.admin, "检查当前项目", "context-project-one", project_id="project-one")
        async def inspect(agent, messages, tools, execute_tool, **kwargs):
            context = json.loads(messages[0]["content"].split("\n", 1)[1])
            self.assertEqual(context["project"]["id"], "project-one")
            self.assertEqual([member["id"] for member in context["members"]], [self.first["id"]])
            self.assertEqual([task["task_id"] for task in context["tasks"]], ["one"])
            self.assertEqual([message["content"] for message in messages[1:]], ["prior-one", "检查当前项目"])
            self.assertNotIn(store.FEEDBACK_ID, [source["id"] for source in context["sources"]])
            return {"content": "当前项目材料已核对", "usage": {}, "model": "test-only"}
        with patch.object(CentralAgent, "run", inspect):
            await service.run_agent(run["id"])
        latest = store.get("run", run["id"])
        self.assertEqual(latest["status"], "succeeded", latest)
        self.assertIn("elapsed_seconds", latest)

    async def test_agent_context_includes_current_onboarding_readback_without_private_instructions(self):
        feedback_url = store.FEISHU_HOST + "/docx/" + store.FEEDBACK_ID
        store.put("onboarding", "legacy-onb", {"member_id": self.first["id"], "project_id": "project-one", "stage": 1,
            "run_label": "old-run", "source_url": feedback_url, "remote_feedback_verified": False, "evidence_status": "旧版自述记录"})
        store.put("onboarding_session", self.first["id"], {"id": self.first["id"], "project_id": "", "run_label": "new-run",
            "path": "new", "stage": 1, "stage_status": "pending", "ready_for_work": False, "scheduled_verified": False,
            "checked_at": "2026-10-10T12:00:00+00:00", "summary": "第0步已核对；入口 /Users/example/private/work；http://127.0.0.1/api/central/instructions/guide.private-secret",
            "missing": ["尚缺本人明确确认本地工作范围与允许共享范围"],
            "body": "PRIVATE_INSTRUCTION_BODY", "work_scope": "/Users/example/private/unused", "token_hash": "private-token-hash",
            "history": [{"stage": 0, "summary": "连接反馈已读回", "at": "2026-10-10T12:00:00+00:00", "source_url": feedback_url}],
            "evidence": [{"source_url": feedback_url}, {"source_url": "http://127.0.0.1/api/central/instructions/guide.private-secret"}]})
        store.put("onboarding_session", self.other["id"], {"id": self.other["id"], "path": "refresh", "stage": 3, "summary": "OTHER_MEMBER_SUMMARY"})
        store.put("run", "old-analysis", {"id": "old-analysis", "operation_id": "old-analysis", "project_id": "project-one", "status": "succeeded", "completed_at": db.utc_now()})
        store.put("message", "old-analysis", {"run_id": "old-analysis", "project_id": "project-one", "role": "assistant",
            "content": "旧分析：第一阶段需要新会话恢复", "created_at": db.utc_now()})
        run = service.new_run(self.admin, "看看当前同事接入进展", "onboarding-context", project_id="project-one")
        async def inspect(agent, messages, tools, execute_tool, **kwargs):
            from server.central_onboarding import assessment_scope
            serialized = messages[0]["content"].split("\n", 1)[1]
            context = json.loads(serialized)
            entries = context["onboarding"]
            self.assertEqual(len(entries), 2)
            self.assertEqual({entry["record_source"] for entry in entries}, {"legacy_onboarding", "onboarding_session"})
            current = next(entry for entry in entries if entry["record_source"] == "onboarding_session")
            self.assertEqual(current["member_id"], self.first["id"])
            self.assertEqual(current["stage"], 1)
            self.assertTrue(current["connection_feedback_readback"]["verified"])
            self.assertEqual(current["connection_feedback_readback"]["source_url"], feedback_url)
            self.assertFalse(current["ready_for_work"])
            self.assertFalse(current["scheduled_verified"])
            self.assertIn("不代表独立核验本地", current["evidence_status"])
            self.assertEqual(current["missing"], ["尚缺本人明确确认本地工作范围与允许共享范围"])
            self.assertEqual(current["assessment_scope"], assessment_scope({"path": "new", "stage": 1}))
            self.assertIn("旧分析", messages[-3]["content"])
            self.assertEqual(messages[-2]["role"], "system")
            latest = json.loads(messages[-2]["content"].split("\n", 1)[1])
            self.assertEqual(latest["onboarding"], entries)
            self.assertEqual(messages[-1]["role"], "user")
            for secret in ("private-secret", "/Users/example", "PRIVATE_INSTRUCTION_BODY", "private-token-hash", "OTHER_MEMBER_SUMMARY"):
                self.assertNotIn(secret, serialized)
            return {"content": "连接反馈已核对，目前进入第一阶段；本地工作仍需本人Agent提供依据。", "usage": {}, "model": "test-only"}
        with patch.object(CentralAgent, "run", inspect):
            await service.run_agent(run["id"])
        latest = store.get("run", run["id"])
        self.assertEqual(latest["status"], "succeeded", latest)

    async def test_read_feedback_returns_latest_onboarding_alongside_historical_feedback(self):
        feedback_url = store.FEISHU_HOST + "/docx/" + store.FEEDBACK_ID
        store.put("onboarding_session", self.first["id"], {"id": self.first["id"], "path": "new", "stage": 1,
            "checked_at": "2026-10-10T12:00:00+00:00", "summary": "第0步已通过，当前第一阶段待完成",
            "missing": ["尚缺本人确认范围"],
            "history": [{"stage": 0, "summary": "连接反馈已读回", "at": "2026-10-10T12:00:00+00:00", "source_url": feedback_url}]})
        document = self.document()
        document["content"] += "\n历史自述：当时无法写回，权限尚未取得。"
        remote = MagicMock()
        remote.list_tasks.return_value = {"items": []}
        remote.read_document.return_value = document
        run = service.new_run(self.admin, "读取近期反馈", "read-feedback-onboarding", project_id="project-one")
        with patch.object(service, "feishu", return_value=remote):
            result = await service.execute_tool("read_feedback", {}, run, self.admin)
        self.assertTrue(result["ok"])
        self.assertLess(list(result).index("onboarding"), list(result).index("feedback"))
        self.assertIn("当时无法写回", result["feedback"][0]["content"])
        current = result["onboarding"][0]
        self.assertEqual(current["stage"], 1)
        self.assertTrue(current["connection_feedback_readback"]["verified"])
        self.assertEqual(current["connection_feedback_readback"]["source_url"], feedback_url)
        self.assertFalse(current["ready_for_work"])
        self.assertEqual(current["missing"], ["尚缺本人确认范围"])
        from server.central_onboarding import assessment_scope
        self.assertEqual(current["assessment_scope"], assessment_scope({"path": "new", "stage": 1}))

    async def test_scheduler_checks_each_active_project_in_one_shared_loop(self):
        state = store.get("state", "runtime")
        state["next_due"] = "2000-01-01T00:00:00+00:00"
        store.put("state", "runtime", state)
        stop = asyncio.Event()
        checked = []
        async def collect(project_id):
            checked.append(project_id)
            if len(checked) == 2:
                stop.set()
            return {"pending_feedback": [], "changes": []}
        with patch.object(service, "collect_materials", collect):
            await asyncio.wait_for(service.scheduler(stop), timeout=10)
            await asyncio.gather(*list(service.ACTIVE_TASKS))
        self.assertEqual(set(checked), {store.PROJECT_ID, "project-one"})
        self.assertEqual(len(checked), 2)
        runs = store.get("state", "runtime")["project_runs"]
        self.assertTrue(all(item["trigger"] == "scheduled" and item["status"] == "succeeded" for item in runs.values()))


if __name__ == "__main__":
    unittest.main()
