"""Real guarded subprocesses for phase-1 tests; no patched safety boundary.

run_guarded(body) exposes root, db, store, backend, asyncio, admin,
admin_username, admin_password and project_id. Runtime.initialize is deliberately
left to each test. The parent never installs the irreversible audit hook.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap

CODE = Path(__file__).resolve().parents[1]
ADMIN_USERNAME = "phase1_admin"
ADMIN_PASSWORD = "phase1-test-password-only"


def run_guarded(body: str, *, timeout: float = 30,
                restart_body: str | None = None) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory(prefix="rdos-phase1-") as temporary:
        root = Path(temporary).resolve()
        (root / "code").mkdir()
        for name in ("server", "lab"):
            shutil.copytree(CODE / name, root / "code" / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(CODE / 'node_skills', root / 'code/node_skills',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for source in CODE.glob("rdos*.py"):
            shutil.copy2(source, root / "code" / source.name)
        for name in ("config", "runtime", "runtime/tmp", "nodes"):
            (root / name).mkdir(exist_ok=True)
        env_id, generation = "rdos2_lab_123456abcdef", "1" * 32
        cfg = {"schema_version": 1, "environment": "rdos_local_lab",
               "env_id": env_id, "root": str(root), "port": 18761,
               "backends": {"documents": "local_mock", "model": "local_mock"},
               "external_resources": {"enabled": False, "folder": "", "documents": [],
                                      "task_table": "", "feedback": "", "recipient": ""}}
        (root / "config/lab.json").write_text(json.dumps(cfg))
        (root / "runtime/instance.json").write_text(json.dumps({"env_id": env_id, "generation": generation}))
        (root / "runtime/credentials.json").write_text(json.dumps({
            "admin_username": ADMIN_USERNAME, "admin_password": ADMIN_PASSWORD,
            "session_secret": "phase1-test-session-secret-only-000000",
            "node_tokens": {"node_lab_codex": "phase1-test-codex-only-00000000000000",
                            "node_lab_workbuddy": "phase1-test-workbuddy-only-000000000"}}))
        # Do not inherit provider credentials or proxy configuration.
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "en_US.UTF-8"}
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(root / "code"),
                   RDOS_LAB_MODE="1", RDOS_LAB_ROOT=str(root), RDOS_LAB_RUNTIME=str(root / "runtime"),
                   RDOS_LAB_ENV_ID=env_id, RDOS_LAB_GENERATION=generation,
                   BAITE_DB_PATH=str(root / "runtime/panel.sqlite3"),
                   BAITE_PUBLIC_URL="http://127.0.0.1:18761",
                   BAITE_ADMIN_USERNAME=ADMIN_USERNAME, BAITE_ADMIN_PASSWORD=ADMIN_PASSWORD,
                   BAITE_SESSION_SECRET="phase1-test-session-secret-only-000000",
                   TMPDIR=str(root / "runtime/tmp"))
        bootstrap = """
import asyncio, json, os, sys
from pathlib import Path
# Load dependency code before the guard. This imports no real connectors and
# performs no external I/O; application actions below run with the real hook.
import fastapi, fastapi.staticfiles, fastapi.testclient, httpx
import anyio._backends._asyncio
import anyio._core._fileio
from lab import config, guard
root = Path(os.environ['RDOS_LAB_ROOT'])
cfg = config.load(root)
config.instance(cfg)
from server import db, central_store as store, lab_central_backend as backend
from server import project_sync, rag_sync, onboarding, lab_backend, lab_routes
backend.configure(cfg)
guard.install(root, cfg['port'])
db.init_db()
store.init()
store.seed_test_tasks()
backend.seed()
admin_username = os.environ['BAITE_ADMIN_USERNAME']
admin_password = os.environ['BAITE_ADMIN_PASSWORD']
admin = store.user('mem_lab_admin_0001')
project_id = 'lab_project_phase0'
"""
        result = subprocess.run([sys.executable, "-c", textwrap.dedent(bootstrap) + "\n" + textwrap.dedent(body)],
                                cwd=root / "code", env=env, capture_output=True, text=True, timeout=timeout)
        if restart_body is not None and result.returncode == 0:
            # A fresh process loads the same database and installs a new real
            # guard. No child is permitted to spawn another process.
            return subprocess.run([sys.executable, "-c", textwrap.dedent(bootstrap) + "\n" + textwrap.dedent(restart_body)],
                                  cwd=root / "code", env=env, capture_output=True, text=True, timeout=timeout)
        return result
