"""Start reviewed rollback code on an isolated copy of current formal state.

Run only as the existing baite-rdos service account. The release script makes
the SQLite backup and copies runtime files first; this probe never opens the
live database or live user files for writing.
"""
from __future__ import annotations

import hashlib
import json
import os
import asyncio
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch


@contextmanager
def quiesce_maintenance_runs():
    from server import central_maintenance
    async def no_background_tick():
        # Existing schedules in a copied DB may be enabled and due. Exercise
        # the real lifespan/initialize path without running a real connector.
        await asyncio.Event().wait()
    with patch.object(central_maintenance, '_loop', no_background_tick):
        yield


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit('expected rollback release and isolated state directory')
    release, root = (Path(arg).resolve(strict=True) for arg in sys.argv[1:])
    if not release.joinpath('deploy/STARTUP_FILE_PRESERVATION').is_file():
        raise SystemExit('rollback marker missing')
    sys.path.insert(0, str(release))
    from cutover.production_config import load, LIVE_CLI
    from server import production_runtime
    from fastapi.testclient import TestClient

    database = root / 'baite.db'
    manifest = root / 'rdos2-resources.json'
    resources = json.loads(manifest.read_text(encoding='utf-8'))
    paths = [Path(resources[key]) for key in ('agent_md', 'memory_md')]
    paths.append(root / 'runtime/phase3_resources.json')
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as conn:
        assert conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        memory_before = conn.execute("SELECT id,payload FROM central_records WHERE kind='central_memory' ORDER BY id").fetchall()
        policy_before = conn.execute("SELECT payload FROM central_records WHERE kind='state' AND id='central_agent_policy'").fetchone()

    os.environ['BAITE_DB_PATH'] = str(database)
    os.environ['RDOS_MAINTENANCE_ENABLED'] = '0'
    formal = load(manifest=manifest, database=database, cli=LIVE_CLI,
                  state_root=root, environ=os.environ)
    with patch.object(production_runtime, 'load', return_value=formal):
        from server.production_app import app
    with quiesce_maintenance_runs():
        with TestClient(app, root_path='/baite-rdos', base_url='https://yjmt.cn') as client:
            health = client.get('/baite-rdos/api/health')
            assert health.status_code == 200 and health.json().get('mode') == 'production'
            assert client.get('/baite-rdos/').status_code == 200
            assert client.get('/baite-rdos/api/runtime/info').status_code == 200

    after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    if after != before:
        raise SystemExit('rollback startup changed copied memory, duties or registry files')
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as conn:
        assert conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert memory_before == conn.execute("SELECT id,payload FROM central_records WHERE kind='central_memory' ORDER BY id").fetchall()
        assert policy_before == conn.execute("SELECT payload FROM central_records WHERE kind='state' AND id='central_agent_policy'").fetchone()
    print('RDOS rollback startup copy healthy; current memory, duties, registry and selected DB records preserved')


if __name__ == '__main__':
    main()
