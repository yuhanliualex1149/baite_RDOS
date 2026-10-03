"""Bootstrap enrollment and diagnostic events stay isolated from business progress."""
from __future__ import annotations

import uuid
import unittest
import json
import os
from pathlib import Path

from tests import test_api


class EnrollmentTest(unittest.TestCase):
    setUp = test_api.ApiFlowTest.setUp
    tearDown = test_api.ApiFlowTest.tearDown
    login = test_api.ApiFlowTest.login
    create_runner = test_api.ApiFlowTest.create_runner

    def test_claim_idempotency_rotation_and_self_test(self):
        self.login()
        item = self.create_runner("pilot")
        old = {"Authorization": "Bearer " + item["runner_token"]}
        issued = self.client.post(f"/api/admin/runners/{item['id']}/enrollment", json={}).json()
        code = issued["enrollment_code"]
        self.assertNotIn(code, str(self.client.get("/api/admin/runners").json()))
        installation_id = uuid.uuid4().hex
        candidate = "brt_" + uuid.uuid4().hex + uuid.uuid4().hex
        claim = {"runner_id": item["id"], "enrollment_code": code,
                 "installation_id": installation_id, "candidate_token": candidate}
        self.assertEqual(self.client.post("/api/bootstrap/claim", json=claim).status_code, 200)
        self.assertEqual(self.client.post("/api/bootstrap/claim", json=claim).status_code, 200)
        self.assertIsNone(self.client.get("/api/admin/runners/" + item["id"] + "/installation").json()["heartbeat_at"])
        self.assertEqual(self.client.post("/api/runner/heartbeat", headers=old).status_code, 401)
        self.assertEqual(self.client.post("/api/bootstrap/claim", json={**claim, "installation_id": uuid.uuid4().hex}).status_code, 409)
        auth = {"Authorization": "Bearer " + candidate}
        self.assertEqual(self.client.post("/api/runner/heartbeat", headers=auth, json={
            "installation_id": installation_id, "platform": "darwin", "bootstrapper_version": "0.1.0",
            "protocol_version": "1"}).status_code, 200)
        self.assertEqual(self.client.post("/api/runner/heartbeat", headers=auth, json={
            "installation_id": uuid.uuid4().hex}).status_code, 409)
        event = {"type": "installation_self_test", "event_id": str(uuid.uuid4()),
                 "installation_id": installation_id, "shared_revision": 1, "project_count": 0}
        first = self.client.post("/api/runner/events", headers=auth, json=event)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["ack"])
        self.assertTrue(self.client.post("/api/runner/events", headers=auth, json=event).json()["duplicate"])
        self.assertEqual(self.client.get("/api/admin/runners/" + item["id"] + "/installation").json()["self_test"]["event_id"], event["event_id"])
        self.assertEqual(self.client.get("/api/admin/runners").json()["items"][0]["self_test"]["event_id"], event["event_id"])
        self.assertEqual(self.client.get("/api/runner/installation-status", headers=auth).json()["self_test"]["event_id"], event["event_id"])
        overview = self.client.get("/api/admin/overview").json()
        self.assertEqual(overview["runners"][0]["status"], "idle")
        self.assertFalse(any("installation_self_test" in str(item) for items in overview["work_queue"].values() for item in items))

    def test_reissue_revoke_and_legacy_runner(self):
        self.login()
        item = self.create_runner("existing")
        url = f"/api/admin/runners/{item['id']}/enrollment"
        one = self.client.post(url, json={}).json()["enrollment_code"]
        two = self.client.post(url, json={}).json()["enrollment_code"]
        body = {"runner_id": item["id"], "installation_id": uuid.uuid4().hex,
                "candidate_token": "brt_" + uuid.uuid4().hex + uuid.uuid4().hex}
        self.assertEqual(self.client.post("/api/bootstrap/claim", json={**body, "enrollment_code": one}).status_code, 409)
        self.assertEqual(self.client.post("/api/runner/heartbeat", headers={"Authorization": "Bearer " + item["runner_token"]}).status_code, 200)
        self.assertEqual(self.client.delete(url).status_code, 200)
        self.assertEqual(self.client.post("/api/bootstrap/claim", json={**body, "enrollment_code": two}).status_code, 409)

    def test_create_enrollment_does_not_disclose_long_term_token(self):
        self.login()
        response = self.client.post("/api/admin/runners", json={"display_name": "new", "delivery": "enrollment"})
        self.assertEqual(response.status_code, 201)
        self.assertNotIn("runner_token", str(response.json()))
        self.assertIn("enrollment_code", response.json()["enrollment"])

    def test_expired_code_and_public_manifest_boundary(self):
        self.login()
        item = self.create_runner("expiry")
        code = self.client.post(f"/api/admin/runners/{item['id']}/enrollment", json={}).json()["enrollment_code"]
        from server import db
        with db.connection() as conn:
            conn.execute("UPDATE runner_enrollments SET expires_at='2000-01-01T00:00:00+00:00' WHERE runner_id=?", (item["id"],))
        claim = {"runner_id": item["id"], "enrollment_code": code,
                 "installation_id": uuid.uuid4().hex,
                 "candidate_token": "brt_" + uuid.uuid4().hex + uuid.uuid4().hex}
        self.assertEqual(self.client.post("/api/bootstrap/claim", json=claim).status_code, 409)
        self.assertEqual(self.client.get("/api/bootstrap/manifest").status_code, 503)
        manifest = Path(self.temp_dir.name) / "manifest.json"
        manifest.write_text(json.dumps({"protocol_version": "1", "platforms": {}}), encoding="utf-8")
        os.environ["BAITE_BOOTSTRAP_MANIFEST_PATH"] = str(manifest)
        try:
            self.assertEqual(self.client.get("/api/bootstrap/manifest").json()["protocol_version"], "1")
        finally:
            os.environ.pop("BAITE_BOOTSTRAP_MANIFEST_PATH", None)


if __name__ == "__main__":
    unittest.main()
