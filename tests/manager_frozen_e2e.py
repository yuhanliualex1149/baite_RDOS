"""Upgrade two frozen Runtime copies through a real isolated Panel and Workspace."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bootstrapper import core
from bootstrapper.manager import RunnerManager, initial_state
from tests.e2e_demo import free_port, stop
from tests.frozen_runtime_e2e import wait_for


def runtime_copy(source: Path, destination: Path, version: str) -> None:
    shutil.copytree(source, destination, symlinks=True)
    (destination / "_internal" / "release.json").write_text(json.dumps({"release": version}) + "\n", encoding="utf-8")


def archive_runtime(folder: Path, archive: Path) -> str:
    if os.name == "nt":
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for path in folder.rglob("*"):
                if path.is_file():
                    target.write(path, path.relative_to(folder.parent))
        return "zip"
    with tarfile.open(archive, "w:gz", dereference=False) as target:
        target.add(folder, arcname="rdos-runner", recursive=True)
    return "tar.gz"


def main(artifacts: Path) -> None:
    source = artifacts / "dist" / "rdos-runner"
    old_version, new_version = "0.1.0-isolated", "0.1.1-isolated"
    with tempfile.TemporaryDirectory(prefix="rdos-manager-e2e-") as temporary:
        base = Path(temporary)
        install = base / "install"
        install.mkdir()
        workspace = base / "workspace"
        old_folder = install / "runtime" / old_version / "rdos-runner"
        new_folder = base / "candidate" / "rdos-runner"
        old_folder.parent.mkdir(parents=True)
        new_folder.parent.mkdir(parents=True)
        runtime_copy(source, old_folder, old_version)
        runtime_copy(source, new_folder, new_version)
        archive = base / ("candidate.zip" if os.name == "nt" else "candidate.tar.gz")
        kind = archive_runtime(new_folder, archive)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        port = free_port()
        url = f"http://127.0.0.1:{port}"
        key = core.platform_key()
        artifact = {"version": new_version, "url": "https://example.invalid/rdos/downloads/" + archive.name,
                    "size": archive.stat().st_size, "sha256": digest, "archive": kind,
                    "protocol_version": core.PROTOCOL, "supported_contract_versions": ["1"],
                    "min_manager_version": core.VERSION, "min_bootstrapper_version": core.VERSION}
        manifest_path = base / "manifest.json"
        manifest_path.write_text(json.dumps({"schema_version": 1, "protocol_version": core.PROTOCOL,
                                             "platforms": {key: {"runtime": artifact}}}), encoding="utf-8")
        env = {**os.environ, "BAITE_DB_PATH": str(base / "baite.db"), "BAITE_PUBLIC_URL": url,
               "BAITE_ADMIN_USERNAME": "admin", "BAITE_ADMIN_PASSWORD": "IsolatedPassword123!",
               "BAITE_SESSION_SECRET": "manager-e2e-isolated-test-secret-value",
               "BAITE_BOOTSTRAP_MANIFEST_PATH": str(manifest_path),
               "BAITE_DISABLE_EXTERNAL_SYNC": "true", "BAITE_COOKIE_SECURE": "false"}
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "server.main:app", "--host", "127.0.0.1",
                                   "--port", str(port), "--log-level", "error"], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        manager = None
        try:
            wait_for(lambda: httpx.get(url + "/api/health").status_code == 200, "isolated Panel")
            with httpx.Client(base_url=url, headers={"Origin": url}) as client:
                assert client.post("/api/auth/login", json={"username": "admin", "password": "IsolatedPassword123!"}).status_code == 200
                created = client.post("/api/admin/runners", json={"display_name": "manager-e2e", "delivery": "enrollment"}).json()
                node = created["item"]["id"]
                installation_id = uuid.uuid4().hex
                token = "brt_" + secrets.token_urlsafe(48)
                claim = {"runner_id": node, "enrollment_code": created["enrollment"]["enrollment_code"],
                         "installation_id": installation_id, "candidate_token": token}
                assert client.post("/api/bootstrap/claim", json=claim).status_code == 200
                config = {"control_url": url, "runner_id": node, "runner_token": token,
                          "workspace": str(workspace), "installation_id": installation_id,
                          "bootstrapper_version": core.VERSION, "protocol_version": core.PROTOCOL}
                core.private_write(install / "config.json", config)
                core.private_write(install / "runtime-state.json", initial_state(old_version, "a" * 64))
                manager = RunnerManager(install / "config.json", poll_seconds=0.5)
                old_instance = manager.start_child(old_version)
                manager.wait_healthy(old_version, old_instance, timeout=60)
                def local_download(_origin, _artifact, destination):
                    shutil.copy2(archive, destination)
                with patch.object(core, "download_verified", side_effect=local_download):
                    result = manager.check_once()
                assert result == "updated", (result, manager.state)
                assert manager.state["current"] == new_version
                new_instance = manager.state["child_instance_id"]
                assert new_instance != old_instance
                auth = {"Authorization": "Bearer " + token}
                status = client.get("/api/runner/installation-status", headers=auth).json()
                assert status["process_instance_id"] == new_instance
                assert status["sync_instance_id"] == new_instance
                reported = client.get("/api/admin/runners").json()["items"][0]
                assert reported["update_state"] == "updated" and reported["update_current_version"] == new_version
                revision = json.loads((workspace / "runtime.json").read_text(encoding="utf-8"))["revision"]
                event_id = str(uuid.uuid4())
                outbox = workspace / "outbox"
                outbox.mkdir(exist_ok=True)
                (outbox / (event_id + ".json")).write_text(json.dumps({"type": "installation_self_test",
                    "event_id": event_id, "installation_id": installation_id,
                    "shared_revision": revision, "project_count": 0}), encoding="utf-8")
                wait_for(lambda: (outbox / "sent" / (event_id + ".json")).is_file(), "upgraded Runner ACK")
                failed_version = "0.1.2-isolated"
                failed_folder = base / "failed-candidate" / "rdos-runner"
                failed_folder.parent.mkdir(parents=True)
                runtime_copy(source, failed_folder, failed_version)
                failed_archive = base / ("failed-candidate.zip" if os.name == "nt" else "failed-candidate.tar.gz")
                archive_runtime(failed_folder, failed_archive)
                failed_artifact = {**artifact, "version": failed_version,
                                   "size": failed_archive.stat().st_size,
                                   "sha256": hashlib.sha256(failed_archive.read_bytes()).hexdigest()}
                manifest_path.write_text(json.dumps({"schema_version": 1, "protocol_version": core.PROTOCOL,
                    "platforms": {key: {"runtime": failed_artifact}}}), encoding="utf-8")
                with patch.object(core, "download_verified", side_effect=lambda _o, _a, d: shutil.copy2(failed_archive, d)), \
                     patch.object(manager, "wait_healthy", side_effect=ValueError("模拟候选实例无健康心跳")):
                    assert manager.check_once() == "rolled_back"
                assert manager.state["current"] == new_version
                assert manager.state["failed"] == {"version": failed_version, "sha256": failed_artifact["sha256"]}
                manager.wait_healthy(new_version, manager.state["child_instance_id"], timeout=60)
                manager._stop_recorded_child()
                if os.name == "nt":
                    stable = install / "manager" / "rdos-runner"
                    shutil.copytree(source, stable)
                    gui = stable / "rdos-runner.exe"
                else:
                    gui = artifacts / "dist" / "RDOS Runner.app" / "Contents" / "MacOS" / "RDOS Runner"
                frozen_env = os.environ.copy()
                for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX", "DYLD_LIBRARY_PATH"):
                    frozen_env.pop(name, None)
                frozen_env["PATH"] = (os.path.join(frozen_env.get("SystemRoot", r"C:\Windows"), "System32")
                                      if os.name == "nt" else "/usr/bin:/bin")
                frozen_manager = subprocess.Popen([str(gui), "--manager", "--config", str(install / "config.json"),
                    "--poll-seconds", "0.5"], env=frozen_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    def managed_child_ready():
                        state = json.loads((install / "runtime-state.json").read_text(encoding="utf-8"))
                        return (frozen_manager.poll() is None and state["child_pid"] > 0
                                and state["child_instance_id"] != new_instance
                                and client.get("/api/runner/installation-status", headers=auth).json().get(
                                    "process_instance_id") == state["child_instance_id"])
                    try:
                        wait_for(managed_child_ready, "frozen Manager did not launch current Runtime")
                    except AssertionError as exc:
                        state = json.loads((install / "runtime-state.json").read_text(encoding="utf-8"))
                        log_path = install / "logs" / "manager-error.log"
                        error_log = log_path.read_text(encoding="utf-8")[-300:] if log_path.is_file() else "(none)"
                        runtime_log = install / "logs" / "manager-runtime.log"
                        runtime_tail = runtime_log.read_text(encoding="utf-8", errors="replace")[-300:] if runtime_log.is_file() else "(none)"
                        remote = client.get("/api/runner/installation-status", headers=auth).json()
                        raise AssertionError(f"{exc}; manager_exit={frozen_manager.poll()}; "
                            f"phase={state['phase']}; child_pid={state['child_pid']}; "
                            f"child_instance={state['child_instance_id']}; "
                            f"remote_instance={remote.get('process_instance_id')}; "
                            f"manager_error={error_log}; runtime_log={runtime_tail}") from exc
                finally:
                    managed_pid = json.loads((install / "runtime-state.json").read_text(encoding="utf-8"))["child_pid"]
                    if frozen_manager.poll() is None:
                        frozen_manager.send_signal(signal.SIGTERM)
                    frozen_manager.wait(timeout=15)
                    if managed_pid:
                        wait_for(lambda: not manager._recorded_command(managed_pid),
                                 "Manager stop left Runtime child running", seconds=10)
                    manager.state = json.loads((install / "runtime-state.json").read_text(encoding="utf-8"))
                    manager._stop_recorded_child()
                print("MANAGER FROZEN E2E PASS: upgrade, ACK, failed candidate rollback, frozen Manager supervision")
        finally:
            if manager:
                try:
                    manager._stop_recorded_child()
                except Exception:
                    if manager.child:
                        stop(manager.child)
            stop(server)


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
