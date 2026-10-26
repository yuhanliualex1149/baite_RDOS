"""Audit-hook checks run in disposable subprocesses, without a network target.

Sockets are closed before connect/sendto so even a missing guard cannot transmit.
Subprocess internals are mocked before exercising the guarded Popen path.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


CODE = Path(__file__).resolve().parents[1]


class LabGuardTest(unittest.TestCase):
    def run_guard(self, body, prelude=""):
        with tempfile.TemporaryDirectory(prefix="lab-guard-", dir=Path(__file__).parent) as temp:
            base = Path(temp).resolve()
            root = base / "experiment"
            (root / "runtime").mkdir(parents=True)
            (root / "code").mkdir()
            outside = base / "outside"
            outside.mkdir()
            (outside / "private.txt").write_text("synthetic-private-sentinel")
            script = textwrap.dedent('''
                import json, os, socket, subprocess, sys
                from pathlib import Path
                from unittest.mock import patch
                from lab.guard import install, require_active
                root, outside = map(Path, sys.argv[1:])
                closed_socket = socket.socket()
                closed_socket.close()
                def denied(action):
                    try:
                        action()
                    except PermissionError:
                        return
                    raise AssertionError("expected guard refusal")
                os.environ["RDOS_LAB_MODE"] = "1"
                os.environ["RDOS_LAB_ENV_ID"] = "synthetic-env"
                os.environ["RDOS_LAB_GENERATION"] = "synthetic-generation"
                install(root, 18760)
                require_active()
            ''').replace('install(root, 18760)', textwrap.dedent(prelude) + '\ninstall(root, 18760)') + textwrap.dedent(body)
            result = subprocess.run([sys.executable, "-B", "-c", script, str(root), str(outside)],
                cwd=root / "code", capture_output=True, text=True, timeout=15,
                env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(CODE), "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(result.returncode, 0, result.stderr)
            log = root / "runtime/blocked.jsonl"
            records = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            self.assertNotIn("synthetic-private-sentinel", log.read_text() if log.exists() else "")
            self.assertNotIn(str(outside), log.read_text() if log.exists() else "")
            return records

    def test_real_socket_entry_and_mocked_subprocess_are_blocked(self):
        records = self.run_guard('''
            # TEST-NET-1 is not contacted: the socket is already closed.
            denied(lambda: closed_socket.connect(("192.0.2.10", 443)))
            denied(lambda: closed_socket.sendto(b"synthetic", ("192.0.2.10", 443)))
            with patch("subprocess._fork_exec", side_effect=AssertionError("must not spawn")) as spawn:
                denied(lambda: subprocess.Popen(["/usr/bin/true"]))
                spawn.assert_not_called()
        ''')
        self.assertTrue(any(item["channel"] == "network.connect" for item in records))
        self.assertTrue(any(item["channel"].startswith("process.") for item in records))

    def test_file_reads_writes_database_and_environment_file_refused(self):
        records = self.run_guard('''
            import sqlite3
            denied(lambda: (outside / "private.txt").read_text())
            denied(lambda: (outside / "created.txt").write_text("not allowed"))
            denied(lambda: (root / "code/.env.local").read_text())
            denied(lambda: sqlite3.connect(str(outside / "database.sqlite3")))
            (root / "runtime/allowed.txt").write_text("synthetic allowed data")
            assert (root / "runtime/allowed.txt").read_text() == "synthetic allowed data"
        ''')
        self.assertGreaterEqual(len(records), 4)

    def test_directory_enumeration_outside_lab_is_refused(self):
        self.run_guard('''
            denied(lambda: os.listdir(outside))
            denied(lambda: list(os.scandir(outside)))
        ''')

    def test_relative_write_and_working_directory_bypass_are_refused(self):
        self.run_guard('''
            directory_fd = os.open(root / "code", os.O_RDONLY)
            try:
                denied(lambda: os.chdir(root / "runtime"))
                denied(lambda: os.fchdir(directory_fd))
                denied(lambda: os.open("tampered.py", os.O_WRONLY | os.O_CREAT, dir_fd=directory_fd))
                assert not (root / "code/tampered.py").exists()
                (root / "runtime/allowed.txt").write_text("still allowed")
            finally:
                os.close(directory_fd)
        ''')

    def test_real_provider_entries_deny_before_mocked_transports(self):
        records = self.run_guard('''
            with patch("subprocess.run") as spawn, patch.object(httpx.AsyncClient, "post") as post:
                denied(lambda: FeishuTools(executable="synthetic-cli"))
                denied(lambda: FeishuTools.__new__(FeishuTools)._run(["synthetic-command"]))
                denied(lambda: FeishuRagSource(executable="synthetic-cli"))
                denied(lambda: FeishuRagSource.__new__(FeishuRagSource)._run(["synthetic-command"]))
                agent = CentralAgent.__new__(CentralAgent)
                request = agent._request(None, {"synthetic": True}, {}, None, None, 100, 0)
                run = agent._run([], [], None, None, None)
                # Both coroutine bodies must refuse before their first await.
                denied(lambda: request.send(None))
                denied(lambda: run.send(None))
                spawn.assert_not_called()
                post.assert_not_called()
        ''', prelude='''
            import httpx
            from server.feishu_tools import FeishuTools
            from server.rag_sync import FeishuRagSource
            from server.central_agent import CentralAgent
        ''')
        channels = {item["channel"] for item in records}
        self.assertEqual(channels, {"real_feishu_cli_constructor", "real_feishu_cli_subprocess",
            "real_rag_cli_constructor", "real_rag_cli_subprocess", "real_central_model_http", "real_central_model_run"})

    def test_lifespan_has_only_explicit_maintenance_no_business_scheduler_or_replay(self):
        import ast
        module = ast.parse((CODE / "server/main.py").read_text())
        lifespan = next(node for node in module.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan")
        calls = {ast.unparse(node.func) for node in ast.walk(lifespan) if isinstance(node, ast.Call)}
        self.assertEqual(calls, {"require_active", "db.init_db", "central_store.init", "phase1_runtime.initialize", "phase3_service.initialize", "_sync_registered_projects", "central_memory.initialize", "central_settings.initialize", "peer_tasks.initialize", "central_maintenance.start", "central_maintenance.stop", "central_service.shutdown"})
        self.assertEqual(sum(isinstance(node, ast.Yield) for node in ast.walk(lifespan)), 1)
        self.assertFalse(any(isinstance(node, (ast.For, ast.While, ast.AsyncFor)) for node in ast.walk(lifespan)))
        self.assertEqual([ast.unparse(node.value.func) for node in ast.walk(lifespan) if isinstance(node, ast.Await)], ["central_maintenance.start", "central_maintenance.stop", "central_service.shutdown"])

    def test_old_server_entry_requires_active_guard(self):
        result = subprocess.run([sys.executable, "-B", "-c", "import server.main"], cwd=CODE,
            capture_output=True, text=True, timeout=15,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(CODE), "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("labctl.py start", result.stderr)


if __name__ == "__main__":
    unittest.main()
