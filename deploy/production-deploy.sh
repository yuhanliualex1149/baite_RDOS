#!/usr/bin/env bash
# Installed root-owned entry for routine RDOS 2.0 code releases.
# First-time database/Feishu migration is deliberately not part of this path.
set -Eeuo pipefail
umask 077
[[ $# == 1 ]] || exit 64
if [[ $1 == --protocol ]]; then
  echo RDOS2_DEPLOY_PROTOCOL=2
  exit 0
fi
[[ $(id -u) == 0 && $1 =~ ^[0-9a-f]{40}$ ]] || exit 64
sha=$1
base=/opt/baite-rdos
repo=$base/repository
release=$base/releases/$sha
current=$base/current
original_old_sha=6634ff01e542492cf0173372c1ba043cba68ca5d
first_upgrade_main=1a3888218bde12875df52f1a035d442f800113d6
first_upgrade_ref=refs/heads/codex/rdos2-review-fixes-20261028
safe_old_ref=refs/heads/codex/rdos2-safe-rollback-20261028
safe_old_sha=5272f506ecfe47212cf4a6820aac3eb12ec9a1f7
database=/var/lib/baite-rdos/baite.db
python=$base/tools/bin/python3.12
wheelhouse=$base/wheelhouse/requirements-8a14fbc486afe3a44e7c3a8a0564547b56e8dfa44778a50ab736b3d38af6fcbf
proxy=/www/server/panel/vhost/nginx/proxy/www.yjmt.cn
nginx=/www/server/nginx/sbin/nginx
override=/etc/systemd/system/baite-rdos.service.d/rdos2.conf
export PATH=/www/server/nodejs/v26.1.0/bin:/usr/sbin:/usr/bin:/sbin:/bin
exec 9>/run/lock/baite-rdos-deploy.lock
flock -n 9 || { echo 'Another RDOS release is running' >&2; exit 75; }
[[ -x $python && -d $repo && -L $current && -f $database && -f $override &&
   -f /etc/baite-rdos/rdos.env && -f /etc/baite-rdos/rdos2.env &&
   -f /etc/baite-rdos/rdos2-resources.json &&
   -f $proxy/baite-rdos.conf && -f $proxy/baite-rdos-downloads.conf &&
   -x $nginx ]] || { echo 'Existing RDOS 2.0 prerequisites missing' >&2; exit 1; }
old=$(readlink -f "$current")
[[ $old == "$base/releases/"* && -d $old ]] || exit 1
old_sha=$(basename "$old")
[[ $old_sha =~ ^[0-9a-f]{40}$ ]] || exit 1

local_health() {
  curl -fsS --max-time 5 http://127.0.0.1:8010/api/health |
    "$python" -c 'import json,sys; x=json.load(sys.stdin); assert x.get("status")=="ok" and x.get("mode")=="production" and x.get("app_release")==sys.argv[1]' "$1"
}
public_health() {
  curl -fsS --max-time 10 https://yjmt.cn/baite-rdos/api/health |
    "$python" -c 'import json,sys; x=json.load(sys.stdin); assert x.get("status")=="ok" and x.get("mode")=="production" and x.get("app_release")==sys.argv[1]' "$1"
}
wait_public_health() {
  for attempt in {1..15}; do
    if public_health "$1"; then return 0; fi
    sleep 2
  done
  return 1
}
wait_public_maintenance() {
  for attempt in {1..15}; do
    if [[ $(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 \
      https://yjmt.cn/baite-rdos/api/health || true) == 503 ]]; then return 0; fi
    sleep 2
  done
  return 1
}
wait_local_health() {
  for attempt in {1..12}; do
    if local_health "$1"; then return 0; fi
    sleep 2
  done
  return 1
}
release_sealed() {
  # The marker is meaningful only while the complete tested tree stays under
  # root ownership and cannot be changed by the service/deployment accounts.
  local tree=$1 owner bad link target trusted_python
  owner=$(id -u)
  [[ -d $tree && ! -L $tree ]] || return 1
  bad=$(find "$tree" \( ! -type d ! -type f ! -type l \) -print -quit) || return 1
  [[ -z $bad ]] || return 1
  bad=$(find "$tree" ! -type l \( ! -uid "$owner" -o -perm /022 \) -print -quit) || return 1
  [[ -z $bad ]] || return 1
  bad=$(find "$tree" -type l ! -uid "$owner" -print -quit) || return 1
  [[ -z $bad ]] || return 1
  trusted_python=$(readlink -e "$python") || return 1
  while IFS= read -r -d '' link; do
    target=$(readlink -e "$link") || return 1
    [[ $target == "$tree/"* || $target == "$trusted_python" ]] || return 1
  done < <(find "$tree" -type l -print0)
}
verify_wheelhouse() {
  "$python" - "$wheelhouse" "$1" <<'PYWHEELS'
import hashlib, os, stat, sys
from pathlib import Path
root, lock = map(Path, sys.argv[1:])
lock_sha = '8a14fbc486afe3a44e7c3a8a0564547b56e8dfa44778a50ab736b3d38af6fcbf'
manifest_sha = '090ffc0c304e6cdc08a6b4400bbe33cb20dcb076485cf318e6291b50dd134438'
def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
if not root.is_dir() or root.is_symlink() or root.stat().st_uid != 0 or root.stat().st_mode & 0o222:
    raise SystemExit('Trusted RDOS offline wheelhouse missing or changed')
if not lock.is_file() or lock.is_symlink():
    raise SystemExit('RDOS release requirements.lock is not a regular file')
for path in (root / 'requirements.lock', root / 'SHA256SUMS'):
    if not path.exists():
        raise SystemExit('RDOS offline wheelhouse metadata missing')
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or path.stat().st_uid != 0 or mode & 0o222:
        raise SystemExit('RDOS offline wheelhouse metadata is not sealed')
if (digest(lock) != lock_sha
    or digest(root / 'requirements.lock') != lock_sha
    or digest(root / 'SHA256SUMS') != manifest_sha):
    raise SystemExit('Trusted RDOS offline wheelhouse missing or changed')
entries = {}
for line in (root / 'SHA256SUMS').read_text().splitlines():
    sha, name = line.split('  ', 1)
    if name in entries or '/' in name or not name.endswith('.whl'):
        raise SystemExit('Invalid RDOS wheelhouse manifest')
    entries[name] = sha
if len(entries) != 17 or set(p.name for p in root.iterdir()) != set(entries) | {'SHA256SUMS', 'requirements.lock'}:
    raise SystemExit('RDOS wheelhouse file set differs from reviewed lock')
for name, sha in entries.items():
    path = root / name
    mode = path.lstat().st_mode
    if (not stat.S_ISREG(mode) or path.stat().st_uid != 0 or mode & 0o222
        or digest(path) != sha):
        raise SystemExit('RDOS offline wheel missing or hash changed: ' + name)
PYWHEELS
}
fingerprint_live_state() {
  "$python" - "$database" /etc/baite-rdos/rdos2-resources.json <<'PYFINGERPRINT'
import hashlib, json, sqlite3, sys
from pathlib import Path
resources=json.loads(Path(sys.argv[2]).read_text())
paths=(Path(resources['agent_md']), Path(resources['memory_md']),
       Path(sys.argv[1]).parent/'runtime/phase3_resources.json')
files={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
with sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True) as conn:
    if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok':
        raise SystemExit('Current RDOS SQLite quick_check failed')
    rows=conn.execute("SELECT kind,id,payload FROM central_records WHERE kind='central_memory' OR (kind='state' AND id='central_agent_policy') ORDER BY kind,id").fetchall()
    records=hashlib.sha256(json.dumps(rows,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
print(json.dumps({'files':files,'selected_records_sha256':records,'sqlite_quick_check':'ok'},sort_keys=True))
PYFINGERPRINT
}
fetch_ref_with_retry() {
  local ref=$1 expected=$2 attempt
  for attempt in 1 2 3; do
    if timeout --signal=TERM --kill-after=5s 60s \
      git -C "$repo" fetch --no-tags origin "$ref"; then
      [[ $(git -C "$repo" rev-parse FETCH_HEAD) == "$expected" ]] && return 0
      echo "RDOS source head changed: $ref" >&2
      return 1
    fi
    if (( attempt < 3 )); then sleep 2; fi
  done
  echo "Could not fetch RDOS ref within three bounded attempts: $ref" >&2
  return 1
}
maintenance_gate() {
  printf '%s\n' 'location = /baite-rdos { default_type text/plain; return 503 "RDOS maintenance\n"; }' \
    'location ^~ /baite-rdos/ { default_type text/plain; return 503 "RDOS maintenance\n"; }' > "$proxy/baite-rdos.conf.next"
  printf '%s\n' 'location ^~ /baite-rdos/downloads/ { default_type text/plain; return 503 "RDOS maintenance\n"; }' \
    > "$proxy/baite-rdos-downloads.conf.next"
  cp -p "$proxy/baite-rdos.conf.next" "$backup/baite-rdos.conf.maintenance"
  cp -p "$proxy/baite-rdos-downloads.conf.next" "$backup/baite-rdos-downloads.conf.maintenance"
  proxy_safe_to_restore
  mv -Tf "$proxy/baite-rdos.conf.next" "$proxy/baite-rdos.conf"
  mv -Tf "$proxy/baite-rdos-downloads.conf.next" "$proxy/baite-rdos-downloads.conf"
  timeout --signal=TERM --kill-after=5s 10s "$nginx" -t
  timeout --signal=TERM --kill-after=5s 10s "$nginx" -s reload
}
proxy_safe_to_restore() {
  local name
  for name in baite-rdos.conf baite-rdos-downloads.conf; do
    if ! cmp -s "$proxy/$name" "$backup/$name.maintenance" &&
       ! cmp -s "$proxy/$name" "$backup/$name"; then
      proxy_conflict=1
      echo "RDOS proxy snippet changed outside this release; preserving current file: $name" >&2
      return 1
    fi
  done
}
require_gate_unchanged() {
  if ! cmp -s "$proxy/baite-rdos.conf" "$backup/baite-rdos.conf.maintenance" ||
     ! cmp -s "$proxy/baite-rdos-downloads.conf" "$backup/baite-rdos-downloads.conf.maintenance"; then
    proxy_conflict=1
    echo 'RDOS maintenance proxy changed outside this release; preserving current snippets' >&2
    return 1
  fi
}
[[ $(systemctl is-active baite-rdos) == active ]] || exit 1
local_health "$old_sha"
public_health "$old_sha"
if [[ $old_sha == $sha ]]; then
  public_health "$sha"
  echo "Already serving RDOS 2.0 $sha"
  exit 0
fi
release_ref=refs/heads/main
rollback_release=$old
rollback_sha=$old_sha
if [[ $old_sha == "$safe_old_sha" ]]; then
  # A failed first cutover leaves this reviewed old-business package live.
  # The fix branch remains the authorized source until a successful switch;
  # failure never causes an early merge to main.
  release_ref=$first_upgrade_ref
elif ! release_sealed "$old" ||
   [[ ! -f "$old/deploy/STARTUP_FILE_PRESERVATION" ]] ||
   [[ $(cat "$old/deploy/STARTUP_FILE_PRESERVATION") != RDOS2_STARTUP_FILES_PRESERVED=1 ]]; then
  [[ $old_sha == "$original_old_sha" ]] || {
    echo 'Unprotected previous RDOS release differs from the reviewed first-upgrade base' >&2
    exit 1
  }
  release_ref=$first_upgrade_ref
  rollback_release=$base/releases/$safe_old_sha
  rollback_sha=$safe_old_sha
fi
fetch_ref_with_retry "$release_ref" "$sha"
if [[ $release_ref == "$first_upgrade_ref" ]]; then
  fetch_ref_with_retry refs/heads/main "$first_upgrade_main"
fi

stage=
probe=
backup=
maintenance=0
service_stopped=0
switched=0
proxy_conflict=0
state_conflict=0
recover() {
  code=$1
  trap - ERR INT TERM
  if [[ -n $stage && -d $stage ]]; then rm -rf -- "$stage" || true; fi
  if [[ -n $probe && -d $probe ]]; then rm -rf -- "$probe" || true; fi
  if (( ! maintenance && ! service_stopped && ! switched )); then
    echo "Release refused before maintenance; live code $old_sha remains in service. Latest data retained." >&2
    exit "$code"
  fi
  restored=1
  recovery_sha=$rollback_sha
  if (( ! service_stopped && ! switched )); then recovery_sha=$old_sha; fi
  if (( proxy_conflict )); then restored=0; fi
  if (( service_stopped || switched )); then
    systemctl stop baite-rdos || true
    linked=0
    if ln -sfn "$rollback_release" "$base/current.rollback" &&
       mv -Tf "$base/current.rollback" "$current"; then
      linked=1
    else
      restored=0
      echo 'Could not switch RDOS symlink to safe rollback package' >&2
    fi
    # The first upgrade uses a separately sealed copy of old business code
    # with only startup-file protection. Never restart the unprotected package.
    if (( ! linked )) || ! release_sealed "$rollback_release" ||
       [[ ! -f "$rollback_release/deploy/STARTUP_FILE_PRESERVATION" || -L "$rollback_release/deploy/STARTUP_FILE_PRESERVATION" ]] ||
       [[ $(cat "$rollback_release/deploy/STARTUP_FILE_PRESERVATION") != RDOS2_STARTUP_FILES_PRESERVED=1 ]]; then
      echo 'Safe rollback release proof missing; keeping maintenance and user files intact' >&2
      restored=0
    elif ! systemctl start baite-rdos || ! wait_local_health "$rollback_sha"; then
      restored=0
      systemctl stop baite-rdos || true
    fi
  fi
  if (( maintenance && restored )); then
    if proxy_safe_to_restore; then
      cp -p "$backup/baite-rdos.conf" "$proxy/baite-rdos.conf" || restored=0
      cp -p "$backup/baite-rdos-downloads.conf" "$proxy/baite-rdos-downloads.conf" || restored=0
    else
      restored=0
    fi
    if (( restored )) && { ! timeout --signal=TERM --kill-after=5s 10s "$nginx" -t ||
       ! timeout --signal=TERM --kill-after=5s 10s "$nginx" -s reload ||
       ! wait_public_health "$recovery_sha"; }; then
      restored=0
    fi
  fi
  if (( ! restored && maintenance && ! proxy_conflict )); then maintenance_gate || true; fi
  if (( state_conflict )); then
    echo 'Live RDOS state differed after candidate startup. Original file copies are in the root-only backup; latest files and database were retained. Inspect the difference before claiming preservation.' >&2
  fi
  if (( restored )); then
    echo "Release failed; rollback code $recovery_sha healthy. Latest database retained. Backup: $backup" >&2
  else
    echo "Release failed; recovery health unverified. Maintenance was attempted; inspect code, proxy and data. Current=$(readlink -f "$current" 2>/dev/null || echo unknown), service=$(systemctl is-active baite-rdos 2>/dev/null || echo unknown). Backup: $backup" >&2
  fi
  exit "$code"
}
trap 'recover $?' ERR
trap 'recover 130' INT
trap 'recover 143' TERM

# Build both the candidate and first-upgrade rollback package before
# maintenance. A failed test never leaves a verified release.
build_release() {
  local build_sha=$1 build_release=$base/releases/$1 unreadable
  if [[ -e $build_release ]]; then
    verify_wheelhouse "$build_release/requirements.lock"
    if ! { [[ -f $build_release/.verified && $(cat "$build_release/.verified") == "$build_sha" &&
       $(cat "$build_release/release.env") == "BAITE_APP_RELEASE=$build_sha" &&
       $(stat -c %u "$build_release/.verified") == 0 &&
       -x $build_release/.venv/bin/python && -x $build_release/.venv/bin/uvicorn ]] &&
       release_sealed "$build_release"; }; then
      echo "Existing release lacks a sealed verified build: $build_sha" >&2
      return 1
    fi
  else
    stage=$(mktemp -d "$base/.rdos2-stage-$build_sha.XXXXXX")
    git -C "$repo" archive "$build_sha" | tar -x -C "$stage"
    verify_wheelhouse "$stage/requirements.lock"
    [[ -f $stage/cutover/check_release.py && -f $stage/server/production_app.py &&
       $(cat "$stage/deploy/STARTUP_FILE_PRESERVATION") == RDOS2_STARTUP_FILES_PRESERVED=1 ]] || return 1
    chown -R baite-rdos-deploy:baite-rdos-deploy "$stage"
    chmod -R go-w "$stage"
    systemd-run --quiet --wait --pipe --collect --unit="rdos2-build-$build_sha" \
      -p Type=oneshot -p User=baite-rdos-deploy -p NoNewPrivileges=yes \
      -p KillMode=control-group -p ProtectHome=yes -p ProtectSystem=strict \
      -p "ReadWritePaths=$stage" -p PrivateTmp=yes -- env -i \
      PATH="$PATH" HOME="$stage" PYTHONDONTWRITEBYTECODE=1 \
      PIP_NO_INDEX=1 PIP_FIND_LINKS="$wheelhouse" bash -c '
        set -Eeuo pipefail
        cd "$1"
        "$2" -m venv .venv
        .venv/bin/python -m pip install -r requirements.lock
        .venv/bin/python -m pip check
        .venv/bin/python -B -m cutover.check_release
        .venv/bin/python -B -m compileall -q cutover server lab node_skills
      ' _ "$stage" "$python"
    [[ -z $(find "$stage" ! -type d ! -type f ! -type l -print -quit) ]] || return 1
    chown -R root:root "$stage"
    find "$stage" -type d -exec chmod 0755 {} +
    find "$stage" -type f -perm /111 -exec chmod 0755 {} +
    find "$stage" -type f ! -perm /111 -exec chmod 0644 {} +
    find "$stage" \( -type d -o -type f \) -exec chmod a-s {} +
    unreadable=$(cd / && runuser -u baite-rdos -- find "$stage" -type f ! -readable -print -quit)
    [[ -z $unreadable ]] || return 1
    runuser -u baite-rdos -- test -x "$stage/.venv/bin/uvicorn" || return 1
    printf 'BAITE_APP_RELEASE=%s\n' "$build_sha" > "$stage/release.env"
    printf '%s\n' "$build_sha" > "$stage/.verified.tmp"
    chmod 0644 "$stage/release.env" "$stage/.verified.tmp"
    mv -Tf "$stage/.verified.tmp" "$stage/.verified"
    release_sealed "$stage" || return 1
    mv -T "$stage" "$build_release"
    stage=
  fi
  release_sealed "$build_release" || return 1
  [[ -f "$build_release/deploy/STARTUP_FILE_PRESERVATION" &&
     $(cat "$build_release/deploy/STARTUP_FILE_PRESERVATION") == RDOS2_STARTUP_FILES_PRESERVED=1 ]] || return 1
  unreadable=$(cd / && runuser -u baite-rdos -- find "$build_release" -type f ! -readable -print -quit)
  [[ -z $unreadable ]] || return 1
  runuser -u baite-rdos -- test -x "$build_release/.venv/bin/uvicorn"
}
if [[ $rollback_sha == "$safe_old_sha" ]]; then
  fetch_ref_with_retry "$safe_old_ref" "$safe_old_sha"
  build_release "$safe_old_sha"
fi
build_release "$sha"
if [[ $release_ref == "$first_upgrade_ref" ]]; then
  [[ -f "$release/deploy/probe-agent-plan.py" && ! -L "$release/deploy/probe-agent-plan.py" ]] ||
    { echo 'Candidate Agent Plan acceptance probe missing' >&2; exit 1; }
  # Use only the existing service credentials and a harmless synthetic prompt.
  # A provider rejection or timeout refuses release before maintenance.
  systemd-run --quiet --wait --pipe --collect --unit="rdos2-agent-plan-probe-$sha" \
    -p Type=oneshot -p User=baite-rdos -p NoNewPrivileges=yes \
    -p KillMode=control-group -p ProtectHome=yes -p ProtectSystem=strict \
    -p PrivateTmp=yes -p RuntimeMaxSec=45 \
    -p "WorkingDirectory=$release" \
    -p EnvironmentFile=/etc/baite-rdos/rdos.env \
    -p EnvironmentFile=/etc/baite-rdos/rdos2.env \
    -p Environment=HOME=/var/lib/baite-rdos \
    -p "Environment=PATH=$PATH" -- \
    "$release/.venv/bin/python" -B "$release/deploy/probe-agent-plan.py"
  "$python" - /etc/baite-rdos/rdos2.env <<'PYQUOTA'
import re,sys
from pathlib import Path
lines=Path(sys.argv[1]).read_text().splitlines()
values=[line.split('=',1)[1].strip().strip('"\'') for line in lines
        if re.match(r'^BAITE_LLM_MAX_COMPLETION_TOKENS\s*=',line)]
if values != ['393216']:
    raise SystemExit('Verified Agent Plan quota is not the sole explicit RDOS 2.0 environment setting; update only that field before release')
PYQUOTA
fi
if [[ $rollback_sha == "$safe_old_sha" ]]; then
  # Exercise the actual rollback ASGI lifespan as the service account against
  # a consistent copy. The real database and user files remain untouched.
  [[ -f "$release/deploy/probe-rollback-startup.py" &&
     ! -L "$release/deploy/probe-rollback-startup.py" ]] ||
    { echo 'Candidate rollback startup probe missing' >&2; exit 1; }
  probe=$(mktemp -d /var/lib/baite-rdos/rdos2-rollback-probe.XXXXXX)
  install -d -m 0700 "$probe/central" "$probe/runtime"
  timeout --signal=TERM --kill-after=5s 60s "$python" - "$database" "$probe/baite.db" <<'PYPROBE_BACKUP'
import sqlite3,sys
source=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True)
target=sqlite3.connect(sys.argv[2])
source.backup(target)
assert target.execute('PRAGMA quick_check').fetchone()[0]=='ok'
target.close();source.close()
PYPROBE_BACKUP
  cp -p /var/lib/baite-rdos/central/agent.md /var/lib/baite-rdos/central/memory.md "$probe/central/"
  cp -p /var/lib/baite-rdos/runtime/phase3_resources.json \
    /var/lib/baite-rdos/runtime/peer_identity.json "$probe/runtime/"
  "$python" - /etc/baite-rdos/rdos2-resources.json "$probe" <<'PYPROBE_CONFIG'
import json,sys
from pathlib import Path
root=Path(sys.argv[2]); value=json.loads(Path(sys.argv[1]).read_text())
value['agent_md']=str(root/'central/agent.md')
value['memory_md']=str(root/'central/memory.md')
(root/'rdos2-resources.json').write_text(json.dumps(value))
PYPROBE_CONFIG
  chown -R baite-rdos:baite-rdos "$probe"
  chmod 0700 "$probe" "$probe/central" "$probe/runtime"
  chmod 0600 "$probe/baite.db" "$probe/rdos2-resources.json" \
    "$probe/central/agent.md" "$probe/central/memory.md" "$probe/runtime/"*.json
  # Start the candidate on this copy first, then start the protected old code
  # against any candidate startup writes. Both must preserve the copied user
  # files and selected records. No real service or live data is touched.
  for probe_release in "$release" "$rollback_release"; do
    probe_sha=$(basename "$probe_release")
    systemd-run --quiet --wait --pipe --collect --unit="rdos2-startup-probe-$probe_sha" \
      -p Type=oneshot -p User=baite-rdos -p NoNewPrivileges=yes \
      -p KillMode=control-group -p ProtectHome=yes -p ProtectSystem=strict \
      -p "ReadWritePaths=$probe" -p PrivateTmp=yes \
      -p "WorkingDirectory=$probe_release" \
      -p EnvironmentFile=/etc/baite-rdos/rdos.env \
      -p EnvironmentFile=/etc/baite-rdos/rdos2.env \
      -p "Environment=BAITE_APP_RELEASE=$probe_sha" \
      -p Environment=HOME=/var/lib/baite-rdos \
      -p "Environment=PATH=$PATH" -- \
      "$probe_release/.venv/bin/python" -B "$release/deploy/probe-rollback-startup.py" \
      "$probe_release" "$probe"
  done
  rm -rf -- "$probe"
  probe=
fi

# This checks installed migration markers and credentials; it never migrates.
cd "$release"
"$release/.venv/bin/python" -B -m cutover.preflight_formal >/dev/null
if [[ $rollback_sha == "$safe_old_sha" ]]; then
  cd "$rollback_release"
  "$rollback_release/.venv/bin/python" -B -m cutover.preflight_formal >/dev/null
  cd "$release"
fi
# A normal code release must never trigger startup schema/memory migration or
# initialize missing administrator policy over a user's current settings.
"$python" - "$database" /etc/baite-rdos/rdos2-resources.json <<'PYSCHEMA'
import json, sqlite3, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
resources = json.loads(Path(sys.argv[2]).read_text())
with sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True) as conn:
    schema = conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
    def state(name):
        row = conn.execute("SELECT payload FROM central_records WHERE kind='state' AND id=?", (name,)).fetchone()
        return json.loads(row[0]) if row else None
    phase1 = state('phase1_schema')
    memory = state('central_memory_schema')
    policy = state('central_agent_policy')
    runtime = state('runtime')
    if (not schema or schema[0] != '8' or not isinstance(phase1, dict)
        or phase1.get('version') != 2 or not isinstance(memory, dict)
        or memory.get('version') != 2 or not isinstance(policy, dict)
        or not isinstance(policy.get('content'), str) or not policy['content']):
        raise SystemExit('Existing RDOS schema or memory policy requires migration; release refused before maintenance')
    if (not isinstance(runtime, dict) or type(runtime.get('paused')) is not bool
        or runtime.get('phase') != 3 or runtime.get('scheduler_enabled') is not False
        or runtime.get('model_mode') != 'agent_plan' or runtime.get('backend') != 'feishu_production'):
        raise SystemExit('Current RDOS runtime settings would be rewritten at startup; release refused')
    agent = Path(resources['agent_md'])
    memory_view = Path(resources['memory_md'])
    peer_keys = Path(sys.argv[1]).parent / 'runtime/peer_identity.json'
    registry_view = Path(sys.argv[1]).parent / 'runtime/phase3_resources.json'
    if (not agent.is_file() or not memory_view.is_file()
        or not peer_keys.is_file() or peer_keys.is_symlink()
        or peer_keys.stat().st_nlink != 1 or not registry_view.is_file()
        or registry_view.is_symlink() or registry_view.stat().st_nlink != 1):
        raise SystemExit('Current RDOS memory or identity files need reconciliation; release refused')
    rows = conn.execute("SELECT id,payload FROM central_records WHERE kind='project'").fetchall()
    projects = {key: json.loads(payload) for key, payload in rows}
    default_id = resources['default_project_id']
    default = projects.get(default_id) or {}
    finance = projects.get(resources['finance_project_id']) or {}
    legacy = projects.get('prj_0d9b2a0ecec486cdf8')
    human_url = 'https://scn33386s7ui.feishu.cn/drive/folder/' + resources['human_folder']
    if (default.get('rdos_project_schema') != 1 or default.get('nodes_explicit') is not True
        or not default.get('owner_id') or default.get('folder_id') != resources['human_folder']
        or default.get('folder_url') != human_url or default.get('source_environment') != 'feishu_production'
        or finance.get('rdos_project_schema') != 1 or not finance.get('owner_id')
        or finance.get('folder_id') != resources['finance_folder']
        or (legacy and legacy.get('merged_into') != default_id)
        or any(p.get('rdos_project_schema') == 1 and not p.get('owner_id') for p in projects.values())):
        raise SystemExit('Existing RDOS projects would require startup migration; release refused')
    rows = conn.execute("SELECT kind,id,payload FROM phase3_local WHERE kind IN ('node','state')").fetchall()
    local = {(kind, key): json.loads(payload) for kind, key, payload in rows}
    if (not all(isinstance(local.get(('state', name)), dict) for name in ('phase3_schema', 'business_workflow'))
        or local[('state', 'phase3_schema')].get('version') != 1
        or any((local.get(('node', node)) or {}).get('member_id') != member
               for node, member in resources['node_members'].items())
        or any(not conn.execute("SELECT 1 FROM central_records WHERE kind='maintenance_schedule' AND id=?", (name,)).fetchone()
               for name in ('hourly_refresh', 'weekly_inspection'))):
        raise SystemExit('Existing RDOS node or maintenance state needs initialization; release refused')
    # Nginx maintenance blocks HTTP, not the in-process hourly/weekly jobs.
    # Do not cut over near a due job or while a prior job may still write.
    horizon=datetime.now(timezone.utc)+timedelta(minutes=30)
    for name in ('hourly_refresh','weekly_inspection'):
        row=conn.execute("SELECT payload FROM central_records WHERE kind='maintenance_schedule' AND id=?",(name,)).fetchone()
        schedule=json.loads(row[0])
        if schedule.get('enabled'):
            try:
                due=datetime.fromisoformat(schedule['next_due_at'])
                if due.tzinfo is None:
                    raise ValueError('missing zone')
            except (KeyError,TypeError,ValueError):
                raise SystemExit('Enabled RDOS maintenance has an invalid due time; release refused')
            if due <= horizon:
                raise SystemExit('Enabled RDOS maintenance is due within 30 minutes; wait for a quiet release window')
    for (payload,) in conn.execute("SELECT payload FROM central_records WHERE kind='maintenance_run'"):
        if json.loads(payload).get('status') in ('queued','running','draining'):
            raise SystemExit('RDOS maintenance run may still be active; release refused')
PYSCHEMA
fetch_ref_with_retry "$release_ref" "$sha"
if [[ $release_ref == "$first_upgrade_ref" ]]; then
  fetch_ref_with_retry refs/heads/main "$first_upgrade_main"
fi
if [[ $rollback_sha == "$safe_old_sha" ]]; then
  fetch_ref_with_retry "$safe_old_ref" "$safe_old_sha"
fi
[[ $(readlink -f "$current") == "$old" ]] ||
  { echo 'Active release changed while building' >&2; exit 1; }
local_health "$old_sha"
install -d -m 0700 /var/lib/baite-rdos/cutover-backups
backup=$(mktemp -d "/var/lib/baite-rdos/cutover-backups/pre-deploy-$sha.XXXXXX")
chmod 0700 "$backup"
cp -p /etc/baite-rdos/rdos.env /etc/baite-rdos/rdos2.env \
  /etc/baite-rdos/rdos2-resources.json "$backup/"
cp -p "$proxy/baite-rdos.conf" "$proxy/baite-rdos-downloads.conf" "$backup/"
maintenance=1
maintenance_started_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
maintenance_deadline_epoch=$(($(date +%s)+600))
echo "RDOS maintenance starts $maintenance_started_utc; candidate health deadline $(date -u -d '@'"$maintenance_deadline_epoch" +%Y-%m-%dT%H:%M:%SZ). Recovery may add bounded service drain/start time."
within_window() {
  (( $(date +%s) <= maintenance_deadline_epoch )) || {
    echo 'RDOS candidate maintenance deadline exceeded; starting safe recovery' >&2
    return 1
  }
}
maintenance_gate
if ! wait_public_maintenance; then
  echo 'Public maintenance gate did not take effect' >&2
  recover 1
fi
service_stopped=1
systemctl stop baite-rdos
within_window
timeout --signal=TERM --kill-after=5s 60s "$python" - "$database" "$backup/baite.db" <<'PYBACKUP'
import sqlite3,sys
source=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True)
target=sqlite3.connect(sys.argv[2])
source.backup(target)
assert target.execute('PRAGMA quick_check').fetchone()[0]=='ok'
target.close();source.close()
PYBACKUP
chmod 0600 "$backup/baite.db"
install -d -m 0700 "$backup/user-files" "$backup/user-files/central" "$backup/user-files/runtime"
cp -p /var/lib/baite-rdos/central/agent.md /var/lib/baite-rdos/central/memory.md "$backup/user-files/central/"
cp -p /var/lib/baite-rdos/runtime/phase3_resources.json "$backup/user-files/runtime/"
chmod -R go-rwx "$backup/user-files"
within_window
state_before=$(fingerprint_live_state)
printf '%s\n' "$state_before" > "$backup/state-before.json"
ln -sfn "$rollback_release" "$base/previous.next"
mv -Tf "$base/previous.next" "$base/previous"
ln -sfn "$release" "$base/current.next"
mv -Tf "$base/current.next" "$current"
switched=1
systemctl start baite-rdos
wait_local_health "$sha"
within_window
[[ $(systemctl is-active baite-rdos) == active ]] || recover 1
curl -fsS --max-time 10 http://127.0.0.1:8010/ >/dev/null
curl -fsS --max-time 10 http://127.0.0.1:8010/static/central.js >/dev/null
curl -fsS --max-time 10 http://127.0.0.1:8010/api/runtime/info |
  "$python" -c 'import json,sys; assert json.load(sys.stdin).get("mode")=="production"'
if ! state_after=$(fingerprint_live_state) || [[ $state_after != "$state_before" ]]; then
  state_conflict=1
  echo 'Live RDOS files or selected records changed during candidate startup; refusing public cutover' >&2
  false
fi
require_gate_unchanged
cp -p "$backup/baite-rdos.conf" "$proxy/baite-rdos.conf"
cp -p "$backup/baite-rdos-downloads.conf" "$proxy/baite-rdos-downloads.conf"
timeout --signal=TERM --kill-after=5s 10s "$nginx" -t
timeout --signal=TERM --kill-after=5s 10s "$nginx" -s reload
wait_public_health "$sha"
within_window
curl -fsS --max-time 10 https://yjmt.cn/baite-rdos/ >/dev/null
curl -fsS --max-time 10 https://yjmt.cn/baite-rdos/static/central.js >/dev/null
maintenance=0
service_stopped=0
trap - ERR INT TERM
echo "Deployed RDOS 2.0 $sha; latest database retained. Backup: $backup"
