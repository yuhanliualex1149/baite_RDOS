from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import central_service as service, central_store as store, db
from server.feishu_tools import FEEDBACK_DOC, ONBOARDING_DOCS, FeishuTools, FeishuError
from server.central_agent import AgentError
from tests.test_central_feishu import FakeCli


class HumanErrorCopyTests(unittest.TestCase):
    def test_known_error_types_keep_distinct_human_recovery(self):
        cases = [
            (PermissionError("当前账号尚未加入这个项目，请联系管理员。"), "当前账号不能执行此操作"),
            (AgentError("model_network", "Connection refused at private endpoint"), "暂时无法连接模型服务"),
            (FeishuError("authorization_failed", "OAuth grant failed"), "飞书登录"),
            (FeishuError("remote_failed", "Server returned unexpected response"), "本次飞书操作未完成"),
            (json.JSONDecodeError("Expecting value", "invalid", 0), "暂时无法解析检查结果"),
            (FeishuError("write_uncertain", "write result not reconciled"), "操作结果尚未确认"),
            (FeishuError("write_unverified", "read-back mismatch"), "操作结果尚未确认"),
            (RuntimeError("OAuth JSON socket error"), "本次操作暂未完成"),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__, code=getattr(error, "code", "")):
                display = service.human_error(error)
                self.assertIn(expected, display)
                if not isinstance(error, FeishuError) or error.code != "authorization_failed":
                    self.assertNotIn("飞书登录", display)
        precise = FeishuError("source_changed", "正文 hash mismatch; reread required", "source-actual")
        self.assertEqual(service.safe_error(precise), "正文 hash mismatch; reread required")
        self.assertEqual(precise.code, "source_changed")
        self.assertEqual(precise.object_id, "source-actual")

    def test_api_error_details_preserve_types_and_mask_credentials(self):
        from server import central_api as api, central_projects as projects
        with patch.dict(os.environ, {"BAITE_LLM_API_KEY": "unit-test-private-model-key"}):
            error = AgentError("model_auth", 'HTTP 401 api_key=unit-test-private-model-key Authorization: Bearer unknown-private-token password="unknown-private-password"')
            response = api.failure(error)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.detail["code"], "model_auth")
        self.assertIn("模型服务未接受", response.detail["message"])
        self.assertIn("HTTP 401", response.detail["technical_detail"])
        for secret in ("unit-test-private-model-key", "unknown-private-token", "unknown-private-password"):
            self.assertNotIn(secret, response.detail["technical_detail"])
        self.assertIn("凭据已隐藏", response.detail["technical_detail"])
        missing = api.failure(projects.ProjectNotFoundError("项目不存在"))
        permission = api.failure(PermissionError("请选择已加入的项目"))
        parsing = api.failure(json.JSONDecodeError("Expecting value", "invalid", 0))
        self.assertEqual((missing.status_code, missing.detail["code"]), (404, "project_not_found"))
        self.assertEqual((permission.status_code, permission.detail["code"]), (403, "permission_denied"))
        self.assertEqual((parsing.status_code, parsing.detail["code"]), (502, "parse_error"))


class CentralServiceFeishuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "BAITE_DB_PATH": str(Path(self.temporary.name) / "service-feishu.db"),
            "BAITE_SESSION_SECRET": "central-service-tests-secret-long-enough",
            "BAITE_ADMIN_USERNAME": "service-admin", "BAITE_ADMIN_PASSWORD": "ServiceAdmin123!",
        })
        self.env.start()
        db.init_db()
        store.init()
        self.admin = store.users()[0]
        self.first = store.create_user("first", "成员一", ["researcher"], "FirstUser123!", "role_test")["user"]
        self.second = store.create_user("second", "成员二", ["admin", "researcher"], "SecondUser123!", "role_test")["user"]
        self.fake = FakeCli()
        self.subprocess = patch("server.feishu_tools.subprocess.run", side_effect=self.fake)
        self.subprocess.start()
        with patch("server.feishu_tools.shutil.which", return_value="/test-only/lark-cli"):
            self.tool = FeishuTools(executable="test-only-cli")
        self.source = patch("server.central_service.feishu", return_value=self.tool)
        self.source.start()

    def tearDown(self):
        self.source.stop()
        self.subprocess.stop()
        self.env.stop()
        self.temporary.cleanup()

    def add_task(self, task_id="RDOS-1", version="1", member=None, status="已发布"):
        self.fake.records.append({"record_id": "rec-project", "fields": {
            "任务ID": task_id, "任务说明": "项目原要求", "版本": version, "发布状态": [status],
            "阶段": ["第三阶段"], "任务性质": ["一次性"], "项目ID": store.PROJECT_ID,
            "适用成员": member or self.first["id"], "关联任务": "", "写回标识": "prior-op",
        }})
        for name in ("项目ID", "适用成员", "关联任务", "写回标识"):
            if name not in self.fake.fields:
                self.fake.fields.append(name)

    async def test_current_project_update_passes_only_registered_fields_and_preserves_body(self):
        self.add_task()
        result = await service.save_task({"task_id": "RDOS-1", "status": "停用"}, "stop-task", self.admin)
        self.assertEqual(result["version"], "2")
        self.assertEqual(result["instructions"], "项目原要求")
        self.assertEqual(result["status"], "停用")
        self.assertEqual(result["member_ids"], [self.first["id"]])
        self.assertEqual(len(self.fake.writes(["base", "+record-upsert"])), 1)
        again = await service.save_task({"task_id": "RDOS-1", "status": "停用"}, "stop-task", self.admin)
        self.assertEqual(again["record_id"], result["record_id"])
        self.assertEqual(len(self.fake.writes(["base", "+record-upsert"])), 1)

    async def test_shared_organization_task_is_preserved_by_project_editor(self):
        with self.assertRaisesRegex(ValueError, "保留原任务"):
            await service.save_task({"task_id": "ONB-1", "instructions": "不要误改", "member_ids": [self.first["id"]]}, "unrelated-task", self.admin)
        self.assertEqual(self.fake.records[0]["fields"]["任务说明"], "保留既有要求")
        self.assertEqual(self.fake.writes(["base", "+record-upsert"]), [])

    async def test_non_numeric_existing_version_fails_without_task_write(self):
        self.add_task(version="人工评审版")
        with self.assertRaisesRegex(ValueError, "版本格式"):
            await service.save_task({"task_id": "RDOS-1", "instructions": "继续"}, "invalid-version", self.admin)
        self.assertEqual(self.fake.writes(["base", "+record-upsert"]), [])

    async def test_uncertain_write_retry_verifies_prepared_content_before_success(self):
        self.fake.read_failure_after_write = True
        # ensure_task_fields writes would trigger the fake read failure too.
        self.tool.ensure_task_fields()
        self.fake.written = False
        args = {"task_id": "RDOS-NEW", "instructions": "已确认的实际任务", "member_ids": [self.first["id"]]}
        with self.assertRaises(Exception):
            await service.save_task(args, "uncertain-retry", self.admin)
        self.assertEqual(store.get("operation", "uncertain-retry")["status"], "uncertain")
        self.fake.read_failure_after_write = False
        self.fake.records[-1]["fields"]["任务说明"] = "后来有人改过的正文"
        with self.assertRaises(Exception) as caught:
            await service.save_task(args, "uncertain-retry", self.admin)
        self.assertEqual(getattr(caught.exception, "code", ""), "operation_conflict")
        self.assertEqual(len(self.fake.writes(["base", "+record-upsert"])), 1)

    def feedback_document(self, version="1", member=None, run="local-run-1", task="RDOS-1"):
        member = member or self.first["id"]
        return {"content": "<title>百特工作反馈记录</title>"
                "<h2>成员标识 待填写 本次运行标识 待填写</h2>"
                "<h3>第二阶段</h3><p>任务 ID 与版本：</p><p>成果摘要与可分享位置：</p>"
                f"<h2>成员标识 {member} 本次运行标识 {run}</h2>"
                f"<h3>第二阶段</h3><p>任务 ID 与版本：{task} @ v{version}</p>"
                f"<p>项目ID：{store.PROJECT_ID}</p><p>成果摘要与可分享位置：已完成读取，尚未核验效果。</p>",
                "source_url": "https://scn33386s7ui.feishu.cn/docx/" + FEEDBACK_DOC}

    def test_native_doc_parser_skips_template_and_preserves_member_run_and_version(self):
        self.add_task()
        rows = service.parse_feedback(self.feedback_document(), self.tool.list_tasks()["items"], store.users())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["member_id"], self.first["id"])
        self.assertEqual(rows[0]["run_label"], "local-run-1")
        self.assertEqual(rows[0]["task_id"], "RDOS-1")
        self.assertEqual(rows[0]["task_version"], "1")
        self.assertEqual(rows[0]["version_status"], "当前版本")
        self.assertEqual(rows[0]["identity_mode"], "role_test")
        self.assertTrue(rows[0]["source_readback"])
        self.assertIn("尚未核验", rows[0]["content"])

    def test_empty_field_does_not_consume_next_line(self):
        self.assertEqual(service.field("任务ID：\n成果摘要：实际成果", "任务ID"), "")
        self.assertEqual(service.field("**成员ID**：mem_test\n其他：x", "成员ID"), "mem_test")

    def test_wrong_member_old_version_and_withdrawn_are_not_current_feedback(self):
        self.add_task(version="2")
        tasks = self.tool.list_tasks()["items"]
        rows = service.parse_feedback(self.feedback_document(version="2", member=self.second["id"]), tasks, store.users())
        self.assertEqual(rows[0]["version_status"], "归属或版本待核对")
        self.assertEqual(rows[0]["member_id"], self.second["id"])
        rows = service.parse_feedback(self.feedback_document(version="1"), tasks, store.users())
        self.assertEqual(rows[0]["version_status"], "旧版本反馈")
        rows = service.parse_feedback(self.feedback_document(version="3"), tasks, store.users())
        self.assertEqual(rows[0]["version_status"], "版本待核对")
        tasks[-1]["status"] = "停用"
        rows = service.parse_feedback(self.feedback_document(version="2"), tasks, store.users())
        self.assertEqual(rows[0]["version_status"], "任务已停用或未发布")

    def test_two_simulated_runs_remain_separate_records(self):
        self.add_task()
        tasks = self.tool.list_tasks()["items"]
        first = service.parse_feedback(self.feedback_document(run="role-run-1"), tasks, store.users())[0]
        second = service.parse_feedback(self.feedback_document(run="role-run-2"), tasks, store.users())[0]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["identity_mode"], "role_test")
        self.assertEqual(second["identity_mode"], "role_test")

    async def test_append_proposal_stays_draft_and_does_not_write_feishu(self):
        token = ONBOARDING_DOCS[0]
        run = {"id": "run-test", "operation_id": "proposal-op", "identity_mode": "role_test"}
        proposal = await service.create_proposal({"target_id": token, "target_kind": "docx", "summary": "补充归属标记说明",
            "old_text": "", "new_text": "补充说明：使用本人成员ID记录反馈。", "evidence": "本次角色测试出现归属不明确的反馈。"}, run)
        self.assertEqual(proposal["status"], "draft")
        self.assertEqual(proposal["old_text"], "")
        self.assertTrue(proposal["base_hash"])
        self.assertEqual(self.fake.writes(["docs", "+update"]), [])
        self.assertEqual(store.get("proposal", proposal["id"])["draft_hash"], service.proposal_hash(proposal))


if __name__ == "__main__":
    unittest.main()
