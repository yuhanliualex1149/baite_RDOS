"""Native installer smoke: launchd / Task Scheduler with an isolated fake Control API.

Never uses a real Runner Token or writes to the deployed Panel.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy import install_macos_runner as installer
from rdos_protocol import seal_snapshot
from tests.test_runner import snapshot
from runner.runner import runtime_directory


def wait_for(check, description):
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.2)
    raise AssertionError(description)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", help="Full Git SHA to download; never main/latest")
    parser.add_argument("--local-runtime", action="store_true", help="Test current candidate files using a simulated GitHub tree")
    args = parser.parse_args()
    if not args.release and not args.local_runtime:
        parser.error("choose --release or --local-runtime")
    if args.local_runtime:
        args.release = "a" * 40
    if sys.platform not in {"darwin", "win32"} or sys.version_info < (3, 10):
        raise SystemExit("Requires macOS Python 3.10+ or Windows Python 3.13 x64")
    os.umask(0o077)
    events, heartbeats = [], []
    token = "test-token-" + uuid.uuid4().hex
    node = "rnr_smoke_" + uuid.uuid4().hex[:12]

    class API(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, payload, code=200):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/baite-rdos/api/health":
                return self.reply({"app_release": args.release})
            if self.headers.get("Authorization") != "Bearer " + token:
                return self.reply({}, 401)
            if self.path.startswith("/baite-rdos/api/runner/projects/sync"):
                return self.reply({**seal_snapshot({"sync_token": "test-1", "projects": []}), "changed": True})
            if self.path.startswith("/baite-rdos/api/runner/sync"):
                data = snapshot(7)
                data.update(retry_nonce=0)
                return self.reply({**seal_snapshot(data), "changed": True})
            return self.reply({}, 404)

        def do_POST(self):
            if self.headers.get("Authorization") != "Bearer " + token:
                return self.reply({}, 401)
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path.endswith("/events"):
                event = json.loads(body)
                events.append(event["event_id"])
                return self.reply({"ack": True, "event_id": event["event_id"]})
            if self.path.endswith("/heartbeat"):
                heartbeats.append(time.time())
            return self.reply({"ok": True})

    server = ThreadingHTTPServer(("127.0.0.1", 0), API)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="rdos-installer-smoke-") as temporary:
            base = Path(temporary).resolve()
            work, root = base / "中文 workspace", base / "Application Support/runners"
            plist = base / "LaunchAgents" / (installer.service_label(node) + ".plist")
            config = base / "input.json"
            config.write_text(json.dumps({"runner_id": node, "runner_token": token,
                                          "control_url": f"http://127.0.0.1:{server.server_port}/baite-rdos",
                                          "workspace": str(work)}))
            real_fetch = installer.fetch
            def fixture_fetch(url, token=""):
                if not args.local_runtime or url.startswith("http://127.0.0.1:"):
                    return real_fetch(url, token)
                root = Path(__file__).resolve().parents[1]
                files = {name: (root / name).read_bytes() for name in installer.RUNTIME_FILES}
                if "/git/trees/" in url:
                    return json.dumps({"tree": [{"path": name, "type": "blob", "mode": "100644", "sha": installer.git_blob_hash(content)} for name, content in files.items()]}).encode()
                name = url.split(args.release + "/", 1)[1]
                return files[name]
            with patch.object(installer, "plist_path", return_value=plist), patch.object(installer, "fetch", side_effect=fixture_fetch):
                try:
                    assert not root.exists() and not work.exists()
                    directory = installer.install(config, None, root)
                    def synced():
                        if (work / "runtime.json").is_file():
                            return (runtime_directory(work) / "control/sync.json").is_file()
                        return (work / "control/sync.json").is_file()
                    wait_for(synced, "Runner never synced")
                    assert heartbeats
                    info = installer.status(node, root)
                    assert info["running"] and info["revision"] == 7, info
                    original_instance = info["instance_id"] if installer.WINDOWS else info["pid"]
                    assert original_instance
                    (work / "work/keep.md").write_text("user work")
                    # Second installation must preserve user work and the already-running process.
                    installer.install(config, None, root)
                    again = installer.status(node, root)
                    assert (again["instance_id"] if installer.WINDOWS else again["pid"]) == original_instance
                    assert (work / "work/keep.md").read_text() == "user work"
                    installer.stop_service(node)
                    assert not installer.status(node, root)["running"]
                    installer.start_service(node)
                    try:
                        wait_for(lambda: installer.status(node, root)["running"], "Restart failed")
                    except AssertionError:
                        print(json.dumps(installer.status(node, root), ensure_ascii=False))
                        print((directory / "logs/runner.log").read_text(encoding="utf-8")[-4000:].replace(token, "[redacted]"))
                        raise
                    # Rotate only this fake API's credential, then reinstall the same node.
                    old_token = token
                    token = "rotated-test-token-" + uuid.uuid4().hex
                    payload = json.loads(config.read_text(encoding="utf-8"))
                    payload["runner_token"] = token
                    installer.atomic_write(config, json.dumps(payload).encode())
                    installer.install(config, None, root)
                    wait_for(lambda: installer.status(node, root)["running"], "Credential update restart failed")
                    event_id = "installer-smoke-" + uuid.uuid4().hex
                    event = {"event_id": event_id, "type": "progress", "current_focus": "installer test",
                             "status": "working", "summary": "Isolated test", "needs_collaboration": ""}
                    installer.atomic_write(work / "outbox" / (event_id + ".json"), json.dumps(event).encode())
                    wait_for(lambda: (work / "outbox/sent" / (event_id + ".json")).is_file(), "No ACK")
                    assert events.count(event_id) == 1
                    log = (directory / "logs/runner.log").read_text(encoding="utf-8")
                    assert token not in log and old_token not in log
                    installer.uninstall(node, root)
                    assert not directory.exists() and (work / "work/keep.md").is_file()
                    print("PASS: verified runtime, isolated venv, native background task, sync, repeat/instance, stop/start, ACK, credential redaction, uninstall preserving work")
                finally:
                    if installer.service_info(node).returncode == 0:
                        installer.stop_service(node)
                    if installer.WINDOWS:
                        installer.windows_task(node, "delete")
                    else:
                        subprocess.run(["launchctl", "enable", f"gui/{os.getuid()}/{installer.service_label(node)}"],
                                       check=True, capture_output=True)
                    assert installer.service_info(node).returncode != 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    print("CLEAN: disposable service and its files removed; no cloud writes")


if __name__ == "__main__":
    main()
