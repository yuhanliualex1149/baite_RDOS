"""Formal configuration cannot silently select lab or unknown resources."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from cutover import production_config as formal


class FormalConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        (self.state / "central").mkdir()
        self.cli = self.root / "run.js"
        self.cli.write_text("// pinned CLI fixture\n", encoding="utf-8")
        self.database = self.state / "baite.db"
        (self.state / "central" / "agent.md").write_text("# Formal central policy\n", encoding="utf-8")
        (self.state / "central" / "agent.md").chmod(0o600)
        self.manifest = self.root / "resources.json"
        self.resources = {
            "schema_version": 1, "mode": "production",
            "base_token": formal.BASE_TOKEN, "task_table": formal.TASK_TABLE,
            "task_view": formal.TASK_VIEW, "event_table": "tblFormalEvents123",
            "default_project_id": formal.DEFAULT_PROJECT,
            "finance_project_id": formal.FINANCE_PROJECT,
            "finance_folder": formal.FINANCE_FOLDER,
            "human_folder": formal.HUMAN_FOLDER, "agent_folder": formal.AGENT_FOLDER,
            "selected_rag_folder": formal.SELECTED_RAG_FOLDER,
            "shared_skills_folder": formal.SHARED_SKILLS_FOLDER,
            "cli_path": str(self.cli),
            "cli_sha256": hashlib.sha256(self.cli.read_bytes()).hexdigest(),
            "account_fingerprint": "a" * 64,
            "node_members": {
                "node_yuhan": "mem_p6BbcYJrwAcv_M5Y",
                "node_xiaofei": "mem_x_rLn9VBAe_z8xol",
                "node_duoduo": "mem_g3nKLUxRovywCSTk",
            },
            "agent_md": str(self.state / "central" / "agent.md"),
            "memory_md": str(self.state / "central" / "memory.md"),
        }
        self.environment = {
            "RDOS_RUNTIME_MODE": "production",
            "BAITE_PUBLIC_URL": formal.PUBLIC_URL + "/",
            "BAITE_DB_PATH": str(self.database),
            "BAITE_SESSION_SECRET": "x" * 32,
            "BAITE_LLM_API_KEY": "test-only",
            "BAITE_LLM_BASE_URL": formal.MODEL_URL,
            "BAITE_LLM_MODEL": formal.MODEL_NAME,
            "BAITE_LLM_THINKING": "enabled",
            "BAITE_LLM_REASONING_EFFORT": "high",
        }

    def load(self):
        self.manifest.write_text(json.dumps(self.resources), encoding="utf-8")
        self.manifest.chmod(0o600)
        return formal.load(self.manifest, environ=self.environment,
                           database=self.database, cli=self.cli,
                           state_root=self.state)

    def test_exact_formal_resources_load(self):
        result = self.load()
        self.assertEqual(result.resources["base_token"], formal.BASE_TOKEN)
        self.assertEqual(result.cli, self.cli)

    def test_lab_base_cannot_become_formal_authority(self):
        self.resources["base_token"] = "lab_task_store"
        with self.assertRaises(formal.ConfigurationError):
            self.load()

    def test_missing_real_model_configuration_fails(self):
        for name in ("BAITE_LLM_BASE_URL", "BAITE_LLM_MODEL", "BAITE_LLM_THINKING"):
            with self.subTest(name=name):
                value = self.environment.pop(name)
                try:
                    with self.assertRaises(formal.ConfigurationError):
                        self.load()
                finally:
                    self.environment[name] = value

    def test_symlink_cli_and_changed_content_fail(self):
        link = self.root / "link.js"
        link.symlink_to(self.cli)
        self.resources["cli_path"] = str(link)
        self.manifest.write_text(json.dumps(self.resources), encoding="utf-8")
        self.manifest.chmod(0o600)
        with self.assertRaises(formal.ConfigurationError):
            formal.load(self.manifest, environ=self.environment,
                        database=self.database, cli=link, state_root=self.state)
        self.resources["cli_path"] = str(self.cli)
        self.cli.write_text("// changed\n", encoding="utf-8")
        with self.assertRaises(formal.ConfigurationError):
            self.load()

    def test_manifest_permissions_and_memory_scope_fail(self):
        self.resources["memory_md"] = str(self.root / "outside.md")
        with self.assertRaises(formal.ConfigurationError):
            self.load()
        self.resources["memory_md"] = str(self.state / "central" / "memory.md")
        self.manifest.write_text(json.dumps(self.resources), encoding="utf-8")
        self.manifest.chmod(0o644)
        with self.assertRaises(formal.ConfigurationError):
            formal.load(self.manifest, environ=self.environment,
                        database=self.database, cli=self.cli, state_root=self.state)


if __name__ == "__main__":
    unittest.main()
