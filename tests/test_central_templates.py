"""Local template activation and versioned member instructions, isolated SQLite."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, Request

from server import central_onboarding, central_service, central_store as store, central_templates, db, project_feedback_guidance


def request() -> Request:
    return Request({"type": "http", "scheme": "http", "server": ("testserver", 80),
                    "root_path": "", "path": "/api/central/onboarding/instruction", "headers": []})


class ControlledTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"BAITE_DB_PATH": str(Path(self.temp.name) / "templates.db")})
        self.environment.start()
        with db.connection() as conn:
            conn.executescript("""
                CREATE TABLE central_records(kind TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,
                                             updated_at TEXT NOT NULL,PRIMARY KEY(kind,id));
                CREATE TABLE central_users(id TEXT PRIMARY KEY,username TEXT NOT NULL,display_name TEXT NOT NULL,
                    roles_json TEXT NOT NULL,password_hash TEXT NOT NULL,identity_mode TEXT NOT NULL,
                    enabled INTEGER NOT NULL,created_at TEXT NOT NULL);
            """)
            conn.execute("INSERT INTO central_users VALUES(?,?,?,?,?,?,?,?)",
                         ("mem_admin001", "admin", "管理员", '["admin"]', "unused", "actual", 1, db.utc_now()))
            conn.execute("INSERT INTO central_users VALUES(?,?,?,?,?,?,?,?)",
                         ("mem_research001", "researcher", "研究员", '["researcher"]', "unused", "role_test", 1, db.utc_now()))
        self.admin = store.user("mem_admin001")
        self.researcher = store.user("mem_research001")

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def activate(self, template_id: str, before: str, after: str, operation_id: str = "activation-1") -> dict:
        current = central_templates.snapshot(template_id)
        return central_templates.apply_approved(template_id, current["content_hash"], before, after,
                                                operation_id, self.admin["id"])

    def test_default_is_read_only_until_admin_activation_and_retry_is_idempotent(self):
        original = central_templates.snapshot("onboarding.0")
        self.assertEqual(original["version"], 1)
        self.assertEqual(store.items("instruction_template"), [])
        with self.assertRaises(PermissionError):
            central_templates.apply_approved("onboarding.0", original["content_hash"], "先检查", "先核对",
                                             "activation-1", self.researcher["id"])
        self.assertEqual(central_templates.snapshot("onboarding.0"), original)
        changed = self.activate("onboarding.0", "先检查已有飞书连接并复用", "先复用并核验已有飞书连接")
        self.assertEqual(changed["version"], 2)
        self.assertIn("先复用并核验", changed["content"])
        same = central_templates.apply_approved("onboarding.0", original["content_hash"],
            "先检查已有飞书连接并复用", "先复用并核验已有飞书连接", "activation-1", self.admin["id"])
        self.assertEqual(same, changed)
        self.assertEqual(central_templates.snapshot("onboarding.0"), changed)
        self.assertEqual(len(store.items("template_activation")), 1)

    def test_conflict_or_changed_operation_cannot_overwrite_current_template(self):
        original = central_templates.snapshot("project.instructions")
        self.activate("project.instructions", "读取项目背景", "先读取项目背景")
        with self.assertRaisesRegex(ValueError, "已变化"):
            central_templates.apply_approved("project.instructions", original["content_hash"],
                                             "读取项目背景", "重写项目背景", "activation-2", self.admin["id"])
        with self.assertRaisesRegex(ValueError, "不同模板内容"):
            central_templates.apply_approved("project.instructions", original["content_hash"],
                                             "读取项目背景", "另一个版本", "activation-1", self.admin["id"])
        self.assertEqual(central_templates.snapshot("project.instructions")["version"], 2)

    def test_protected_fields_are_program_generated(self):
        current = central_templates.snapshot("onboarding.1")
        with self.assertRaisesRegex(ValueError, "由系统生成"):
            central_templates.apply_approved("onboarding.1", current["content_hash"], current["content"],
                current["content"] + "\n反馈ID：fake", "bad-fields", self.admin["id"])
        self.assertEqual(central_templates.snapshot("onboarding.1"), current)
        with self.assertRaisesRegex(ValueError, "原文"):
            central_templates.apply_approved("onboarding.1", current["content_hash"], "", "补充说明",
                                             "bad-anchor", self.admin["id"])

    def test_feedback_section_wording_and_order_can_change_without_losing_structure(self):
        current = central_templates.snapshot("project.feedback")
        changed = current["content"].replace(
            "#### 1. 当前进度与下一步\n<工作推进到了哪里，与上次相比有什么变化；下一步是什么>\n"
            "#### 2. 本期成果／阶段性产出\n<成果名称、版本或日期、获准共享的链接、草稿／待评审／已确认状态>",
            "#### 1. 本期成果／阶段性产出\n<先列本期可分享的成果与版本>\n"
            "#### 2. 当前进度与下一步\n<再说明工作推进和下一步>")
        self.assertNotEqual(changed, current["content"])
        result = self.activate("project.feedback", current["content"], changed)
        self.assertEqual(result["version"], 2)
        record = project_feedback_guidance.record_template(member_id=self.researcher["id"],
            identity_mode="role_test", run_label="project-run", project_id="prj_test001")
        self.assertLess(record.index("#### 1. 本期成果"), record.index("#### 2. 当前进度"))
        self.assertIn("先列本期可分享的成果", record)
        parsed = central_service.feedback_categories(record)
        self.assertEqual(len(parsed), 6)
        self.assertIn("先列本期可分享的成果", parsed["outputs"])
        with self.assertRaisesRegex(ValueError, "反馈栏目"):
            central_templates.apply_approved("project.feedback", result["content_hash"],
                central_templates.FEEDBACK_DIVIDER, "\n反馈说明\n", "bad-format", self.admin["id"])

    def test_old_link_expires_after_template_activation_new_link_uses_new_version(self):
        store.put("onboarding_session", self.researcher["id"], {
            "id": self.researcher["id"], "run_label": "onb_test001", "stage": 0, "path": "new",
            "identity_mode": "role_test", "ready_for_work": False, "missing": [], "history": []})
        link = central_onboarding.instruction(central_onboarding.Context(), request(), self.researcher)
        key = link["url"].rsplit("/", 1)[-1]
        first = central_onboarding.read_instruction(key, request()).body.decode()
        self.assertIn("本步指令版本：v1", first)
        self.assertIn(f"成员ID：{self.researcher['id']}", first)
        self.assertNotIn(self.admin["id"], first)
        original = central_templates.snapshot("onboarding.0")
        self.activate("onboarding.0", "先检查已有飞书连接并复用", "先复用并核验已有飞书连接")
        with self.assertRaises(HTTPException) as captured:
            central_onboarding.read_instruction(key, request())
        self.assertEqual(captured.exception.status_code, 410)
        self.assertIn("新版本", captured.exception.detail)
        new_link = central_onboarding.instruction(central_onboarding.Context(), request(), self.researcher)
        newer = central_onboarding.read_instruction(new_link["url"].rsplit("/", 1)[-1], request()).body.decode()
        self.assertIn("本步指令版本：v2", newer)
        self.assertIn("先复用并核验已有飞书连接", newer)
        self.assertNotEqual(original["content_hash"], central_templates.snapshot("onboarding.0")["content_hash"])

    def test_project_and_periodic_use_same_approved_feedback_version(self):
        store.put("project", "prj_test001", {"id": "prj_test001", "name": "测试项目",
                  "member_ids": [self.researcher["id"]], "feedback_id": "doc_test001"})
        project = {"id": "prj_test001", "name": "测试项目", "member_ids": [self.researcher["id"]],
                   "feedback_id": "doc_test001"}
        old_link = central_onboarding.instruction(
            central_onboarding.Context(project_id="prj_test001", purpose="project"), request(), self.researcher)
        before = central_templates.snapshot("project.feedback")
        self.activate("project.feedback", "没有新事实就不写空周报", "没有新事实就不写空周报，也不推断没有工作")
        with self.assertRaises(HTTPException) as captured:
            central_onboarding.read_instruction(old_link["url"].rsplit("/", 1)[-1], request())
        self.assertEqual(captured.exception.status_code, 410)
        body = central_onboarding.instruction_body(self.researcher,
            {"run_label": "project-run", "stage": 0, "path": "project"}, project, "project")
        periodic = project_feedback_guidance.periodic_requirement(project, self.researcher)
        record = project_feedback_guidance.record_template(member_id=self.researcher["id"],
            identity_mode="role_test", run_label="project-run", project_id=project["id"])
        for value in (body, periodic, record):
            self.assertIn("反馈模板版本：project-feedback-v2", value)
        self.assertIn("也不推断没有工作", body)
        self.assertIn("也不推断没有工作", periodic)
        self.assertNotEqual(before["content_hash"], central_templates.snapshot("project.feedback")["content_hash"])

    def test_proposal_remains_draft_until_admin_confirms_then_activates_once(self):
        current = central_templates.snapshot("project.periodic")
        run = {"id": "run-template-1", "operation_id": "run-operation-1", "project_id": "",
               "identity_mode": "role_test"}
        proposal = asyncio.run(central_service.create_proposal({
            "target_kind": "template", "target_id": "project.periodic",
            "summary": "让周期检查说明更清楚", "evidence": "隔离测试反馈显示误以为每天都需写日报",
            "old_text": "没有变化就不生成日报", "new_text": "没有变化就不生成日报，也不用补空记录",
        }, run))
        self.assertEqual(proposal["status"], "draft")
        self.assertEqual(central_templates.snapshot("project.periodic"), current)
        first = asyncio.run(central_service.publish_proposal(proposal["id"], proposal["draft_hash"],
                                                              "publish-template-1", self.admin))
        self.assertEqual(first["status"], "published")
        self.assertEqual(first["publication"]["version"], 2)
        self.assertIn("实际使用和效果待后续反馈", first["adoption_status"])
        periodic = project_feedback_guidance.periodic_requirement(
            {"id": "prj_test001", "name": "测试项目", "feedback_id": "doc_test001"}, self.researcher)
        self.assertIn("周期要求模板版本：project.periodic@v2", periodic)
        self.assertIn("也不用补空记录", periodic)
        repeated = asyncio.run(central_service.publish_proposal(proposal["id"], proposal["draft_hash"],
                                                                 "publish-template-1", self.admin))
        self.assertEqual(repeated["publication"], first["publication"])
        self.assertEqual(central_templates.snapshot("project.periodic")["version"], 2)


if __name__ == "__main__":
    unittest.main()
