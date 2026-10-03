"""Packaged Runtime against an isolated local Panel, never the production server."""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.e2e_demo import free_port, stop


def wait_for(check, label, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (OSError, httpx.HTTPError, ValueError, KeyError):
            pass
        time.sleep(0.5)
    raise AssertionError(label + " did not complete")


def main(artifacts: Path):
    executable = artifacts / "dist" / "rdos-runner" / ("rdos-runner.exe" if os.name == "nt" else "rdos-runner")
    with tempfile.TemporaryDirectory(prefix="rdos-frozen-e2e-") as temporary:
        base = Path(temporary)
        port = free_port()
        url = f"http://127.0.0.1:{port}"
        environment = {**os.environ, "BAITE_DB_PATH": str(base / "baite.db"), "BAITE_PUBLIC_URL": url,
                       "BAITE_ADMIN_USERNAME": "admin", "BAITE_ADMIN_PASSWORD": "IsolatedPassword123!",
                       "BAITE_SESSION_SECRET": "frozen-e2e-isolated-test-secret-value",
                       "BAITE_DISABLE_EXTERNAL_SYNC": "true", "BAITE_COOKIE_SECURE": "false"}
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "server.main:app", "--host", "127.0.0.1",
                                   "--port", str(port), "--log-level", "error"], env=environment,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        runner = None
        runtime_log = base / "runtime.log"
        try:
            wait_for(lambda: httpx.get(url + "/api/health").status_code == 200, "Panel")
            with httpx.Client(base_url=url, headers={"Origin": url}) as client:
                assert client.post("/api/auth/login", json={"username": "admin", "password": "IsolatedPassword123!"}).status_code == 200
                created = client.post("/api/admin/runners", json={"display_name": "packaged-test", "delivery": "enrollment"}).json()
                node = created["item"]["id"]
                code = created["enrollment"]["enrollment_code"]
                installation_id = uuid.uuid4().hex
                token = "brt_" + secrets.token_urlsafe(48)
                assert client.post("/api/bootstrap/claim", json={"runner_id": node, "enrollment_code": code,
                                   "installation_id": installation_id, "candidate_token": token}).status_code == 200
                work = base / "workspace"
                config = base / "config.json"
                config.write_text(json.dumps({"control_url": url, "runner_id": node, "runner_token": token,
                                              "workspace": str(work), "installation_id": installation_id,
                                              "bootstrapper_version": "0.1.0", "protocol_version": "1"}), encoding="utf-8")
                config.chmod(0o600)
                runtime_env = os.environ.copy()
                for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX"):
                    runtime_env.pop(key, None)
                with runtime_log.open("w", encoding="utf-8") as output:
                    runner = subprocess.Popen([str(executable), "--config", str(config), "--poll-seconds", "0.5"],
                                              env=runtime_env, stdout=output, stderr=subprocess.STDOUT)
                auth = {"Authorization": "Bearer " + token}
                def synced():
                    if runner.poll() is not None:
                        raise AssertionError("Frozen Runner exited: " + runtime_log.read_text(encoding="utf-8", errors="replace")[-1500:])
                    status = client.get("/api/runner/installation-status", headers=auth).json()
                    return status["heartbeat_at"] and status["sync_health"] == "healthy" and (work / "runtime.json").is_file()
                try:
                    wait_for(synced, "frozen Runtime heartbeat and snapshot")
                except AssertionError as exc:
                    raise AssertionError(str(exc) + "\n" + runtime_log.read_text(encoding="utf-8", errors="replace")[-2000:]) from exc
                reported = client.get("/api/admin/runners").json()["items"][0]
                assert reported["runner_version"] == "0.1.0-candidate", reported["runner_version"]
                revision = json.loads((work / "runtime.json").read_text(encoding="utf-8"))["revision"]
                event_id = str(uuid.uuid4())
                outbox = work / "outbox"
                outbox.mkdir(exist_ok=True)
                (outbox / (event_id + ".json")).write_text(json.dumps({"type": "installation_self_test",
                    "event_id": event_id, "installation_id": installation_id,
                    "shared_revision": revision, "project_count": 0}), encoding="utf-8")
                wait_for(lambda: (outbox / "sent" / (event_id + ".json")).is_file(), "diagnostic ACK")
                evidence = client.get("/api/admin/runners/" + node + "/installation").json()
                assert evidence["self_test"]["event_id"] == event_id
                print("FROZEN E2E PASS: heartbeat, verified snapshot, diagnostic ACK, server receipt")
        finally:
            if runner:
                stop(runner)
            stop(server)


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
