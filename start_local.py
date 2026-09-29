from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import List


PROJECT_ROOT = Path(__file__).resolve().parent


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def wait_for_server(process: subprocess.Popen, port: int, timeout: float = 25) -> None:
    health_url = f"http://127.0.0.1:{port}/api/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Control Panel 启动失败，请查看上方日志")
        try:
            with urllib.request.urlopen(health_url, timeout=1):
                return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.3)
    raise RuntimeError("等待 Control Panel 超时")


def stop_processes(processes: List[subprocess.Popen]) -> None:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
    deadline = time.time() + 5
    for process in reversed(processes):
        if process.poll() is not None:
            continue
        try:
            process.wait(timeout=max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            process.kill()


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 RDOS Control Panel 和可选的 Local Runner")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env.local")
    parser.add_argument("--runner-config", type=Path, action="append", default=[])
    args = parser.parse_args()
    load_env_file(args.env_file.expanduser().resolve())

    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "缺少依赖。请先创建虚拟环境并安装 requirements.txt。"
        ) from exc

    if not os.environ.get("BAITE_SESSION_SECRET"):
        raise SystemExit("缺少 BAITE_SESSION_SECRET，请先复制并填写 .env.local")

    host = os.environ.get("BAITE_HOST", "127.0.0.1")
    port = int(os.environ.get("BAITE_PORT", "8000"))
    server_command = [
        sys.executable,
        "-m",
        "uvicorn",
        "server.main:app",
        "--host",
        host,
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]
    processes: List[subprocess.Popen] = []
    try:
        server = subprocess.Popen(server_command, cwd=PROJECT_ROOT, env=os.environ.copy())
        processes.append(server)
        wait_for_server(server, port)
        for config_path in args.runner_config:
            resolved = config_path.expanduser().resolve()
            process = subprocess.Popen(
                [
                    sys.executable,
                    "runner/runner.py",
                    "--config",
                    str(resolved),
                ],
                cwd=PROJECT_ROOT,
                env=os.environ.copy(),
            )
            processes.append(process)
        public_url = os.environ.get("BAITE_PUBLIC_URL", f"http://127.0.0.1:{port}")
        print(f"\nRDOS 系统管理员工作台已启动：{public_url}", flush=True)
        print(f"同时启动的 Local Runner：{len(args.runner_config)}", flush=True)
        print("按 Ctrl+C 停止本次启动的所有进程。\n", flush=True)
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n正在停止……", flush=True)
    finally:
        stop_processes(processes)


if __name__ == "__main__":
    main()
