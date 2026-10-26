"""Boot the formal ASGI entry against an isolated existing database.

The fake CLI stops at identity.  This verifies startup and HTTP routing, not
formal Feishu access, which must be proved separately at cutover.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest


class FormalAppStartupTests(unittest.TestCase):
    def test_existing_accounts_and_subpath_boot_without_lab_guard(self):
        source = textwrap.dedent(r'''
            import hashlib, os, sqlite3, tempfile
            from pathlib import Path
            from unittest.mock import patch
            from fastapi.testclient import TestClient
            from cutover.production_config import FormalConfig, BASE_TOKEN, TASK_TABLE, TASK_VIEW, HUMAN_FOLDER, AGENT_FOLDER, SELECTED_RAG_FOLDER, SHARED_SKILLS_FOLDER, FINANCE_PROJECT, FINANCE_FOLDER
            from server import production_runtime, db

            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / 'central').mkdir()
                policy = root / 'central/agent.md'
                policy.write_text('# Formal central duties\n', encoding='utf-8')
                policy.chmod(0o600)
                database = root / 'baite.db'
                with sqlite3.connect(database) as conn:
                    conn.execute('CREATE TABLE central_users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL, roles_json TEXT NOT NULL, password_hash TEXT NOT NULL, identity_mode TEXT NOT NULL, enabled INTEGER NOT NULL, created_at TEXT NOT NULL)')
                    conn.executemany('INSERT INTO central_users VALUES(?,?,?,?,?,?,?,?)', [
                        ('mem_p6BbcYJrwAcv_M5Y', 'admin', 'Formal Admin', '["admin"]', db.hash_password('fixture-password-123'), 'actual', 1, '2026-01-01'),
                        ('mem_x_rLn9VBAe_z8xol', 'xiaofei', 'Xiaofei', '["researcher"]', 'placeholder', 'actual', 1, '2026-01-01'),
                        ('mem_g3nKLUxRovywCSTk', 'duoduo', 'Duoduo', '["researcher"]', 'placeholder', 'actual', 1, '2026-01-01'),
                    ])
                    import json
                    conn.execute('CREATE TABLE phase3_local(kind TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(kind,id))')
                    marker = {'source_count': 18, 'task_ids': ['task-%02d' % i for i in range(18)], 'content_sha256': 'a'*64}
                    conn.execute('INSERT INTO phase3_local VALUES(?,?,?,?)',
                                 ('state', 'formal_migration', json.dumps(marker), '2026-01-01'))
                cli = root / 'cli'
                cli.write_text('fixture', encoding='utf-8')
                cli.chmod(0o755)
                fingerprint = 'a' * 64
                resources = {'schema_version': 1, 'mode': 'production',
                    'base_token': BASE_TOKEN, 'task_table': TASK_TABLE,
                    'task_view': TASK_VIEW, 'event_table': 'tblFormalEvents123',
                    'default_project_id': 'project_rdos',
                    'finance_project_id': FINANCE_PROJECT, 'finance_folder': FINANCE_FOLDER,
                    'human_folder': HUMAN_FOLDER, 'agent_folder': AGENT_FOLDER,
                    'selected_rag_folder': SELECTED_RAG_FOLDER,
                    'shared_skills_folder': SHARED_SKILLS_FOLDER,
                    'cli_path': str(cli), 'cli_sha256': hashlib.sha256(cli.read_bytes()).hexdigest(),
                    'account_fingerprint': fingerprint,
                    'node_members': {'node_yuhan': 'mem_p6BbcYJrwAcv_M5Y',
                                     'node_xiaofei': 'mem_x_rLn9VBAe_z8xol',
                                     'node_duoduo': 'mem_g3nKLUxRovywCSTk'},
                    'agent_md': str(policy), 'memory_md': str(root / 'central/memory.md')}
                formal = FormalConfig(resources, database, cli, policy, root / 'central/memory.md')
                os.environ.update(RDOS_RUNTIME_MODE='production', BAITE_PUBLIC_URL='https://yjmt.cn/baite-rdos',
                    BAITE_DB_PATH=str(database), BAITE_SESSION_SECRET='x'*32,
                    BAITE_ADMIN_USERNAME='admin', BAITE_ADMIN_PASSWORD='not-used-in-existing-db',
                    BAITE_LLM_API_KEY='fixture-only', BAITE_LLM_BASE_URL='https://ark.cn-beijing.volces.com/api/plan/v3',
                    BAITE_LLM_MODEL='deepseek-v4-pro', BAITE_LLM_THINKING='enabled', BAITE_LLM_REASONING_EFFORT='high')
                with patch.object(production_runtime, 'load', return_value=formal):
                    from server.production_app import app
                from server import phase3_feishu
                with patch.object(phase3_feishu._CLIExecutor, 'identity', return_value={'account_fingerprint': fingerprint}):
                    with TestClient(app, root_path='/baite-rdos', base_url='https://yjmt.cn') as client:
                        page = client.get('/baite-rdos/')
                        assert page.status_code == 200, page.text[:300]
                        assert '<base href="/baite-rdos/" />' in page.text
                        assert 'RDOS 实验环境' not in page.text
                        info = client.get('/baite-rdos/api/runtime/info')
                        assert info.status_code == 200 and info.json()['mode'] == 'production', info.text[:300]
                        js = client.get('/baite-rdos/static/central.js')
                        assert js.status_code == 200 and "api/runtime/info" in js.text
                        assert client.get('/baite-rdos/api/lab/info').status_code == 410
                        health = client.get('/baite-rdos/api/health')
                        assert health.status_code == 200 and health.json()['mode'] == 'production'
                        login = client.post('/baite-rdos/api/central/auth/login',
                            headers={'Origin': 'https://yjmt.cn'},
                            json={'username': 'admin', 'password': 'fixture-password-123'})
                        assert login.status_code == 200, login.text[:300]
                        assert 'Path=/baite-rdos/' in login.headers['set-cookie']
                        assert client.get('/baite-rdos/api/central/auth/me').status_code == 200
                        projects = client.get('/baite-rdos/api/central/projects')
                        assert projects.status_code == 200, projects.text[:300]
                        assert 'rdos-self' in projects.text
                        assert FINANCE_FOLDER in projects.text
                with sqlite3.connect(database) as conn:
                    assert conn.execute('SELECT COUNT(*) FROM central_users').fetchone()[0] == 3
                    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='phase3_mock'").fetchone()[0] == 0
        ''')
        env = dict(os.environ)
        env.pop('RDOS_LAB_RUNTIME', None)
        result = subprocess.run([sys.executable, '-B', '-c', source], cwd=os.getcwd(),
                                env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
