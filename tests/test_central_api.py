"""Central trial API risks; isolated database and explicit remote-call test fixtures."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


async def idle_scheduler(stop):
    await stop.wait()


class CentralApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "BAITE_DB_PATH": str(Path(self.temp.name) / "central.db"),
            "BAITE_SESSION_SECRET": "central-test-session-secret-with-enough-length",
            "BAITE_ADMIN_USERNAME": "admin",
            "BAITE_ADMIN_PASSWORD": "AdminPassword123!",
            "BAITE_ADMIN_DISPLAY_NAME": "测试管理员",
            "BAITE_PUBLIC_URL": "http://testserver",
            "BAITE_ENABLE_LEGACY_SYNC": "false",
            "BAITE_LLM_API_KEY": "",
            "ARK_API_KEY": "",
        })
        self.environment.start()
        self.scheduler = patch("server.central_service.scheduler", idle_scheduler)
        self.scheduler.start()
        from server.main import app
        from server import central_store, central_service
        self.store = central_store
        self.service = central_service
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        self.client.headers["Origin"] = "http://testserver"
        self.login("admin", "AdminPassword123!")
        self.admin = self.client.get("/api/central/auth/me").json()["user"]
        self.researcher = self.create_user("researcher", ["researcher"])
        self.dual = self.create_user("dual", ["admin", "researcher"])

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.scheduler.stop()
        self.environment.stop()
        self.temp.cleanup()

    def login(self, username, password="MemberPassword123!"):
        response = self.client.post("/api/central/auth/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def create_user(self, username, roles):
        response = self.client.post("/api/central/users", json={
            "username": username, "display_name": username, "roles": roles,
            "password": "MemberPassword123!", "identity_mode": "role_test",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("password_hash", response.json()["user"])
        return response.json()["user"]

    def test_login_limit_blocks_bursts_and_recovers_after_window(self):
        from server import central_api
        central_api._LOGIN_ATTEMPTS.clear()
        body = {"username": "admin", "password": "incorrect-test-password"}
        with patch("server.central_api.time.monotonic", return_value=100.0):
            for _ in range(10):
                self.assertEqual(self.client.post("/api/central/auth/login", json=body).status_code, 401)
            blocked = self.client.post("/api/central/auth/login", json={"username": "admin", "password": "AdminPassword123!"})
            self.assertEqual(blocked.status_code, 429)
            self.assertIn("Retry-After", blocked.headers)
        with patch("server.central_api.time.monotonic", return_value=161.0):
            self.login("admin", "AdminPassword123!")

    def test_subpath_cookie_and_logout_use_same_scope(self):
        from server.main import app
        with patch.dict(os.environ, {"BAITE_PUBLIC_URL": "https://yjmt.cn/baite-rdos", "BAITE_COOKIE_SECURE": "true"}):
            with TestClient(app, root_path="/baite-rdos", base_url="https://yjmt.cn") as client:
                client.headers["Origin"] = "https://yjmt.cn"
                prefix = "/baite-rdos/api/central"
                login = client.post(prefix + "/auth/login", json={"username": "admin", "password": "AdminPassword123!"})
                self.assertEqual(login.status_code, 200, login.text)
                self.assertIn("Path=/baite-rdos/", login.headers["set-cookie"])
                self.assertIn("Secure", login.headers["set-cookie"])
                self.assertEqual(client.get(prefix + "/auth/me").status_code, 200)
                logout = client.post(prefix + "/auth/logout")
                self.assertEqual(logout.status_code, 200, logout.text)
                self.assertIn("Path=/baite-rdos/", logout.headers["set-cookie"])
                self.assertEqual(client.get(prefix + "/auth/me").status_code, 401)

    def test_admin_can_preserve_existing_member_marker_and_rename_login(self):
        marker = "mem_TestExisting001"
        created = self.client.post("/api/central/users", json={
            "username": "previous-name", "display_name": "成员", "roles": ["researcher"],
            "password": "MemberPassword123!", "identity_mode": "role_test", "member_id": marker,
        })
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["user"]["id"], marker)
        self.assertEqual(self.client.post("/api/central/users", json={
            "username": "another-name", "display_name": "另一位", "roles": ["researcher"],
            "password": "MemberPassword123!", "member_id": marker,
        }).status_code, 409)
        self.assertEqual(self.client.post("/api/central/users", json={
            "username": "invalid-marker", "display_name": "另一位", "roles": ["researcher"],
            "password": "MemberPassword123!", "member_id": "not-a-member-id",
        }).status_code, 422)
        changed = self.client.patch("/api/central/users/" + marker, json={"username": "new-name"})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["user"]["id"], marker)
        self.assertEqual(self.client.post("/api/central/auth/login", json={
            "username": "previous-name", "password": "MemberPassword123!"}).status_code, 401)
        self.login("new-name")
        self.assertEqual(self.client.get("/api/central/auth/me").json()["user"]["id"], marker)
        self.assertEqual(self.client.post("/api/central/users", json={
            "username": "forbidden", "display_name": "不允许", "roles": ["researcher"],
            "password": "MemberPassword123!", "member_id": "mem_AnotherExisting001",
        }).status_code, 403)

    def test_old_page_actor_cannot_write_using_changed_login_cookie(self):
        self.login("researcher")
        self.client.headers["X-RDOS-Actor"] = self.researcher["id"]
        self.login("admin", "AdminPassword123!")
        payload = {"username": "new-colleague", "display_name": "新同事", "roles": ["researcher"],
                   "password": "MemberPassword123!", "identity_mode": "role_test"}
        denied = self.client.post("/api/central/users", json=payload)
        self.assertEqual(denied.status_code, 409, denied.text)
        self.assertEqual(denied.json()["detail"]["code"], "session_changed")
        self.assertIn("当前账号已变化", denied.json()["detail"]["message"])
        self.assertNotIn("new-colleague", [user["username"] for user in self.store.users()])
        self.client.headers["X-RDOS-Actor"] = self.admin["id"]
        self.assertEqual(self.client.post("/api/central/users", json=payload).status_code, 200)

    def onboarding(self, run_label="record-1"):
        return {"work_scope": "本地项目文件夹", "share_scope": "工作摘要",
                "stage": 1, "evidence": "实际读取接入材料，效果尚未验证", "run_label": run_label}

    def test_workspace_model_matches_agent_default_and_explicit_configuration(self):
        from server.central_agent import CentralAgent
        for value in (None, "", "configured-model"):
            with self.subTest(value=value), patch.dict(os.environ):
                if value is None:
                    os.environ.pop("BAITE_LLM_MODEL", None)
                else:
                    os.environ["BAITE_LLM_MODEL"] = value
                workspace = self.client.get("/api/central/workspace").json()
                self.assertEqual(workspace["runtime"]["model"], CentralAgent().model)
                self.assertEqual(workspace["runtime"]["model"], value or "deepseek-v4-pro")

    def draft(self):
        item = {"id": "proposal-1", "target_id": "allowed-doc", "target_kind": "docx",
                "summary": "补充工作反馈格式", "old_text": "原文", "new_text": "新文",
                "base_hash": self.store.digest("保留段落\n原文\n其他内容"), "evidence": "反馈记录指出关联信息不足",
                "source_url": "https://example.test/docx/allowed-doc", "status": "draft"}
        item["draft_hash"] = self.service.proposal_hash(item)
        self.store.put("proposal", item["id"], item)
        return item

    def test_roles_relevance_and_origin_are_checked_on_backend(self):
        self.store.put("task", "mine", {"task_id": "mine", "member_ids": [self.researcher["id"]], "version": "1", "status": "已发布"})
        self.store.put("task", "other", {"task_id": "other", "member_ids": [self.dual["id"]], "version": "1", "status": "已发布"})
        self.store.put("feedback", "mine-feedback", {"id": "mine-feedback", "member_id": self.researcher["id"]})
        self.store.put("feedback", "other-feedback", {"id": "other-feedback", "member_id": self.dual["id"]})
        draft = self.draft()
        self.login("researcher")
        workspace = self.client.get("/api/central/workspace").json()
        self.assertEqual([item["task_id"] for item in workspace["tasks"]], ["mine"])
        self.assertEqual([item["id"] for item in workspace["feedback"]], ["mine-feedback"])
        self.assertEqual(workspace["proposals"], [])
        self.assertEqual(workspace["runs"], [])
        self.assertFalse(workspace["runtime"]["model_configured"])
        forbidden = [
            ("post", "/chat", {"message": "执行任务", "operation_id": "denied-chat"}),
            ("post", "/sync", {"operation_id": "denied-sync"}),
            ("post", "/tasks", {"instructions": "发布任务", "member_ids": [self.researcher["id"]], "operation_id": "denied-task"}),
            ("patch", "/runtime", {"paused": True}),
            ("patch", f"/users/{self.researcher['id']}", {"roles": ["admin"]}),
            ("post", "/proposals/proposal-1/publish", {"expected_hash": draft["draft_hash"], "operation_id": "denied-publish"}),
        ]
        with patch("server.central_service.feishu") as remote:
            for method, path, body in forbidden:
                response = getattr(self.client, method)("/api/central" + path, json=body)
                self.assertEqual(response.status_code, 403, (path, response.text))
            remote.assert_not_called()
        self.assertEqual(self.client.get("/api/central/runs/hidden").status_code, 403)
        response = self.client.post("/api/central/onboarding", json={**self.onboarding(), "member_id": self.dual["id"]})
        self.assertEqual(response.status_code, 422)
        self.login("admin", "AdminPassword123!")
        with patch("server.central_service.feishu") as remote:
            remote.return_value.read_document.return_value = {"content": "", "source_url": "https://example.test/feedback"}
            self.assertEqual(self.client.post("/api/central/onboarding", json=self.onboarding()).status_code, 200)
        response = self.client.patch("/api/central/runtime", json={"paused": True}, headers={"Origin": "https://unrelated.test"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.store.get("state", "runtime")["paused"])
        self.client.post("/api/central/auth/logout")
        self.assertEqual(self.client.get("/api/central/workspace").status_code, 401)

    def test_onboarding_matches_remote_member_and_run_not_another_identity(self):
        remote = MagicMock()
        remote.read_document.return_value = {"source_url": "https://example.test/feedback", "content":
            f"## 成员 {self.researcher['id']}\n### 实际反馈\n成员ID：{self.researcher['id']}\n项目ID：rdos-self\n任务ID：TASK-1\n任务版本：1\n本次运行标识：shared-label\n已读取任务，尚未验证效果。"}
        self.store.put("task", "TASK-1", {"task_id": "TASK-1", "member_ids": [self.researcher["id"]], "project_id": "rdos-self", "version": "1", "status": "已发布"})
        self.login("researcher")
        with patch("server.central_service.feishu", return_value=remote):
            response = self.client.post("/api/central/onboarding", json=self.onboarding("shared-label"))
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["item"]
        self.assertEqual(item["member_id"], self.researcher["id"])
        self.assertTrue(item["remote_feedback_verified"])
        self.assertEqual(item["identity_mode"], "role_test")
        self.login("dual")
        with patch("server.central_service.feishu", return_value=remote):
            response = self.client.post("/api/central/onboarding", json=self.onboarding("shared-label"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["item"]["remote_feedback_verified"])
        self.assertEqual(response.json()["item"]["member_id"], self.dual["id"])
        with patch("server.central_service.launch") as launch:
            response = self.client.post("/api/central/chat", json={"message": "读取工作反馈", "operation_id": "dual-chat"})
        self.assertEqual(response.status_code, 200, response.text)
        launch.assert_called_once()

    def test_onboarding_read_failure_stays_unverified(self):
        from server.feishu_tools import FeishuError
        self.login("researcher")
        remote = MagicMock()
        remote.read_document.side_effect = FeishuError("authorization_failed", "飞书授权失效")
        with patch("server.central_service.feishu", return_value=remote):
            response = self.client.post("/api/central/onboarding", json=self.onboarding())
        self.assertEqual(response.status_code, 200)
        item = response.json()["item"]
        self.assertFalse(item["remote_feedback_verified"])
        self.assertIn("飞书登录", item["read_error"])
        self.assertIn("授权失效", item["technical_detail"])
        self.assertNotIn("feedback_ids", item)

    def test_missing_project_returns_404_and_permission_remains_403(self):
        response = self.client.get("/api/central/workspace", params={"project_id": "missing-project"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "project_not_found")
        self.assertIn("重新选择", response.json()["detail"]["message"])
        self.store.put("project", "restricted", {"id": "restricted", "name": "受限项目", "member_ids": [self.admin["id"]]})
        self.login("researcher")
        response = self.client.get("/api/central/workspace", params={"project_id": "restricted"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "permission_denied")
        self.assertIn("已加入的项目", response.json()["detail"]["message"])
        self.assertNotIn("飞书", response.json()["detail"]["message"])

    def test_project_related_reads_and_management_roles_are_checked(self):
        from server import central_projects
        project = {"id": "separate-project", "name": "关联项目", "member_ids": [self.researcher["id"]],
                   "feedback_id": "project-feedback", "feedback_url": "https://example.test/project-feedback", "status": "active"}
        self.store.put("project", project["id"], project)
        self.store.put("task", "project-task", {"task_id": "project-task", "project_id": project["id"], "member_ids": [self.researcher["id"]]})
        self.login("researcher")
        self.assertIn(project["id"], [item["id"] for item in self.client.get("/api/central/projects").json()["items"]])
        workspace = self.client.get("/api/central/workspace", params={"project_id": project["id"]}).json()
        self.assertEqual(workspace["project"]["feedback_id"], "project-feedback")
        self.assertEqual([item["task_id"] for item in workspace["tasks"]], ["project-task"])
        with patch("server.central_service.feishu") as remote:
            self.assertEqual(self.client.post("/api/central/projects", json={"name": "不应创建", "member_ids": [self.researcher["id"]], "folder_url": "https://example.test/folder", "operation_id": "denied"}).status_code, 403)
            self.assertEqual(self.client.patch("/api/central/projects/separate-project", json={"name": "不应修改", "operation_id": "denied-edit"}).status_code, 403)
            self.assertEqual(self.client.post("/api/central/sources", json={"url": "https://example.test/docx/x", "operation_id": "denied-source"}).status_code, 403)
            remote.assert_not_called()
        # Removing a project association changes relevant results; it does not change Feishu sharing.
        project["member_ids"] = [self.dual["id"]]
        self.store.put("project", project["id"], project)
        self.assertEqual(self.client.get("/api/central/workspace", params={"project_id": project["id"]}).status_code, 403)

    def test_failed_project_returns_saved_id_and_edit_recovers_same_project(self):
        from server.feishu_tools import FeishuError
        fields = {"name": "待修复项目", "member_ids": [self.researcher["id"]],
                  "folder_url": self.store.FEISHU_HOST + "/drive/folder/first-folder"}
        remote = MagicMock()
        remote.read_source.side_effect = FeishuError("authorization_failed", "资料读取权限不足")
        with patch("server.central_service.feishu", return_value=remote):
            created = self.client.post("/api/central/projects", json={**fields, "operation_id": "failed-create"})
            self.assertEqual(created.status_code, 502, created.text)
            record_id = self.store.get("operation", "failed-create")["result"]["project_id"]
            self.assertEqual(created.json()["detail"]["project_id"], record_id)
            edited = self.client.patch("/api/central/projects/" + record_id, json={**fields, "operation_id": "failed-edit"})
            self.assertEqual(edited.status_code, 502, edited.text)
            self.assertEqual(edited.json()["detail"]["project_id"], self.store.get("operation", "failed-edit")["result"]["project_id"])
            remote.read_source.side_effect = None
            remote.read_source.return_value = {"ok": True, "items": []}
            remote.ensure_project_feedback.return_value = {"ok": True, "object_id": "recovered-feedback", "source_url": self.store.FEISHU_HOST + "/docx/recovered-feedback"}
            recovered = self.client.patch("/api/central/projects/" + record_id, json={**fields,
                "folder_url": self.store.FEISHU_HOST + "/drive/folder/corrected-folder", "operation_id": "corrected-edit"})
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json()["item"]["id"], record_id)
        self.assertEqual(len([item for item in self.store.items("project") if item["name"] == fields["name"]]), 1)

    def test_admin_restore_project_uses_existing_feedback_document(self):
        fields = {"name": "已有工作", "member_ids": [self.researcher["id"]],
                  "folder_url": self.store.FEISHU_HOST + "/drive/folder/restore-folder",
                  "feedback_url": self.store.FEISHU_HOST + "/docx/restore-feedback",
                  "project_id": "prj_existing_trial_01", "operation_id": "restore-existing-trial"}
        self.login("researcher")
        with patch("server.central_service.feishu") as remote:
            self.assertEqual(self.client.post("/api/central/projects", json=fields).status_code, 403)
            remote.assert_not_called()
        self.login("admin", "AdminPassword123!")
        remote = MagicMock()
        remote.read_source.side_effect = lambda token, kind: (
            {"ok": True, "object_id": "restore-folder", "items": []}
            if kind == "folder" else {"ok": True, "object_id": "restore-feedback", "content": "原有内容"})
        with patch("server.central_service.feishu", return_value=remote):
            response = self.client.post("/api/central/projects", json=fields)
            again = self.client.post("/api/central/projects", json=fields)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), again.json())
        self.assertEqual(response.json()["item"]["id"], fields["project_id"])
        remote.ensure_project_feedback.assert_not_called()

    def test_project_validation_without_failed_record_does_not_return_id(self):
        with patch("server.central_service.feishu") as remote:
            response = self.client.post("/api/central/projects", json={"name": "输入错误", "member_ids": [self.researcher["id"]],
                "folder_url": "https://example.test/drive/folder/unrelated", "operation_id": "invalid-project"})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertNotIn("project_id", response.json()["detail"])
        self.assertIsNone(self.store.get("operation", "invalid-project"))
        remote.assert_not_called()

    def test_organization_overview_and_chat_keep_project_work_explicit(self):
        self.store.put("project", "team-project", {"id": "team-project", "name": "团队项目",
            "member_ids": [self.researcher["id"]], "feedback_id": "team-feedback", "status": "active"})
        self.store.put("task", "team-task", {"task_id": "team-task", "project_id": "team-project",
            "member_ids": [self.researcher["id"]], "instructions": "完成工作"})
        self.store.put("feedback", "team-feedback-one", {"id": "team-feedback-one", "project_id": "team-project",
            "member_id": self.researcher["id"], "content": "进展"})
        overview = self.client.get("/api/central/overview")
        self.assertEqual(overview.status_code, 200, overview.text)
        data = overview.json()
        self.assertIsNone(data["project"])
        self.assertIn("team-project", [item["id"] for item in data["projects"]])
        self.assertEqual(data["tasks"][-1]["project_id"], "team-project")
        self.assertEqual(data["feedback"][-1]["project_id"], "team-project")
        self.assertIn(self.admin["id"], [item["id"] for item in data["users"]])
        with patch("server.central_service.launch") as launch:
            response = self.client.post("/api/central/chat", json={"message": "团队最近怎样？", "operation_id": "org-conversation"})
            self.assertEqual(response.status_code, 200, response.text)
            run = self.store.get("run", response.json()["run_id"])
            self.assertEqual(run["project_id"], "")
            launch.assert_called_once()
            run["status"] = "succeeded"
            self.store.put("run", run["id"], run)
            response = self.client.post("/api/central/chat", json={"message": "这个项目怎样？",
                "operation_id": "project-conversation", "project_id": "team-project"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.store.get("run", response.json()["run_id"])["project_id"], "team-project")
        state = self.store.get("state", "runtime")
        state["last_manual"] = {"id": run["id"], "status": "succeeded"}
        self.store.put("state", "runtime", state)
        self.assertNotIn("last_manual", self.client.get("/api/central/workspace").json()["runtime"])
        self.login("researcher")
        self.assertEqual(self.client.get("/api/central/overview").status_code, 403)
        self.login("dual")
        self.assertEqual(self.client.get("/api/central/overview").status_code, 200)

    def test_rejected_method_cannot_publish_without_reviewing_an_edited_draft(self):
        item = self.draft()
        response = self.client.post("/api/central/proposals/proposal-1/reject", json={"reason": "依据不足，请补充实际例子"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["item"]["status"], "rejected")
        with patch("server.central_service.feishu") as remote:
            publication = self.client.post("/api/central/proposals/proposal-1/publish", json={"expected_hash": item["draft_hash"], "operation_id": "rejected-publish"})
            self.assertEqual(publication.status_code, 409, publication.text)
            remote.assert_not_called()

    def test_draft_text_edit_keeps_existing_onboarding_fields_when_omitted(self):
        item = self.service.prepare_task({"task_id": "ONB-EDIT-ONE", "task_kind": "onboarding", "stage": "第二阶段", "nature": "周期",
            "instructions": "当前接入要求", "member_ids": [self.researcher["id"]]}, {"operation_id": "draft-edit", "id": "draft-run"}, self.admin)
        response = self.client.post(f"/api/central/task-drafts/{item['id']}/edit", json={
            "instructions": "更清楚的接入要求", "member_ids": [self.researcher["id"]]})
        self.assertEqual(response.status_code, 200, response.text)
        edited = response.json()["item"]
        self.assertEqual((edited["task_kind"], edited["stage"], edited["nature"]), ("onboarding", "第二阶段", "周期"))
        self.assertEqual(edited["task_id"], item["task_id"])

    def test_dialogue_filters_old_periodic_messages_beyond_run_window_by_metadata(self):
        text = "读取新的实际工作反馈和指定重要资料变化"
        for run_id, kind, trigger in (("old-periodic", "sync", "scheduled"), ("old-manual-sync", "sync", "manual"), ("old-chat", "chat", "manual")):
            self.store.put("run", run_id, {"id": run_id, "kind": kind, "trigger": trigger, "status": "succeeded"})
            self.store.put("message", run_id + ":user", {"run_id": run_id, "role": "user", "content": text, "created_at": "2026-10-10T00:00:00+00:00"})
        # More than 60 newer diagnostics push all three source runs out of the UI run list.
        for number in range(65):
            run_id = "new-periodic-" + str(number)
            self.store.put("run", run_id, {"id": run_id, "kind": "sync", "trigger": "scheduled", "status": "succeeded"})
            self.store.put("message", run_id + ":assistant", {"run_id": run_id, "role": "assistant", "content": "周期结果", "created_at": "2026-10-10T01:00:00+00:00"})
        workspace = self.client.get("/api/central/workspace").json()
        self.assertEqual(len(workspace["runs"]), 60)
        self.assertNotIn("old-chat", [run["id"] for run in workspace["runs"]])
        self.assertEqual([message["run_id"] for message in workspace["messages"]], ["old-chat"])
        self.assertEqual(workspace["messages"][0]["content"], text)  # Same wording remains when it is a real dialogue.
        self.assertEqual(workspace["messages"][0]["kind"], "chat")
        self.assertEqual(workspace["messages"][0]["trigger"], "manual")
        overview = self.client.get("/api/central/overview").json()
        self.assertEqual([message["run_id"] for message in overview["messages"]], ["old-chat"])
        self.assertEqual(overview["messages"][0]["project_id"], self.store.PROJECT_ID)

    def test_publishing_requires_the_exact_saved_draft_and_is_deduplicated(self):
        item = self.draft()
        material = {"content": "保留段落\n原文\n其他内容", "content_hash": item["base_hash"]}
        remote = MagicMock()
        remote.publish_patch.return_value = {"source_url": item["source_url"], "verified": True}
        with patch("server.central_service.source_read", return_value=material), patch("server.central_service.feishu", return_value=remote):
            edited = self.client.post("/api/central/proposals/proposal-1/edit", json={"old_text": "原文", "new_text": "新文，增加关联说明", "summary": "补充任务 ID 和版本"})
            self.assertEqual(edited.status_code, 200, edited.text)
            new_hash = edited.json()["item"]["draft_hash"]
            self.assertNotEqual(new_hash, item["draft_hash"])
            self.assertEqual(self.store.get("proposal", item["id"])["status"], "draft")
            remote.publish_patch.assert_not_called()
            stale = self.client.post("/api/central/proposals/proposal-1/publish", json={"expected_hash": item["draft_hash"], "operation_id": "stale-approval"})
            self.assertEqual(stale.status_code, 409, stale.text)
            remote.publish_patch.assert_not_called()
            body = {"expected_hash": new_hash, "operation_id": "exact-approval"}
            published = self.client.post("/api/central/proposals/proposal-1/publish", json=body)
            self.assertEqual(published.status_code, 200, published.text)
            duplicate = self.client.post("/api/central/proposals/proposal-1/publish", json=body)
            self.assertEqual(duplicate.status_code, 200, duplicate.text)
        remote.publish_patch.assert_called_once_with("docx", "allowed-doc", item["base_hash"], "原文", "新文，增加关联说明", "exact-approval")
        published_item = published.json()["item"]
        self.assertEqual(published_item["status"], "published")
        self.assertEqual(published_item["confirmed_by"], self.admin["id"])
        self.assertEqual(published_item["draft_hash"], new_hash)

    def test_source_change_rejects_draft_edit(self):
        item = self.draft()
        with patch("server.central_service.source_read", return_value={"content": "人工修改的原文", "content_hash": self.store.digest("changed")}), patch("server.central_service.feishu") as remote:
            response = self.client.post("/api/central/proposals/proposal-1/edit", json={"old_text": "原文", "new_text": "新文二", "summary": "修改"})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.get("proposal", item["id"])["draft_hash"], item["draft_hash"])
        remote.assert_not_called()

    def test_uncertain_task_write_reconciles_after_restart_without_duplicate(self):
        from server.feishu_tools import FeishuError
        remote = MagicMock()
        tasks = []
        remote.list_tasks.side_effect = lambda: {"items": [dict(item) for item in tasks]}
        def write(data, operation_id, record_id):
            if tasks:
                self.assertEqual(record_id, tasks[0]["record_id"])
                self.assertEqual(operation_id, tasks[0]["operation_id"])
                for key, value in data.items():
                    self.assertEqual(value, tasks[0][key])
                return {"task": tasks[0]}
            item = {**data, "record_id": "rec-1", "source_url": "https://example.test/task"}
            tasks.append(item)
            raise FeishuError("uncertain", "请求超时，写入结果不确定")
        remote.save_task.side_effect = write
        body = {"instructions": "记录实际接入问题", "member_ids": [self.researcher["id"]], "operation_id": "uncertain-task"}
        with patch("server.central_service.feishu", return_value=remote):
            first = self.client.post("/api/central/tasks", json=body)
            self.assertEqual(first.status_code, 502, first.text)
            self.assertEqual(self.store.get("operation", body["operation_id"])["status"], "uncertain")
            self.store.init()
            retry = self.client.post("/api/central/tasks", json=body)
            self.assertEqual(retry.status_code, 200, retry.text)
            self.assertEqual(self.store.get("operation", body["operation_id"])["status"], "succeeded")
            again = self.client.post("/api/central/tasks", json=body)
            self.assertEqual(again.status_code, 200, again.text)
            changed = self.client.post("/api/central/tasks", json={**body, "instructions": "另一项任务"})
            self.assertEqual(changed.status_code, 409, changed.text)
            stale = self.client.post("/api/central/tasks", json={**body, "task_id": retry.json()["item"]["task_id"], "version": "1", "operation_id": "stale-task"})
            self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(remote.save_task.call_count, 2)  # First uncertain write, then remote-content reconciliation.

    def test_restart_marks_running_interrupted_and_retry_keeps_original_identity(self):
        run = {"id": "run-restart", "status": "running", "operation_id": "original-operation", "user_id": self.admin["id"], "identity_mode": "actual", "message": "读取工作反馈", "trigger": "manual", "steps": [], "created_at": "2026-10-10T00:00:00+00:00"}
        self.store.put("run", run["id"], run)
        self.store.init()
        restored = self.client.get("/api/central/runs/run-restart").json()["run"]
        self.assertEqual(restored["status"], "interrupted")
        self.assertIn("重启", restored["error"])
        with patch("server.central_service.launch") as launch:
            response = self.client.post("/api/central/runs/run-restart/retry", json={})
        self.assertEqual(response.status_code, 200, response.text)
        launched = launch.call_args.args[0]
        self.assertEqual(launched["operation_id"], "original-operation")
        self.assertEqual(launched["user_id"], self.admin["id"])
        self.assertEqual(launched["status"], "queued")
        self.assertEqual(launched["retry_count"], 1)
        launched["status"] = "succeeded"
        self.store.put("run", run["id"], launched)
        self.assertEqual(self.client.post("/api/central/runs/run-restart/retry", json={}).status_code, 409)


if __name__ == "__main__":
    unittest.main()
