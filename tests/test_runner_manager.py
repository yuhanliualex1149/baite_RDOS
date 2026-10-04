"""Isolated Manager switching tests; never contact a real Panel or service."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bootstrapper import core
from bootstrapper.manager import RunnerManager, initial_state


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "install"
        self.root.mkdir()
        self.workspace = Path(self.temp.name) / "workspace"
        self.config = {"control_url": "https://example.invalid/rdos", "runner_id": "rnr_aaaaaaaaaaaa",
                       "runner_token": "fictional-token", "workspace": str(self.workspace),
                       "installation_id": "synthetic-installation-id"}
        core.private_write(self.root / "config.json", self.config)
        core.private_write(self.root / "runtime-state.json", initial_state("1.0.0", "a" * 64))
        self.manager = RunnerManager(self.root / "config.json")
        self.old = self.manager.executable("1.0.0")
        self.new = self.manager.executable("1.1.0")
        for path in (self.old, self.new):
            path.parent.mkdir(parents=True)
            path.write_text("synthetic", encoding="utf-8")
        self.artifact = {"version": "1.1.0", "sha256": "b" * 64, "protocol_version": "1",
                         "supported_contract_versions": ["1"], "min_manager_version": "0.1.0",
                         "archive": "zip"}
        self.manifest = {"schema_version": 1, "protocol_version": "1",
                         "platforms": {"macos-arm64": {"runtime": self.artifact}}}
        self.calls = []

    def api(self, origin, path, data=None, token=""):
        self.calls.append((path, data))
        if path == "/api/runner/installation-status":
            return {"update_retry_nonce": 0}
        if path == "/api/bootstrap/manifest":
            return self.manifest
        if path == "/api/runner/update-status":
            return {"ok": True}
        raise AssertionError(path)

    def test_normal_upgrade_then_same_version_no_download(self):
        with patch.object(core, "api_json", side_effect=self.api), patch.object(core, "platform_key", return_value="macos-arm64"), \
             patch.object(self.manager, "_download", return_value="1.1.0") as download, \
             patch.object(self.manager, "_stop_recorded_child"), \
             patch.object(self.manager, "start_child", return_value="c" * 32), \
             patch.object(self.manager, "wait_healthy") as healthy:
            self.assertEqual(self.manager.check_once(), "updated")
            self.assertEqual(self.manager.state["current"], "1.1.0")
            healthy.assert_called_once_with("1.1.0", "c" * 32)
            self.assertEqual(self.manager.check_once(), "unchanged")
            download.assert_called_once()
        persisted = json.loads((self.root / "runtime-state.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["current"], "1.1.0")

    def test_bad_download_or_program_pauses_same_hash(self):
        for reason in ("下载文件大小或 SHA-256 不匹配", "候选 Runtime 独立自检失败", "Windows 文件占用"):
            with self.subTest(reason=reason):
                core.private_write(self.root / "runtime-state.json", initial_state("1.0.0", "a" * 64))
                self.manager = RunnerManager(self.root / "config.json")
                with patch.object(core, "api_json", side_effect=self.api), patch.object(core, "platform_key", return_value="macos-arm64"), \
                     patch.object(self.manager, "_download", side_effect=ValueError(reason)) as download:
                    self.assertEqual(self.manager.check_once(), "failed")
                    self.assertEqual(self.manager.state["current"], "1.0.0")
                    self.assertEqual(self.manager.check_once(), "paused_failed_candidate")
                    download.assert_called_once()

    def test_no_new_heartbeat_or_unhealthy_sync_rolls_back(self):
        for reason in ("没有本次实例的新心跳", "同步快照不健康"):
            with self.subTest(reason=reason):
                core.private_write(self.root / "runtime-state.json", initial_state("1.0.0", "a" * 64))
                self.manager = RunnerManager(self.root / "config.json")
                with patch.object(core, "api_json", side_effect=self.api), patch.object(core, "platform_key", return_value="macos-arm64"), \
                     patch.object(self.manager, "_download", return_value="1.1.0"), \
                     patch.object(self.manager, "_stop_recorded_child"), \
                     patch.object(self.manager, "start_child", return_value="c" * 32), \
                     patch.object(self.manager, "wait_healthy", side_effect=ValueError(reason)):
                    self.assertEqual(self.manager.check_once(), "rolled_back")
                    self.assertEqual(self.manager.state["current"], "1.0.0")
                    self.assertEqual(self.manager.state["failed"]["sha256"], "b" * 64)

    def test_interrupted_switch_recovers_previous(self):
        self.manager.state.update(current="1.1.0", previous="1.0.0", candidate={"version": "1.1.0", "sha256": "b" * 64},
                                  phase="validating")
        self.manager.save()
        restarted = RunnerManager(self.root / "config.json")
        with patch.object(restarted, "_stop_recorded_child"), patch.object(restarted, "start_child", return_value="d" * 32), \
             patch.object(core, "api_json", side_effect=self.api):
            restarted.recover_interrupted_switch()
        self.assertEqual(restarted.state["current"], "1.0.0")
        self.assertEqual(restarted.state["phase"], "rolled_back")

    def test_offline_retry_incompatible_skip_and_admin_retry(self):
        with patch.object(core, "api_json", side_effect=self.api), patch.object(core, "platform_key", return_value="macos-arm64"), \
             patch.object(self.manager, "_download", side_effect=ValueError("无法连接 RDOS 服务")) as download:
            self.assertEqual(self.manager.check_once(), "failed")
            self.assertIsNone(self.manager.state["failed"])
            self.assertEqual(self.manager.check_once(), "failed")
            self.assertEqual(download.call_count, 2)
        self.artifact["supported_contract_versions"] = ["99"]
        with patch.object(core, "api_json", side_effect=self.api), patch.object(core, "platform_key", return_value="macos-arm64"):
            self.assertEqual(self.manager.check_once(), "skipped_incompatible")
        self.artifact["supported_contract_versions"] = ["1"]
        self.manager.state["failed"] = {"version": "1.1.0", "sha256": "b" * 64}
        def retry_api(origin, path, data=None, token=""):
            if path == "/api/runner/installation-status":
                return {"update_retry_nonce": 1}
            return self.api(origin, path, data, token)
        with patch.object(core, "api_json", side_effect=retry_api), patch.object(core, "platform_key", return_value="macos-arm64"), \
             patch.object(self.manager, "_download", side_effect=ValueError("bad")) as download:
            self.assertEqual(self.manager.check_once(), "failed")
            download.assert_called_once()

    def test_health_requires_matching_new_process_and_snapshot(self):
        instance = "f" * 32
        status = {"installation_id": self.config["installation_id"], "runner_version": "1.1.0",
                  "process_instance_id": instance, "sync_instance_id": instance,
                  "sync_health": "healthy", "shared_revision": 4}
        def status_api(origin, path, data=None, token=""):
            if path == "/api/runner/installation-status":
                return status
            if path == "/api/runner/projects/sync":
                return {"projects": [{"id": "prj_test"}]}
            raise AssertionError(path)
        with patch.object(core, "api_json", side_effect=status_api), patch.object(core, "_verified_local_snapshot", return_value=4) as verify:
            self.assertTrue(self.manager.health_ready("1.1.0", instance))
            self.assertEqual(verify.call_count, 2)
            status["sync_instance_id"] = "old-instance"
            self.assertFalse(self.manager.health_ready("1.1.0", instance))
            status["sync_instance_id"] = instance
            status["sync_health"] = "recovering"
            self.assertFalse(self.manager.health_ready("1.1.0", instance))


if __name__ == "__main__":
    unittest.main()
