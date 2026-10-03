"""Install archive boundary and download integrity tests, independent of OS services."""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
import shutil
import zipfile
from pathlib import Path
from unittest.mock import patch

from bootstrapper import core


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, members):
        archive = self.root / "runtime.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            for name, value in members.items():
                info = tarfile.TarInfo(name)
                if isinstance(value, tuple):
                    info.type = tarfile.SYMTYPE
                    info.linkname = value[0]
                    output.addfile(info)
                else:
                    data = value.encode()
                    info.size = len(data)
                    info.mode = 0o755
                    output.addfile(info, io.BytesIO(data))
        return archive

    def test_relative_internal_link_is_preserved(self):
        archive = self.archive({"rdos-runner/bin": "ok", "rdos-runner/alias": ("bin",)})
        destination = self.root / "unpacked"
        core.extract_runtime(archive, destination, "tar.gz")
        self.assertEqual((destination / "rdos-runner/alias").read_text(), "ok")
        self.assertTrue((destination / "rdos-runner/alias").is_symlink())

    def test_traversal_absolute_and_outside_symlink_rejected(self):
        for index, members in enumerate((
            {"../escape": "bad"},
            {"/absolute": "bad"},
            {"rdos-runner/alias": ("../../escape",)},
        )):
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    core.extract_runtime(self.archive(members), self.root / f"bad-{index}", "tar.gz")
        self.assertFalse((self.root.parent / "escape").exists())

    def test_case_collision_and_windows_archive_traversal_rejected(self):
        archive = self.archive({"rdos-runner/A": "one", "rdos-runner/a": "two"})
        with self.assertRaisesRegex(ValueError, "重名"):
            core.extract_runtime(archive, self.root / "collision", "tar.gz")
        zipped = self.root / "runtime.zip"
        with zipfile.ZipFile(zipped, "w") as target:
            target.writestr("../outside", "bad")
        with self.assertRaises(ValueError):
            core.extract_runtime(zipped, self.root / "zip", "zip")

    def test_origin_and_code_validation(self):
        node = "rnr_" + "a" * 12
        self.assertEqual(core.node_from_code(node + "." + "b" * 32), node)
        with self.assertRaises(ValueError):
            core.node_from_code("invalid")
        core._safe_download_url(core.ORIGIN, core.ORIGIN + "/downloads/0.1/runtime.tar.gz")
        for value in ("http://yjmt.cn/baite-rdos/downloads/file", "https://elsewhere.test/downloads/file",
                      core.ORIGIN + "/api/health"):
            with self.assertRaises(ValueError):
                core._safe_download_url(core.ORIGIN, value)

    def test_install_is_private_resumable_and_reuses_same_runtime(self):
        node = "rnr_" + "z" * 12
        code = node + "." + "s" * 43
        archive = self.archive({"rdos-runner/rdos-runner": "frozen runtime"})
        manifest = {"protocol_version": "1", "platforms": {"macos-arm64": {"runtime": {
            "version": "0.1.0-test", "min_bootstrapper_version": "0.1.0", "archive": "tar.gz"}}}}
        calls = []
        def api(origin, path, data=None, token=""):
            calls.append(path)
            if path == "/api/bootstrap/manifest":
                return manifest
            if path == "/api/bootstrap/claim":
                return {"runner_id": node}
            raise AssertionError(path)
        def download(origin, artifact, destination):
            shutil.copy2(archive, destination)
        with patch.object(core, "platform_key", return_value="macos-arm64"), \
             patch.object(core, "installation_root", return_value=self.root / "install"), \
             patch.object(core, "default_workspace", return_value=self.root / "workspace"), \
             patch.object(core, "api_json", side_effect=api), \
             patch.object(core, "download_verified", side_effect=download), \
             patch.object(core, "register_service"), patch.object(core, "start_service"), \
             patch.object(core, "_wait_self_test"):
            one = core.install(code)
            config = self.root / "install/config.json"
            self.assertEqual(json.loads(config.read_text())["runner_id"], node)
            self.assertEqual(config.stat().st_mode & 0o077, 0)
            self.assertFalse((self.root / "install/pending.json").exists())
            two = core.install(code)
        self.assertEqual(one["installation_id"], two["installation_id"])
        self.assertEqual(one["runtime_version"], two["runtime_version"])
        self.assertNotEqual(one["self_test_event_id"], two["self_test_event_id"])
        self.assertEqual(calls.count("/api/bootstrap/claim"), 1)


if __name__ == "__main__":
    unittest.main()
