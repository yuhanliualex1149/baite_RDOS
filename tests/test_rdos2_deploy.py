"""No-host regression for the root-owned RDOS 2.0 release protocol."""
from __future__ import annotations

import os
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

    def fixture(self, *, verified=True):
        root = Path(tempfile.mkdtemp(prefix='rdos2-release-fixture-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(root))
        base = root / 'opt/baite-rdos'
        releases = base / 'releases'
        releases.mkdir(parents=True)
        (base / 'repository').mkdir()
        tool = base / 'tools/bin'
        tool.mkdir(parents=True)
        (tool / 'python3.12').symlink_to(sys.executable)
        old = releases / OLD
        old.mkdir()
        candidate = releases / NEW
        if verified:
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
            ):
                conn.execute('INSERT INTO central_records VALUES (?,?,?,?)',
                    ('state', name, json.dumps(payload), 'fixture'))
            conn.execute('INSERT INTO central_records VALUES (?,?,?,?)',
                ('central_memory', 'user-memory', json.dumps({'content': 'current user memory'}), 'fixture'))
        memory = dbdir / 'runtime/central/memory.md'
        memory.parent.mkdir(parents=True)
        memory.write_text('current user memory view\n')
        config = root / 'etc/baite-rdos'
        config.mkdir(parents=True)
        for name in ('rdos.env', 'rdos2.env', 'rdos2-resources.json'):
            (config / name).write_text('fixture-only\n')
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
    [ "$FIXTURE_FETCH_OK" = 1 ]; exit $? ;;
  *" rev-parse FETCH_HEAD "*) echo "$FIXTURE_NEW"; exit 0 ;;
  *" archive "*) cat "$FIXTURE_ARCHIVE"; exit 0 ;;
esac
exit 1
''')
        command('timeout', '''
[ "$1" = '--signal=TERM' ] && [ "$2" = '--kill-after=5s' ] && [ "$3" = 60s ] || exit 97
shift 3
"$@"
''')
        command('systemctl', '''
case "$1" in
  is-active) cat "$FIXTURE_SERVICE" ;;
  stop) echo inactive > "$FIXTURE_SERVICE" ;;
  start)
    echo active > "$FIXTURE_SERVICE"
    if [ "$(basename "$(readlink -f "$FIXTURE_CURRENT")")" = "$FIXTURE_NEW" ]; then
      "$FIXTURE_PYTHON" -c 'import os,sqlite3; c=sqlite3.connect(os.environ["FIXTURE_DB"]); c.execute("INSERT INTO business VALUES (?)",("new-business-record",)); c.commit(); c.close()'
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
if [ "$FIXTURE_UNREADABLE" = 1 ] && [ "$1" = find ]; then echo "$FIXTURE_BASE/unreadable"; exit 0; fi
"$@"
''')
        command('systemd-run', '''
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
        (root / 'sleep-count').write_text('0\n')
        archive = root / 'archive.tar'
        with tarfile.open(archive, 'w') as tar:
            for name in ('cutover/check_release.py', 'server/production_app.py'):
                item = root / name
                item.parent.mkdir(parents=True, exist_ok=True)
                item.write_text('fixture\n')
                tar.add(item, arcname=name)
        # Rewrite only an isolated copy. No fixture command uses real paths,
        # credentials, network, service managers or repository state.
        body = SCRIPT.read_text()
        substitutions = {
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
            '$(stat -c %u "$release/.verified") == 0': '1 == 1',
            '"$release/.venv/bin/python" -B -m cutover.preflight_formal >/dev/null':
                ': # the real preflight is checked separately',
        }
        for original, replacement in substitutions.items():
            self.assertIn(original, body)
            body = body.replace(original, replacement)
        isolated = root / 'release-under-test.sh'
        isolated.write_text(body)
        isolated.chmod(0o755)
        env = {**os.environ, 'FIXTURE_BASE': str(base), 'FIXTURE_FETCH_OK': '1',
            'FIXTURE_STAGE_OK': '0', 'FIXTURE_UNREADABLE': '0',
            'FIXTURE_NEW': NEW, 'FIXTURE_CURRENT': str(base / 'current'),
            'FIXTURE_SERVICE': str(root / 'service-state'), 'FIXTURE_PROXY': str(proxy),
            'FIXTURE_BAD_NEW_HEALTH': '0', 'FIXTURE_DB': str(database),
            'FIXTURE_PYTHON': sys.executable, 'FIXTURE_ARCHIVE': str(archive)}
        env['FIXTURE_FETCH_COUNT'] = str(root / 'fetch-count')
        env['FIXTURE_SLEEP_COUNT'] = str(root / 'sleep-count')
        return root, isolated, env

    def test_failed_staging_does_not_mark_a_release_verified(self):
        root, script, env = self.fixture(verified=False)
        result = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((root / 'opt/baite-rdos/releases' / NEW).exists())
        self.assertFalse(list((root / 'opt/baite-rdos').glob('.rdos2-stage-*')))
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, OLD)
        self.assertEqual((root / 'service-state').read_text().strip(), 'active')

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
        self.assertEqual((root / 'var/lib/baite-rdos/runtime/central/memory.md').read_text(),
                         'current user memory view\n')
        self.assertFalse((root / 'var/lib/baite-rdos/cutover-backups').exists())

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
        result = subprocess.run(['bash', str(script), NEW], env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((root / 'opt/baite-rdos/current').resolve().name, OLD)
        self.assertEqual((root / 'service-state').read_text().strip(), 'active')
        with sqlite3.connect(root / 'var/lib/baite-rdos/baite.db') as conn:
            self.assertIn(('before',), conn.execute('SELECT payload FROM business').fetchall())
            self.assertIn(('new-business-record',), conn.execute('SELECT payload FROM business').fetchall())
            self.assertEqual(json.loads(conn.execute("SELECT payload FROM central_records WHERE kind='central_memory' AND id='user-memory'").fetchone()[0])['content'],
                             'current user memory')
        self.assertEqual((root / 'var/lib/baite-rdos/runtime/central/memory.md').read_text(),
                         'current user memory view\n')
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
        self.assertEqual((root / 'var/lib/baite-rdos/runtime/central/memory.md').read_text(),
                         'current user memory view\n')
        self.assertEqual((root / 'etc/baite-rdos/rdos2.env').read_text(), 'fixture-only\n')
        self.assertTrue(list((root / 'var/lib/baite-rdos/cutover-backups').glob('pre-deploy-*/baite.db')))

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
