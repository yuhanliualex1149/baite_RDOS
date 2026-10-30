"""One explicit, recoverable release migration; no CLI business operations.

Invoked only by the sealed deployment procedure after stopping the service and
backing up SQLite and user files. No caller-supplied paths or commands exist.
The internal functions take fixtures for isolated tests, never HTTP/tool input.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

BASE = Path('/opt/baite-rdos')
DATABASE = Path('/var/lib/baite-rdos/baite.db')
PLAN_ID = 'next_fixes_120_business_rules_v2'


class MigrationFailure(ValueError):
    pass


def digest(value):
    return hashlib.sha256(value).hexdigest()


def file_snapshot(path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise MigrationFailure('migration_file_not_regular')
    content = path.read_bytes()
    after = path.lstat()
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    if identity(before) != identity(after):
        raise MigrationFailure('migration_file_changed_during_read')
    return {'sha256': digest(content), 'identity': list(identity(after))}


def database_snapshot(database):
    with sqlite3.connect(Path(database).as_uri() + '?mode=ro', uri=True) as conn:
        rows = conn.execute("SELECT kind,id,payload FROM central_records WHERE kind='central_memory' "
                            "OR (kind='state' AND id='central_agent_policy') ORDER BY kind,id").fetchall()
    return digest(json.dumps(rows, ensure_ascii=False, separators=(',', ':')).encode())


def capture(database, files, views):
    before = database_snapshot(database)
    projected = views()
    snapshots = {name: {**file_snapshot(path), 'generated_sha256': digest(projected[name])}
                 for name, path in files.items()}
    if database_snapshot(database) != before:
        raise MigrationFailure('migration_database_changed_during_snapshot')
    return {'database_sha256': before, 'files': snapshots}


def _load_plan(database):
    with sqlite3.connect(Path(database).as_uri() + '?mode=ro', uri=True) as conn:
        row = conn.execute("SELECT payload FROM central_records WHERE kind='release_migration' AND id=?", (PLAN_ID,)).fetchone()
    return json.loads(row[0]) if row else None


def _save_plan(database, plan):
    with sqlite3.connect(database) as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT INTO central_records VALUES(?,?,?,?) '
                     'ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at',
                     ('release_migration', PLAN_ID, json.dumps(plan, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))


def _atomic_write(path, content, original):
    """Same-hash/inode recheck in the stopped-service window, then atomic rename.

    This is not a filesystem CAS across non-cooperating editors. The deployment
    owns the service window and retains its independent file/SQLite backups.
    """
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.release-', dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if file_snapshot(path) != original:
            raise MigrationFailure('migration_file_conflict')
        os.replace(temporary, path)
        # The replacement is durable before its per-file result is committed.
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def run_migration(database, files, old_snapshot, expected_views, migrate, actual_views, *, old_release, candidate_release):
    """Persist the original plan before DB changes; retries reconcile that plan."""
    plan = _load_plan(database)
    if plan and plan.get('phase') == 'completed':
        return summary(plan, reused=True)
    if plan is None:
        observed = old_snapshot()
        expected = expected_views()
        if set(files) != {'agent', 'memory'} or set(observed['files']) != set(files) or set(expected) != set(files):
            raise MigrationFailure('migration_snapshot_incomplete')
        if database_snapshot(database) != observed['database_sha256']:
            raise MigrationFailure('migration_database_conflict')
        for name, path in files.items():
            now = file_snapshot(path)
            if any(now[key] != observed['files'][name][key] for key in ('sha256', 'identity')):
                raise MigrationFailure('migration_file_conflict')
        plan = {'id': PLAN_ID, 'old_release': old_release, 'candidate_release': candidate_release,
                'operation_id': 'release-migration:' + PLAN_ID,
                'source_kind': 'explicit_release_migration', 'actor_id': 'system:release_migration',
                'phase': 'prepared', 'database_before': observed['database_sha256'], 'files': {}}
        for name, data in observed['files'].items():
            plan['files'][name] = {**data, 'target_sha256': digest(expected[name]),
                                  'state': 'pending' if data['sha256'] == data['generated_sha256'] else 'preserved_custom'}
        _save_plan(database, plan)
    if plan.get('id') != PLAN_ID or set(plan.get('files', {})) != set(files):
        raise MigrationFailure('migration_plan_conflict')
    # A lost response after the real migration commits is safe: initialize is
    # marker-idempotent and does not regenerate files in this explicit mode.
    migrate()
    current = actual_views()
    if any(digest(current[name]) != plan['files'][name]['target_sha256'] for name in files):
        raise MigrationFailure('migration_result_conflict')
    plan['phase'] = 'database_migrated'
    _save_plan(database, plan)
    for name, path in files.items():
        step = plan['files'][name]
        if step['state'] == 'preserved_custom':
            continue
        now = file_snapshot(path)
        if now['sha256'] == step['target_sha256']:
            step['state'] = 'verified'
            _save_plan(database, plan)
            continue
        original = {key: step[key] for key in ('sha256', 'identity')}
        if now != original:
            step['error'] = 'migration_file_conflict'
            _save_plan(database, plan)
            raise MigrationFailure('migration_file_conflict')
        _atomic_write(path, current[name], original)
        if file_snapshot(path)['sha256'] != step['target_sha256']:
            raise MigrationFailure('migration_file_readback_failed')
        step.update(state='verified')
        step.pop('error', None)
        _save_plan(database, plan)
    plan['phase'] = 'completed'
    _save_plan(database, plan)
    return summary(plan)


def summary(plan, reused=False):
    return {'migration_id': PLAN_ID, 'completed': plan['phase'] == 'completed', 'reused': reused,
            'verified_files': sum(value['state'] == 'verified' for value in plan['files'].values()),
            'preserved_custom_files': sum(value['state'] == 'preserved_custom' for value in plan['files'].values()),
            'target_sha256': {key: value['target_sha256'] for key, value in plan['files'].items()}}


def _release(path):
    path = path.resolve(strict=True)
    if path.parent != BASE / 'releases' or not re.fullmatch('[0-9a-f]{40}', path.name):
        raise MigrationFailure('migration_release_path_invalid')
    if (path / '.verified').read_text().strip() != path.name:
        raise MigrationFailure('migration_release_unverified')
    return path


def _context(release):
    sys.path.insert(0, str(release))
    from server import production_runtime
    configured = production_runtime.activate()
    from server import central_memory, central_settings
    if configured.database != DATABASE:
        raise MigrationFailure('migration_database_path_invalid')
    files = {'agent': configured.agent_md, 'memory': configured.memory_md}
    def views():
        return {'agent': (central_settings.policy_snapshot()['content'] + '\n').encode(),
                'memory': central_memory._summary_content().encode()}
    return files, views


def main():
    if sys.argv[1:] not in ([], ['--snapshot']):
        raise MigrationFailure('migration_arguments_not_supported')
    old = _release(BASE / 'current')
    candidate = _release(Path(__file__).resolve().parents[1])
    if sys.argv[1:]:
        files, views = _context(old)
        print(json.dumps(capture(DATABASE, files, views), sort_keys=True))
        return
    files, views = _context(candidate)
    from server import central_memory, central_settings, central_store, next_release_migration as migration
    def expected():
        memories = [copy.deepcopy(item) for item in central_store.items('central_memory') if item.get('status') == 'active']
        for item in memories:
            item['content'] = migration.rewrite(item['content'])
        return {'agent': (migration.policy_content(central_settings.policy_snapshot()['content']) + '\n').encode(),
                'memory': central_memory._render_memory(memories).encode()}
    def old_snapshot():
        result = subprocess.run([str(old / '.venv/bin/python'), '-B', str(Path(__file__).resolve()), '--snapshot'],
                                capture_output=True, text=True, check=False, timeout=30, cwd=str(old))
        if result.returncode or len(result.stdout) > 64000:
            raise MigrationFailure('migration_old_snapshot_failed')
        return json.loads(result.stdout)
    result = run_migration(DATABASE, files, old_snapshot, expected,
                           lambda: migration.initialize(regenerate_views=False), views,
                           old_release=old.name, candidate_release=candidate.name)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('RDOS explicit release migration incomplete; original plan and latest data retained') from None
