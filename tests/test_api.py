from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient


class ApiFlowTest(unittest.TestCase):
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
        self.client.headers["Origin"] = "http://127.0.0.1:8000"

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

    def login(self):
        response = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "AdminPassword123!"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def create_runner(self, name: str) -> dict:
        response = self.client.post(
            "/api/admin/runners",
            json={"display_name": name, "workspace": str(Path(self.temp_dir.name) / name)},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["item"]

    @staticmethod
    def headers(item: dict) -> dict:
        return {"Authorization": f"Bearer {item['runner_token']}"}

    def event(self, headers: dict, payload: dict):
        payload.setdefault("event_id", str(uuid.uuid4()))
        return self.client.post("/api/runner/events", headers=headers, json=payload)

    def test_admin_authentication_and_password_change(self) -> None:
        self.assertEqual(self.client.get("/api/admin/overview").status_code, 401)
        self.assertEqual(
            self.client.post(
                "/api/auth/login", json={"username": "admin", "password": "wrong"}
            ).status_code,
            401,
        )
        login = self.login()
        cookie = login.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)
        changed = self.client.post(
            "/api/auth/change-password",
            json={
                "current_password": "AdminPassword123!",
                "new_password": "ChangedPassword123!",
            },
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        self.assertEqual(
            self.client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "ChangedPassword123!"},
            ).status_code,
            200,
        )
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_expired_admin_session_is_rejected(self) -> None:
        self.login()
        from server import db

        with db.connection() as conn:
            conn.execute(
                "UPDATE admin_sessions SET expires_at = '2000-01-01T00:00:00+00:00'"
            )
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_dynamic_runner_registry_token_rotation_and_disable(self) -> None:
        self.login()
        runners = [self.create_runner(f"user{number}") for number in range(1, 5)]
        self.assertEqual(len(self.client.get("/api/admin/runners").json()["items"]), 4)

        for runner in runners:
            self.assertEqual(
                self.client.post("/api/runner/heartbeat", headers=self.headers(runner)).status_code,
                200,
            )
        from server.main import app

        with TestClient(app) as token_only:
            token_only.headers["Origin"] = "http://127.0.0.1:8000"
            token_cannot_admin = token_only.get(
                "/api/admin/overview", headers=self.headers(runners[0])
            )
            self.assertEqual(token_cannot_admin.status_code, 401)
        admin_cannot_runner = self.client.post("/api/runner/heartbeat")
        self.assertEqual(admin_cannot_runner.status_code, 401)

        old_headers = self.headers(runners[0])
        rotated = self.client.post(
            f"/api/admin/runners/{runners[0]['id']}/rotate-token", json={}
        ).json()
        self.assertNotEqual(rotated["runner_token"], runners[0]["runner_token"])
        self.assertEqual(
            self.client.post("/api/runner/heartbeat", headers=old_headers).status_code, 401
        )
        self.assertEqual(
            self.client.post(
                "/api/runner/heartbeat",
                headers={"Authorization": f"Bearer {rotated['runner_token']}"},
            ).status_code,
            200,
        )

        self.client.patch(
            f"/api/admin/runners/{runners[1]['id']}", json={"enabled": False}
        )
        self.assertEqual(
            self.client.post(
                "/api/runner/heartbeat", headers=self.headers(runners[1])
            ).status_code,
            401,
        )
        self.client.patch(
            f"/api/admin/runners/{runners[1]['id']}", json={"enabled": True}
        )
        self.assertEqual(
            self.client.post(
                "/api/runner/heartbeat", headers=self.headers(runners[1])
            ).status_code,
            200,
        )

    def test_rules_activity_and_skill_proposal(self) -> None:
        self.login()
        source = self.create_runner("user1")
        headers = self.headers(source)
        current = self.client.get("/api/admin/global-contract").json()
        self.assertEqual(
            self.client.put("/api/admin/global-contract", json={
                "organization_guidance": "# New Rule",
                "selected_rag_required": True,
                "shared_skill_policy": "recommended",
                "expected_contract_revision": current["contract_revision"],
            }).status_code,
            200,
        )
        from server.main import app

        with TestClient(app) as token_only:
            token_only.headers["Origin"] = "http://127.0.0.1:8000"
            self.assertEqual(
                token_only.put(
                    "/api/admin/global-contract",
                    headers=headers,
                    json={"organization_guidance": "runner must not publish",
                          "selected_rag_required": True, "shared_skill_policy": "recommended",
                          "expected_contract_revision": 1},
                ).status_code,
                401,
            )

        activity = self.event(
            headers,
            {
                "type": "activity_record",
                "kind": "method",
                "name": "Review Method",
                "purpose": "记录用途元数据",
                "recorded_at": "2026-09-27T09:00:00+00:00",
            },
        )
        self.assertEqual(activity.status_code, 200, activity.text)
        rejected_prompt = self.event(
            headers,
            {
                "type": "activity_record",
                "kind": "method",
                "name": "Unsafe Detail",
                "purpose": "test",
                "recorded_at": "2026-09-27T09:00:00+00:00",
                "prompt": "full private work content",
            },
        )
        self.assertEqual(rejected_prompt.status_code, 422)

        event_id = str(uuid.uuid4())
        proposal = {
            "event_id": event_id,
            "type": "skill_change_proposal",
            "skill_name": "Review Notes",
            "base_version": "v0.1",
            "proposed_version": "v0.2",
            "summary": "稳定改进",
            "content": "# Review Notes\n\nCandidate content.",
        }
        first = self.client.post("/api/runner/events", headers=headers, json=proposal)
        duplicate = self.client.post("/api/runner/events", headers=headers, json=proposal)
        self.assertFalse(first.json()["duplicate"])
        self.assertTrue(duplicate.json()["duplicate"])
        proposal_id = first.json()["result"]["proposal_id"]
        decided = self.client.post(
            f"/api/admin/skill-proposals/{proposal_id}/decision",
            json={"decision": "publish", "feedback": ""},
        )
        self.assertEqual(decided.status_code, 200, decided.text)
        skills = self.client.get("/api/admin/shared-skills").json()["items"]
        self.assertEqual(skills[0]["version"], "v0.2")

        returned = self.event(
            headers,
            {
                "type": "skill_change_proposal",
                "skill_name": "Review Notes",
                "base_version": "v0.2",
                "proposed_version": "v0.3",
                "summary": "另一项候选改进",
                "content": "# Review Notes v0.3",
            },
        ).json()["result"]["proposal_id"]
        revision_before_return = self.client.get("/api/admin/global-rules").json()["revision"]
        response = self.client.post(
            f"/api/admin/skill-proposals/{returned}/decision",
            json={"decision": "return", "feedback": "请补充适用范围"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        sync = self.client.get("/api/runner/sync", headers=headers).json()
        returned_item = next(
            item for item in sync["skill_proposals"] if item["proposed_version"] == "v0.3"
        )
        self.assertEqual(returned_item["feedback"], "请补充适用范围")
        self.assertGreater(sync["revision"], revision_before_return)

    def test_ordinary_collaboration_and_admin_escalation(self) -> None:
        self.login()
        source = self.create_runner("user1")
        target = self.create_runner("user2")
        source_headers = self.headers(source)
        target_headers = self.headers(target)

        ordinary = self.event(
            source_headers,
            {
                "type": "collaboration_request",
                "to_runner_id": target["id"],
                "category": "ordinary",
                "topic": "核对资料",
                "summary": "请确认资料是否完整",
            },
        ).json()["result"]["collaboration_id"]
        queue = self.client.get("/api/admin/work-queue").json()
        self.assertFalse(queue["judgments"])
        confirmed = self.event(
            target_headers,
            {
                "type": "collaboration_response",
                "collaboration_id": ordinary,
                "action": "confirm",
                "note": "已核对",
            },
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)

        escalated = self.event(
            source_headers,
            {
                "type": "collaboration_request",
                "to_runner_id": target["id"],
                "category": "ordinary",
                "topic": "需要升级",
                "summary": "双方无法闭环",
            },
        ).json()["result"]["collaboration_id"]
        self.event(
            target_headers,
            {
                "type": "collaboration_response",
                "collaboration_id": escalated,
                "action": "escalate",
                "note": "请管理员判断",
            },
        )
        queue = self.client.get("/api/admin/work-queue").json()
        self.assertEqual([item["id"] for item in queue["judgments"]], [escalated])
        decision = self.client.post(
            f"/api/admin/collaborations/{escalated}/decision",
            json={"decision": "return", "feedback": "请补充事实依据"},
        )
        self.assertEqual(decision.json()["item"]["status"], "returned")

        risk = self.event(
            source_headers,
            {
                "type": "collaboration_request",
                "to_runner_id": target["id"],
                "category": "risk",
                "topic": "重大风险",
                "summary": "需要管理员判断",
            },
        ).json()["result"]["collaboration_id"]
        queue = self.client.get("/api/admin/work-queue").json()
        self.assertIn(risk, [item["id"] for item in queue["judgments"]])

    def test_operating_contract_publish_history_and_legacy_migration(self) -> None:
        import json
        from server import db
        from rdos_contract import render_rules
        from runner.runner import apply_snapshot, runtime_directory

        self.login()
        first = self.client.get("/api/admin/global-contract").json()
        self.assertEqual(first["contract"]["contract_version"], "1")
        self.assertIn("installation_self_test", first["contract"]["events"]["allowed"])
        payload = {"organization_guidance": first["contract"]["organization_guidance"],
                   "selected_rag_required": True, "shared_skill_policy": "recommended",
                   "expected_contract_revision": 1}
        self.assertEqual(self.client.put("/api/admin/global-contract", json=payload).json()["shared_revision"],
                         first["shared_revision"])
        payload["organization_guidance"] = "新的组织说明"
        changed = self.client.put("/api/admin/global-contract", json=payload)
        self.assertEqual(changed.status_code, 200, changed.text)
        current = changed.json()
        self.assertEqual(current["contract_revision"], 2)
        self.assertEqual(current["shared_revision"], first["shared_revision"] + 1)
        self.assertEqual(self.client.put("/api/admin/global-contract", json=payload).status_code, 409)
        self.assertEqual(self.client.put("/api/admin/global-contract", json={**payload, "unknown": True}).status_code, 422)
        self.assertEqual(self.client.put("/api/admin/global-contract", json={**payload, "selected_rag_required": "true"}).status_code, 422)
        self.assertEqual(self.client.put("/api/admin/global-rules", json={"content": "旧写入"}).status_code, 409)
        self.assertEqual(self.client.get("/api/admin/global-rules").json()["content"],
                         render_rules(current["contract"]))

        runner = self.create_runner("contract-test")
        response = self.client.get("/api/runner/sync", headers=self.headers(runner)).json()
        workspace = Path(self.temp_dir.name) / "workspace"
        apply_snapshot(workspace, runner["id"], response)
        active = runtime_directory(workspace)
        self.assertEqual(json.loads((active / "shared/global_contract.json").read_text())["organization_guidance"],
                         "新的组织说明")
        self.assertEqual((active / "shared/global_rules.md").read_text(), render_rules(current["contract"]))
        self.assertEqual(json.loads((workspace / "runtime.json").read_text())["contract_hash"], current["contract_hash"])

        restored = self.client.post("/api/admin/global-contract/history/1/restore",
                                    json={"expected_contract_revision": 2}).json()
        self.assertEqual(restored["contract_revision"], 3)
        self.assertEqual(restored["contract"]["organization_guidance"], first["contract"]["organization_guidance"])
        self.assertEqual(len(self.client.get("/api/admin/global-contract/history").json()["items"]), 3)
        db.init_db()
        self.assertEqual(db.get_global_contract()["contract_revision"], 3)

        # Simulate a v6 database to prove the old handwritten text is retained and backed up.
        with db.connection() as conn:
            conn.execute("DELETE FROM global_contract_history")
            conn.execute("DELETE FROM settings WHERE key='current_contract_revision'")
            conn.execute("UPDATE settings SET value='6' WHERE key='schema_version'")
            conn.execute("UPDATE settings SET value='旧规则正文' WHERE key='current_rule'")
        db.init_db()
        self.assertEqual(db.get_global_contract()["contract"]["organization_guidance"], "旧规则正文")
        self.assertTrue(list((Path(self.temp_dir.name) / "backups").glob("pre-schema-v7-*.db")))


if __name__ == "__main__":
    unittest.main()
