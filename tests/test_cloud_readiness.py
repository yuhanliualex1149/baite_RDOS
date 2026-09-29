import copy
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from rdos_protocol import seal_snapshot, verify_snapshot
from runner.runner import apply_snapshot, apply_project_snapshot, prepare_workspace, recover_release, submit_outbox, submit_project_report
from server import db
from server.backup import backup_database
from tests import test_api
from tests import test_projects
from tests.test_runner import snapshot


class CloudApiTest(unittest.TestCase):
    setUp = test_api.ApiFlowTest.setUp
    tearDown = test_api.ApiFlowTest.tearDown
    login = test_api.ApiFlowTest.login
    create_runner = test_api.ApiFlowTest.create_runner
    headers = staticmethod(test_api.ApiFlowTest.headers)
    create_project = test_projects.ProjectFlowTest.create_project

    def event(self):
        return {"event_id": "cloud-event-001", "type": "progress", "current_focus": "Cloud test",
                "status": "working", "summary": "Test only", "needs_collaboration": ""}

    def test_origin_is_required_even_for_same_site_sibling(self):
        self.login()
        for origin in ("https://yjmt.cn", "https://other.example", "null", ""):
            result = self.client.put("/api/admin/global-rules", headers={"Origin": origin}, json={"content": "bad"})
            self.assertEqual(result.status_code, 403)
        self.assertEqual(self.client.post("/api/auth/login", headers={"Origin": "https://yjmt.cn"},
                         json={"username": "admin", "password": "AdminPassword123!"}).status_code, 403)

    def test_https_cookie_and_release(self):
        with patch.dict(os.environ, {"BAITE_COOKIE_SECURE": "true", "BAITE_APP_RELEASE": "test-sha"}):
            response = self.login()
            self.assertIn("secure", response.headers["set-cookie"].lower())
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(self.client.get("/api/health").json()["app_release"], "test-sha")

    def test_subpath_assets_origin_cookie_and_runner_config(self):
        from server.main import app
        with patch.dict(os.environ, {"BAITE_PUBLIC_URL": "https://yjmt.cn/baite-rdos", "BAITE_COOKIE_SECURE": "true"}):
            with TestClient(app, root_path="/baite-rdos", base_url="https://yjmt.cn") as client:
                prefix = "/baite-rdos"
                page = client.get(prefix + "/")
                self.assertIn('href="static/styles.css"', page.text)
                self.assertEqual(client.get(prefix + "/static/app.js").status_code, 200)
                body = {"username": "admin", "password": "AdminPassword123!"}
                self.assertEqual(client.post(prefix + "/api/auth/login", json=body).status_code, 403)
                client.headers["Origin"] = "https://yjmt.cn"
                result = client.post(prefix + "/api/auth/login", json=body)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertIn("Path=/baite-rdos/", result.headers["set-cookie"])
                self.assertEqual(result.headers["cache-control"], "no-store")
                runner = client.post(prefix + "/api/admin/runners", json={"display_name": "subpath-test", "workspace": self.temp_dir.name})
                self.assertEqual(runner.status_code, 201, runner.text)
                self.assertEqual(runner.json()["item"]["config"]["control_url"], "https://yjmt.cn/baite-rdos")
                rejected = client.put(prefix + "/api/admin/global-rules", headers={"Origin": "https://other.example"}, json={"content": "bad"})
                self.assertEqual(rejected.status_code, 403)
                logout = client.post(prefix + "/api/auth/logout")
                self.assertIn("Path=/baite-rdos/", logout.headers["set-cookie"])
                self.assertEqual(client.get(prefix + "/api/admin/overview").status_code, 401)

    def test_receipt_conflicts_and_concurrent_retries(self):
        self.login()
        first, second = self.create_runner("first"), self.create_runner("second")
        event = self.event()
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: db.process_runner_event(first["id"], event), range(4)))
        self.assertEqual(sum(not duplicate for _, duplicate in replies), 1)
        for runner, body in ((second, event), (first, {**event, "summary": "different"})):
            result = self.client.post("/api/runner/events", headers=self.headers(runner), json=body)
            self.assertEqual(result.status_code, 409)
        response = self.client.post("/api/runner/events", headers=self.headers(first), json=event).json()
        self.assertTrue(response["duplicate"])
        self.assertEqual(response["event_id"], event["event_id"])

    def test_project_poll_detects_writeback_state_without_revision_change(self):
        self.login()
        runner = self.create_runner("writeback-test")
        project = self.create_project([runner])
        event = {"event_id": "project-writeback-state-test", "type": "project_update", "project_id": project["id"], "summary": "Test", "complete": True}
        self.assertEqual(self.client.post("/api/runner/events", headers=self.headers(runner), json=event).status_code, 200)
        before = self.client.get("/api/runner/projects/sync", headers=self.headers(runner)).json()
        db.record_project_export_success(project["id"], runner["id"])
        after = self.client.get("/api/runner/projects/sync", params={"known_token": before["sync_token"]}, headers=self.headers(runner)).json()
        self.assertTrue(after["changed"])
        self.assertNotEqual(before["sync_token"], after["sync_token"])
        self.assertEqual(before["projects"][0]["revision"], after["projects"][0]["revision"])
        verify_snapshot(after)
        unchanged = self.client.get("/api/runner/projects/sync", params={"known_token": after["sync_token"]}, headers=self.headers(runner)).json()
        self.assertFalse(unchanged["changed"])

    def test_commit_before_ack_loss_retries_once_and_wrong_ack_stays_pending(self):
        self.login()
        runner = self.create_runner("test")
        workspace = Path(self.temp_dir.name) / "workspace"
        prepare_workspace(workspace)
        pending = workspace / "outbox" / "progress.json"
        event = self.event()
        pending.write_text(json.dumps(event))
        attempts = []

        def send(config, path, method, body):
            response = self.client.post(path, headers=self.headers(runner), json=body)
            self.assertEqual(response.status_code, 200)
            attempts.append(response.json())
            if len(attempts) == 1:
                raise urllib.error.URLError("simulated ACK loss after commit")
            return response.json()

        with patch("runner.runner.api_request", side_effect=send):
            submit_outbox(runner["config"], workspace)
            self.assertTrue(pending.exists())
            submit_outbox(runner["config"], workspace)
        self.assertFalse(pending.exists())
        self.assertTrue(attempts[1]["duplicate"])
        self.assertTrue((workspace / "outbox" / "sent" / "progress.json").exists())
        pending.write_text(json.dumps({**event, "event_id": "cloud-event-002"}))
        with patch("runner.runner.api_request", return_value={"ack": True, "event_id": "wrong-event"}):
            submit_outbox(runner["config"], workspace)
        self.assertTrue(pending.exists())

    def test_external_disable_covers_manual_and_background_entrypoints(self):
        self.login()
        from server.project_sync import sync_project, export_pending_progress, check_workflow_reference
        from server.rag_sync import sync_selected_rag
        with patch.dict(os.environ, {"BAITE_DISABLE_EXTERNAL_SYNC": "true"}):
            self.assertEqual(self.client.post("/api/admin/selected-rag/sync").status_code, 503)
            self.assertTrue(sync_selected_rag()["disabled"])
            self.assertTrue(sync_project("unused")["disabled"])
            self.assertTrue(export_pending_progress()[0]["disabled"])
            self.assertTrue(check_workflow_reference()["disabled"])

    def test_backup_restore_preserves_auth_and_snapshot(self):
        self.login()
        runner = self.create_runner("restored")
        expected = db.shared_snapshot(runner["id"])
        verify_snapshot(expected)
        target = Path(self.temp_dir.name) / "restore.db"
        backup_database(db.db_path(), target)
        with patch.dict(os.environ, {"BAITE_DB_PATH": str(target)}):
            self.assertTrue(db.authenticate_admin("admin", "AdminPassword123!"))
            self.assertEqual(db.authenticate_runner(runner["runner_token"])["id"], runner["id"])
            self.assertEqual(db.shared_snapshot(runner["id"])["snapshot_hash"], expected["snapshot_hash"])

    def test_project_report_is_durable_before_any_network_call(self):
        self.login()
        runner = self.create_runner("offline")
        self.create_project([runner])
        bundle = db.runner_project_snapshot(runner["id"])
        verify_snapshot(bundle)
        project = bundle["projects"][0]
        workspace = Path(self.temp_dir.name) / "offline-workspace"
        prepare_workspace(workspace)
        apply_project_snapshot(workspace, project)
        state = {}
        with patch("runner.runner.api_request", side_effect=AssertionError("must queue first")):
            first = submit_project_report(runner["config"], workspace, project, state)
            second = submit_project_report(runner["config"], workspace, project, state)
        self.assertEqual(first, second)
        self.assertEqual(len(list((workspace / "outbox").glob("*.json"))), 1)
        self.assertEqual(json.loads((workspace / ".runner/state.json").read_text())["project_daily_reports"], state["project_daily_reports"])
        def send(config, path, method, body):
            response = self.client.post(path, headers=self.headers(runner), json=body)
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()
        with patch("runner.runner.api_request", side_effect=send):
            submit_outbox(runner["config"], workspace)
        self.assertEqual(len(list((workspace / "outbox/sent").glob("*.json"))), 1)

    def test_project_metadata_and_bundle_hash_are_verified(self):
        self.login()
        runner = self.create_runner("project-hash")
        self.create_project([runner])
        bundle = db.runner_project_snapshot(runner["id"])
        project = bundle["projects"][0]
        workspace = Path(self.temp_dir.name) / "project-workspace"
        release = apply_project_snapshot(workspace, project)
        bad = copy.deepcopy(project)
        bad["description"] = "tampered metadata"
        with self.assertRaises(ValueError):
            apply_project_snapshot(workspace, bad)
        root = workspace / "projects" / project["id"]
        self.assertEqual((root / ".runner/current").resolve(), release.resolve())
        bundle["projects"] = []
        with self.assertRaises(ValueError):
            verify_snapshot(bundle)
        (release / "control/project.md").unlink()
        self.assertIsNone(recover_release(root, workspace / ".runner/project-releases" / project["id"]))


class CloudStorageTest(unittest.TestCase):
    def test_online_backup_includes_uncheckpointed_wal(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, target = Path(temporary) / "live.db", Path(temporary) / "backup.db"
            with sqlite3.connect(source) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA wal_autocheckpoint=0")
                connection.execute("CREATE TABLE evidence (value TEXT)")
                connection.execute("INSERT INTO evidence VALUES ('committed in WAL')")
                connection.commit()
                self.assertGreater(Path(str(source) + "-wal").stat().st_size, 0)
                backup_database(source, target)
                with sqlite3.connect(target) as restored:
                    self.assertEqual(restored.execute("SELECT value FROM evidence").fetchone()[0], "committed in WAL")
                    self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_whole_snapshot_tamper_and_local_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            first = apply_snapshot(workspace, "test", snapshot(1, "# One"))
            second = apply_snapshot(workspace, "test", snapshot(2, "# Two"))
            corrupt = copy.deepcopy(snapshot(3))
            corrupt["global_rules"]["content"] = "changed outside RAG"
            with self.assertRaises(ValueError):
                apply_snapshot(workspace, "test", corrupt)
            self.assertEqual((workspace / ".runner" / "current").resolve(), second.resolve())
            (second / "shared" / "global_rules.md").write_text("disk corruption")
            recovered = recover_release(workspace, workspace / ".runner" / "releases")
            self.assertEqual(recovered["revision"], 1)
            self.assertEqual((workspace / ".runner" / "current").resolve(), first.resolve())
            self.assertIn("# One", (workspace / "shared" / "selected_rag" / "Context.md").read_text())
            self.assertEqual(json.loads((workspace / "control" / "sync.json").read_text())["revision"], 1)

    def test_interrupted_pointer_switch_keeps_old_content_and_control(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            original = apply_snapshot(workspace, "test", snapshot(1))
            replace = os.replace

            def fail_switch(source, target):
                if Path(target).name == "current":
                    raise OSError("simulated interruption")
                replace(source, target)

            with patch("runner.runner.os.replace", side_effect=fail_switch):
                with self.assertRaises(OSError):
                    apply_snapshot(workspace, "test", snapshot(2))
            self.assertEqual((workspace / ".runner" / "current").resolve(), original.resolve())
            self.assertEqual(json.loads((workspace / "control" / "sync.json").read_text())["revision"], 1)


if __name__ == "__main__":
    unittest.main()
