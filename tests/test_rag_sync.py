from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import db
from server.rag_sync import FeishuRagSource, sync_selected_rag


def file_item(token: str, name: str, content: str, source_type: str = "file") -> dict:
    encoded = content.encode("utf-8")
    return {
        "source_token": token,
        "source_type": source_type,
        "source_name": name.removesuffix(".md"),
        "runtime_name": name,
        "source_url": f"https://example.invalid/{token}",
        "modified_time": "2026-09-27T00:00:00+00:00",
        "content_hash": hashlib.sha256(encoded).hexdigest(),
        "content": content,
        "size_bytes": len(encoded),
    }


class FakeSource:
    def __init__(self, files: list[dict], ignored: list[dict] | None = None) -> None:
        self.files = files
        self.ignored = ignored or []

    def fetch(self, folder_token: str):
        if folder_token != "folder-test":
            raise RuntimeError("wrong folder")
        return self.files, self.ignored


class FailingSource:
    def fetch(self, folder_token: str):
        raise RuntimeError("simulated download failure")


class RagSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        os.environ.update(
            {
                "BAITE_DB_PATH": str(Path(self.temp.name) / "rag.db"),
                "BAITE_SESSION_SECRET": "test-session-secret-with-enough-length",
                "BAITE_ADMIN_USERNAME": "admin",
                "BAITE_ADMIN_PASSWORD": "AdminPassword123!",
                "BAITE_SELECTED_RAG_FOLDER_TOKEN": "folder-test",
            }
        )
        db.init_db()

    def tearDown(self) -> None:
        self.temp.cleanup()
        for key in (
            "BAITE_DB_PATH",
            "BAITE_SESSION_SECRET",
            "BAITE_ADMIN_USERNAME",
            "BAITE_ADMIN_PASSWORD",
            "BAITE_SELECTED_RAG_FOLDER_TOKEN",
        ):
            os.environ.pop(key, None)

    def test_new_removed_and_unchanged_files(self) -> None:
        first_source = FakeSource(
            [
                file_item("md-1", "Context.md", "# Context\n"),
                file_item("doc-1", "Framework.md", "# Framework\n", "docx"),
            ],
            [
                {
                    "source_token": "folder-1",
                    "source_type": "folder",
                    "source_name": "Archive",
                    "status": "subfolder_ignored",
                }
            ],
        )
        first = sync_selected_rag(first_source, manual=True)
        self.assertTrue(first["ok"])
        self.assertTrue(first["changed"])
        first_revision = db.current_revision()
        self.assertEqual(db.rag_state()["status"], "healthy")
        self.assertEqual(len(db.active_rag_files()), 2)

        unchanged = sync_selected_rag(first_source)
        self.assertFalse(unchanged["changed"])
        self.assertEqual(db.current_revision(), first_revision)

        second_source = FakeSource(
            [
                file_item("doc-1", "Framework.md", "# Framework\n", "docx"),
                file_item("md-2", "New.md", "# New\n"),
            ]
        )
        changed = sync_selected_rag(second_source)
        self.assertTrue(changed["changed"])
        self.assertGreater(db.current_revision(), first_revision)
        self.assertEqual(
            [item["runtime_name"] for item in db.active_rag_files()],
            ["Framework.md", "New.md"],
        )
        with db.connection() as conn:
            history = conn.execute("SELECT COUNT(*) AS total FROM rag_snapshots").fetchone()
        self.assertEqual(history["total"], 2)

    def test_unsupported_item_is_visible_but_not_in_runtime(self) -> None:
        result = sync_selected_rag(
            FakeSource(
                [file_item("md-1", "Context.md", "# Context\n")],
                [
                    {
                        "source_token": "sheet-1",
                        "source_type": "sheet",
                        "source_name": "Unsupported Sheet",
                        "status": "unsupported",
                    }
                ],
            ),
            manual=True,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(db.rag_state()["status"], "needs_admin")
        self.assertEqual(len(db.active_rag_files()), 1)
        self.assertEqual(db.rag_state()["ignored_items"][0]["status"], "unsupported")

    def test_failures_keep_last_known_good_and_pause_at_ten(self) -> None:
        sync_selected_rag(
            FakeSource([file_item("md-1", "Context.md", "# Known Good\n")]),
            manual=True,
        )
        snapshot_id = db.rag_state()["active_snapshot_id"]
        for expected in range(1, 11):
            result = sync_selected_rag(FailingSource())
            self.assertFalse(result["ok"])
            self.assertEqual(result["state"]["failure_count"], expected)
            self.assertEqual(result["state"]["active_snapshot_id"], snapshot_id)
        self.assertEqual(db.rag_state()["status"], "needs_admin")
        self.assertIn("Known Good", db.active_rag_files(include_content=True)[0]["content"])

        recovered = sync_selected_rag(
            FakeSource([file_item("md-2", "Recovered.md", "# Recovered\n")]),
            manual=True,
        )
        self.assertTrue(recovered["ok"])
        self.assertEqual(db.rag_state()["failure_count"], 0)
        self.assertEqual(db.active_rag_files()[0]["runtime_name"], "Recovered.md")

    def test_hash_error_does_not_switch_snapshot(self) -> None:
        sync_selected_rag(
            FakeSource([file_item("md-1", "Context.md", "# Known Good\n")]),
            manual=True,
        )
        snapshot_id = db.rag_state()["active_snapshot_id"]
        broken = file_item("md-2", "Broken.md", "# Broken\n")
        broken["content_hash"] = "f" * 64
        result = sync_selected_rag(FakeSource([broken]))
        self.assertFalse(result["ok"])
        self.assertEqual(db.rag_state()["active_snapshot_id"], snapshot_id)

    def test_project_folder_may_be_empty(self) -> None:
        with patch("server.rag_sync.shutil.which", return_value="/test-only/lark-cli"):
            source = FeishuRagSource()
        with patch.object(source, "_run", return_value={"data": {"files": []}}):
            files, ignored = source.fetch_project("empty-project-folder")
        self.assertEqual(files, [])
        self.assertEqual(ignored, [])


if __name__ == "__main__":
    unittest.main()
