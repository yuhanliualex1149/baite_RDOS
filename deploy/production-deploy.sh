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
database=/var/lib/baite-rdos/baite.db
python=$base/tools/bin/python3.12
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
wait_local_health() {
  for attempt in {1..30}; do
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
fetch_main_with_retry() {
  local attempt
  for attempt in 1 2 3; do
    if timeout --signal=TERM --kill-after=5s 60s \
      git -C "$repo" fetch --no-tags origin main; then
      return 0
    fi
    if (( attempt < 3 )); then sleep 2; fi
  done
  echo 'Could not fetch current RDOS main within three bounded attempts' >&2
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
  "$nginx" -t
  "$nginx" -s reload
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
fetch_main_with_retry
[[ $(git -C "$repo" rev-parse FETCH_HEAD) == "$sha" ]] ||
  { echo 'Only exact current main may deploy' >&2; exit 1; }

stage=
backup=
maintenance=0
service_stopped=0
switched=0
proxy_conflict=0
recover() {
  code=$1
  trap - ERR INT TERM
  if [[ -n $stage && -d $stage ]]; then rm -rf -- "$stage"; fi
  restored=1
  if (( proxy_conflict )); then restored=0; fi
  if (( service_stopped || switched )); then
    systemctl stop baite-rdos || true
    ln -sfn "$old" "$base/current.rollback"
    mv -Tf "$base/current.rollback" "$current"
    # An older release rewrites agent.md and memory.md during lifespan. Do not
    # start it after a failed first upgrade: data preservation outranks uptime.
    if ! release_sealed "$old" ||
       [[ ! -f "$old/deploy/STARTUP_FILE_PRESERVATION" || -L "$old/deploy/STARTUP_FILE_PRESERVATION" ]] ||
       [[ $(cat "$old/deploy/STARTUP_FILE_PRESERVATION") != RDOS2_STARTUP_FILES_PRESERVED=1 ]]; then
      echo 'Previous code has no startup-file preservation proof; keeping maintenance and user files intact' >&2
      restored=0
    elif ! systemctl start baite-rdos || ! wait_local_health "$old_sha"; then
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
    if (( restored )) && { ! "$nginx" -t || ! "$nginx" -s reload || ! public_health "$old_sha"; }; then
      restored=0
    fi
  fi
  if (( ! restored && maintenance && ! proxy_conflict )); then maintenance_gate || true; fi
  if (( restored )); then
    echo "Release failed; old code healthy. Latest database retained. Backup: $backup" >&2
  else
    echo "Release failed; recovery health unverified. Maintenance was attempted; inspect code, proxy and data. Backup: $backup" >&2
  fi
  exit "$code"
}
trap 'recover $?' ERR
trap 'recover 130' INT
trap 'recover 143' TERM

# Build in a fresh private directory. A failed test never leaves .verified.
if [[ -e $release ]]; then
  if ! { [[ -f $release/.verified && $(cat "$release/.verified") == "$sha" &&
     $(cat "$release/release.env") == "BAITE_APP_RELEASE=$sha" &&
     $(stat -c %u "$release/.verified") == 0 &&
     -x $release/.venv/bin/python && -x $release/.venv/bin/uvicorn ]] &&
     release_sealed "$release"; }; then
    echo 'Existing target lacks a sealed verified build' >&2
    exit 1
  fi
else
  stage=$(mktemp -d "$base/.rdos2-stage-$sha.XXXXXX")
  git -C "$repo" archive "$sha" | tar -x -C "$stage"
  [[ -f $stage/cutover/check_release.py && -f $stage/server/production_app.py &&
     $(cat "$stage/deploy/STARTUP_FILE_PRESERVATION") == RDOS2_STARTUP_FILES_PRESERVED=1 ]] || exit 1
  chown -R baite-rdos-deploy:baite-rdos-deploy "$stage"
  chmod -R go-w "$stage"
  systemd-run --quiet --wait --pipe --collect --unit="rdos2-build-$sha" \
    -p Type=oneshot -p User=baite-rdos-deploy -p NoNewPrivileges=yes \
    -p KillMode=control-group -p ProtectHome=yes -p ProtectSystem=strict \
    -p "ReadWritePaths=$stage" -p PrivateTmp=yes -- env -i \
    PATH="$PATH" HOME="$stage" PYTHONDONTWRITEBYTECODE=1 bash -c '
      set -Eeuo pipefail
      cd "$1"
      "$2" -m venv .venv
      .venv/bin/python -m pip install -r requirements.lock
      .venv/bin/python -m pip check
      .venv/bin/python -B -m cutover.check_release
      .venv/bin/python -B -m compileall -q cutover server lab node_skills
    ' _ "$stage" "$python"
  [[ -z $(find "$stage" ! -type d ! -type f ! -type l -print -quit) ]] || exit 1
  chown -R root:root "$stage"
  find "$stage" -type d -exec chmod 0755 {} +
  find "$stage" -type f -perm /111 -exec chmod 0755 {} +
  find "$stage" -type f ! -perm /111 -exec chmod 0644 {} +
  find "$stage" \( -type d -o -type f \) -exec chmod a-s {} +
  unreadable=$(runuser -u baite-rdos -- find "$stage" -type f ! -readable -print -quit)
  [[ -z $unreadable ]]
  runuser -u baite-rdos -- test -x "$stage/.venv/bin/uvicorn"
  printf 'BAITE_APP_RELEASE=%s\n' "$sha" > "$stage/release.env"
  printf '%s\n' "$sha" > "$stage/.verified.tmp"
  chmod 0644 "$stage/release.env" "$stage/.verified.tmp"
  mv -Tf "$stage/.verified.tmp" "$stage/.verified"
  release_sealed "$stage"
  mv -T "$stage" "$release"
  stage=
fi
release_sealed "$release"
[[ $(cat "$release/deploy/STARTUP_FILE_PRESERVATION") == RDOS2_STARTUP_FILES_PRESERVED=1 ]] ||
  { echo 'Candidate does not preserve existing startup files' >&2; exit 1; }
unreadable=$(runuser -u baite-rdos -- find "$release" -type f ! -readable -print -quit)
[[ -z $unreadable ]]
runuser -u baite-rdos -- test -x "$release/.venv/bin/uvicorn"

# This checks installed migration markers and credentials; it never migrates.
cd "$release"
"$release/.venv/bin/python" -B -m cutover.preflight_formal >/dev/null
# A normal code release must never trigger startup schema/memory migration or
# initialize missing administrator policy over a user's current settings.
"$python" - "$database" /etc/baite-rdos/rdos2-resources.json <<'PYSCHEMA'
import json, sqlite3, sys
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
PYSCHEMA
fetch_main_with_retry
[[ $(git -C "$repo" rev-parse FETCH_HEAD) == "$sha" &&
   $(readlink -f "$current") == "$old" ]] ||
  { echo 'Main or active release changed while building' >&2; exit 1; }
local_health "$old_sha"
install -d -m 0700 /var/lib/baite-rdos/cutover-backups
backup=$(mktemp -d "/var/lib/baite-rdos/cutover-backups/pre-deploy-$sha.XXXXXX")
chmod 0700 "$backup"
cp -p /etc/baite-rdos/rdos.env /etc/baite-rdos/rdos2.env \
  /etc/baite-rdos/rdos2-resources.json "$backup/"
cp -p "$proxy/baite-rdos.conf" "$proxy/baite-rdos-downloads.conf" "$backup/"
maintenance=1
maintenance_gate
[[ $(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 \
  https://yjmt.cn/baite-rdos/api/health) == 503 ]] ||
  { echo 'Public maintenance gate did not take effect' >&2; exit 1; }
service_stopped=1
systemctl stop baite-rdos
"$python" - "$database" "$backup/baite.db" <<'PYBACKUP'
import sqlite3,sys
source=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True)
target=sqlite3.connect(sys.argv[2])
source.backup(target)
assert target.execute('PRAGMA quick_check').fetchone()[0]=='ok'
target.close();source.close()
PYBACKUP
chmod 0600 "$backup/baite.db"
ln -sfn "$old" "$base/previous.next"
mv -Tf "$base/previous.next" "$base/previous"
ln -sfn "$release" "$base/current.next"
mv -Tf "$base/current.next" "$current"
switched=1
systemctl start baite-rdos
wait_local_health "$sha"
[[ $(systemctl is-active baite-rdos) == active ]] || exit 1
curl -fsS --max-time 10 http://127.0.0.1:8010/ >/dev/null
curl -fsS --max-time 10 http://127.0.0.1:8010/static/central.js >/dev/null
curl -fsS --max-time 10 http://127.0.0.1:8010/api/runtime/info |
  "$python" -c 'import json,sys; assert json.load(sys.stdin).get("mode")=="production"'
require_gate_unchanged
cp -p "$backup/baite-rdos.conf" "$proxy/baite-rdos.conf"
cp -p "$backup/baite-rdos-downloads.conf" "$proxy/baite-rdos-downloads.conf"
"$nginx" -t
"$nginx" -s reload
public_health "$sha"
curl -fsS --max-time 10 https://yjmt.cn/baite-rdos/ >/dev/null
curl -fsS --max-time 10 https://yjmt.cn/baite-rdos/static/central.js >/dev/null
maintenance=0
service_stopped=0
trap - ERR INT TERM
echo "Deployed RDOS 2.0 $sha; latest database retained. Backup: $backup"
