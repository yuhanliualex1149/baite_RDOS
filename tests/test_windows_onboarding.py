"""Isolated credentials/API only. Windows-only cases exercise native APIs in CI."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from deploy import install_runner as installer
from runner.platform_support import is_linklike, workspace_lock
from runner.runner import apply_snapshot, runtime_directory, scan_project_files
from rdos_protocol import seal_snapshot
from server import db
from tests import test_api
from tests.test_runner import snapshot, rag_item


class OnboardingApiTest(unittest.TestCase):
    setUp = test_api.ApiFlowTest.setUp
    tearDown = test_api.ApiFlowTest.tearDown
    login = test_api.ApiFlowTest.login
    create_runner = test_api.ApiFlowTest.create_runner
    headers = staticmethod(test_api.ApiFlowTest.headers)

    def test_optional_client_paths_and_old_configuration(self):
        self.login()
        for path in (None, "", "/Users/example/工作 space", "C:\\Users\\example\\工作 space", "\\\\host\\share\\work"):
            payload = {"display_name": "test"}
            if path is not None:
                payload["workspace"] = path
            response = self.client.post("/api/admin/runners", json=payload)
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["item"]["config"]["workspace"], path or "")
        for path in ("relative", "~/work", "C:relative", "C:\\work\\..\\other", "/work/../other", "/bad\x00path"):
            response = self.client.post("/api/admin/runners", json={"display_name": "bad", "workspace": path})
            self.assertEqual(response.status_code, 400, path)

    def test_heartbeat_runtime_is_token_scoped_and_not_admin_configuration(self):
        self.login()
        one, two = self.create_runner("one"), self.create_runner("two")
        headers = self.headers(one)
        self.assertEqual(self.client.post("/api/runner/heartbeat", headers=headers).status_code, 200)
        runtime = {"platform": "windows", "runner_version": "a" * 40, "actual_workspace": "C:\\RDOS\\专用"}
        self.assertEqual(self.client.post("/api/runner/heartbeat", headers=headers, json=runtime).status_code, 200)
        self.assertEqual(db.get_runner(one["id"])["actual_workspace"], runtime["actual_workspace"])
        self.assertEqual(db.get_runner(one["id"])["workspace_path"], one["config"]["workspace"])
        self.assertEqual(db.get_runner(two["id"])["actual_workspace"], "")
        self.assertEqual(self.client.post("/api/runner/heartbeat", headers=headers, json={**runtime, "runner_id": two["id"]}).status_code, 422)
        self.assertEqual(self.client.post("/api/runner/heartbeat").status_code, 401)
        self.client.post("/api/runner/heartbeat", headers=headers, json={})
        self.assertEqual(db.get_runner(one["id"])["platform"], "windows")

    def test_instructions_are_private_to_admin_but_contain_no_credentials(self):
        self.login()
        node = self.create_runner("personal-display-name")
        original = db.get_runner(node["id"])
        endpoint = f"/api/admin/runners/{node['id']}/onboarding"
        with patch.dict(os.environ, {"BAITE_APP_RELEASE": "b" * 40}):
            for platform in ("windows", "macos"):
                result = self.client.get(endpoint, params={"platform": platform})
                self.assertEqual(result.status_code, 200)
                text = result.json()["instructions"]
                self.assertIn(node["id"], text)
                self.assertIn("b" * 40, text)
                self.assertIn("runtime.json", text)
                for secret in (node["runner_token"], node["config"]["workspace"], "personal-display-name"):
                    self.assertNotIn(secret, text)
                self.assertEqual(result.json()["channel"], "candidate")
        self.assertEqual(db.get_runner(node["id"]), original)
        with patch.dict(os.environ, {"BAITE_APP_RELEASE": "development"}):
            result = self.client.get(endpoint).json()
            self.assertFalse(result["installable"])
            self.assertIsNone(result["installer_url"])
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.get(endpoint, headers=self.headers(node)).status_code, 401)

    def test_incremental_migration_preserves_nodes_and_backs_up(self):
        self.login()
        node = self.create_runner("legacy")
        with db.connection() as conn:
            conn.execute("UPDATE settings SET value='4' WHERE key='schema_version'")
            for column in ("platform", "runner_version", "actual_workspace"):
                # SQLite shipped on supported Python versions supports DROP COLUMN.
                conn.execute(f"ALTER TABLE runner_nodes DROP COLUMN {column}")
        db.init_db()
        self.assertEqual(db.get_runner(node["id"])["workspace_path"], node["config"]["workspace"])
        self.assertEqual(db.authenticate_runner(node["runner_token"])["id"], node["id"])
        self.assertTrue(list(Path(self.temp_dir.name).glob("**/*backup*")))


class PortableFilesystemTest(unittest.TestCase):
    def test_native_lock_blocks_second_process_and_releases_on_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / "中文 space/runner.lock"
            script = "from pathlib import Path; from runner.platform_support import workspace_lock; import sys; workspace_lock(Path(sys.argv[1]))"
            with workspace_lock(path):
                blocked = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True)
                self.assertNotEqual(blocked.returncode, 0)
            free = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True)
            self.assertEqual(free.returncode, 0, free.stderr)

    def test_snapshot_entry_failure_collision_and_bad_names_preserve_old(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "中文 workspace"
            first = apply_snapshot(root, "test", snapshot(1))
            old = (root / "runtime.json").read_bytes()
            original = os.replace
            def fail(source, target):
                if Path(target).name == "runtime.json":
                    raise PermissionError("file in use")
                return original(source, target)
            with patch("runner.runner.os.replace", side_effect=fail), self.assertRaises(PermissionError):
                apply_snapshot(root, "test", snapshot(2))
            self.assertEqual((root / "runtime.json").read_bytes(), old)
            self.assertEqual(runtime_directory(root), first.resolve())
            for name in ("CON.md", "bad:name.md", "trailing. ", "..\\escape.md", "nul.txt.md"):
                bad = snapshot(3)
                bad["selected_rag"] = [rag_item("content", name)]
                with self.assertRaises(ValueError):
                    apply_snapshot(root, "test", seal_snapshot(bad))
            bad = snapshot(3)
            bad["selected_rag"] = [rag_item("one", "Case.md"), rag_item("two", "case.md")]
            with self.assertRaisesRegex(ValueError, "冲突"):
                apply_snapshot(root, "test", seal_snapshot(bad))
            self.assertEqual((root / "runtime.json").read_bytes(), old)
            # A reader holding an old entry keeps a consistent immutable batch after publication.
            apply_snapshot(root, "test", snapshot(4))
            self.assertNotEqual(runtime_directory(root), first.resolve())
            self.assertEqual(json.loads((first / "snapshot.json").read_text(encoding="utf-8"))["revision"], 1)

    def test_entry_cannot_escape_managed_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "runtime.json").write_text(json.dumps({"layout_version": 1, "revision": 1, "snapshot_dir": str(root.parent)}))
            with self.assertRaises(ValueError):
                runtime_directory(root)

    def test_shanghai_timezone_without_host_iana_database(self):
        with patch.dict(os.environ, {"PYTHONTZPATH": ""}):
            code = "from zoneinfo import ZoneInfo; from datetime import datetime; assert datetime(2026,9,29,18,tzinfo=ZoneInfo('Asia/Shanghai')).utcoffset().total_seconds()==28800"
            result = subprocess.run([sys.executable, "-c", code], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows native file sharing")
    def test_windows_open_entry_keeps_last_known_good(self):
        import ctypes
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            first = apply_snapshot(root, "test", snapshot(1))
            kernel = ctypes.windll.kernel32
            kernel.CreateFileW.restype = ctypes.c_void_p
            handle = kernel.CreateFileW(str(root / "runtime.json"), 0x80000000, 1, None, 3, 0, None)
            self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
            try:
                with self.assertRaises(PermissionError):
                    apply_snapshot(root, "test", snapshot(2))
                self.assertEqual(runtime_directory(root), first.resolve())
            finally:
                kernel.CloseHandle(ctypes.c_void_p(handle))

    @unittest.skipUnless(os.name == "nt", "Windows junction exclusion")
    def test_junction_does_not_escape_project_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            project, outside = base / "project", base / "outside"
            files = project / "work/files"
            files.mkdir(parents=True); outside.mkdir()
            (outside / "secret.txt").write_text("not for upload")
            junction = files / "external"
            subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], check=True, capture_output=True)
            try:
                self.assertTrue(is_linklike(junction))
                self.assertEqual(scan_project_files(project, []), [])
                with self.assertRaises(ValueError):
                    installer.reject_links(junction / "child")
            finally:
                junction.rmdir()


@unittest.skipUnless(os.name == "nt", "Windows ACL and Task Scheduler require Windows CI")
class WindowsInstallTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "Local AppData/BaiteRDOS/runners"
        self.work = self.base / "中文 workspace"
        self.node = "rnr_native_test_" + uuid.uuid4().hex[:8]
        self.config = self.base / "personal.json"
        self.payload = {"runner_id": self.node, "runner_token": "fake-test-token-not-secret",
                        "control_url": "http://127.0.0.1:9876/test", "workspace": str(self.work)}
        self.config.write_text(json.dumps(self.payload), encoding="utf-8")
        self.runtime = {"runner/runner.py": b"# fake runtime\n", "rdos_protocol.py": b"# test\n"}

    def test_private_acl_has_no_other_normal_users(self):
        installer.protect_path(self.config)
        result = installer.powershell("(Get-Acl -LiteralPath $env:RDOS_PRIVATE_PATH).Access | ForEach-Object {$_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value}", {"RDOS_PRIVATE_PATH": str(self.config)})
        sid = installer.powershell("[Security.Principal.WindowsIdentity]::GetCurrent().User.Value").stdout.strip()
        self.assertEqual(set(result.stdout.split()), {sid, "S-1-5-18"})

    def test_fresh_repeat_token_update_uninstall_preserves_work(self):
        absent = subprocess.CompletedProcess([], 1, "", "")
        real_run = subprocess.run
        with patch.object(installer, "download_runtime", return_value=("a" * 40, self.runtime)), \
                patch.object(installer, "service_info", return_value=absent), \
                patch.object(installer, "windows_task") as task, \
                patch.object(installer.venv.EnvBuilder, "create"), \
                patch.object(installer.subprocess, "run", wraps=subprocess.run) as command:
            # Keep ACL / SID PowerShell native, only avoid a real venv/network/program.
            def execute(args, **kwargs):
                if str(args[0]).endswith("python.exe"):
                    return subprocess.CompletedProcess(args, 0, "", "")
                return real_run(args, **kwargs)
            command.side_effect = execute
            directory = installer.install(self.config, None, self.root, False)
            (self.work / "work.md").write_text("keep")
            installer.install(self.config, None, self.root, False)
            self.payload["runner_token"] = "new-fake-test-token-abcdef"
            self.config.write_text(json.dumps(self.payload), encoding="utf-8")
            installer.install(self.config, None, self.root, False)
            self.assertEqual(json.loads((directory / "config.json").read_text(encoding="utf-8"))["runner_token"], self.payload["runner_token"])
            self.assertNotIn(self.payload["runner_token"], (directory / "task.xml").read_text(encoding="utf-16"))
            self.assertTrue(installer.status(self.node, self.root)["installed"])
            installer.uninstall(self.node, self.root)
            self.assertFalse(directory.exists())
            self.assertEqual((self.work / "work.md").read_text(), "keep")

    def test_task_settings_and_native_registration_without_start(self):
        xml = installer.build_task(self.node, self.base / "pythonw.exe", self.base, self.config, self.base / "logs")
        tree = ET.fromstring(xml)
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        for name, value in {"ExecutionTimeLimit": "PT0S", "MultipleInstancesPolicy": "IgnoreNew", "WakeToRun": "false", "DisallowStartIfOnBatteries": "false", "StopIfGoingOnBatteries": "false", "RunLevel": "LeastPrivilege", "LogonType": "InteractiveToken"}.items():
            self.assertEqual(tree.find(f".//t:{name}", ns).text, value)
        # Register disabled: CI registration is not interactive-logon acceptance.
        tree.find(".//t:Settings/t:Enabled", ns).text = "false"
        path = self.base / "task.xml"
        path.write_bytes(ET.tostring(tree, encoding="utf-16", xml_declaration=True))
        try:
            installer.windows_task(self.node, "register", path, check=True)
            result = installer.windows_task(self.node, "status", check=True)
            self.assertFalse(json.loads(result.stdout)["running"])
            installer.windows_task(self.node, "stop", check=True)
        finally:
            installer.windows_task(self.node, "delete")


if __name__ == "__main__":
    unittest.main()
