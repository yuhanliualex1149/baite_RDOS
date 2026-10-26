"""Live loopback integration checks. These are explicitly script simulations."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import httpx

from lab import config


def main():
    cfg = config.load()
    instance = config.instance(cfg)
    credentials = config.read_json(config.ROOT / "runtime/credentials.json")
    base = f'http://127.0.0.1:{cfg["port"]}'
    checks, feedback_ids = [], {}

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append({"name": name, "passed": True})

    with httpx.Client(base_url=base, trust_env=False, follow_redirects=False, timeout=10) as client:
        info = client.get("/api/lab/info").json()
        check("explicit_lab_identity", info["env_id"] == cfg["env_id"] and info["generation"] == instance["generation"])
        page = client.get("/")
        check("real_panel_assets_and_banner", page.status_code == 200 and "RDOS 本地实验环境" in page.text)
        login = client.post("/api/central/auth/login", headers={"Origin": base}, json={
            "username": credentials["admin_username"], "password": credentials["admin_password"]})
        check("real_panel_login", login.status_code == 200)
        check("isolated_cookie_name", cfg["env_id"] in login.headers.get("set-cookie", "") and instance["generation"] in login.headers.get("set-cookie", ""))
        workspace = client.get("/api/central/workspace", params={"project_id": "lab_project_phase0"})
        check("synthetic_workspace", workspace.status_code == 200 and "lab_task_codex" in workspace.text and "lab_task_workbuddy" in workspace.text)
        for method, path in (("GET", "/api/central/onboarding/setup"), ("POST", "/api/central/chat"),
                             ("POST", "/api/central/sync"), ("POST", "/api/central/runtime"),
                             ("POST", "/api/central/tasks"), ("GET", "/api/runner/sync"), ("GET", "/legacy")):
            response = client.request(method, path, headers={"Origin": base})
            check("disabled:" + path, response.status_code == 503)
        check("missing_node_auth", client.get("/api/lab/tasks", headers={"X-RDOS-Env-ID": cfg["env_id"],
              "X-RDOS-Generation": instance["generation"]}).status_code == 401)
        for node in ("node_lab_codex", "node_lab_workbuddy"):
            headers = {"Authorization": "Bearer " + credentials["node_tokens"][node], "X-RDOS-Env-ID": cfg["env_id"],
                       "X-RDOS-Generation": instance["generation"]}
            context = client.get("/api/lab/context", headers=headers)
            check(node + ":context", context.status_code == 200 and context.json()["node_id"] == node)
            tasks = client.get("/api/lab/tasks", headers=headers)
            other = "lab_task_workbuddy" if node == "node_lab_codex" else "lab_task_codex"
            check(node + ":only_own_task", tasks.status_code == 200 and other not in tasks.text)
            material = client.get("/api/lab/documents/lab_doc_intro", headers=headers)
            check(node + ":read_document", material.status_code == 200 and "LAB-BLUE-07" in material.text)
            operation = "script-selftest-" + uuid.uuid4().hex
            payload = {"operation_id": operation, "content": "脚本自测记录；local_mock；不代表真实客户端。"}
            written = client.post("/api/lab/feedback", headers=headers, json=payload)
            check(node + ":submit", written.status_code == 200 and written.json()["status"] == "created")
            receipt = written.json()
            feedback_ids[node] = receipt["feedback_id"]
            check(node + ":submit_not_readback", receipt["readback_verified"] is False and "content" not in receipt)
            duplicate = client.post("/api/lab/feedback", headers=headers, json=payload).json()
            check(node + ":deduplicate", duplicate["feedback_id"] == receipt["feedback_id"] and duplicate["status"] == "duplicate")
            conflict = client.post("/api/lab/feedback", headers=headers, json={**payload, "content": "different"})
            check(node + ":operation_conflict", conflict.status_code == 409)
            readback = client.get("/api/lab/feedback/" + receipt["feedback_id"], headers=headers).json()
            check(node + ":independent_readback", readback["content"] == payload["content"] and readback["content_hash"] == receipt["content_hash"])
            forged = client.post("/api/lab/feedback", headers=headers, json={**payload, "member_id": "lab_wrong_member"})
            check(node + ":forged_identity", forged.status_code == 403)
            stale = client.get("/api/lab/tasks", headers={**headers, "X-RDOS-Generation": "stale-script-generation"})
            check(node + ":stale_generation", stale.status_code == 409)
        headers["Authorization"] = "Bearer " + credentials["node_tokens"]["node_lab_codex"]
        crossed = client.get("/api/lab/feedback/" + feedback_ids["node_lab_workbuddy"], headers=headers)
        check("cross_node_feedback_read_denied", crossed.status_code == 404)
        client.post("/api/central/auth/logout", headers={"Origin": base})
    result = {"tested_at": datetime.now(timezone.utc).isoformat(), "evidence_kind": "script_selftest",
              "env_id": cfg["env_id"], "generation": instance["generation"], "checks": checks,
              "script_feedback_ids": feedback_ids, "real_client_acceptance": "not_tested"}
    config.write_json(config.ROOT / "evidence/live-smoke.json", result)
    print(json.dumps({"passed": len(checks), "evidence_kind": "script_selftest", "real_clients": "not_tested"}))


if __name__ == "__main__":
    main()
