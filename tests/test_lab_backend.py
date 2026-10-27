"""Isolated protocol tests; these are not evidence of real client sessions."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import lab_backend as backend
from server.lab_routes import router


class LabBackendTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lab-unit-", dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.tokens = {"node_lab_codex": "codex-synthetic-token-" + "x" * 32,
                       "node_lab_workbuddy": "workbuddy-synthetic-token-" + "y" * 32}
        (self.runtime / "credentials.json").write_text(json.dumps({"node_tokens": self.tokens,
            "admin_password": "synthetic-admin-secret-123", "session_secret": "synthetic-session-secret-123"}))
        self.env = patch.dict(os.environ, {"RDOS_LAB_ROOT": str(self.root), "RDOS_LAB_RUNTIME": str(self.runtime),
            "RDOS_LAB_ENV_ID": "lab_unit_only", "RDOS_LAB_GENERATION": "unit-1", "BAITE_DB_PATH": str(self.runtime / "test.db")})
        self.env.start()
        backend.init()
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)
        self.headers = self.node_headers("node_lab_codex")

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.temp.cleanup()

    def node_headers(self, node):
        return {"Authorization": "Bearer " + self.tokens[node], "X-RDOS-Env-ID": "lab_unit_only", "X-RDOS-Generation": "unit-1"}

    def events(self):
        path = self.runtime / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def submit(self, content="蓝色纸卡编号 LAB-BLUE-07；这是合成实验反馈。", operation="op-1", headers=None, **extra):
        return self.client.post("/api/lab/feedback", headers=headers or self.headers,
                                json={"operation_id": operation, "content": content, **extra})

    def test_explicit_read_append_and_independent_readback(self):
        for route in ("context", "tasks", "documents/lab_doc_intro"):
            response = self.client.get("/api/lab/" + route, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["node_id"], "node_lab_codex")
            self.assertEqual(response.json()["client_kind"], "unverified_client")
        receipt = self.submit().json()
        self.assertEqual(receipt["status"], "created")
        self.assertFalse(receipt["readback_verified"])
        self.assertNotIn("content", receipt)
        self.assertNotIn("feedback_readback", [event["event_type"] for event in self.events()])
        read = self.client.get("/api/lab/feedback/" + receipt["feedback_id"], headers=self.headers)
        self.assertEqual(read.status_code, 200)
        self.assertIn("LAB-BLUE-07", read.json()["content"])
        self.assertEqual(receipt["content_hash"], read.json()["content_hash"])
        self.assertEqual(self.events()[-1]["event_type"], "feedback_readback")
        self.assertFalse(read.json()["client_identity_verified"])
        log = (self.runtime / "events.jsonl").read_text()
        self.assertNotIn("LAB-BLUE-07", log)
        for token in self.tokens.values():
            self.assertNotIn(token, log)
        self.assertEqual([event["sequence"] for event in self.events()], list(range(1, len(self.events()) + 1)))

    def test_idempotency_and_conflicting_content(self):
        first = self.submit().json()
        second = self.submit().json()
        self.assertEqual(first["feedback_id"], second["feedback_id"])
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(self.submit(content="不同正文").status_code, 409)
        with sqlite3.connect(self.runtime / "test.db") as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM lab_feedback").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='central_records'").fetchone()[0], 0)

    def test_bound_identity_and_cross_node_access(self):
        first = self.submit().json()
        other = self.node_headers("node_lab_workbuddy")
        self.assertEqual(self.client.get("/api/lab/feedback/" + first["feedback_id"], headers=other).status_code, 404)
        self.assertEqual(self.submit(headers=other).status_code, 409)
        for field, value in (("node_id", "node_lab_workbuddy"), ("member_id", "mem_lab_workbuddy_0001"),
                             ("project_id", "real_project"), ("task_id", "lab_task_workbuddy")):
            self.assertEqual(self.submit(operation="bad-" + field, **{field: value}).status_code, 403)
        tasks = self.client.get("/api/lab/tasks", headers=other).json()["items"]
        self.assertEqual([task["task_id"] for task in tasks], ["lab_task_workbuddy"])
        self.assertEqual(self.client.get("/api/lab/documents/real-doc", headers=other).status_code, 404)

    def test_invalid_token_and_stale_environment_are_rejected(self):
        wrong = {**self.headers, "Authorization": "Bearer invalid"}
        self.assertEqual(self.client.get("/api/lab/context", headers=wrong).status_code, 401)
        for header in ("X-RDOS-Env-ID", "X-RDOS-Generation"):
            self.assertEqual(self.client.get("/api/lab/context", headers={**self.headers, header: "stale"}).status_code, 409)

    def test_restart_preserves_feedback_and_evidence_without_duplicate_export(self):
        first = self.submit().json()
        before = (self.runtime / "events.jsonl").read_text()
        backend.init()
        self.assertEqual((self.runtime / "events.jsonl").read_text(), before)
        self.assertEqual(self.submit().json()["feedback_id"], first["feedback_id"])
        with patch.dict(os.environ, {"RDOS_LAB_GENERATION": "other-generation"}):
            with self.assertRaises(backend.LabError):
                backend.init()

    def test_reject_credential_symlink_and_outside_database(self):
        credentials = self.runtime / "credentials.json"
        credentials.rename(self.runtime / "original.json")
        credentials.symlink_to(self.runtime / "original.json")
        self.assertEqual(self.client.get("/api/lab/context", headers=self.headers).status_code, 503)
        with patch.dict(os.environ, {"BAITE_DB_PATH": str(self.root / "outside.db")}):
            with self.assertRaises(backend.LabError):
                backend.init()
        self.assertFalse((self.root / "outside.db").exists())

    def test_read_adapter_does_not_claim_external_client_calls(self):
        tools = backend.LocalMockTools("node_lab_codex")
        self.assertEqual(tools.list_tasks()["source_environment"], "local_mock")
        self.assertEqual(tools.read_document(backend.DOCUMENT_ID)["object_id"], backend.DOCUMENT_ID)
        self.assertEqual(self.events(), [])
        self.assertTrue(backend.deterministic_model("sample")["is_mock"])

    def test_secret_content_and_validation_errors_never_echo_credentials(self):
        secret = self.tokens["node_lab_codex"]
        for value in (secret, self.tokens["node_lab_workbuddy"], "synthetic-admin-secret-123", "synthetic-session-secret-123"):
            rejected = self.submit(content="accidentally pasted " + value)
            self.assertEqual(rejected.status_code, 422)
            self.assertNotIn(value, rejected.text)
        for payload in ({"operation_id": "bad-field", "content": "synthetic", "token": secret},
                        {"operation_id": {"private": secret}, "content": "synthetic"}):
            rejected = self.client.post("/api/lab/feedback", headers=self.headers, json=payload)
            self.assertEqual(rejected.status_code, 422)
            self.assertNotIn(secret, rejected.text)
        rejected = self.submit(operation=secret)
        self.assertEqual(rejected.status_code, 422)
        self.assertNotIn(secret, (self.runtime / "events.jsonl").read_text())
        with sqlite3.connect(self.runtime / "test.db") as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM lab_feedback").fetchone()[0], 0)


class ClientBoundaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("lab_client_test", Path(__file__).parents[1] / "lab/client.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_only_exact_http_loopback_with_explicit_port(self):
        with tempfile.TemporaryDirectory(prefix="lab-client-unit-", dir=Path(__file__).parent) as directory:
            root = Path(directory)
            access = {"base_url": "http://127.0.0.1:18620", "node_id": "node_lab_codex", "token": "synthetic-token-" + "x" * 32,
                      "env_id": "test", "generation": "1"}
            for url in ("http://example.org:18620", "http://localhost:18620", "https://127.0.0.1:18620",
                        "http://127.0.0.1", "http://name@127.0.0.1:18620", "http://127.0.0.1:18620/path"):
                (root / ".lab-access.json").write_text(json.dumps({**access, "base_url": url}))
                with self.assertRaises(ValueError):
                    self.module.load_access(root)
            (root / ".lab-access.json").write_text(json.dumps(access))
            self.assertEqual(self.module.load_access(root)["base_url"], access["base_url"])

    def test_redirect_handler_never_follows(self):
        self.assertIsNone(self.module.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://example.org"))


if __name__ == "__main__":
    unittest.main()
