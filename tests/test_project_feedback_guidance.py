import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import central_onboarding, central_store, db, project_feedback_guidance


class ProjectFeedbackGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"BAITE_DB_PATH": str(Path(self.temp.name) / "guidance.db")})
        self.environment.start()
        with db.connection() as conn:
            conn.execute("CREATE TABLE central_records(kind TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,"
                         "updated_at TEXT NOT NULL,PRIMARY KEY(kind,id))")

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def test_project_record_carries_period_correction_and_six_sections(self):
        body = project_feedback_guidance.record_template(
            member_id="member-a", identity_mode="role_test", run_label="project-run",
            project_id="project-a",
        )
        self.assertIn("反馈模板版本：" + project_feedback_guidance.template_version(), body)
        self.assertIn("反馈周期：", body)
        self.assertIn("报告时间：", body)
        self.assertIn("更正原反馈ID：", body)
        self.assertIn("关联任务及版本：", body)
        self.assertEqual(body.count("#### "), 6)
        self.assertIn("[RDOS 写回标识:<与本条反馈ID相同>]", body)

    def test_project_instruction_keeps_project_feedback_separate_from_onboarding(self):
        user = {"id": "member-a", "display_name": "测试成员", "identity_mode": "role_test"}
        item = {"run_label": "project-run", "stage": 0, "path": "project"}
        project = {"id": "project-a", "name": "项目 A", "folder_url": "https://example.com/folder",
                   "feedback_id": "project-feedback"}
        body = central_onboarding.instruction_body(user, item, project, "project")
        self.assertIn("project-feedback", body)
        self.assertNotIn(central_store.FEEDBACK_ID, body)
        self.assertIn("没有新事实就不写空周报", body)
        self.assertIn("不重复建立", body)
        self.assertIn("反馈模板版本：" + project_feedback_guidance.template_version(), body)
        self.assertNotIn("安装飞书", body)

    def test_periodic_task_draft_reuses_template_and_existing_local_plan(self):
        project = {"id": "project-a", "name": "项目 A", "feedback_id": "feedback-a"}
        member = {"id": "member-a"}
        body = project_feedback_guidance.periodic_requirement(project, member)
        self.assertIn("反馈模板版本：" + project_feedback_guidance.template_version(), body)
        self.assertIn("不重新接入或另建计划", body)
        self.assertIn("没有变化就不生成日报", body)
        self.assertIn("feedback-a", body)
        self.assertNotIn(central_store.FEEDBACK_ID, body)
        with self.assertRaises(ValueError):
            project_feedback_guidance.periodic_requirement({"id": "project-a"}, member)


if __name__ == "__main__":
    unittest.main()
