"""Configuration and control checks use disposable synthetic roots only."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lab import config
import labctl


class LabConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lab-config-", dir=Path(__file__).parent)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "experiment"
        self.root.mkdir()
        for name in ("config", "nodes", "evidence"):
            (self.root / name).mkdir()
        self.cfg = {
            "schema_version": 1, "environment": "rdos_local_lab",
            "env_id": "rdos2_lab_123456abcdef", "root": str(self.root), "port": 18760,
            "backends": {"documents": "local_mock", "model": "local_mock"},
            "external_resources": {"enabled": False, "folder": "", "documents": [],
                "task_table": "", "feedback": "", "recipient": ""},
        }
        self.write_config(self.cfg)

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self, value):
        (self.root / "config/lab.json").write_text(json.dumps(value))

    def test_explicit_local_configuration_and_no_fallback(self):
        self.assertEqual(config.load(self.root), self.cfg)
        cases = []
        missing = copy.deepcopy(self.cfg)
        missing.pop("backends")
        cases.append(missing)
        external = copy.deepcopy(self.cfg)
        external["external_resources"]["feedback"] = "https://example.invalid/fake"
        cases.append(external)
        model = copy.deepcopy(self.cfg)
        model["backends"]["model"] = "real"
        cases.append(model)
        wrong_root = copy.deepcopy(self.cfg)
        wrong_root["root"] = str(self.base)
        cases.append(wrong_root)
        wrong_port = copy.deepcopy(self.cfg)
        wrong_port["port"] = True
        cases.append(wrong_port)
        for case in cases:
            with self.subTest(case=case):
                self.write_config(case)
                with self.assertRaises(ValueError):
                    config.load(self.root)

    def test_agent_plan_requires_private_exact_provider_settings(self):
        cfg = copy.deepcopy(self.cfg)
        cfg['backends']['model'] = 'agent_plan'
        self.write_config(cfg)
        with self.assertRaisesRegex(ValueError, '配置缺失'):
            config.load(self.root)
        runtime = self.root / 'runtime'
        runtime.mkdir()
        path = runtime / 'model.json'
        data = {'api_key': 'synthetic-test-only',
                'base_url': 'https://ark.cn-beijing.volces.com/api/plan/v3',
                'model': 'deepseek-v4-pro', 'thinking': 'enabled', 'reasoning_effort': 'high'}
        path.write_text(json.dumps(data))
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, '权限过宽'):
            config.load(self.root)
        path.chmod(0o600)
        self.assertEqual(config.load(self.root), cfg)
        data['base_url'] = 'https://example.invalid/api/plan/v3'
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, '不在本轮登记范围'):
            config.load(self.root)

    def test_paths_reject_escape_symlinks_hardlinks_and_special_files(self):
        runtime = self.root / "runtime"
        runtime.mkdir()
        outside = self.base / "outside.txt"
        outside.write_text("synthetic sentinel")
        with self.assertRaises(ValueError):
            config.checked_path(self.root / ".." / "outside.txt", self.root)
        alias = runtime / "alias"
        alias.symlink_to(outside)
        with self.assertRaises(ValueError):
            config.load(self.root)
        alias.unlink()
        os.link(outside, alias)
        with self.assertRaises(ValueError):
            config.load(self.root)
        alias.unlink()
        os.mkfifo(alias)
        with self.assertRaises(ValueError):
            config.load(self.root)
        alias.unlink()
        directory_alias = runtime / "linked-dir"
        directory_alias.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(ValueError):
            config.checked_path(directory_alias / "outside.txt", self.root)
        self.assertEqual(outside.read_text(), "synthetic sentinel")

    def test_json_write_does_not_truncate_a_preexisting_hardlinked_temp(self):
        outside = self.base / "outside.txt"
        outside.write_text("synthetic sentinel")
        target = self.root / "config/new.json"
        os.link(outside, target.with_suffix(".json.new"))
        try:
            config.write_json(target, {"safe": True})
        except (ValueError, FileExistsError):
            pass
        self.assertEqual(outside.read_text(), "synthetic sentinel")

    def test_process_command_mismatch_never_sends_a_signal(self):
        runtime = self.root / "runtime"
        runtime.mkdir()
        generation = "1" * 32
        config.write_json(runtime / "instance.json", {"env_id": self.cfg["env_id"], "generation": generation})
        config.write_json(runtime / "process.json", {"env_id": self.cfg["env_id"], "generation": generation,
            "instance_id": "2" * 32, "pid": 987654})
        original_load = config.load
        with patch.object(labctl, "ROOT", self.root), patch.object(config, "load", lambda: original_load(self.root)), \
             patch.object(labctl.subprocess, "run") as ps, patch.object(labctl.os, "kill") as kill:
            ps.return_value.stdout = "/synthetic/other/python unrelated.py\n"
            with self.assertRaisesRegex(ValueError, "PID"):
                labctl.stop()
            kill.assert_not_called()
            ps.assert_called_once_with(["/bin/ps", "-p", "987654", "-o", "command="],
                                       capture_output=True, text=True, check=False)

    def test_reset_requires_confirmation_and_preserves_evidence_and_workspace(self):
        original_load = config.load
        with patch.object(labctl, "ROOT", self.root), patch.object(labctl, "CODE", self.root / "code"), \
             patch.object(labctl, "PYTHON", self.root / ".venv/bin/python"), \
             patch.object(config, "load", lambda: original_load(self.root)), patch.object(labctl, "owned_process", return_value=None):
            old = labctl.prepare_runtime(self.cfg)
            personal = self.root / "nodes/codex/feedback.txt"
            personal.write_text("synthetic user edit")
            events = self.root / "runtime/events.jsonl"
            events.write_text('{"event_type":"synthetic"}\n')
            with self.assertRaisesRegex(ValueError, "confirm"):
                labctl.reset("wrong-env")
            self.assertTrue(events.exists())
            after = labctl.reset(self.cfg["env_id"])
            self.assertNotEqual(after["generation"], old["generation"])
            self.assertEqual(personal.read_text(), "synthetic user edit")
            archive = self.root / "evidence" / ("reset-" + old["generation"])
            self.assertEqual((archive / "events.jsonl").read_text(), '{"event_type":"synthetic"}\n')
            self.assertFalse((archive / "credentials.json").exists())
            self.assertFalse(events.exists())


if __name__ == "__main__":
    unittest.main()
