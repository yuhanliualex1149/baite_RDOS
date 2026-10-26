"""Explicit destructive lab-data test. Requires the current environment ID."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone

import httpx

from lab import config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    root = config.ROOT
    cfg = config.load()
    if args.confirm != cfg["env_id"]:
        raise SystemExit("环境标识未确认；未执行生命周期测试")
    old = config.instance(cfg)
    old_access = config.read_json(root / "nodes/codex/.lab-access.json")
    base = f'http://127.0.0.1:{cfg["port"]}'
    checks = []

    def check(name, passed):
        if not passed:
            raise AssertionError(name)
        checks.append({"name": name, "passed": True})

    def ctl(action, *extra, injected=False):
        env = dict(os.environ)
        if injected:
            env.update(ARK_API_KEY="synthetic-inheritance-test", BAITE_LLM_API_KEY="synthetic-inheritance-test",
                       BAITE_DB_PATH=str(root / "evidence/MUST_NOT_CREATE.sqlite3"), BAITE_ENABLE_LEGACY_SYNC="true")
        result = subprocess.run([str(root / ".venv/bin/python"), "-B", str(root / "code/labctl.py"), action, *extra],
                                cwd=root / "code", env=env, capture_output=True, text=True, timeout=35)
        if result.returncode:
            raise AssertionError("labctl " + action + " failed: " + result.stderr)
        return json.loads(result.stdout)

    check("stop_owned_instance", ctl("stop")["running"] is False)
    database = root / "runtime/panel.sqlite3"
    with sqlite3.connect(database) as conn:
        original_count = conn.execute("SELECT COUNT(*) FROM lab_feedback").fetchone()[0]
        original_events = conn.execute("SELECT COUNT(*) FROM lab_events").fetchone()[0]
        conn.execute("INSERT OR REPLACE INTO central_records VALUES(?,?,?,?)", (
            "run", "lab_script_pending", json.dumps({"id": "lab_script_pending", "status": "queued",
             "project_id": "lab_project_phase0", "trigger": "scheduled"}), "2000-01-01T00:00:00+00:00"))
        conn.execute("INSERT OR REPLACE INTO central_records VALUES(?,?,?,?)", (
            "state", "runtime", json.dumps({"paused": False, "next_due": "2000-01-01T00:00:00+00:00",
             "pending": True, "retry": True}), "2000-01-01T00:00:00+00:00"))
    restarted = ctl("start", injected=True)
    check("restart_same_generation", restarted["running"] and restarted["generation"] == old["generation"])
    check("parent_business_environment_not_inherited", not (root / "evidence/MUST_NOT_CREATE.sqlite3").exists())
    # Scheduling absence is also asserted structurally in the unit tests; this
    # pause only checks that restart does not immediately replay pending work.
    time.sleep(1)
    with sqlite3.connect(database) as conn:
        check("test_feedback_survives_restart", conn.execute("SELECT COUNT(*) FROM lab_feedback").fetchone()[0] == original_count)
        check("restart_does_not_fabricate_node_events", conn.execute("SELECT COUNT(*) FROM lab_events").fetchone()[0] == original_events)
        pending = json.loads(conn.execute("SELECT payload FROM central_records WHERE kind='run' AND id='lab_script_pending'").fetchone()[0])
        check("queued_record_not_replayed_or_recovered", pending["status"] == "queued")
    with httpx.Client(base_url=base, trust_env=False, timeout=5) as client:
        check("runtime_reports_disabled_scheduler", client.get("/api/lab/info").json()["runtime"]["scheduler_enabled"] is False)
    check("stop_before_reset", ctl("stop")["running"] is False)
    sentinels = [root / "nodes/codex/manual-preservation-selftest.txt", root / "nodes/workbuddy/manual-preservation-selftest.txt"]
    for path in sentinels:
        if path.exists():
            raise AssertionError("selftest sentinel already exists; will not overwrite")
        path.write_text("synthetic manual file preservation test", encoding="utf-8")
    evidence = root / "evidence/lifecycle-evidence-sentinel.txt"
    evidence.write_text("Evidence must survive reset. This is a script selftest.", encoding="utf-8")
    reset = ctl("reset", "--confirm", cfg["env_id"])
    check("reset_changes_generation", reset["generation"] != old["generation"])
    check("reset_preserves_node_manual_files_and_evidence", evidence.is_file() and all(path.read_text() == "synthetic manual file preservation test" for path in sentinels))
    check("reset_preserves_source", (root / "code/server/main.py").is_file())
    for path in sentinels:
        path.unlink()
    final = ctl("start")
    with sqlite3.connect(database) as conn:
        check("clean_node_feedback_and_events", conn.execute("SELECT COUNT(*) FROM lab_feedback").fetchone()[0] == 0 and conn.execute("SELECT COUNT(*) FROM lab_events").fetchone()[0] == 0)
        check("old_pending_records_removed", conn.execute("SELECT COUNT(*) FROM central_records WHERE id='lab_script_pending'").fetchone()[0] == 0)
    with httpx.Client(base_url=base, trust_env=False, timeout=5) as client:
        stale = client.get("/api/lab/tasks", headers={"Authorization": "Bearer " + old_access["token"],
            "X-RDOS-Env-ID": old_access["env_id"], "X-RDOS-Generation": old_access["generation"]})
        check("old_node_credentials_and_generation_refused", stale.status_code in (401, 409))
    config.write_json(root / "evidence/lifecycle-result.json", {"tested_at": datetime.now(timezone.utc).isoformat(),
        "evidence_kind": "script_selftest", "checks": checks, "final_state": final,
        "old_generation": old["generation"], "new_generation": reset["generation"],
        "real_clients": "not_tested", "pending_test_limit": "No immediate replay observed; no scheduler startup also asserted in unit tests."})
    print(json.dumps({"passed": len(checks), "fresh_generation": reset["generation"], "running": final["running"], "real_clients": "not_tested"}))


if __name__ == "__main__":
    main()
