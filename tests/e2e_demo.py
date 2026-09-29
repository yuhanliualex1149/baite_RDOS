from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request(
    opener,
    base_url: str,
    path: str,
    method: str = "GET",
    payload: Optional[dict] = None,
    headers: Optional[dict] = None,
) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    actual_headers = {"Content-Type": "application/json"} if body else {}
    actual_headers.update(headers or {})
    if path.startswith(("/api/auth/", "/api/admin/")):
        actual_headers["Origin"] = base_url
    req = urllib.request.Request(
        base_url + path, data=body, headers=actual_headers, method=method
    )
    with opener.open(req, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_until(check: Callable[[], bool], message: str, timeout: float = 15) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if check():
                return
        except (urllib.error.URLError, urllib.error.HTTPError, FileNotFoundError, KeyError):
            pass
        time.sleep(0.1)
    raise AssertionError(message)


def stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        process.kill()


def seed_rag(environment: dict) -> None:
    os.environ.update(environment)
    from server import db

    db.init_db()
    content = "# Test Shared Context\n\nThis is an isolated E2E snapshot.\n"
    encoded = content.encode("utf-8")
    item = {
        "source_token": "test-context-token",
        "source_type": "file",
        "source_name": "Context.md",
        "runtime_name": "Context.md",
        "source_url": "https://example.invalid/context",
        "modified_time": "2026-09-27T00:00:00+00:00",
        "content_hash": hashlib.sha256(encoded).hexdigest(),
        "content": content,
        "size_bytes": len(encoded),
    }
    manifest = [{key: value for key, value in item.items() if key != "content"}]
    manifest_text = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    db.commit_rag_snapshot(
        "folder-test",
        hashlib.sha256(manifest_text.encode("utf-8")).hexdigest(),
        manifest,
        [item],
        [],
    )


def main() -> None:
    processes: List[subprocess.Popen] = []
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        port = free_port()
        base_url = f"http://127.0.0.1:{port}"
        environment = os.environ.copy()
        environment.update(
            {
                "BAITE_DB_PATH": str(temp / "baite.db"),
                "BAITE_SESSION_SECRET": "e2e-session-secret-with-enough-length",
                "BAITE_ADMIN_USERNAME": "admin",
                "BAITE_ADMIN_PASSWORD": "AdminPassword123!",
                "BAITE_SELECTED_RAG_FOLDER_TOKEN": "folder-test",
                "BAITE_PUBLIC_URL": base_url,
                "BAITE_ONLINE_WINDOW_SECONDS": "0.6",
            }
        )
        seed_rag(environment)
        cookie_jar = http.cookiejar.CookieJar()
        admin = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
        plain = urllib.request.build_opener()

        def start_server() -> subprocess.Popen:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "server.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--log-level",
                    "warning",
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            wait_until(
                lambda: request(plain, base_url, "/api/health")["status"] == "ok",
                "Control Panel 未启动",
            )
            return process

        def start_runner(config: Path) -> subprocess.Popen:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "runner/runner.py",
                    "--config",
                    str(config),
                    "--poll-seconds",
                    "0.1",
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            processes.append(process)
            return process

        def simulate(config: Path, scenario: str, *extra: str) -> None:
            subprocess.run(
                [sys.executable, "simulate_agent.py", "--config", str(config), scenario, *extra],
                cwd=PROJECT_ROOT,
                env=environment,
                check=True,
                stdout=subprocess.DEVNULL,
            )

        try:
            server = start_server()
            processes.append(server)
            login = request(
                admin,
                base_url,
                "/api/auth/login",
                "POST",
                {"username": "admin", "password": "AdminPassword123!"},
            )
            assert login["admin"]["username"] == "admin"

            runners: Dict[str, dict] = {}
            configs: Dict[str, Path] = {}
            runner_processes: Dict[str, subprocess.Popen] = {}
            for index in range(1, 4):
                name = f"user{index}"
                workspace = temp / "workspace" / name
                created = request(
                    admin,
                    base_url,
                    "/api/admin/runners",
                    "POST",
                    {"display_name": name, "workspace": str(workspace)},
                )["item"]
                runners[name] = created
                config_path = temp / f"{name}.json"
                config_path.write_text(
                    json.dumps(created["config"], ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                config_path.chmod(0o600)
                configs[name] = config_path
                runner_processes[name] = start_runner(config_path)

            for name in runners:
                workspace = temp / "workspace" / name
                wait_until(
                    lambda workspace=workspace: (
                        workspace / "shared" / "selected_rag" / "Context.md"
                    ).is_file(),
                    f"{name} 未收到 RAG Snapshot",
                )
            wait_until(
                lambda: sum(
                    item["online"]
                    for item in request(admin, base_url, "/api/admin/runners")["items"]
                )
                == 3,
                "三个 Runner 未全部 Online",
            )

            request(
                admin,
                base_url,
                "/api/admin/global-rules",
                "PUT",
                {"content": "# E2E Global Rules\n\nLocal Agent 自主决定具体工作方式。"},
            )
            for name in runners:
                rules_path = temp / "workspace" / name / "shared" / "global_rules.md"
                wait_until(
                    lambda path=rules_path: path.is_file()
                    and "E2E Global Rules" in path.read_text(encoding="utf-8"),
                    f"{name} 未同步 Global Rules",
                )

            simulate(configs["user1"], "progress")
            simulate(configs["user1"], "activity", "--kind", "workflow")
            wait_until(
                lambda: next(
                    item
                    for item in request(admin, base_url, "/api/admin/runners")["items"]
                    if item["id"] == runners["user1"]["id"]
                )["status"]
                == "working",
                "进度未回传",
            )
            wait_until(
                lambda: len(request(admin, base_url, "/api/admin/activities")["items"]) == 1,
                "Activity 未回传",
            )
            assert not request(admin, base_url, "/api/admin/work-queue")["judgments"]

            simulate(
                configs["user1"],
                "collaboration-request",
                "--target-runner-id",
                runners["user2"]["id"],
            )
            wait_until(
                lambda: len(request(admin, base_url, "/api/admin/collaborations")["items"]) == 1,
                "普通协作未到达目标节点",
            )
            ordinary = request(admin, base_url, "/api/admin/collaborations")["items"][0]
            assert not ordinary["requires_admin"]
            simulate(
                configs["user2"],
                "collaboration-response",
                "--collaboration-id",
                str(ordinary["id"]),
                "--action",
                "confirm",
            )
            wait_until(
                lambda: request(admin, base_url, "/api/admin/collaborations")["items"][0][
                    "status"
                ]
                == "confirmed",
                "普通协作未自行闭环",
            )

            simulate(
                configs["user2"],
                "collaboration-request",
                "--target-runner-id",
                runners["user1"]["id"],
                "--category",
                "risk",
                "--name",
                "风险事项",
            )
            wait_until(
                lambda: len(request(admin, base_url, "/api/admin/work-queue")["judgments"]) == 1,
                "风险事项未进入待判断",
            )
            risk_id = request(admin, base_url, "/api/admin/work-queue")["judgments"][0]["id"]
            request(
                admin,
                base_url,
                f"/api/admin/collaborations/{risk_id}/decision",
                "POST",
                {"decision": "confirm", "feedback": "已确认"},
            )

            simulate(configs["user1"], "skill-proposal")
            wait_until(
                lambda: len(request(admin, base_url, "/api/admin/skill-proposals")["items"]) == 1,
                "Skill Proposal 未回传",
            )
            proposal = request(admin, base_url, "/api/admin/skill-proposals")["items"][0]
            request(
                admin,
                base_url,
                f"/api/admin/skill-proposals/{proposal['id']}/decision",
                "POST",
                {"decision": "publish", "feedback": ""},
            )
            for name in runners:
                skill_path = temp / "workspace" / name / "shared" / "skills" / "example-shared-skill.md"
                wait_until(lambda path=skill_path: path.is_file(), f"{name} 未同步 Shared Skill")

            stop(runner_processes["user3"])
            wait_until(
                lambda: not next(
                    item
                    for item in request(admin, base_url, "/api/admin/runners")["items"]
                    if item["id"] == runners["user3"]["id"]
                )["online"],
                "停止后 Runner 未显示 Offline",
                timeout=4,
            )
            old_token = runners["user3"]["runner_token"]
            rotated = request(
                admin,
                base_url,
                f"/api/admin/runners/{runners['user3']['id']}/rotate-token",
                "POST",
                {},
            )
            try:
                request(
                    plain,
                    base_url,
                    "/api/runner/heartbeat",
                    "POST",
                    {},
                    {"Authorization": f"Bearer {old_token}"},
                )
                raise AssertionError("旧 Runner Token 仍可使用")
            except urllib.error.HTTPError as exc:
                assert exc.code == 401
            configs["user3"].write_text(
                json.dumps(rotated["config"], ensure_ascii=False, indent=2), encoding="utf-8"
            )
            configs["user3"].chmod(0o600)
            runner_processes["user3"] = start_runner(configs["user3"])
            wait_until(
                lambda: next(
                    item
                    for item in request(admin, base_url, "/api/admin/runners")["items"]
                    if item["id"] == runners["user3"]["id"]
                )["online"],
                "轮换 Token 后 Runner 未恢复",
            )

            stop(server)
            simulate(configs["user1"], "progress", "--summary", "等待网络恢复后补传")
            pending = list((temp / "workspace" / "user1" / "outbox").glob("*.json"))
            assert pending, "未收到 ACK 的 outbox 事件不应被归档"
            server = start_server()
            processes.append(server)
            wait_until(
                lambda: not list((temp / "workspace" / "user1" / "outbox").glob("*.json")),
                "网络恢复后 outbox 未自动补传",
            )
            assert request(admin, base_url, "/api/admin/shared-skills")["items"][0]["version"] == "v0.2"

            print(
                "E2E PASS: 3 个动态 Runner、原子共享同步、进度/Activity、普通与升级协作、"
                "Skill 发布、Offline、Token 轮换、重启恢复和 outbox 补传均通过"
            )
        finally:
            for process in reversed(processes):
                stop(process)


if __name__ == "__main__":
    main()
