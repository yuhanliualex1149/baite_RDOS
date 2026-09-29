from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from rdos_protocol import seal_snapshot

from runner.runner import (
    apply_project_snapshot,
    apply_snapshot,
    load_config,
    load_project_status,
    mark_stale_project_status,
    render_collaborations,
    safe_filename,
    scan_project_files,
)


def rag_item(content: str, runtime_name: str = "Context.md") -> dict:
    encoded = content.encode("utf-8")
    return {
        "source_token": "file-token",
        "source_type": "file",
        "source_name": runtime_name,
        "runtime_name": runtime_name,
        "source_url": "https://example.invalid/file",
        "modified_time": "2026-09-27T00:00:00+00:00",
        "content_hash": hashlib.sha256(encoded).hexdigest(),
        "size_bytes": len(encoded),
        "content": content,
    }


def snapshot(revision: int, content: str = "# Context\n") -> dict:
    return seal_snapshot({
        "revision": revision,
        "global_rules": {"content": "# Global Rules\n\nLocal Agent 自主决定。"},
        "shared_skills": [
            {
                "id": 1,
                "name": "Review Method",
                "version": "v0.2",
                "updated_at": "2026-09-27T00:00:00+00:00",
                "content": "# Review Method",
            }
        ],
        "selected_rag": [rag_item(content)],
        "rag_snapshot": {
            "snapshot_id": f"snapshot-{revision}",
            "status": "healthy",
            "folder_token": "folder-test",
        },
        "skill_proposals": [
            {
                "skill_name": "Review Method",
                "proposed_version": "v0.3",
                "summary": "候选改进",
                "status": "returned",
                "feedback": "请补充适用范围",
                "updated_at": "2026-09-27T00:00:00+00:00",
            }
        ],
        "collaborations": [],
    })


class RunnerSyncTest(unittest.TestCase):
    def test_safe_filename(self) -> None:
        self.assertEqual(safe_filename("Context.md", "fallback"), "Context.md")
        self.assertEqual(safe_filename("Review Method", "fallback"), "review-method.md")
        with self.assertRaises(ValueError):
            safe_filename("../escape.md", "fallback")

    def test_config_is_restricted_to_current_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "runner.json"
            path.write_text(
                json.dumps(
                    {
                        "control_url": "http://127.0.0.1:8000",
                        "runner_id": "runner-test",
                        "runner_token": "secret-token",
                        "workspace": str(Path(temp) / "workspace"),
                    }
                ),
                encoding="utf-8",
            )
            path.chmod(0o644)
            loaded = load_config(path)
            self.assertEqual(loaded["runner_id"], "runner-test")
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_snapshot_switch_is_atomic_and_preserves_last_known_good(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            apply_snapshot(workspace, "runner-test", snapshot(1, "# Good Context\n"))
            active = workspace / "shared" / "selected_rag" / "Context.md"
            self.assertTrue((workspace / "shared").is_symlink())
            self.assertIn("Good Context", active.read_text(encoding="utf-8"))
            self.assertIn(
                "Local Agent 自主决定",
                (workspace / "shared" / "global_rules.md").read_text(encoding="utf-8"),
            )
            self.assertTrue((workspace / "control" / "sync.json").is_file())
            self.assertIn(
                "请补充适用范围",
                (workspace / "control" / "skill_proposals.md").read_text(encoding="utf-8"),
            )

            broken = snapshot(2, "# Broken Context\n")
            broken["selected_rag"][0]["content_hash"] = "0" * 64
            with self.assertRaises(ValueError):
                apply_snapshot(workspace, "runner-test", broken)
            self.assertIn("Good Context", active.read_text(encoding="utf-8"))
            self.assertNotIn("Broken Context", active.read_text(encoding="utf-8"))

    def test_collaboration_file_contains_only_relevant_state(self) -> None:
        content = render_collaborations(
            "runner-target",
            [
                {
                    "id": 7,
                    "from_runner_id": "runner-source",
                    "to_runner_id": "runner-target",
                    "from_name": "user1",
                    "to_name": "user2",
                    "topic": "核对资料",
                    "summary": "请确认资料是否完整",
                    "reference": "workspace/files/context.md",
                    "requires_admin": 0,
                    "status": "pending",
                    "feedback": "",
                }
            ],
        )
        self.assertIn("等待当前节点处理", content)
        self.assertIn("Confirm / Return / Escalate", content)
        self.assertIn("user1", content)

    def test_project_snapshot_status_and_file_scan_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            project = {
                "id": "prj_test123",
                "name": "Project A",
                "description": "验证项目同步",
                "status": "active",
                "computed_status": "due_today",
                "start_date": "2026-09-01",
                "end_date": "2026-10-01",
                "daily_cutoff": "18:00",
                "timezone": "Asia/Shanghai",
                "development_mode": "new_product",
                "feishu_folder_url": "https://example.invalid/folder",
                "revision": 2,
                "active_snapshot_id": "snapshot-project",
                "workflow_nodes": [],
                "todos": [],
                "gate_records": [],
                "content_files": [rag_item("# Brief\n", "brief.md")],
            }
            apply_project_snapshot(workspace, seal_snapshot(project))
            root = workspace / "projects" / project["id"]
            self.assertIn("Project A", (root / "control" / "project.md").read_text())
            self.assertEqual((root / "shared" / "brief.md").read_text(), "# Brief\n")

            status = {
                "summary": "完成原型",
                "workflow_nodes": [],
                "todos": [],
                "file_notes": [
                    {"path": "files/prototype.md", "status": "in_progress", "summary": "开发中"}
                ],
                "issues": [],
                "gate_claim": None,
            }
            (root / "work" / "project_status.json").write_text(
                json.dumps(status, ensure_ascii=False), encoding="utf-8"
            )
            (root / "work" / "files" / "prototype.md").write_text("prototype")
            outside = workspace / "outside.txt"
            outside.write_text("secret")
            if os.name != "nt":
                os.symlink(outside, root / "work" / "files" / "outside-link")
            loaded = load_project_status(root)
            manifest = scan_project_files(root, loaded["file_notes"])
            self.assertTrue(loaded["complete"])
            self.assertEqual([item["path"] for item in manifest], ["files/prototype.md"])
            self.assertNotIn("secret", json.dumps(manifest))

            status_path = root / "work" / "project_status.json"
            stale_time = datetime(2020, 1, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
            os.utime(status_path, (stale_time, stale_time))
            stale = mark_stale_project_status(root, project, load_project_status(root))
            self.assertFalse(stale["complete"])
            self.assertIn("已过期", stale["issues"][-1])

            if os.name != "nt":
                linked_project = workspace / "projects" / "linked-project"
                (linked_project / "work").mkdir(parents=True)
                os.symlink(workspace, linked_project / "work" / "files")
                self.assertEqual(scan_project_files(linked_project, []), [])


if __name__ == "__main__":
    unittest.main()
