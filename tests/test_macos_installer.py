import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy import install_macos_runner as installer


@unittest.skipIf(os.name == "nt", "Mac backend is covered on macOS CI")
class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rdos-installer-unit-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.config = self.base / "input.json"
        self.work = self.base / "工作目录 with spaces"
        self.root = self.base / "Application Support/runners"
        self.node = "rnr_test_user1"
        self.payload = {"runner_id": self.node, "runner_token": "test-token-not-a-real-secret",
                        "control_url": "https://example.invalid/baite-rdos", "workspace": str(self.work)}
        self.config.write_text(json.dumps(self.payload))
        self.plist = self.base / "LaunchAgents" / (installer.service_label(self.node) + ".plist")
        self.runtime = {"runner/runner.py": b"print('test runtime')\n", "rdos_protocol.py": b"# protocol\n"}
        self.sha = "a" * 40
        patches = [patch.object(installer, "plist_path", return_value=self.plist),
                   patch.object(installer, "download_runtime", return_value=(self.sha, self.runtime)),
                   patch.object(installer, "service_info", return_value=subprocess.CompletedProcess([], 113, "", "")),
                   patch.object(installer.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")),
                   patch.object(installer.venv.EnvBuilder, "create"),
                   patch.object(installer, "start_service")]
        self.mocks = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)

    def install(self, start=True):
        return installer.install(self.config, None, self.root, start)

    def test_fresh_install_and_private_config(self):
        directory = self.install()
        config = directory / "config.json"
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        data = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(data["Label"], "org.baite.rdos.runner.rnr_test_user1")
        self.assertNotIn(self.payload["runner_token"], self.plist.read_text())
        self.assertIn(str(config), data["ProgramArguments"])
        self.assertTrue((directory / "manage.py").is_file())
        self.assertTrue((directory / "releases" / self.sha / "rdos_protocol.py").is_file())
        self.mocks[-1].assert_called_once_with(self.node)

    def test_no_start_and_repeat_preserve_workspace(self):
        self.install(start=False)
        self.mocks[-1].assert_not_called()
        (self.work / "notes.md").write_text("keep me")
        self.install(start=False)
        self.assertEqual((self.work / "notes.md").read_text(), "keep me")
        self.assertEqual(len(list((self.root / self.node / "releases").iterdir())), 1)

    def test_workspace_override_does_not_modify_original_config(self):
        other = self.base / "new workspace"
        before = self.config.read_bytes()
        installer.install(self.config, other, self.root, False)
        self.assertTrue(other.is_dir())
        self.assertFalse(self.work.exists())
        self.assertEqual(self.config.read_bytes(), before)

    def test_refuse_nonempty_or_broad_or_relative_workspace(self):
        self.work.mkdir()
        (self.work / "real-work.md").write_text("do not overwrite")
        with self.assertRaisesRegex(ValueError, "空 Workspace"):
            self.install()
        for path in (Path("/"), Path.home(), Path("relative")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                installer.load_config(self.config, path)
        self.assertFalse(self.root.exists())

    def test_refuse_conflicting_install_and_corrupt_runtime(self):
        directory = self.install(False)
        self.payload["workspace"] = str(self.base / "another")
        self.config.write_text(json.dumps(self.payload))
        with self.assertRaisesRegex(ValueError, "workspace"):
            self.install()
        self.payload["workspace"] = str(self.work)
        self.config.write_text(json.dumps(self.payload))
        runtime = directory / "releases" / self.sha / "runner/runner.py"
        runtime.write_text("corrupt")
        with self.assertRaisesRegex(ValueError, "校验失败"):
            self.install()
        self.assertEqual(runtime.read_text(), "corrupt")

    def test_failed_download_keeps_existing_install_and_service(self):
        directory = self.install(False)
        before = (directory / "config.json").read_bytes(), self.plist.read_bytes()
        with patch.object(installer, "download_runtime", side_effect=ValueError("network")), \
                patch.object(installer, "stop_service") as stop:
            with self.assertRaises(ValueError):
                self.install()
            stop.assert_not_called()
        self.assertEqual(before, ((directory / "config.json").read_bytes(), self.plist.read_bytes()))

    def test_token_rotation_restarts_own_service_without_touching_work(self):
        directory = self.install(False)
        self.payload["runner_token"] = "another-test-token-not-a-secret"
        self.config.write_text(json.dumps(self.payload))
        with patch.object(installer, "service_info", return_value=subprocess.CompletedProcess([], 0, "pid = 123", "")), \
                patch.object(installer, "stop_service") as stop:
            self.install()
            stop.assert_called_once_with(self.node)
        self.assertEqual(json.loads((directory / "config.json").read_text())["runner_token"], self.payload["runner_token"])

    def test_other_node_and_other_install_are_not_overwritten(self):
        self.assertNotEqual(installer.service_label(self.node), installer.service_label("rnr_test_user2"))
        self.plist.parent.mkdir()
        self.plist.write_bytes(plistlib.dumps({"Label": installer.service_label(self.node), "ProgramArguments": ["elsewhere"]}))
        with self.assertRaisesRegex(ValueError, "另一处安装"):
            self.install()
        self.assertFalse(self.root.exists())

    def test_plaintext_remote_url_and_unsafe_id_rejected(self):
        for value in ("http://example.com", "https://token@example.com", "https://example.com/?secret=x"):
            self.payload["control_url"] = value
            self.config.write_text(json.dumps(self.payload))
            with self.assertRaises(ValueError):
                installer.load_config(self.config, None)
        with self.assertRaises(ValueError):
            installer.runner_id("../escape")

    def test_status_is_read_only_and_does_not_print_token(self):
        self.install(False)
        self.mocks[3].reset_mock()
        status = installer.status(self.node, self.root)
        self.assertFalse(status["running"])
        self.assertNotIn(self.payload["runner_token"], json.dumps(status))
        self.mocks[3].assert_not_called()


class DownloadAndServiceTest(unittest.TestCase):
    def test_verified_files_and_exact_cloud_release(self):
        config = {"control_url": "https://control.example/baite-rdos", "runner_token": "secret"}
        release = "b" * 40
        content = b"# runtime\n"
        tree = {"tree": [{"path": name, "mode": "100644", "type": "blob",
                          "sha": installer.git_blob_hash(content)} for name in installer.RUNTIME_FILES]}
        replies = [json.dumps({"app_release": release}).encode(), b"{}", json.dumps(tree).encode()] + [content] * len(installer.RUNTIME_FILES)
        with patch.object(installer, "fetch", side_effect=replies) as fetch:
            version, files = installer.download_runtime(config)
            self.assertEqual(version, release)
            self.assertEqual(set(files), set(installer.RUNTIME_FILES))
            self.assertEqual(fetch.call_args_list[1].args, (config["control_url"] + "/api/runner/sync", "secret"))
            self.assertTrue(all(len(call.args) == 1 for call in fetch.call_args_list[2:]))
            self.assertIn(release, fetch.call_args_list[3].args[0])
        replies[-1] = b"# corrupt\n"
        with patch.object(installer, "fetch", side_effect=replies), self.assertRaisesRegex(ValueError, "校验失败"):
            installer.download_runtime(config)
        with patch.object(installer, "fetch", return_value=b'{"app_release":"main"}'), self.assertRaises(ValueError):
            installer.download_runtime(config)

    def test_redirect_never_forwards_token(self):
        with self.assertRaisesRegex(ValueError, "不转发 Token"):
            installer.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid")

    @unittest.skipIf(os.name == "nt", "launchctl is macOS only")
    def test_start_reenables_disabled_service_and_does_not_duplicate(self):
        node = "rnr_service_test"
        with tempfile.NamedTemporaryFile() as file, \
                patch.object(installer, "plist_path", return_value=Path(file.name)), \
                patch.object(installer, "service_info", return_value=subprocess.CompletedProcess([], 113, "", "")), \
                patch.object(installer.subprocess, "run") as run:
            installer.start_service(node)
            self.assertEqual(run.call_args_list[0].args[0][1], "enable")
            self.assertEqual(run.call_args_list[1].args[0][1], "bootstrap")
        with tempfile.NamedTemporaryFile() as file, \
                patch.object(installer, "plist_path", return_value=Path(file.name)), \
                patch.object(installer, "service_info", return_value=subprocess.CompletedProcess([], 0, "pid = 42", "")), \
                patch.object(installer.subprocess, "run") as run:
            installer.start_service(node)
            self.assertEqual(len(run.call_args_list), 1)


if __name__ == "__main__":
    unittest.main()
