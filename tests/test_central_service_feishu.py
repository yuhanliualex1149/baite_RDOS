from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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
        self.assertEqual(getattr(caught.exception, "code", ""), "write_uncertain")
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

    def test_workflow_context_is_local_reference_not_verified_decision(self):
        context = service.workflow_reference_context()
        self.assertEqual(context["version"], "v0.3")
        self.assertEqual(len(context["gates"]), 8)
        self.assertIn("本次尚未实时读取", context["basis"])
        self.assertIn("不得写成已通过", context["decision_boundary"])

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

    def test_six_work_categories_stay_in_one_record_and_keep_reported_time(self):
        tasks = [
            {"task_id": "TASK-A", "project_id": store.PROJECT_ID, "version": "2",
             "status": "已发布", "member_ids": [self.first["id"]]},
            {"task_id": "TASK-B", "project_id": store.PROJECT_ID, "version": "3",
             "status": "已发布", "member_ids": [self.first["id"]]},
        ]
        for heading_level in ("##", "###", "####"):
            with self.subTest(heading_level=heading_level):
                categories = "\n".join([
                    f"{heading_level} 1. 当前进度与下一步\n已完成资料审阅，下一步请专家看样稿。",
                    f"{heading_level} 2. 本期成果／阶段性产出\n样稿 v2，待评审，https://example.com/draft",
                    f"{heading_level} 3. 卡点与协作需求\n需要专家确认术语。",
                    f"{heading_level} 4. Human Gate 相关线索\nG2 可能需要判断样稿 v2；成员报告已请专家审阅，尚无决定来源。",
                    f"{heading_level} 5. Skill／RAG 实际使用\n已读取质量 RAG，实际用于术语核对。",
                    f"{heading_level} 6. 本地 Skill 更新\n本地 Skill v0.2 已试用，尚未共享。",
                ])
                body = (f"## 成员标识 {self.first['id']} 本次运行标识 weekly-1\n"
                        "### 项目反馈\n记录类型：project_update\n"
                        f"成员ID：{self.first['id']}\n项目ID：{store.PROJECT_ID}\n"
                        "反馈ID：weekly-1\n反馈周期：2026-10-05 至 2026-10-11 Asia/Shanghai\n"
                        "报告时间：2026-10-12 09:15:00 +08:00\n"
                        "反馈模板版本：project-feedback-v1\n更正原反馈ID：无\n"
                        "关联任务及版本：TASK-A v2；TASK-B v2\n"
                        f"{categories}\n来源与尚未核实部分：实际 Gate 决定尚未核实。\n"
                        "[RDOS 写回标识:weekly-1]\n")
                document = {"object_id": FEEDBACK_DOC, "source_url": store.FEISHU_HOST + "/docx/" + FEEDBACK_DOC,
                            "content": body}
                rows = service.parse_feedback(document, tasks, store.users())
                self.assertEqual(len(rows), 1)
                item = rows[0]
                self.assertEqual(item["period_start"], "2026-10-05")
                self.assertEqual(item["period_end"], "2026-10-11")
                self.assertEqual(item["period_timezone"], "Asia/Shanghai")
                self.assertEqual(item["reported_at"], "2026-10-12 09:15:00 +08:00")
                self.assertNotEqual(item["reported_at"], item["observed_at"])
                self.assertEqual(item["template_version"], "project-feedback-v1")
                self.assertEqual(item["correction_of"], "")
                self.assertEqual(len(item["task_refs"]), 2)
                self.assertEqual([ref["version_status"] for ref in item["task_refs"]],
                                 ["当前版本", "旧版本反馈"])
                self.assertEqual(item["version_status"], "旧版本反馈")
                self.assertIn("样稿 v2", item["sections"]["outputs"])
                self.assertIn("术语", item["sections"]["blockers"])
                self.assertIn("本地 Skill v0.2", item["sections"]["local_skill"])
                self.assertEqual(item["gate_clues"][0]["reported_gate"], "G2")
                self.assertIn("尚未由中央核对", item["gate_clues"][0]["status"])
                self.assertNotIn("写回标识", item["sections"]["local_skill"])

    def test_empty_weekly_report_is_not_counted_as_work(self):
        body = (f"## 成员标识 {self.first['id']} 本次运行标识 empty-1\n"
                "### 项目反馈\n记录类型：project_update\n"
                f"成员ID：{self.first['id']}\n项目ID：{store.PROJECT_ID}\n"
                "反馈ID：empty-1\n反馈周期：2026-10-05 至 2026-10-11 Asia/Shanghai\n"
                "报告时间：2026-10-12 09:15:00 +08:00\n关联任务及版本：未关联\n"
                "#### 1. 当前进度与下一步\n本周无变化\n"
                "#### 2. 本期成果／阶段性产出\n无\n"
                "#### 3. 卡点与协作需求\n暂无\n"
                "#### 4. Human Gate 相关线索\n未提供\n"
                "#### 5. Skill／RAG 实际使用\n无\n"
                "#### 6. 本地 Skill 更新\n无\n"
                "[RDOS 写回标识:empty-1]\n")
        document = {"object_id": FEEDBACK_DOC, "source_url": store.FEISHU_HOST + "/docx/" + FEEDBACK_DOC,
                    "content": body}
        self.assertEqual(service.parse_feedback(document, [], store.users()), [])

    def test_no_gate_and_reported_decision_do_not_become_approved_gate(self):
        source = store.FEISHU_HOST + "/docx/" + FEEDBACK_DOC
        for value in ("无", "暂无", "未涉及"):
            with self.subTest(value=value):
                body = (f"## 成员标识 {self.first['id']} 本次运行标识 gate-none\n"
                        "### 项目反馈\n记录类型：project_update\n"
                        f"成员ID：{self.first['id']}\n项目ID：{store.PROJECT_ID}\n"
                        "反馈ID：gate-none\n#### 1. 当前进度与下一步\n完成资料核对。\n"
                        f"#### 4. Human Gate 相关线索\n{value}\n")
                rows = service.parse_feedback({"object_id": FEEDBACK_DOC, "source_url": source, "content": body},
                                              [], store.users())
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["gate_clues"], [])
        body = (f"## 成员标识 {self.first['id']} 本次运行标识 gate-report\n"
                "### 项目反馈\n记录类型：project_update\n"
                f"成员ID：{self.first['id']}\n项目ID：{store.PROJECT_ID}\n"
                "反馈ID：gate-report\n#### 1. 当前进度与下一步\n完成样稿 v2。\n"
                "#### 4. Human Gate 相关线索\nG2 样稿评审。\n"
                "产出版本：v2\n负责人：研究负责人\n实际决定：成员称已通过\n"
                "决定来源：https://example.com/decision\n")
        row = service.parse_feedback({"object_id": FEEDBACK_DOC, "source_url": source, "content": body},
                                     [], store.users())[0]
        clue = row["gate_clues"][0]
        self.assertEqual(clue["reported_gate"], "G2")
        self.assertEqual(clue["reported_decision"], "成员称已通过")
        self.assertEqual(clue["decision_source"], "https://example.com/decision")
        self.assertIn("尚未由中央核对", clue["status"])

    def test_separate_reports_do_not_borrow_member_or_report_time(self):
        doc = {"object_id": FEEDBACK_DOC, "source_url": store.FEISHU_HOST + "/docx/" + FEEDBACK_DOC,
               "content": (
                   f"## 成员标识 {self.first['id']} 本次运行标识 r1\n"
                   f"### 项目反馈\n记录类型：project_update\n项目ID：{store.PROJECT_ID}\n"
                   "反馈ID：r1\n报告时间：2026-10-12 10:00:00 +08:00\n"
                   "#### 1. 当前进度与下一步\n完成第一次资料核对。\n"
                   f"## 成员标识 {self.second['id']} 本次运行标识 r2\n"
                   f"### 项目反馈\n记录类型：project_update\n项目ID：{store.PROJECT_ID}\n"
                   "反馈ID：r2\n#### 1. 当前进度与下一步\n完成第二次资料核对。\n")}
        rows = service.parse_feedback(doc, [], store.users())
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["member_id"] for row in rows], [self.first["id"], self.second["id"]])
        self.assertEqual([row["run_label"] for row in rows], ["r1", "r2"])
        self.assertEqual(rows[1]["reported_at"], "")
        self.assertIn("第二次", rows[1]["sections"]["progress"])
        self.assertNotIn("第一次", rows[1]["content"])

    def test_new_top_level_report_cannot_inherit_previous_member(self):
        doc = {"object_id": FEEDBACK_DOC, "source_url": store.FEISHU_HOST + "/docx/" + FEEDBACK_DOC,
               "content": (
                   f"## 成员标识 {self.first['id']} 本次运行标识 r1\n"
                   f"### 项目反馈\n记录类型：project_update\n项目ID：{store.PROJECT_ID}\n"
                   "反馈ID：r1\n#### 1. 当前进度与下一步\n完成资料核对。\n"
                   "## 项目反馈\n记录类型：project_update\n"
                   f"项目ID：{store.PROJECT_ID}\n反馈ID：r2\n"
                   "#### 1. 当前进度与下一步\n另一段缺少成员身份。\n")}
        rows = service.parse_feedback(doc, [], store.users())
        self.assertEqual(len(rows), 1)
        self.assertIn("完成资料核对", rows[0]["content"])

    async def test_same_remote_body_read_twice_is_not_new_work_or_new_report_time(self):
        doc = {"object_id": FEEDBACK_DOC, "source_url": store.FEISHU_HOST + "/docx/" + FEEDBACK_DOC,
               "content": (f"## 成员标识 {self.first['id']} 本次运行标识 weekly-1\n"
                           "### 项目反馈\n记录类型：project_update\n"
                           f"成员ID：{self.first['id']}\n项目ID：{store.PROJECT_ID}\n"
                           "反馈ID：weekly-1\n#### 1. 当前进度与下一步\n已完成资料核对。\n")}
        remote = MagicMock()
        remote.list_tasks.return_value = {"items": []}
        remote.read_document.return_value = doc
        with patch.object(service, "feishu", return_value=remote):
            first = await service.collect_materials()
            saved = store.get("feedback", first["feedback"][0]["id"])
            second = await service.collect_materials()
        self.assertEqual(len(first["new_feedback"]), 1)
        self.assertEqual(second["new_feedback"], [])
        self.assertEqual(second["feedback"][0]["observed_at"], saved["observed_at"])
        self.assertEqual(second["feedback"][0]["reported_at"], "")
        self.assertTrue(second["feedback"][0]["last_read_at"])

    def test_edit_to_identified_feedback_keeps_local_record_identity(self):
        source = store.FEISHU_HOST + "/docx/" + FEEDBACK_DOC
        before = self.feedback_document(version="1")
        before["content"] += "<p>反馈ID：same-feedback</p>"
        after = {**before, "content": before["content"].replace("v1", "v2").replace("已完成读取", "已完成第二轮核对")}
        first = service.parse_feedback(before, [], store.users())[0]
        second = service.parse_feedback(after, [], store.users())[0]
        self.assertEqual(first["source_url"], source)
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["content_hash"], second["content_hash"])

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
