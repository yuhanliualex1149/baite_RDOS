from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient


class FakeProjectSource:
    def __init__(self, content: str = "# Project\n") -> None:
        self.content = content

    def fetch_project(self, folder_token: str):
        encoded = self.content.encode("utf-8")
        return (
            [
                {
                    "source_token": "file-1",
                    "source_type": "file",
                    "source_name": "brief.md",
                    "runtime_name": "brief.md",
                    "source_url": "https://example.test/brief",
                    "modified_time": "2026-09-28T09:00:00+08:00",
                    "content_hash": hashlib.sha256(encoded).hexdigest(),
                    "content": self.content,
                    "size_bytes": len(encoded),
                }
            ],
            [
                {
                    "source_token": "pdf-1",
                    "source_type": "file",
                    "source_name": "appendix.pdf",
                    "source_url": "https://example.test/pdf",
                    "modified_time": "2026-09-28T09:00:00+08:00",
                    "status": "unsupported",
                }
            ],
        )

    def download_raw(self, file_token: str) -> bytes:
        return b"changed workflow"


class FakeWriter:
    def __init__(self) -> None:
        self.writes = []

    def ensure_progress_folder(self, parent_token: str):
        return "progress-folder", "https://example.test/progress"

    def write_markdown(self, folder_token: str, filename: str, content: str) -> None:
        self.writes.append((folder_token, filename, content))


class FailingProjectSource:
    def fetch_project(self, folder_token: str):
        raise RuntimeError("simulated project download failure")


class ProjectFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        os.environ.update(
            {
                "BAITE_DB_PATH": str(Path(self.temp_dir.name) / "test.db"),
                "BAITE_SESSION_SECRET": "test-session-secret-with-enough-length",
                "BAITE_ADMIN_USERNAME": "admin",
                "BAITE_ADMIN_PASSWORD": "AdminPassword123!",
                "BAITE_SELECTED_RAG_FOLDER_TOKEN": "folder-test",
                "BAITE_PUBLIC_URL": "http://127.0.0.1:8000",
            }
        )
        from server.main import app

        self.context = TestClient(app)
        self.client = self.context.__enter__()
        login = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "AdminPassword123!"},
        )
        self.assertEqual(login.status_code, 200)

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)
        self.temp_dir.cleanup()
        for key in (
            "BAITE_DB_PATH",
            "BAITE_SESSION_SECRET",
            "BAITE_ADMIN_USERNAME",
            "BAITE_ADMIN_PASSWORD",
            "BAITE_SELECTED_RAG_FOLDER_TOKEN",
            "BAITE_PUBLIC_URL",
        ):
            os.environ.pop(key, None)

    def create_runner(self, name: str) -> dict:
        response = self.client.post(
            "/api/admin/runners",
            json={"display_name": name, "workspace": str(Path(self.temp_dir.name) / name)},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["item"]

    @staticmethod
    def headers(runner: dict) -> dict:
        return {"Authorization": f"Bearer {runner['runner_token']}"}

    def create_project(self, participants: list[dict]) -> dict:
        response = self.client.post(
            "/api/admin/projects",
            json={
                "name": "Project A",
                "description": "验证项目协作",
                "participant_runner_ids": [item["id"] for item in participants],
                "start_date": "2026-09-01",
                "end_date": "2026-10-31",
                "daily_cutoff": "18:00",
                "feishu_folder_url": "https://example.feishu.cn/drive/folder/folderProjectA",
                "development_mode": "new_product",
                "product_types": ["course"],
                "delivery_scales": ["single_site"],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["item"]

    def test_other_product_type_is_available_and_accepted(self) -> None:
        runner = self.create_runner("user1")
        workflow = self.client.get("/api/admin/workflow-reference")
        self.assertEqual(workflow.status_code, 200, workflow.text)
        self.assertIn(
            {"value": "other", "label": "其他"},
            workflow.json()["catalog"]["product_types"],
        )
        response = self.client.post(
            "/api/admin/projects",
            json={
                "name": "Other Product Project",
                "participant_runner_ids": [runner["id"]],
                "start_date": "2026-09-01",
                "end_date": "2026-10-31",
                "feishu_folder_url": "https://example.feishu.cn/drive/folder/folderOtherType",
                "development_mode": "new_product",
                "product_types": ["other"],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["item"]["product_types"], ["other"])

    def test_membership_report_todo_gate_and_non_blocking_decision(self) -> None:
        user1, user2, outsider = [self.create_runner(f"user{index}") for index in range(1, 4)]
        project = self.create_project([user1, user2])

        member_sync = self.client.get(
            "/api/runner/projects/sync", headers=self.headers(user1)
        )
        self.assertEqual(member_sync.status_code, 200)
        self.assertEqual(len(member_sync.json()["projects"]), 1)
        outsider_sync = self.client.get(
            "/api/runner/projects/sync", headers=self.headers(outsider)
        )
        self.assertEqual(outsider_sync.json()["projects"], [])

        event_id = str(uuid.uuid4())
        update = {
            "event_id": event_id,
            "type": "project_update",
            "project_id": project["id"],
            "summary": "完成第一版原型。",
            "complete": True,
            "workflow_nodes": [
                {"code": "C3", "status": "complete", "evidence": ["files/prototype.md"]}
            ],
            "todos": [
                {
                    "todo_id": "todo-001",
                    "title": "核对原型",
                    "owner": "项目研发负责人",
                    "status": "open",
                    "due_date": "2026-10-01",
                }
            ],
            "issues": [],
            "file_manifest": [
                {
                    "path": "files/prototype.md",
                    "size_bytes": 12,
                    "modified_at": "2026-09-28T10:00:00+08:00",
                    "sha256": "a" * 64,
                    "status": "in_progress",
                    "summary": "主体结构完成",
                }
            ],
            "gate_claim": {
                "code": "G2",
                "claimed_passed": True,
                "evidence": ["files/prototype.md"],
                "note": "已完成内容 Review",
            },
        }
        first = self.client.post(
            "/api/runner/events", headers=self.headers(user1), json=update
        )
        duplicate = self.client.post(
            "/api/runner/events", headers=self.headers(user1), json=update
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertFalse(first.json()["duplicate"])
        self.assertTrue(duplicate.json()["duplicate"])

        denied = self.client.post(
            "/api/runner/events",
            headers=self.headers(outsider),
            json={**update, "event_id": str(uuid.uuid4())},
        )
        self.assertEqual(denied.status_code, 409)

        detail = self.client.get(f"/api/admin/projects/{project['id']}").json()["item"]
        self.assertEqual(detail["workflow_nodes"][0]["node_code"], "C3")
        self.assertEqual(detail["todos"][0]["owner"], "项目研发负责人")
        self.assertEqual(detail["gate_records"][0]["status"], "reported_passed")

        record_id = detail["gate_records"][0]["id"]
        decided = self.client.post(
            f"/api/admin/projects/{project['id']}/gate-records/{record_id}/decision",
            json={"decision": "confirm", "feedback": "证据已核对"},
        )
        self.assertEqual(decided.status_code, 200, decided.text)
        # Gate confirmation is a record check, not a prerequisite for later updates.
        follow_up = self.client.post(
            "/api/runner/events",
            headers=self.headers(user1),
            json={
                **update,
                "event_id": str(uuid.uuid4()),
                "summary": "Gate 核实期间继续完成修改。",
                "gate_claim": None,
            },
        )
        self.assertEqual(follow_up.status_code, 200, follow_up.text)

        second_gate = self.client.post(
            "/api/runner/events",
            headers=self.headers(user1),
            json={
                **update,
                "event_id": str(uuid.uuid4()),
                "summary": "继续推进并报告第二个 Gate。",
                "gate_claim": {
                    "code": "G4",
                    "claimed_passed": True,
                    "evidence": ["files/prototype.md"],
                    "note": "请核对交付边界",
                },
            },
        )
        self.assertEqual(second_gate.status_code, 200, second_gate.text)
        detail = self.client.get(f"/api/admin/projects/{project['id']}").json()["item"]
        second_record = next(
            item for item in detail["gate_records"] if item["gate_code"] == "G4"
        )
        returned = self.client.post(
            f"/api/admin/projects/{project['id']}/gate-records/{second_record['id']}/decision",
            json={"decision": "return", "feedback": "请补一条验收证据"},
        )
        self.assertEqual(returned.status_code, 200, returned.text)
        self.assertEqual(returned.json()["item"]["status"], "returned")

        # A returned Gate is feedback, not a lock on project reporting.
        after_return = self.client.post(
            "/api/runner/events",
            headers=self.headers(user1),
            json={
                **update,
                "event_id": str(uuid.uuid4()),
                "summary": "收到退回反馈后继续补证据。",
                "gate_claim": None,
            },
        )
        self.assertEqual(after_return.status_code, 200, after_return.text)

    def test_snapshot_export_calendar_and_workflow_drift(self) -> None:
        from server import db
        from server.project_sync import (
            _project_manifest,
            check_workflow_reference,
            export_pending_progress,
            sync_project,
        )

        runner = self.create_runner("user1")
        project = self.create_project([runner])
        synced = sync_project(project["id"], FakeProjectSource(), manual=True)
        self.assertTrue(synced["ok"], synced)
        runner_snapshot = db.runner_project_snapshot(runner["id"])
        self.assertEqual(runner_snapshot["projects"][0]["content_files"][0]["content"], "# Project\n")

        update = {
            "event_id": str(uuid.uuid4()),
            "type": "project_update",
            "project_id": project["id"],
            "summary": "日报",
            "complete": True,
            "workflow_nodes": [],
            "todos": [],
            "issues": [],
            "file_manifest": [],
            "gate_claim": None,
        }
        self.client.post("/api/runner/events", headers=self.headers(runner), json=update)
        writer = FakeWriter()
        results = export_pending_progress(writer)
        self.assertTrue(results[0]["ok"])
        self.assertEqual(writer.writes[0][1], f"{runner['id']}.md")
        self.assertIn("日报", writer.writes[0][2])

        monday_evening = datetime(2026, 9, 28, 19, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with patch("server.db._local_now", return_value=monday_evening):
            status = db.get_project(project["id"])
        self.assertEqual(status["computed_status"], "overdue")

        workflow = check_workflow_reference(FakeProjectSource())
        self.assertTrue(workflow["ok"])
        self.assertEqual(workflow["state"]["status"], "changed")

        reserved_a = [{
            "source_token": "progress-folder",
            "source_type": "folder",
            "source_name": "RDOS 项目进展",
            "modified_time": "2026-09-28T09:00:00+08:00",
            "status": "reserved_progress_folder",
        }]
        reserved_b = [{**reserved_a[0], "modified_time": "2026-09-28T10:00:00+08:00"}]
        self.assertEqual(
            _project_manifest([], reserved_a)[1],
            _project_manifest([], reserved_b)[1],
        )

        failed = sync_project(project["id"], FailingProjectSource())
        self.assertFalse(failed["ok"])
        self.assertEqual(
            db.runner_project_snapshot(runner["id"])["projects"][0]["content_files"][0]["content"],
            "# Project\n",
        )

        for _ in range(10):
            db.record_project_export_failure(project["id"], runner["id"], "write failed")
        incidents = db.work_queue()["incidents"]
        self.assertTrue(any(item["kind"] == "project_export" for item in incidents))
        db.retry_project_export(project["id"], runner["id"])
        incidents = db.work_queue()["incidents"]
        self.assertFalse(any(item["kind"] == "project_export" for item in incidents))

    def test_membership_changes_preserve_history_and_folder_is_unique(self) -> None:
        user1, user2, user3 = [self.create_runner(f"user{index}") for index in range(1, 4)]
        project = self.create_project([user1, user2])
        report = {
            "event_id": str(uuid.uuid4()),
            "type": "project_update",
            "project_id": project["id"],
            "summary": "user2 的历史日报",
            "complete": True,
            "workflow_nodes": [],
            "todos": [],
            "issues": [],
            "file_manifest": [],
            "gate_claim": None,
        }
        self.assertEqual(
            self.client.post("/api/runner/events", headers=self.headers(user2), json=report).status_code,
            200,
        )

        updated = self.client.patch(
            f"/api/admin/projects/{project['id']}",
            json={"participant_runner_ids": [user1["id"], user3["id"]]},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(
            self.client.get("/api/runner/projects/sync", headers=self.headers(user2)).json()["projects"],
            [],
        )
        self.assertEqual(
            len(self.client.get("/api/runner/projects/sync", headers=self.headers(user3)).json()["projects"]),
            1,
        )
        detail = self.client.get(f"/api/admin/projects/{project['id']}").json()["item"]
        former_member = next(item for item in detail["members"] if item["runner_id"] == user2["id"])
        self.assertFalse(former_member["active"])
        self.assertTrue(any(item["runner_id"] == user2["id"] for item in detail["reports"]))

        duplicate_folder = self.client.post(
            "/api/admin/projects",
            json={
                "name": "Project Duplicate",
                "participant_runner_ids": [user1["id"]],
                "start_date": "2026-09-01",
                "end_date": "2026-10-31",
                "feishu_folder_url": "https://example.feishu.cn/drive/folder/folderProjectA",
                "development_mode": "new_product",
            },
        )
        self.assertEqual(duplicate_folder.status_code, 409)

    def test_file_changes_todo_updates_and_computed_calendar_states(self) -> None:
        from server import db

        runner = self.create_runner("user1")
        project = self.create_project([runner])

        def post_update(manifest, todo_status="open"):
            return self.client.post(
                "/api/runner/events",
                headers=self.headers(runner),
                json={
                    "event_id": str(uuid.uuid4()),
                    "type": "project_update",
                    "project_id": project["id"],
                    "summary": "文件进度",
                    "complete": True,
                    "workflow_nodes": [],
                    "todos": [
                        {
                            "todo_id": "stable-todo",
                            "title": "核对交付",
                            "owner": "项目研发负责人",
                            "status": todo_status,
                        }
                    ],
                    "issues": [],
                    "file_manifest": manifest,
                    "gate_claim": None,
                },
            )

        base_file = {
            "path": "files/a.md",
            "size_bytes": 1,
            "modified_at": "2026-09-28T10:00:00+08:00",
            "sha256": "a" * 64,
            "status": "in_progress",
            "summary": "初版",
        }
        first = post_update([base_file])
        self.assertEqual(first.status_code, 200, first.text)
        changed_file = {**base_file, "sha256": "b" * 64, "summary": "已修改"}
        added_file = {**base_file, "path": "files/b.md", "sha256": "c" * 64}
        second = post_update([changed_file, added_file], "done")
        changes = {item["path"]: item["change"] for item in second.json()["result"]["file_changes"]}
        self.assertEqual(changes, {"files/a.md": "modified", "files/b.md": "added"})
        third = post_update([added_file], "done")
        self.assertIn(
            {"path": "files/a.md", "change": "removed"},
            third.json()["result"]["file_changes"],
        )

        corrected = self.client.patch(
            f"/api/admin/projects/{project['id']}/todos/stable-todo",
            json={"owner": "交付核对负责人", "status": "done"},
        )
        self.assertEqual(corrected.status_code, 200, corrected.text)
        self.assertEqual(corrected.json()["item"]["owner"], "交付核对负责人")

        friday_before_cutoff = datetime(2026, 9, 25, 17, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        saturday_after_cutoff = datetime(2026, 9, 26, 19, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        after_cycle = datetime(2026, 11, 2, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with patch("server.db._local_now", return_value=friday_before_cutoff):
            self.assertEqual(db.get_project(project["id"])["computed_status"], "due_today")
        with patch("server.db._local_now", return_value=saturday_after_cutoff):
            weekend = db.get_project(project["id"])
            self.assertEqual(weekend["computed_status"], "on_track")
            self.assertFalse(weekend["reporting_required"])
        db.update_project(project["id"], {"status": "paused"})
        with patch("server.db._local_now", return_value=friday_before_cutoff):
            self.assertEqual(db.get_project(project["id"])["computed_status"], "paused")
        db.update_project(project["id"], {"status": "active"})
        with patch("server.db._local_now", return_value=after_cycle):
            self.assertEqual(db.get_project(project["id"])["computed_status"], "cycle_ended")
        db.update_project(project["id"], {"status": "completed"})
        with patch("server.db._local_now", return_value=friday_before_cutoff):
            self.assertEqual(db.get_project(project["id"])["computed_status"], "completed")


if __name__ == "__main__":
    unittest.main()
