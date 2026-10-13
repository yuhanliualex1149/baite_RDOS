"""One isolated A -> B -> C journey across HTTP, parsing, tools, and approval.

The Local Agent and central model are explicit fixtures here. This test checks
the application's handoffs; it is not a real Feishu/model acceptance test.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from server import central_onboarding, central_service, central_store, central_templates, project_feedback_guidance
from server.central_agent import CentralAgent


class WorkbenchJourneyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "BAITE_DB_PATH": str(Path(self.temp.name) / "central.db"),
            "BAITE_SESSION_SECRET": "isolated-journey-session-secret-long-enough",
            "BAITE_ADMIN_USERNAME": "admin", "BAITE_ADMIN_PASSWORD": "AdminPassword123!",
            "BAITE_ADMIN_DISPLAY_NAME": "测试管理员", "BAITE_PUBLIC_URL": "http://testserver",
            "BAITE_ENABLE_LEGACY_SYNC": "false", "BAITE_LLM_API_KEY": "", "ARK_API_KEY": "",
        })
        self.environment.start()

        async def idle_scheduler(stop):
            await stop.wait()

        self.scheduler = patch("server.central_service.scheduler", idle_scheduler)
        self.scheduler.start()
        from server.main import app
        self.store = central_store
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        self.client.headers["Origin"] = "http://testserver"
        self.login("admin", "AdminPassword123!")
        self.admin = self.client.get("/api/central/auth/me").json()["user"]
        response = self.client.post("/api/central/users", json={
            "username": "researcher", "display_name": "隔离研究员", "roles": ["researcher"],
            "password": "MemberPassword123!", "identity_mode": "role_test"})
        self.assertEqual(response.status_code, 200, response.text)
        self.researcher = response.json()["user"]

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.scheduler.stop()
        self.environment.stop()
        self.temp.cleanup()

    def login(self, username: str, password: str = "MemberPassword123!"):
        result = self.client.post("/api/central/auth/login", json={"username": username, "password": password})
        self.assertEqual(result.status_code, 200, result.text)
        return result

    def test_onboarding_feedback_central_review_and_new_template_instruction(self):
        project_id = "prj_isolated_journey"
        feedback_id = "doc_isolated_journey"
        project = {
            "id": project_id, "name": "隔离项目", "status": "active",
            "member_ids": [self.admin["id"], self.researcher["id"]],
            "folder_id": "folder_isolated_journey", "feedback_id": feedback_id,
            "feedback_url": f"https://example.test/docx/{feedback_id}",
        }
        self.store.put("project", project_id, project)
        self.login("researcher")
        started = self.client.post("/api/central/onboarding/start", json={"path": "new"})
        self.assertEqual(started.status_code, 200, started.text)
        run_label = started.json()["session_id"]
        organization_doc = f"{self.store.FEISHU_HOST}/docx/{self.store.FEEDBACK_ID}"
        current_records: list[str] = []

        def stage_record(stage: int) -> str:
            feedback_marker = f"{run_label}-stage{stage}-isolated"
            source = f"{self.store.FEISHU_HOST}/docx/{self.store.ONBOARDING_IDS[0]}"
            stage_body = (
                f"读取来源：{source}\n"
                "连接测试结果：通过。已真实读取本步材料、追加本条反馈并从线上读回核对正文及写回标识。\n"
                if stage == 0 else ""
            )
            return f"""## 成员标识 {self.researcher['id']} 本次运行标识 {run_label}
### {'连接检查' if stage == 0 else '接入反馈'}
记录类型：{'connection_check' if stage == 0 else 'onboarding'}
成员ID：{self.researcher['id']}
身份标记：role_test
本次运行标识：{run_label}
接入阶段：{stage}
接入路径：new
反馈ID：{feedback_marker}
实际结果：已读取本步资料、完成本步动作，并从指定反馈文档读回本条标识。
{stage_body}[RDOS 写回标识:{feedback_marker}]
"""

        organization_remote = MagicMock()

        def read_organization(object_id: str) -> dict:
            if object_id == self.store.FEEDBACK_ID:
                return {"object_id": object_id, "source_url": organization_doc,
                        "content": "\n\n".join(current_records)}
            if object_id in self.store.ONBOARDING_IDS:
                return {"object_id": object_id,
                        "source_url": f"{self.store.FEISHU_HOST}/docx/{object_id}",
                        "content": "隔离测试中的接入材料"}
            raise AssertionError(f"Unexpected organization document: {object_id}")

        organization_remote.read_document.side_effect = read_organization
        model_observations = []
        central_observations = []
        project_remote = MagicMock()
        project_remote.list_tasks.return_value = {"items": []}
        project_remote.read_document.side_effect = lambda object_id: {
            "object_id": object_id,
            "source_url": f"https://example.test/docx/{object_id}",
            "content": project_feedback.format(member_id=self.researcher["id"]),
        }

        async def isolated_model(agent, messages, tools, execute_tool, **kwargs):
            if not tools:
                # Stage 1/3 assessment is a fixture, not a claim that a live
                # model agreed. Check that it received this member's record.
                evidence = json.loads(messages[-1]["content"])
                model_observations.append(evidence)
                self.assertEqual(evidence["remote_feedback_evidence"]["member_id"], self.researcher["id"])
                self.assertEqual(evidence["remote_feedback_evidence"]["run_label"], run_label)
                return {"content": json.dumps({
                    "ready": True, "summary": "本步反馈已核对，可以继续。",
                    "missing": [], "scheduled_verified": False,
                }, ensure_ascii=False), "model": "isolated-model-fixture"}

            read = await execute_tool("read_feedback", {"project_id": project_id})
            self.assertTrue(read["ok"], read)
            self.assertEqual(read["source_url"], project["feedback_url"])
            self.assertEqual(len(read["feedback"]), 1)
            feedback = read["feedback"][0]
            central_observations.append(feedback)
            self.assertEqual(feedback["version_status"], "项目反馈（未关联任务）")
            self.assertEqual(feedback["sections"]["human_gate"],
                             "G2 评审线索：教案 v1 待负责人确认；尚无正式决定。")
            before = central_templates.snapshot("project.feedback")
            proposed = await execute_tool("propose_change", {
                "target_kind": "template", "target_id": "project.feedback",
                "summary": "说清成果版本", "evidence": feedback["source_url"] + " 中的成果版本反馈",
                "old_text": "没有新事实就不写空周报",
                "new_text": "没有新事实就不写空周报；成果请写清版本",
            })
            self.assertTrue(proposed["ok"], proposed)
            self.assertEqual(central_templates.snapshot("project.feedback"), before)
            return {"content": "已收到研究员反馈：教案 v1 待评审；已提出模板草稿，需管理员确认。",
                    "model": "isolated-model-fixture"}

        with patch("server.central_onboarding.organization_feishu", return_value=organization_remote), \
                patch("server.central_service.feishu", return_value=project_remote), \
                patch.object(CentralAgent, "run", isolated_model):
            # A: each completed step advances to a new, usable instruction;
            # the old stage link stops granting access.
            stage_zero = self.client.post("/api/central/onboarding/instruction", json={}).json()
            self.assertIn("步骤：0", self.client.get(stage_zero["url"]).text)
            current_records.append(stage_record(0))
            checked = self.client.post("/api/central/onboarding/check", json={
                "session_id": run_label, "expected_stage": 0})
            self.assertEqual(checked.status_code, 200, checked.text)
            self.assertEqual(checked.json()["stage"], 1)
            self.assertEqual(self.client.get(stage_zero["url"]).status_code, 410)
            stage_one = self.client.post("/api/central/onboarding/instruction", json={}).json()
            self.assertIn("步骤：1", self.client.get(stage_one["url"]).text)
            current_records.append(stage_record(1))
            checked = self.client.post("/api/central/onboarding/check", json={
                "session_id": run_label, "expected_stage": 1})
            self.assertEqual(checked.status_code, 200, checked.text)
            self.assertEqual(checked.json()["stage"], 3)
            self.assertEqual(self.client.get(stage_one["url"]).status_code, 410)
            stage_three = self.client.post("/api/central/onboarding/instruction", json={}).json()
            self.assertIn("步骤：3", self.client.get(stage_three["url"]).text)
            current_records.append(stage_record(3))
            checked = self.client.post("/api/central/onboarding/check", json={
                "session_id": run_label, "expected_stage": 3})
            self.assertEqual(checked.status_code, 200, checked.text)
            self.assertTrue(checked.json()["ready_for_work"])
            self.assertFalse(checked.json()["scheduled_verified"])
            self.assertEqual(self.client.get(stage_three["url"]).status_code, 410)
            self.assertEqual([entry["stage"] for entry in model_observations], [1, 3])

            project_link = self.client.post("/api/central/onboarding/instruction", json={
                "purpose": "project", "project_id": project_id}).json()
            original_project_guide = self.client.get(project_link["url"])
            self.assertEqual(original_project_guide.status_code, 200)
            self.assertIn("反馈模板版本：project-feedback-v1", original_project_guide.text)

            # B: a separate project feedback location is parsed into six
            # categories, with report time and an unconfirmed Human Gate clue.
            self.login("admin", "AdminPassword123!")
            run = central_service.new_run(self.admin, "查看项目反馈并提出方法改进", "isolated-journey-run",
                                          project_id=project_id)
            asyncio.run(central_service.run_agent(run["id"]))
            completed = self.store.get("run", run["id"])
            self.assertEqual(completed["status"], "succeeded", completed)
            self.assertIn("需管理员确认", completed["result"])
            self.assertEqual(len(central_observations), 1)
            parsed = central_observations[0]
            self.assertEqual(parsed["period_start"], "2026-10-06")
            self.assertEqual(parsed["period_end"], "2026-10-12")
            self.assertEqual(parsed["reported_at"], "2026-10-13 09:30 +08:00")
            self.assertEqual(parsed["template_version"], "project-feedback-v1")
            self.assertEqual(len([value for value in parsed["sections"].values() if value]), 6)
            self.assertEqual(parsed["gate_clues"][0]["reported_gate"], "G2")
            self.assertIn("尚未由中央核对", parsed["gate_clues"][0]["status"])
            self.assertEqual(self.store.get("feedback", parsed["id"])["processed_hash"], parsed["content_hash"])

            # C: proposing via a model tool leaves v1 active. Only an
            # authenticated administrator confirmation activates v2.
            proposals = self.store.items("proposal")
            self.assertEqual(len(proposals), 1)
            proposal = proposals[0]
            self.assertEqual(proposal["status"], "draft")
            self.assertEqual(central_templates.snapshot("project.feedback")["version"], 1)
            self.login("researcher")
            denied = self.client.post(f"/api/central/proposals/{proposal['id']}/publish", json={
                "expected_hash": proposal["draft_hash"], "operation_id": "denied-review"})
            self.assertEqual(denied.status_code, 403)
            self.login("admin", "AdminPassword123!")
            approved = self.client.post(f"/api/central/proposals/{proposal['id']}/publish", json={
                "expected_hash": proposal["draft_hash"], "operation_id": "approved-review"})
            self.assertEqual(approved.status_code, 200, approved.text)
            self.assertEqual(approved.json()["item"]["status"], "published")
            self.assertEqual(central_templates.snapshot("project.feedback")["version"], 2)
            self.assertEqual(self.client.get(project_link["url"]).status_code, 410)
            self.login("researcher")
            new_link = self.client.post("/api/central/onboarding/instruction", json={
                "purpose": "project", "project_id": project_id}).json()
            new_guide = self.client.get(new_link["url"])
            self.assertEqual(new_guide.status_code, 200)
            self.assertIn("反馈模板版本：project-feedback-v2", new_guide.text)
            self.assertIn("成果请写清版本", new_guide.text)
            periodic = project_feedback_guidance.periodic_requirement(project, self.researcher)
            self.assertIn("反馈模板版本：project-feedback-v2", periodic)
            self.assertIn("成果请写清版本", periodic)
            project_remote.save_task.assert_not_called()
            project_remote.publish_patch.assert_not_called()

        self.assertEqual(organization_remote.read_document.call_count, 5)
        self.assertEqual(project_remote.read_document.call_count, 1)


project_feedback = """## 成员标识 {member_id} 本次运行标识 project-isolated-run
### 项目反馈
记录类型：project_update
成员ID：{member_id}
项目ID：prj_isolated_journey
身份标记：role_test
本次运行标识：project-isolated-run
反馈ID：project-isolated-run-prj_isolated_journey-1
反馈周期：2026-10-06 至 2026-10-12 Asia/Shanghai
报告时间：2026-10-13 09:30 +08:00
反馈模板版本：project-feedback-v1
更正原反馈ID：无
关联任务及版本：未关联任务

#### 1. 当前进度与下一步
完成教案初稿，下周交负责人评审。
#### 2. 本期成果／阶段性产出
教案 v1，草稿，来源 https://example.test/docx/output-v1 。
#### 3. 卡点与协作需求
需要负责人确认评审时间。
#### 4. Human Gate 相关线索
G2 评审线索：教案 v1 待负责人确认；尚无正式决定。
#### 5. Skill／RAG 实际使用
已读取方法资料；是否调用 Skill 尚未核实。
#### 6. 本地 Skill 更新
本期无更新。
来源与尚未核实部分：工作效果由成员报告，尚未独立核对。
[RDOS 写回标识:project-isolated-run-prj_isolated_journey-1]
"""


if __name__ == "__main__":
    import unittest
    unittest.main()
