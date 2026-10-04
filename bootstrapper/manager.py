"""Stable per-user process supervisor for immutable Runner Runtime versions."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import uuid
from datetime import datetime, timezone
from pathlib import Path

from bootstrapper import core
from rdos_contract import CONTRACT_VERSION


CHECK_INTERVAL = 12 * 60 * 60
HEALTH_TIMEOUT = 120
VERSION_NAME = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initial_state(version: str, artifact_hash: str = "") -> dict:
    if not VERSION_NAME.fullmatch(version):
        raise ValueError("Runtime 版本无效")
    return {"current": version, "previous": "", "candidate": None,
            "installed_hashes": {version: artifact_hash}, "failed": None,
            "download_receipt": None,
            "phase": "idle", "target": "", "error": "", "checked_at": "",
            "seen_retry_nonce": 0, "child_pid": 0, "child_instance_id": "", "child_version": ""}


class RunnerManager:
    def __init__(self, config_path: Path, poll_seconds: float = 5):
        self.config_path = config_path.resolve()
        self.root = self.config_path.parent
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        if self.config_path != (self.root / "config.json") or not self.config.get("installation_id"):
            raise ValueError("Manager 安装配置无效")
        self.workspace = Path(self.config["workspace"])
        self.poll_seconds = poll_seconds
        self.state_path = self.root / "runtime-state.json"
        self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not VERSION_NAME.fullmatch(self.state["current"]):
            raise ValueError("当前 Runtime 版本无效")
        self.child: subprocess.Popen | None = None
        self.stopping = False

    def save(self) -> None:
        core.private_write(self.state_path, self.state)

    def executable(self, version: str) -> Path:
        if not VERSION_NAME.fullmatch(version):
            raise ValueError("Runtime 版本无效")
        name = "rdos-runner.exe" if os.name == "nt" else "rdos-runner"
        return self.root / "runtime" / version / "rdos-runner" / name

    def _recorded_command(self, pid: int) -> str:
        if os.name == "nt":
            script = "$p=Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $env:RDOS_CHILD_PID);if($p){$p.CommandLine}"
            env = core._clean_env()
            env["RDOS_CHILD_PID"] = str(pid)
            return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                  env=env, capture_output=True, text=True, check=False).stdout.strip()
        return subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                              env=core._clean_env(), capture_output=True, text=True, check=False).stdout.strip()

    def _stop_recorded_child(self) -> None:
        pid = int(self.state.get("child_pid") or 0)
        if not pid:
            return
        command = self._recorded_command(pid)
        if command:
            version = self.state.get("child_version", "")
            instance = self.state.get("child_instance_id", "")
            if (not version or not instance or str(self.executable(version)) not in command
                    or str(self.config_path) not in command or instance not in command):
                raise ValueError("已有 Runtime 进程身份无法核实；未终止其他进程")
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                if (self.child and self.child.poll() is not None) or not self._recorded_command(pid):
                    break
                time.sleep(0.1)
            else:
                if self._recorded_command(pid):
                    raise ValueError("Runtime 子进程未退出；保留原状态")
        if self.child:
            try:
                self.child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        self.child = None
        self.state.update(child_pid=0, child_instance_id="", child_version="")
        self.save()

    def start_child(self, version: str) -> str:
        if self.state.get("child_pid"):
            self._stop_recorded_child()
        executable = self.executable(version)
        if not executable.is_file():
            raise ValueError("Runtime 主程序缺失：" + version)
        instance = uuid.uuid4().hex
        logs = self.root / "logs"
        logs.mkdir(exist_ok=True, mode=0o700)
        core.protect_path(logs)
        env = core._clean_env()
        env["RDOS_MANAGER_VERSION"] = core.VERSION
        args = [str(executable), "--config", str(self.config_path), "--poll-seconds", str(self.poll_seconds),
                "--log-directory", str(logs), "--instance-id", instance]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with (logs / "manager-runtime.log").open("ab") as output:
            self.child = subprocess.Popen(args, cwd=executable.parent, env=env, stdout=output,
                                          stderr=subprocess.STDOUT, close_fds=True, creationflags=creationflags)
        core.protect_path(logs / "manager-runtime.log")
        self.state.update(child_pid=self.child.pid, child_instance_id=instance, child_version=version)
        self.save()
        return instance

    def self_check(self, version: str) -> None:
        executable = self.executable(version)
        result = subprocess.run([str(executable), "--self-check"], cwd=executable.parent,
                                env=core._clean_env(), capture_output=True, text=True, timeout=30, check=False)
        if result.returncode:
            raise ValueError("候选 Runtime 独立自检失败：" + result.stderr.strip()[:300])
        try:
            info = json.loads(result.stdout)
        except ValueError as exc:
            raise ValueError("候选 Runtime 自检响应无效") from exc
        if (info.get("runtime_version") != version or core.PROTOCOL not in info.get("protocol_versions", [])
                or CONTRACT_VERSION not in info.get("contract_versions", [])):
            raise ValueError("候选 Runtime 版本或协议不匹配")

    def health_ready(self, version: str, instance: str) -> bool:
        status = core.api_json(self.config["control_url"], "/api/runner/installation-status",
                               token=self.config["runner_token"])
        if (status.get("installation_id") != self.config["installation_id"]
                or status.get("runner_version") != version
                or status.get("process_instance_id") != instance
                or status.get("sync_instance_id") != instance
                or status.get("sync_health") != "healthy"):
            return False
        revision = core._verified_local_snapshot(self.workspace, self.workspace)
        if revision != int(status.get("shared_revision", -1)):
            return False
        projects = core.api_json(self.config["control_url"], "/api/runner/projects/sync",
                                 token=self.config["runner_token"]).get("projects", [])
        for project in projects:
            core._verified_local_snapshot(self.workspace / "projects" / project["id"], self.workspace)
        return True

    def wait_healthy(self, version: str, instance: str, timeout: float = HEALTH_TIMEOUT) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.stopping:
                raise ValueError("Manager 已收到停止请求")
            if self.child and self.child.poll() is not None:
                raise ValueError("候选 Runtime 子进程已退出")
            try:
                if self.health_ready(version, instance):
                    return
            except (OSError, ValueError, KeyError, TypeError, TimeoutError, urllib.error.URLError):
                pass
            time.sleep(2)
        raise ValueError("候选 Runtime 未产生匹配实例的新心跳与健康快照")

    def _report(self) -> None:
        payload = {"current_version": self.state["current"], "target_version": self.state.get("target", ""),
                   "state": self.state["phase"], "checked_at": self.state.get("checked_at", ""),
                   "error": self.state.get("error", "")[:5000]}
        try:
            core.api_json(self.config["control_url"], "/api/runner/update-status", payload,
                          token=self.config["runner_token"])
        except (OSError, ValueError, urllib.error.URLError):
            pass

    def _download(self, artifact: dict) -> str:
        version = str(artifact["version"])
        digest = str(artifact["sha256"])
        if not VERSION_NAME.fullmatch(version) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("发布清单版本或 Hash 无效")
        installed = self.state.setdefault("installed_hashes", {}).get(version)
        if installed is not None:
            if installed != digest:
                raise ValueError("相同 Runtime 版本对应不同 Hash；拒绝覆盖")
            self.self_check(version)
            return version
        runtime_root = self.root / "runtime"
        runtime_root.mkdir(exist_ok=True)
        destination = runtime_root / version
        if destination.exists():
            if self.state.get("download_receipt") != {"version": version, "sha256": digest}:
                raise ValueError("候选版本目录已存在且来源无法核实；未覆盖")
            try:
                self.self_check(version)
            except (OSError, ValueError, subprocess.SubprocessError):
                os.replace(destination, runtime_root / ("quarantine-" + version + "-" + uuid.uuid4().hex))
            else:
                self.state["installed_hashes"][version] = digest
                self.save()
                return version
        archive = self.root / ("runtime-download-" + version + (".zip" if os.name == "nt" else ".tar.gz"))
        core.download_verified(self.config["control_url"], artifact, archive)
        self.state["download_receipt"] = {"version": version, "sha256": digest}
        self.save()
        stage = Path(tempfile.mkdtemp(prefix=".update-", dir=runtime_root))
        try:
            core.extract_runtime(archive, stage / "package", artifact["archive"])
            candidate = stage / "package" / "rdos-runner" / ("rdos-runner.exe" if os.name == "nt" else "rdos-runner")
            if not candidate.is_file() or (os.name != "nt" and not os.access(candidate, os.X_OK)):
                raise ValueError("候选 Runtime 缺少可执行主程序")
            os.replace(stage / "package", destination)
            self.self_check(version)
            self.state["installed_hashes"][version] = digest
            self.save()
            return version
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            archive.unlink(missing_ok=True)

    def _rollback(self, reason: str, failed: dict | None) -> None:
        self._stop_recorded_child()
        previous = self.state.get("previous", "")
        if not previous or not self.executable(previous).is_file():
            raise ValueError("上一版 Runtime 不可用；不能自动回退")
        self.state.update(current=previous, candidate=None, failed=failed, phase="rolled_back",
                          target=failed["version"] if failed else "", error=reason[:5000])
        self.save()
        self.start_child(previous)
        self._report()

    def recover_interrupted_switch(self) -> None:
        if self.state.get("phase") in {"switching", "validating"}:
            self._rollback("Manager 在切换期间重启，已恢复上一版 Runtime", self.state.get("candidate"))

    def check_once(self) -> str:
        pair = None
        try:
            status = core.api_json(self.config["control_url"], "/api/runner/installation-status",
                                   token=self.config["runner_token"])
            nonce = int(status.get("update_retry_nonce", 0))
            if nonce != self.state.get("seen_retry_nonce", 0):
                self.state["seen_retry_nonce"] = nonce
                self.state["failed"] = None
            manifest = core.api_json(self.config["control_url"], "/api/bootstrap/manifest")
            if manifest.get("schema_version") != 1 or str(manifest.get("protocol_version")) != core.PROTOCOL:
                raise ValueError("发布清单协议不兼容")
            artifact = manifest["platforms"][core.platform_key()]["runtime"]
            version = str(artifact["version"])
            digest = str(artifact["sha256"])
            self.state["checked_at"] = _now()
            self.state["target"] = version
            if (artifact.get("protocol_version") != core.PROTOCOL
                    or CONTRACT_VERSION not in artifact.get("supported_contract_versions", [])
                    or core._version_tuple(artifact["min_manager_version"]) > core._version_tuple(core.VERSION)):
                self.state.update(phase="skipped_incompatible", error="候选 Runtime 与当前 Manager／协议／Contract 不兼容")
                self.save(); self._report()
                return "skipped_incompatible"
            if not VERSION_NAME.fullmatch(version) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("发布清单版本或 Hash 无效")
            pair = {"version": version, "sha256": digest}
            if version == self.state["current"]:
                known_hash = self.state.get("installed_hashes", {}).get(version)
                if known_hash and known_hash != digest:
                    raise ValueError("当前 Runtime 版本对应不同 Hash；拒绝覆盖")
                self.state.update(phase="idle", target="", error="")
                self.save(); self._report()
                return "unchanged"
            if self.state.get("failed") == pair:
                return "paused_failed_candidate"
            self.state.update(phase="downloading", error="")
            self.save(); self._report()
            self._download(artifact)
            self.state.update(previous=self.state["current"], candidate=pair, phase="switching")
            self.save()
            self._stop_recorded_child()
            self.state.update(current=version, phase="validating")
            self.save(); self._report()
            instance = self.start_child(version)
            try:
                self.wait_healthy(version, instance)
            except Exception as exc:
                self._rollback(str(exc), pair)
                return "rolled_back"
            self.state.update(candidate=None, phase="updated", target="", error="", failed=None)
            self.save(); self._report()
            return "updated"
        except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
            if self.state.get("phase") in {"switching", "validating"}:
                self._rollback(str(exc), self.state.get("candidate"))
                return "rolled_back"
            if pair and "无法连接" not in str(exc) and "网络" not in str(exc):
                self.state["failed"] = pair
            self.state.update(phase="failed", error=str(exc)[:5000], checked_at=_now())
            self.save(); self._report()
            return "failed"

    def run(self) -> None:
        def stop(_number, _frame):
            self.stopping = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        self.recover_interrupted_switch()
        if self.child is None:
            if self.state.get("child_pid"):
                self._stop_recorded_child()
            self.start_child(self.state["current"])
        self._report()
        next_check = 0.0
        next_retry_poll = 0.0
        while not self.stopping:
            if self.child and self.child.poll() is not None:
                self.state.update(child_pid=0, child_instance_id="", child_version="")
                self.save()
                self.start_child(self.state["current"])
            now = time.monotonic()
            if now >= next_check:
                outcome = self.check_once()
                next_check = time.monotonic() + (60 if outcome == "failed" else CHECK_INTERVAL)
            if now >= next_retry_poll:
                try:
                    status = core.api_json(self.config["control_url"], "/api/runner/installation-status",
                                           token=self.config["runner_token"])
                    if int(status.get("update_retry_nonce", 0)) != self.state.get("seen_retry_nonce", 0):
                        next_check = 0.0
                except (OSError, ValueError, urllib.error.URLError):
                    pass
                next_retry_poll = now + 60
            time.sleep(2)
        self._stop_recorded_child()


def main() -> None:
    parser = argparse.ArgumentParser(description="RDOS Runner Manager")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--log-directory", type=Path)
    args = parser.parse_args()
    if args.poll_seconds <= 0:
        parser.error("poll-seconds 必须大于 0")
    RunnerManager(args.config, args.poll_seconds).run()


if __name__ == "__main__":
    main()
