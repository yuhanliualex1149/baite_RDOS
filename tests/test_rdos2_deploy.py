"""No-host regression for the root-owned RDOS 2.0 release protocol."""
from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1]
SCRIPT = SOURCE / 'deploy/production-deploy.sh'
GATEWAY = SOURCE / 'deploy/ssh-gateway.sh'
WORKFLOW = SOURCE / '.github/workflows/production.yml'
OLD = 'a' * 40
NEW = 'b' * 40
ORIGINAL_OLD = '6634ff01e542492cf0173372c1ba043cba68ca5d'
SAFE_OLD = '5272f506ecfe47212cf4a6820aac3eb12ec9a1f7'


class RoutineReleaseTests(unittest.TestCase):
    def test_protocol_and_static_guards(self):
        result = subprocess.run(['bash', str(SCRIPT), '--protocol'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'RDOS2_DEPLOY_PROTOCOL=2')
        for script in (SCRIPT, GATEWAY):
            subprocess.run(['bash', '-n', str(script)], check=True)
        body = SCRIPT.read_text()
        workflow = WORKFLOW.read_text()
        self.assertIn('python -B -m cutover.check_release', body)
        self.assertNotIn('unittest discover', body)
        self.assertNotIn('migrate_cli', body)
        self.assertLess(body.index('pip check'), body.index('.verified.tmp'))
        self.assertLess(workflow.index('RDOS2_DEPLOY_PROTOCOL=2'), workflow.index('"deploy $COMMIT_SHA"'))
        self.assertLess(workflow.index('RDOS2_DEPLOY_SHA256=$expected'), workflow.index('"deploy $COMMIT_SHA"'))
        self.assertIn('/usr/local/sbin/rdos-production-deploy --protocol', GATEWAY.read_text())
        self.assertIn('RDOS2_DEPLOY_SHA256=', GATEWAY.read_text())
        self.assertIn('PIP_NO_INDEX=1 PIP_FIND_LINKS="$wheelhouse"', body)

    def test_rollback_probe_quiesces_existing_due_maintenance(self):
        source = r'''
import asyncio, importlib.util
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
from server import production_runtime
production_runtime._config = SimpleNamespace(resources={'base_token':'fixture','task_table':'fixture'})
spec = importlib.util.spec_from_file_location('rdos_rollback_probe','deploy/probe-rollback-startup.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
from server import central_maintenance as maintenance
due = {'enabled': True, 'next_due_at': '2020-01-01T00:00:00+08:00'}
async def exercise():
    with patch.object(maintenance.store, 'get', side_effect=lambda kind, key: due if kind == 'maintenance_schedule' else None), \
         patch.object(maintenance.store, 'items', return_value=[]), \
         patch.object(maintenance, 'tick', new_callable=AsyncMock) as tick:
        with probe.quiesce_maintenance_runs():
            await maintenance.start(enabled=False)
            await asyncio.sleep(0.02)
            await maintenance.stop()
        tick.assert_not_awaited()
asyncio.run(exercise())
'''
        result = subprocess.run([sys.executable, '-B', '-c', source], cwd=SOURCE,
            env={**os.environ, 'RDOS_RUNTIME_MODE':'production'}, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def fixture(self, *, verified=True):
        root = Path(tempfile.mkdtemp(prefix='rdos2-release-fixture-'))
        def cleanup():
            cache = root / 'opt/baite-rdos/wheelhouse/requirements-8a14fbc486afe3a44e7c3a8a0564547b56e8dfa44778a50ab736b3d38af6fcbf'
            if cache.exists():
                cache.chmod(0o755)
            __import__('shutil').rmtree(root)
        self.addCleanup(cleanup)
        base = root / 'opt/baite-rdos'
        releases = base / 'releases'
        releases.mkdir(parents=True)
        (base / 'repository').mkdir()
        fixture_lock = b'fixture pinned requirements\n'
        lock_sha = hashlib.sha256(fixture_lock).hexdigest()
        wheelhouse = base / 'wheelhouse/requirements-8a14fbc486afe3a44e7c3a8a0564547b56e8dfa44778a50ab736b3d38af6fcbf'
        wheelhouse.mkdir(parents=True)
        (wheelhouse / 'requirements.lock').write_bytes(fixture_lock)
        manifest = []
        for i in range(17):
            name = f'fixture_{i}-1.0-py3-none-any.whl'
            content = f'fixture wheel {i}\n'.encode()
            (wheelhouse / name).write_bytes(content)
            manifest.append(f'{hashlib.sha256(content).hexdigest()}  {name}\n')
        (wheelhouse / 'SHA256SUMS').write_text(''.join(manifest))
        manifest_sha = hashlib.sha256((wheelhouse / 'SHA256SUMS').read_bytes()).hexdigest()
        for path in wheelhouse.iterdir():
            path.chmod(0o444)
        wheelhouse.chmod(0o555)
        tool = base / 'tools/bin'
        tool.mkdir(parents=True)
        (tool / 'python3.12').symlink_to(sys.executable)
        old = releases / OLD
        old.mkdir()
        (old / 'requirements.lock').write_bytes(fixture_lock)
        (old / 'deploy').mkdir()
        (old / 'deploy/STARTUP_FILE_PRESERVATION').write_text('RDOS2_STARTUP_FILES_PRESERVED=1\n')
        candidate = releases / NEW
        if verified:
            candidate.mkdir()
            (candidate / 'requirements.lock').write_bytes(fixture_lock)
            (candidate / 'deploy').mkdir()
            (candidate / 'deploy/STARTUP_FILE_PRESERVATION').write_text('RDOS2_STARTUP_FILES_PRESERVED=1\n')
            (candidate / '.venv/bin').mkdir(parents=True)
            (candidate / '.venv/bin/python').symlink_to(sys.executable)
            uvicorn = candidate / '.venv/bin/uvicorn'
            uvicorn.write_text('#!/bin/sh\nexit 0\n')
            uvicorn.chmod(0o755)
            (candidate / '.verified').write_text(NEW + '\n')
            (candidate / 'release.env').write_text('BAITE_APP_RELEASE=' + NEW + '\n')
        (base / 'current').symlink_to(old)
        dbdir = root / 'var/lib/baite-rdos'
        dbdir.mkdir(parents=True)
        database = dbdir / 'baite.db'
        with sqlite3.connect(database) as conn:
            conn.execute('CREATE TABLE business (payload TEXT)')
            conn.execute("INSERT INTO business VALUES ('before')")
            conn.execute('CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)')
            conn.execute("INSERT INTO settings VALUES ('schema_version','8','fixture')")
            conn.execute('CREATE TABLE central_records (kind TEXT,id TEXT,payload TEXT,updated_at TEXT,PRIMARY KEY(kind,id))')
            for name, payload in (
                ('phase1_schema', {'version': 2}),
                ('central_memory_schema', {'version': 2}),
                ('central_agent_policy', {'version': 7, 'content': 'current administrator policy'}),
                ('runtime', {'paused': True, 'phase': 3, 'scheduler_enabled': False,
                             'model_mode': 'agent_plan', 'backend': 'feishu_production'}),
            ):
                conn.execute('INSERT INTO central_records VALUES (?,?,?,?)',
                    ('state', name, json.dumps(payload), 'fixture'))
            conn.execute('INSERT INTO central_records VALUES (?,?,?,?)',
                ('central_memory', 'user-memory', json.dumps({'content': 'current user memory'}), 'fixture'))
            for name, payload in (
                ('project_rdos', {'rdos_project_schema': 1, 'nodes_explicit': True,
                    'owner_id': 'fixture-admin', 'folder_id': 'fixture-human',
                    'folder_url': 'https://scn33386s7ui.feishu.cn/drive/folder/fixture-human',
                    'source_environment': 'feishu_production'}),
                ('rdos-self', {'rdos_project_schema': 1, 'owner_id': 'fixture-admin',
                    'folder_id': 'fixture-finance'}),
            ):
                conn.execute('INSERT INTO central_records VALUES (?,?,?,?)',
                    ('project', name, json.dumps(payload), 'fixture'))
            for name in ('hourly_refresh', 'weekly_inspection'):
                conn.execute('INSERT INTO central_records VALUES (?,?,?,?)',
                    ('maintenance_schedule', name, json.dumps({'enabled': False}), 'fixture'))
            conn.execute('CREATE TABLE phase3_local (kind TEXT,id TEXT,payload TEXT,updated_at TEXT,PRIMARY KEY(kind,id))')
            for kind, name, payload in (
                ('state', 'phase3_schema', {'version': 1}),
                ('state', 'business_workflow', {'version': 1}),
                ('node', 'fixture-node', {'member_id': 'fixture-member'}),
            ):
                conn.execute('INSERT INTO phase3_local VALUES (?,?,?,?)',
                    (kind, name, json.dumps(payload), 'fixture'))
        memory = dbdir / 'central/memory.md'
        memory.parent.mkdir(parents=True)
        memory.write_text('current user memory view\n')
        agent = dbdir / 'central/agent.md'
        agent.write_text('current administrator policy\n')
        peer_keys = dbdir / 'runtime/peer_identity.json'
        peer_keys.parent.mkdir(parents=True)
        peer_keys.write_text('fixture identity stays here\n')
        (dbdir / 'runtime/phase3_resources.json').write_text('fixture current registry\n')
        config = root / 'etc/baite-rdos'
        config.mkdir(parents=True)
        for name in ('rdos.env', 'rdos2.env', 'rdos2-resources.json'):
            (config / name).write_text('fixture-only\n')
        (config / 'rdos2-resources.json').write_text(json.dumps({
            'agent_md': str(agent), 'memory_md': str(memory),
            'node_members': {'fixture-node': 'fixture-member'},
            'default_project_id': 'project_rdos', 'finance_project_id': 'rdos-self',
            'human_folder': 'fixture-human', 'finance_folder': 'fixture-finance'}))
        override = root / 'etc/systemd/system/baite-rdos.service.d/rdos2.conf'
        override.parent.mkdir(parents=True)
        override.write_text('fixture-only\n')
        proxy = root / 'www/server/panel/vhost/nginx/proxy/www.yjmt.cn'
        proxy.mkdir(parents=True)
        for name in ('baite-rdos.conf', 'baite-rdos-downloads.conf'):
            (proxy / name).write_text('original RDOS proxy\n')
        (root / 'run/lock').mkdir(parents=True)
        fakebin = root / 'fakebin'
        fakebin.mkdir()
        def command(name, body):
            path = fakebin / name
            path.write_text('#!/bin/sh\nset -eu\n' + body)
            path.chmod(0o755)
        command('git', '''
case " $* " in
  *" fetch "*)
    calls=$(cat "$FIXTURE_FETCH_COUNT")
    calls=$((calls+1))
    echo "$calls" > "$FIXTURE_FETCH_COUNT"
    echo "$*" > "$FIXTURE_LAST_REF"
    [ "$FIXTURE_FETCH_OK" = 1 ]; exit $? ;;
  *" rev-parse FETCH_HEAD "*)
    if grep -q 'rdos2-safe-rollback' "$FIXTURE_LAST_REF"; then
      echo "$FIXTURE_ROLLBACK"
    elif [ "$FIXTURE_FIRST_UPGRADE" = 1 ] && grep -q 'refs/heads/main' "$FIXTURE_LAST_REF"; then
      echo 1a3888218bde12875df52f1a035d442f800113d6
    else
      echo "$FIXTURE_NEW"
    fi
    exit 0 ;;
  *" archive "*) cat "$FIXTURE_ARCHIVE"; exit 0 ;;
esac
exit 1
''')
        command('timeout', '''
[ "$1" = '--signal=TERM' ] && [ "$2" = '--kill-after=5s' ] &&
  { [ "$3" = 60s ] || [ "$3" = 10s ]; } || exit 97
shift 3
"$@"
''')
        command('systemctl', '''
case "$1" in
  is-active) cat "$FIXTURE_SERVICE" ;;
  stop) echo inactive > "$FIXTURE_SERVICE" ;;
  start)
    echo active > "$FIXTURE_SERVICE"
    target=$(basename "$(readlink -f "$FIXTURE_CURRENT")")
    if [ "$target" = "$FIXTURE_NEW" ]; then
      "$FIXTURE_PYTHON" -c 'import os,sqlite3; c=sqlite3.connect(os.environ["FIXTURE_DB"]); c.execute("INSERT INTO business VALUES (?)",("new-business-record",)); c.commit(); c.close()'
      if [ "$FIXTURE_EDIT_STARTUP_FILES" = 1 ]; then
        echo 'direct policy edit after gate' > "$FIXTURE_CENTRAL/agent.md"
        echo 'direct memory edit after gate' > "$FIXTURE_CENTRAL/memory.md"
      fi
      if [ "$FIXTURE_PROXY_EDIT" = 1 ]; then echo 'external user edit' > "$FIXTURE_PROXY/baite-rdos.conf"; fi
    elif [ "$target" != "$FIXTURE_ROLLBACK" ] && [ "$FIXTURE_OLD_REWRITES" = 1 ]; then
      echo 'old startup overwrote policy' > "$FIXTURE_CENTRAL/agent.md"
      echo 'old startup overwrote memory' > "$FIXTURE_CENTRAL/memory.md"
    fi
    ;;
  *) exit 1 ;;
esac
''')
        command('curl', '''
case " $* " in
  *" %{http_code} "*) if grep -q 'RDOS maintenance' "$FIXTURE_PROXY/baite-rdos.conf"; then echo 503; else echo 200; fi; exit 0 ;;
  *"/api/runtime/info"*) echo '{"mode":"production"}'; exit 0 ;;
esac
case " $* " in *"/api/health"*)
  if [ "$(cat "$FIXTURE_SERVICE")" != active ]; then exit 22; fi
  target=$(basename "$(readlink -f "$FIXTURE_CURRENT")")
  if [ "$target" = "$FIXTURE_NEW" ] && [ "$FIXTURE_BAD_NEW_HEALTH" = 1 ]; then exit 22; fi
  printf '{"status":"ok","mode":"production","app_release":"%s"}\n' "$target"
  ;;
esac
exit 0
''')
        command('nginx', 'exit 0\n')
        command('runuser', '''
shift
shift
shift
if [ "${FIXTURE_RUNUSER_REQUIRE_ROOT:-0}" = 1 ] && [ "$1" = find ] && [ "$(pwd -P)" != / ]; then
  echo 'find: Failed to restore initial working directory' >&2
  exit 1
fi
if [ "$FIXTURE_UNREADABLE" = 1 ] && [ "$1" = find ]; then echo "$FIXTURE_BASE/unreadable"; exit 0; fi
"$@"
''')
        command('systemd-run', '''
case " $* " in
  *"probe-agent-plan.py"*)
    [ "$(cat "$FIXTURE_SERVICE")" = active ] || exit 80
    [ "$FIXTURE_AGENT_PLAN_OK" = 1 ] || exit 84
    echo accepted > "$FIXTURE_AGENT_PLAN_COUNT"
    exit 0 ;;
  *"probe-rollback-startup.py"*)
    [ "$(cat "$FIXTURE_SERVICE")" = active ] || exit 81
    ! grep -q 'RDOS maintenance' "$FIXTURE_PROXY/baite-rdos.conf" || exit 82
    [ "$FIXTURE_PROBE_FAIL" = 0 ] || exit 83
    probes=$(cat "$FIXTURE_PROBE_COUNT")
    echo "$((probes+1))" > "$FIXTURE_PROBE_COUNT"
    exit 0 ;;
esac
stage=
for arg in "$@"; do
  case "$arg" in "$FIXTURE_BASE"/.rdos2-stage-*) stage="$arg" ;; esac
done
[ -n "$stage" ] || exit 13
[ "$FIXTURE_STAGE_OK" = 1 ] || exit 12
mkdir -p "$stage/.venv/bin"
ln -s "$FIXTURE_PYTHON" "$stage/.venv/bin/python"
printf '#!/bin/sh\nexit 0\n' > "$stage/.venv/bin/uvicorn"
chmod 755 "$stage/.venv/bin/uvicorn"
''')
        command('chown', 'exit 0\n')
        command('sleep', '''
[ "$1" = 2 ] || exit 98
calls=$(cat "$FIXTURE_SLEEP_COUNT")
echo "$((calls+1))" > "$FIXTURE_SLEEP_COUNT"
''')
        (root / 'service-state').write_text('active\n')
        (root / 'fetch-count').write_text('0\n')
        (root / 'last-ref').write_text('none\n')
        (root / 'probe-count').write_text('0\n')
        (root / 'agent-plan-count').write_text('none\n')
        (root / 'sleep-count').write_text('0\n')
        archive = root / 'archive.tar'
        with tarfile.open(archive, 'w') as tar:
            for name in ('cutover/check_release.py', 'server/production_app.py', 'deploy/STARTUP_FILE_PRESERVATION', 'requirements.lock'):
                item = root / name
                item.parent.mkdir(parents=True, exist_ok=True)
                item.write_bytes(fixture_lock if name == 'requirements.lock' else
                    (b'RDOS2_STARTUP_FILES_PRESERVED=1\n' if name.endswith('STARTUP_FILE_PRESERVATION') else b'fixture\n'))
                tar.add(item, arcname=name)
        # Rewrite only an isolated copy. No fixture command uses real paths,
        # credentials, network, service managers or repository state.
        body = SCRIPT.read_text()
        substitutions = {
            "lock_sha = '8a14fbc486afe3a44e7c3a8a0564547b56e8dfa44778a50ab736b3d38af6fcbf'":
                f"lock_sha = '{lock_sha}'",
            '090ffc0c304e6cdc08a6b4400bbe33cb20dcb076485cf318e6291b50dd134438':
                manifest_sha,
            'root.stat().st_uid != 0': 'root.stat().st_uid != os.getuid()',
            'path.stat().st_uid != 0': 'path.stat().st_uid != os.getuid()',
            '/opt/baite-rdos': str(base),
            '/var/lib/baite-rdos': str(dbdir),
            '/etc/baite-rdos': str(config),
            '/etc/systemd/system/baite-rdos.service.d/rdos2.conf': str(override),
            '/www/server/panel/vhost/nginx/proxy/www.yjmt.cn': str(proxy),
            '/www/server/nginx/sbin/nginx': str(fakebin / 'nginx'),
            '/run/lock': str(root / 'run/lock'),
            'export PATH=/www/server/nodejs/v26.1.0/bin:/usr/sbin:/usr/bin:/sbin:/bin':
                'export PATH=' + str(fakebin) + ':/usr/bin:/bin',
            '[[ $(id -u) == 0 &&': '[[ 0 == 0 &&',
            '$(stat -c %u "$build_release/.verified") == 0': '1 == 1',
            '"$release/.venv/bin/python" -B -m cutover.preflight_formal >/dev/null':
                ': # the real preflight is checked separately',
            '"$rollback_release/.venv/bin/python" -B -m cutover.preflight_formal >/dev/null':
                ': # rollback preflight is checked on the real host',
        }
        for original, replacement in substitutions.items():
            self.assertIn(original, body)
            body = body.replace(original, replacement)
        isolated = root / 'release-under-test.sh'
        isolated.write_text(body)
        isolated.chmod(0o755)
        env = {**os.environ, 'FIXTURE_BASE': str(base), 'FIXTURE_FETCH_OK': '1',
            'FIXTURE_STAGE_OK': '0', 'FIXTURE_UNREADABLE': '0',
            'FIXTURE_PROXY_EDIT': '0',
            'FIXTURE_PROBE_FAIL': '0',
            'FIXTURE_AGENT_PLAN_OK': '1',
            'FIXTURE_EDIT_STARTUP_FILES': '0', 'FIXTURE_OLD_REWRITES': '0',
            'FIXTURE_NEW': NEW, 'FIXTURE_CURRENT': str(base / 'current'),
            'FIXTURE_ROLLBACK': SAFE_OLD,
            'FIXTURE_FIRST_UPGRADE': '0',
            'FIXTURE_SERVICE': str(root / 'service-state'), 'FIXTURE_PROXY': str(proxy),
            'FIXTURE_CENTRAL': str(dbdir / 'central'),
            'FIXTURE_BAD_NEW_HEALTH': '0', 'FIXTURE_DB': str(database),
            'FIXTURE_PYTHON': sys.executable, 'FIXTURE_ARCHIVE': str(archive)}
        env['FIXTURE_FETCH_COUNT'] = str(root / 'fetch-count')
        env['FIXTURE_LAST_REF'] = str(root / 'last-ref')
        env['FIXTURE_PROBE_COUNT'] = str(root / 'probe-count')
        env['FIXTURE_AGENT_PLAN_COUNT'] = str(root / 'agent-plan-count')
        env['FIXTURE_SLEEP_COUNT'] = str(root / 'sleep-count')
        return root, isolated, env

    def first_upgrade_fixture(self):
        root, script, env = self.fixture()
        releases = root / 'opt/baite-rdos/releases'
        (releases / OLD).rename(releases / ORIGINAL_OLD)
        current = root / 'opt/baite-rdos/current'
        current.unlink()
        current.symlink_to(releases / ORIGINAL_OLD)
        (releases / ORIGINAL_OLD / 'deploy/STARTUP_FILE_PRESERVATION').unlink()
        fallback = releases / SAFE_OLD
        (fallback / 'deploy').mkdir(parents=True)
        (fallback / 'requirements.lock').write_bytes(b'fixture pinned requirements\n')
        (fallback / 'deploy/STARTUP_FILE_PRESERVATION').write_text('RDOS2_STARTUP_FILES_PRESERVED=1\n')
        (fallback / '.venv/bin').mkdir(parents=True)
        (fallback / '.venv/bin/python').symlink_to(sys.executable)
        uvicorn = fallback / '.venv/bin/uvicorn'
        uvicorn.write_text('#!/bin/sh\nexit 0\n')
        uvicorn.chmod(0o755)
        (fallback / '.verified').write_text(SAFE_OLD + '\n')
        (fallback / 'release.env').write_text('BAITE_APP_RELEASE=' + SAFE_OLD + '\n')
        candidate = releases / NEW
        (candidate / 'deploy/probe-rollback-startup.py').write_text('fixture probe\n')
        (candidate / 'deploy/probe-agent-plan.py').write_text('fixture plan probe\n')
        (root / 'etc/baite-rdos/rdos2.env').write_text('BAITE_LLM_MAX_COMPLETION_TOKENS=393216\n')
        env['FIXTURE_OLD_REWRITES'] = '1'
        env['FIXTURE_FIRST_UPGRADE'] = '1'
        return root, script, env

    def test_first_upgrade_agent_plan_rejection_stops_before_maintenance(self):
        root, script, env = self.first_upgrade_fixture()
        env['FIXTURE_AGENT_PLAN_OK'] = '0'
        result = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, ORIGINAL_OLD)
        self.assertEqual((root / 'service-state').read_text().strip(), 'active')
        self.assertFalse((root / 'var/lib/baite-rdos/cutover-backups').exists())

    def test_first_upgrade_refuses_old_8192_environment_setting(self):
        root, script, env = self.first_upgrade_fixture()
        (root / 'etc/baite-rdos/rdos2.env').write_text('BAITE_LLM_MAX_COMPLETION_TOKENS=8192\n')
        result = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((root / 'agent-plan-count').read_text().strip(), 'accepted')
        self.assertIn('sole explicit RDOS 2.0 environment setting', result.stderr)
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, ORIGINAL_OLD)
        self.assertFalse((root / 'var/lib/baite-rdos/cutover-backups').exists())

    def test_first_upgrade_probe_failure_never_enters_maintenance(self):
        root, script, env = self.first_upgrade_fixture()
        env['FIXTURE_PROBE_FAIL'] = '1'
        result = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('live code ' + ORIGINAL_OLD + ' remains in service', result.stderr)
        self.assertNotIn('rollback code ' + SAFE_OLD + ' healthy', result.stderr)
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, ORIGINAL_OLD)
        self.assertEqual((root / 'service-state').read_text().strip(), 'active')
        self.assertEqual((root / 'www/server/panel/vhost/nginx/proxy/www.yjmt.cn/baite-rdos.conf').read_text(),
                         'original RDOS proxy\n')
        self.assertFalse((root / 'var/lib/baite-rdos/cutover-backups').exists())

    def test_first_upgrade_failure_uses_healthy_safe_old_package_and_latest_data(self):
        root, script, env = self.first_upgrade_fixture()
        env.update(FIXTURE_BAD_NEW_HEALTH='1', FIXTURE_EDIT_STARTUP_FILES='1')
        result = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((root / 'probe-count').read_text().strip(), '2')
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, SAFE_OLD)
        self.assertEqual((root / 'service-state').read_text().strip(), 'active')
        self.assertIn('rollback code ' + SAFE_OLD + ' healthy', result.stderr)
        self.assertEqual((root / 'var/lib/baite-rdos/central/agent.md').read_text(), 'direct policy edit after gate\n')
        self.assertEqual((root / 'var/lib/baite-rdos/central/memory.md').read_text(), 'direct memory edit after gate\n')
        self.assertEqual((root / 'www/server/panel/vhost/nginx/proxy/www.yjmt.cn/baite-rdos.conf').read_text(),
                         'original RDOS proxy\n')
        with sqlite3.connect(root / 'var/lib/baite-rdos/baite.db') as conn:
            self.assertIn(('before',), conn.execute('SELECT payload FROM business').fetchall())
            self.assertIn(('new-business-record',), conn.execute('SELECT payload FROM business').fetchall())

    def test_failed_first_upgrade_can_retry_same_branch_without_merging_main(self):
        root, script, env = self.first_upgrade_fixture()
        env['FIXTURE_BAD_NEW_HEALTH'] = '1'
        failed = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, SAFE_OLD)
        env['FIXTURE_BAD_NEW_HEALTH'] = '0'
        retried = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertEqual((root / 'probe-count').read_text().strip(), '4')
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, NEW)
        self.assertEqual((root / 'service-state').read_text().strip(), 'active')

    def test_failed_staging_does_not_mark_a_release_verified(self):
        root, script, env = self.fixture(verified=False)
        result = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((root / 'opt/baite-rdos/releases' / NEW).exists())
        self.assertFalse(list((root / 'opt/baite-rdos').glob('.rdos2-stage-*')))
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, OLD)
        self.assertEqual((root / 'service-state').read_text().strip(), 'active')

    def test_missing_offline_wheel_refuses_before_maintenance(self):
        root, script, env = self.fixture()
        cache = root / 'opt/baite-rdos/wheelhouse/requirements-8a14fbc486afe3a44e7c3a8a0564547b56e8dfa44778a50ab736b3d38af6fcbf'
        cache.chmod(0o755)
        next(cache.glob('*.whl')).unlink()
        cache.chmod(0o555)
        result = subprocess.run(['bash', str(script), NEW], env=env,
            capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('wheelhouse file set differs', result.stderr)
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, OLD)
        self.assertFalse((root / 'var/lib/baite-rdos/cutover-backups').exists())

    def test_corrupt_offline_wheel_refuses_before_maintenance(self):
        root, script, env = self.fixture()
        cache = root / 'opt/baite-rdos/wheelhouse/requirements-8a14fbc486afe3a44e7c3a8a0564547b56e8dfa44778a50ab736b3d38af6fcbf'
        wheel = next(cache.glob('*.whl'))
        wheel.chmod(0o644)
        wheel.write_bytes(b'altered wheel bytes')
        wheel.chmod(0o444)
        result = subprocess.run(['bash', str(script), NEW], env=env,
            capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('offline wheel missing or hash changed', result.stderr)
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, OLD)
        self.assertFalse((root / 'var/lib/baite-rdos/cutover-backups').exists())

    def test_changed_release_lock_refuses_before_maintenance(self):
        root, script, env = self.fixture()
        candidate_lock = root / 'opt/baite-rdos/releases' / NEW / 'requirements.lock'
        candidate_lock.write_text('unexpected dependency\n')
        result = subprocess.run(['bash', str(script), NEW], env=env,
            capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Trusted RDOS offline wheelhouse missing or changed', result.stderr)
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, OLD)
        self.assertFalse((root / 'var/lib/baite-rdos/cutover-backups').exists())

    def test_failed_main_fetch_never_enters_maintenance_or_switches_code(self):
        root, script, env = self.fixture()
        env['FIXTURE_FETCH_OK'] = '0'
        result = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, OLD)
        self.assertEqual((root / 'service-state').read_text().strip(), 'active')
        self.assertEqual((root / 'fetch-count').read_text().strip(), '3')
        self.assertEqual((root / 'sleep-count').read_text().strip(), '2')
        self.assertEqual((root / 'www/server/panel/vhost/nginx/proxy/www.yjmt.cn/baite-rdos.conf').read_text(),
                         'original RDOS proxy\n')
        self.assertFalse((root / 'var/lib/baite-rdos/cutover-backups').exists())

    def test_missing_runtime_marker_refuses_startup_migration_before_maintenance(self):
        root, script, env = self.fixture()
        database = root / 'var/lib/baite-rdos/baite.db'
        with sqlite3.connect(database) as conn:
            conn.execute("DELETE FROM central_records WHERE kind='state' AND id='central_memory_schema'")
        result = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, OLD)
        self.assertEqual((root / 'service-state').read_text().strip(), 'active')
        self.assertEqual((root / 'var/lib/baite-rdos/central/memory.md').read_text(),
                         'current user memory view\n')
        self.assertFalse((root / 'var/lib/baite-rdos/cutover-backups').exists())

    def test_unfinished_project_upgrade_refuses_before_maintenance(self):
        root, script, env = self.fixture()
        database = root / 'var/lib/baite-rdos/baite.db'
        with sqlite3.connect(database) as conn:
            row = conn.execute("SELECT payload FROM central_records WHERE kind='project' AND id='project_rdos'").fetchone()
            project = json.loads(row[0])
            project['nodes_explicit'] = False
            conn.execute("UPDATE central_records SET payload=? WHERE kind='project' AND id='project_rdos'",
                         (json.dumps(project),))
        result = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, OLD)
        self.assertEqual((root / 'service-state').read_text().strip(), 'active')
        self.assertFalse((root / 'var/lib/baite-rdos/cutover-backups').exists())

    def test_due_real_maintenance_schedule_refuses_before_maintenance(self):
        root, script, env = self.fixture()
        database = root / 'var/lib/baite-rdos/baite.db'
        with sqlite3.connect(database) as conn:
            row = conn.execute("SELECT payload FROM central_records WHERE kind='maintenance_schedule' AND id='hourly_refresh'").fetchone()
            schedule = json.loads(row[0])
            schedule.update(enabled=True, next_due_at='2020-01-01T00:00:00+08:00')
            conn.execute("UPDATE central_records SET payload=? WHERE kind='maintenance_schedule' AND id='hourly_refresh'",
                         (json.dumps(schedule),))
        result = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('due within 30 minutes', result.stderr)
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, OLD)
        self.assertEqual((root / 'service-state').read_text().strip(), 'active')
        self.assertFalse((root / 'var/lib/baite-rdos/cutover-backups').exists())

    def test_external_proxy_edit_during_maintenance_is_not_overwritten(self):
        root, script, env = self.fixture()
        env['FIXTURE_PROXY_EDIT'] = '1'
        result = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, OLD)
        self.assertEqual((root / 'www/server/panel/vhost/nginx/proxy/www.yjmt.cn/baite-rdos.conf').read_text(),
                         'external user edit\n')
        with sqlite3.connect(root / 'var/lib/baite-rdos/baite.db') as conn:
            self.assertIn(('before',), conn.execute('SELECT payload FROM business').fetchall())
            self.assertIn(('new-business-record',), conn.execute('SELECT payload FROM business').fetchall())

    def test_preverified_release_with_writable_or_external_source_is_rejected(self):
        root, script, env = self.fixture()
        candidate = root / 'opt/baite-rdos/releases' / NEW
        source = candidate / 'server/production_app.py'
        source.parent.mkdir()
        source.write_text('changed after verification\n')
        source.chmod(0o666)
        denied = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(denied.returncode, 0, denied.stdout + denied.stderr)
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, OLD)
        self.assertEqual((root / 'service-state').read_text().strip(), 'active')
        source.chmod(0o644)
        source.unlink()
        source.symlink_to(root / 'archive.tar')
        denied_link = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(denied_link.returncode, 0, denied_link.stdout + denied_link.stderr)
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, OLD)

    def test_failed_health_rolls_back_code_and_retains_new_business_data(self):
        root, script, env = self.fixture()
        env['FIXTURE_BAD_NEW_HEALTH'] = '1'
        env['FIXTURE_EDIT_STARTUP_FILES'] = '1'
        result = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, OLD)
        self.assertEqual((root / 'service-state').read_text().strip(), 'active')
        with sqlite3.connect(root / 'var/lib/baite-rdos/baite.db') as conn:
            self.assertIn(('before',), conn.execute('SELECT payload FROM business').fetchall())
            self.assertIn(('new-business-record',), conn.execute('SELECT payload FROM business').fetchall())
            self.assertEqual(json.loads(conn.execute("SELECT payload FROM central_records WHERE kind='central_memory' AND id='user-memory'").fetchone()[0])['content'],
                             'current user memory')
        self.assertEqual((root / 'var/lib/baite-rdos/central/agent.md').read_text(),
                         'direct policy edit after gate\n')
        self.assertEqual((root / 'var/lib/baite-rdos/central/memory.md').read_text(),
                         'direct memory edit after gate\n')
        self.assertEqual((root / 'etc/baite-rdos/rdos2.env').read_text(), 'fixture-only\n')
        self.assertEqual((root / 'www/server/panel/vhost/nginx/proxy/www.yjmt.cn/baite-rdos.conf').read_text(),
                         'original RDOS proxy\n')
        self.assertTrue(list((root / 'var/lib/baite-rdos/cutover-backups').glob('pre-deploy-*/baite.db')))

    def test_verified_release_reaches_public_health_and_restores_proxy(self):
        root, script, env = self.fixture()
        result = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, NEW)
        self.assertEqual((root / 'service-state').read_text().strip(), 'active')
        self.assertEqual((root / 'fetch-count').read_text().strip(), '2')
        self.assertEqual((root / 'www/server/panel/vhost/nginx/proxy/www.yjmt.cn/baite-rdos.conf').read_text(),
                         'original RDOS proxy\n')
        with sqlite3.connect(root / 'var/lib/baite-rdos/baite.db') as conn:
            self.assertIn(('before',), conn.execute('SELECT payload FROM business').fetchall())
            self.assertIn(('new-business-record',), conn.execute('SELECT payload FROM business').fetchall())
            self.assertEqual(json.loads(conn.execute("SELECT payload FROM central_records WHERE kind='central_memory' AND id='user-memory'").fetchone()[0])['content'],
                             'current user memory')
        self.assertEqual((root / 'var/lib/baite-rdos/central/agent.md').read_text(),
                         'current administrator policy\n')
        self.assertEqual((root / 'var/lib/baite-rdos/central/memory.md').read_text(),
                         'current user memory view\n')
        self.assertEqual((root / 'etc/baite-rdos/rdos2.env').read_text(), 'fixture-only\n')
        self.assertTrue(list((root / 'var/lib/baite-rdos/cutover-backups').glob('pre-deploy-*/baite.db')))

    def test_concurrent_file_edit_is_preserved_and_cutover_marked_uncertain(self):
        root, script, env = self.fixture()
        env['FIXTURE_EDIT_STARTUP_FILES'] = '1'
        result = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Inspect the difference before claiming preservation', result.stderr)
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, OLD)
        self.assertEqual((root / 'service-state').read_text().strip(), 'active')
        self.assertEqual((root / 'var/lib/baite-rdos/central/agent.md').read_text(),
                         'direct policy edit after gate\n')
        self.assertEqual((root / 'var/lib/baite-rdos/central/memory.md').read_text(),
                         'direct memory edit after gate\n')
        self.assertEqual((root / 'www/server/panel/vhost/nginx/proxy/www.yjmt.cn/baite-rdos.conf').read_text(),
                         'original RDOS proxy\n')
        evidence = next((root / 'var/lib/baite-rdos/cutover-backups').glob('pre-deploy-*'))
        self.assertEqual((evidence / 'user-files/central/agent.md').read_text(),
                         'current administrator policy\n')

    def test_existing_file_edits_different_from_database_do_not_block_or_get_overwritten(self):
        root, script, env = self.fixture()
        policy = root / 'var/lib/baite-rdos/central/agent.md'
        memory = root / 'var/lib/baite-rdos/central/memory.md'
        policy.write_text('existing direct policy edit\n')
        memory.write_text('existing direct memory edit\n')
        result = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(policy.read_text(), 'existing direct policy edit\n')
        self.assertEqual(memory.read_text(), 'existing direct memory edit\n')

    def test_unknown_unprotected_old_release_refuses_before_maintenance(self):
        root, script, env = self.fixture()
        (root / 'opt/baite-rdos/releases' / OLD / 'deploy/STARTUP_FILE_PRESERVATION').unlink()
        env.update(FIXTURE_BAD_NEW_HEALTH='1', FIXTURE_EDIT_STARTUP_FILES='1', FIXTURE_OLD_REWRITES='1')
        result = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Unprotected previous RDOS release differs', result.stderr)
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, OLD)
        self.assertEqual((root / 'service-state').read_text().strip(), 'active')
        self.assertEqual((root / 'www/server/panel/vhost/nginx/proxy/www.yjmt.cn/baite-rdos.conf').read_text(),
                         'original RDOS proxy\n')
        self.assertEqual((root / 'var/lib/baite-rdos/central/agent.md').read_text(), 'current administrator policy\n')
        self.assertEqual((root / 'var/lib/baite-rdos/central/memory.md').read_text(), 'current user memory view\n')
        with sqlite3.connect(root / 'var/lib/baite-rdos/baite.db') as conn:
            self.assertNotIn(('new-business-record',), conn.execute('SELECT payload FROM business').fetchall())

    def test_fresh_stage_seals_only_after_service_readability_check(self):
        root, script, env = self.fixture(verified=False)
        env['FIXTURE_STAGE_OK'] = '1'
        env['FIXTURE_UNREADABLE'] = '1'
        denied = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(denied.returncode, 0, denied.stdout + denied.stderr)
        self.assertFalse((root / 'opt/baite-rdos/releases' / NEW).exists())
        self.assertFalse(list((root / 'opt/baite-rdos').glob('.rdos2-stage-*')))
        env['FIXTURE_UNREADABLE'] = '0'
        published = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(published.returncode, 0, published.stdout + published.stderr)
        candidate = root / 'opt/baite-rdos/releases' / NEW
        self.assertEqual((candidate / '.verified').read_text().strip(), NEW)
        self.assertEqual((candidate / 'release.env').read_text().strip(), 'BAITE_APP_RELEASE=' + NEW)
        self.assertEqual((root / 'opt/baite-rdos/current').resolve(), candidate)

    def test_service_readability_check_uses_accessible_working_directory(self):
        root, script, env = self.fixture(verified=False)
        env.update(FIXTURE_STAGE_OK='1', FIXTURE_RUNUSER_REQUIRE_ROOT='1')
        admin_private = root / 'admin-private'
        admin_private.mkdir(mode=0o700)
        result = subprocess.run(['bash', str(script), NEW], cwd=admin_private, env=env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, NEW)
