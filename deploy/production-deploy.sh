#!/usr/bin/env bash
# Installed, root-owned server entry point. Never run a downloaded copy as root.
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
[[ $(id -u) == 0 && $# == 1 && $1 =~ ^[0-9a-f]{40}$ ]] || exit 64
sha=$1
base=/opt/baite-rdos
repo=$base/repository
release=$base/releases/$sha
current=$base/current
db=/var/lib/baite-rdos/baite.db
python=$base/tools/bin/python3.12
build_user=baite-rdos-deploy
exec 9>/run/lock/baite-rdos-deploy.lock
flock -n 9 || { echo 'Another RDOS deployment is running'; exit 75; }
[[ -x $python && -f /etc/baite-rdos/rdos.env && -L $current ]] || { echo 'Production prerequisites missing'; exit 1; }
if [[ ! -f $db ]]; then
  "$python" - /etc/baite-rdos/rdos.env <<'PYCONF'
import sys
values={}
for line in open(sys.argv[1]):
    if '=' in line and not line.lstrip().startswith('#'):
        key,value=line.strip().split('=',1); values[key]=value.strip().strip('\"\'')
if not values.get('BAITE_ADMIN_USERNAME') or len(values.get('BAITE_ADMIN_PASSWORD','')) < 10:
    raise SystemExit('Empty database requires a configured administrator username and password of at least 10 characters. No deployment performed.')
PYCONF
fi
old=$(readlink -f "$current")
[[ $old == "$base/releases/"* && -d $old ]] || exit 1
if [[ $old == "$release" ]]; then
  curl -fsS --max-time 5 http://127.0.0.1:8010/api/health | "$python" -c 'import json,sys; x=json.load(sys.stdin); assert x.get("status")=="ok" and x.get("app_release")==sys.argv[1]' "$sha"
  echo "Already active and healthy: $sha"; exit 0
fi
fetch_main_with_retry() {
  local attempt
  for attempt in 1 2 3; do
    if timeout --signal=TERM --kill-after=5s 60s git -C "$repo" fetch --no-tags origin main; then
      return 0
    fi
    if (( attempt < 3 )); then
      echo "GitHub fetch failed (attempt $attempt/3); retrying..." >&2
      sleep 2
    fi
  done
  echo 'Failed to fetch main after 3 attempts; production unchanged' >&2
  return 1
}
fetch_main_with_retry
[[ $(git -C "$repo" rev-parse refs/remotes/origin/main) == "$sha" ]] || { echo 'Only the current main commit may deploy'; exit 1; }
if [[ -e $release ]]; then
  [[ $(readlink -f "$base/previous" || true) != "$release" ]] || { echo 'Target is the retained rollback release; inspect before rebuilding'; exit 1; }
  failed=$(mktemp -d "$base/incomplete-release.XXXXXX")
  mv "$release" "$failed/tree"
fi
install -d -m 0755 "$release"
git -C "$repo" archive "$sha" | tar -x -C "$release"
chown -R "$build_user:$build_user" "$release"
# Build and tests cannot read production credentials or the production database.
systemd-run --quiet --wait --pipe --collect --unit="rdos-build-$sha" -p Type=oneshot -p User="$build_user" -p NoNewPrivileges=yes -p KillMode=control-group -p ProtectHome=yes -p ProtectSystem=strict -p "ReadWritePaths=$release" -p PrivateTmp=yes -- env -i PATH="$PATH" HOME="$release" BAITE_DISABLE_EXTERNAL_SYNC=true BAITE_DB_PATH="$release/data/ci.db" PYTHONDONTWRITEBYTECODE=1 bash -c '
  set -euo pipefail
  cd "$1"
  "$2" -m venv .venv
  .venv/bin/python -m pip install -r requirements.lock
  .venv/bin/python -m pip check
  .venv/bin/python -m unittest discover -v
  .venv/bin/python tests/e2e_demo.py
' _ "$release" "$python"
# systemd has reaped the build control group. Sanitize behind a root-only parent.
quarantine=$(mktemp -d "$base/build-result.XXXXXX")
mv "$release" "$quarantine/tree"
sealed=$(mktemp -d "$base/sealed-release.XXXXXX")
mkdir "$sealed/tree"
cp -a --no-preserve=ownership "$quarantine/tree/." "$sealed/tree/"
if find "$sealed/tree" ! -type f ! -type d ! -type l -print -quit | grep -q .; then
  echo 'Unsupported build artifact type'; exit 1
fi
find "$sealed/tree" -type d -exec chmod 0755 {} +
find "$sealed/tree" -type f -perm /111 -exec chmod 0755 {} +
find "$sealed/tree" -type f ! -perm /111 -exec chmod 0644 {} +
find "$sealed/tree" \( -type f -o -type d \) -exec chmod a-s {} +
meta=$(mktemp "$sealed/release-env.XXXXXX")
printf 'BAITE_APP_RELEASE=%s\n' "$sha" > "$meta"
chmod 0644 "$meta"
mv -Tf "$meta" "$sealed/tree/release.env"
meta=$(mktemp "$sealed/verified.XXXXXX")
chmod 0644 "$meta"
mv -Tf "$meta" "$sealed/tree/.verified"
mv "$sealed/tree" "$release"
# Recheck main after the build, before any service interruption.
fetch_main_with_retry
[[ $(git -C "$repo" rev-parse refs/remotes/origin/main) == "$sha" ]] || { echo 'Superseded by a newer main commit; production unchanged'; exit 1; }
backup=/var/lib/baite-rdos/backups/pre-deploy-$(date -u +%Y%m%dT%H%M%SZ)-$sha.sqlite3
database_fingerprint() {
  "$python" - "$db" <<'PY'
import hashlib, sqlite3, sys
with sqlite3.connect('file:'+sys.argv[1]+'?mode=ro', uri=True) as c:
    digest=hashlib.sha256()
    for statement in c.iterdump():
        digest.update(statement.encode()); digest.update(b'\n')
    digest.update(str(c.execute('PRAGMA user_version').fetchone()[0]).encode())
    print(digest.hexdigest())
PY
}
health() {
  curl -fsS --max-time 3 http://127.0.0.1:8010/api/health |
    "$python" -c 'import json,sys; x=json.load(sys.stdin); assert x.get("status")=="ok" and x.get("app_release")==sys.argv[1]' "$1"
}
proxy=/www/server/panel/vhost/nginx/proxy/www.yjmt.cn
saved_proxy=/opt/baite-rdos/offline-proxy-backup
nginx=/www/server/nginx/sbin/nginx
public_enabled=0
make_public_offline() {
  printf '%s\n' 'location = /baite-rdos { default_type text/plain; return 503 "RDOS is offline. New version pending.\n"; }' 'location ^~ /baite-rdos/ { default_type text/plain; return 503 "RDOS is offline. New version pending.\n"; }' > "$proxy/baite-rdos.conf"
  printf '%s\n' 'location ^~ /baite-rdos/downloads/ { default_type text/plain; return 503 "RDOS is offline. New version pending.\n"; }' > "$proxy/baite-rdos-downloads.conf"
  "$nginx" -t && "$nginx" -s reload
}
had_database=0
old_fingerprint=none
if [[ -f $db ]]; then had_database=1; fi
stopped=0
switched=0
recover() {
  result=${1:-1}
  [[ $result != 0 ]] || result=1
  trap - ERR INT TERM
  if (( stopped )); then
    systemctl stop baite-rdos || true
    if (( had_database )) && { (( ! switched )) || [[ $(database_fingerprint) == "$old_fingerprint" ]]; }; then
      ln -sfn "$old" "$base/current.rollback"
      mv -Tf "$base/current.rollback" "$current"
      systemctl start baite-rdos
      rollback_ok=0
      for attempt in {1..15}; do
        if health "${old##*/}"; then rollback_ok=1; break; fi
        sleep 2
      done
      if (( rollback_ok )); then
        echo 'Deployment failed; previous release restored and healthy; database unchanged.' >&2
      else
        systemctl stop baite-rdos || true
        make_public_offline || true
        echo 'Rollback health check failed; service stopped, database retained.' >&2
      fi
    else
      make_public_offline || true
      echo "Database contents changed: service stopped for safe recovery. Database retained; pre-deploy backup: $backup" >&2
    fi
  fi
  exit "${result:-1}"
}
trap 'recover $?' ERR
trap 'recover 130' INT
trap 'recover 143' TERM
systemctl stop baite-rdos
stopped=1
if (( had_database )); then old_fingerprint=$(database_fingerprint); fi
# SQLite backup only; no second application process or external integration is started.
if (( had_database )); then
"$python" - "$db" "$backup" <<'PY'
import sqlite3, sys
src=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro', uri=True)
dst=sqlite3.connect(sys.argv[2]); src.backup(dst)
assert dst.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
dst.close(); src.close()
PY
chmod 0600 "$backup"
fi
ln -sfn "$old" "$base/previous.next"
mv -Tf "$base/previous.next" "$base/previous"
ln -sfn "$release" "$base/current.next"
mv -Tf "$base/current.next" "$current"
switched=1
systemctl start baite-rdos
ok=0
for attempt in {1..30}; do
  if health "$sha"; then ok=1; break; fi
  sleep 2
done
[[ $ok == 1 ]]
# Restore only the two existing RDOS proxy snippets; unrelated sites are untouched.
if [[ -f $saved_proxy/baite-rdos.conf && -f $saved_proxy/baite-rdos-downloads.conf ]]; then
  cp -p "$saved_proxy/baite-rdos.conf" "$proxy/baite-rdos.conf"
  cp -p "$saved_proxy/baite-rdos-downloads.conf" "$proxy/baite-rdos-downloads.conf"
  "$nginx" -t
  "$nginx" -s reload
  public_enabled=1
fi
curl -fsS --retry 3 --retry-delay 2 --max-time 10 https://yjmt.cn/baite-rdos/api/health |
  "$python" -c 'import json,sys; x=json.load(sys.stdin); assert x.get("status")=="ok" and x.get("app_release")==sys.argv[1]' "$sha"
systemctl enable baite-rdos baite-rdos-backup.timer
systemctl start baite-rdos-backup.timer
stopped=0
trap - ERR INT TERM
echo "Deployed RDOS commit $sha; database and production configuration retained."
