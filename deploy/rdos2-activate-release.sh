#!/usr/bin/env bash
# Switch one staged, migrated RDOS 2.0 release into the existing service slot.
# Never restores an old database over potentially newer business records.
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
[[ $(id -u) == 0 && $# == 1 && $1 =~ ^[0-9a-f]{40}$ ]] || exit 64
sha=$1
base=/opt/baite-rdos
release=$base/releases/$sha
current=$base/current
database=/var/lib/baite-rdos/baite.db
python=$base/tools/bin/python3.12
override=/etc/systemd/system/baite-rdos.service.d/rdos2.conf
envfile=/etc/baite-rdos/rdos2.env
manifest=/etc/baite-rdos/rdos2-resources.json
exec 9>/run/lock/baite-rdos-deploy.lock
flock -n 9 || { echo 'RDOS release operation already running' >&2; exit 75; }
[[ -f $release/.verified && $(cat "$release/.verified") == "$sha" && -x $release/.venv/bin/python ]] || {
  echo 'Requested commit has not passed isolated Linux staging' >&2; exit 1;
}
[[ -f $database && -f $envfile && -f $manifest && -L $current && ! -e $override && ! -L $override ]] || {
  echo 'Formal migration, configuration, or original unit state is not ready' >&2; exit 1;
}
[[ $(stat -c %a "$envfile") == 600 && $(stat -c %u "$envfile") == 0 && $(stat -c %a "$manifest") == 600 && $(stat -c %u "$manifest") == 0 ]] || {
  echo 'Formal configuration permissions are not owner-only' >&2; exit 1;
}
old=$(readlink -f "$current")
[[ $old == "$base/releases/"* && -d $old && $old != "$release" ]] || exit 1
cd "$release"
"$release/.venv/bin/python" -B -m cutover.preflight_formal
backup=/var/lib/baite-rdos/cutover-backups/pre-activate-$(date -u +%Y%m%dT%H%M%SZ)-$sha.db
mkdir -p -m 0700 "$(dirname "$backup")"
stopped=0
switched=0
before=
fingerprint() {
  "$python" - "$database" <<'PY'
import hashlib,sqlite3,sys
with sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True) as db:
    digest=hashlib.sha256()
    for line in db.iterdump(): digest.update(line.encode());digest.update(b'\n')
    print(digest.hexdigest())
PY
}
rollback() {
  code=${1:-1}
  trap - ERR INT TERM
  if (( stopped )); then
    systemctl stop baite-rdos || true
    if [[ $(fingerprint) == "$before" ]]; then
      rm -f -- "$override"
      systemctl daemon-reload
      ln -sfn "$old" "$current.rollback"
      mv -Tf "$current.rollback" "$current"
      systemctl start baite-rdos
      echo 'Activation failed; old release restarted. Database was unchanged.' >&2
    else
      echo "Activation failed after database changed; service stopped. Preserve database and inspect $backup before any rollback." >&2
    fi
  fi
  exit "$code"
}
trap 'rollback $?' ERR
trap 'rollback 130' INT
trap 'rollback 143' TERM
systemctl stop baite-rdos
stopped=1
before=$(fingerprint)
"$python" - "$database" "$backup" <<'PY'
import sqlite3,sys
with sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True) as source:
    with sqlite3.connect(sys.argv[2]) as target:
        source.backup(target)
        assert target.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
PY
chmod 0600 "$backup"
mkdir -p -m 0755 "$(dirname "$override")"
install -m 0644 "$release/deploy/rdos2.service-override.conf" "$override"
systemctl daemon-reload
ln -sfn "$old" "$base/previous.next"
mv -Tf "$base/previous.next" "$base/previous"
ln -sfn "$release" "$current.next"
mv -Tf "$current.next" "$current"
switched=1
systemctl start baite-rdos
ready=0
for attempt in {1..30}; do
  if curl -fsS --max-time 3 http://127.0.0.1:8010/api/health |
    "$python" -c 'import json,sys;x=json.load(sys.stdin);assert x.get("status")=="ok" and x.get("mode")=="production" and x.get("app_release")==sys.argv[1]' "$sha"; then
    ready=1; break
  fi
  sleep 2
done
[[ $ready == 1 ]]
curl -fsS --max-time 10 https://yjmt.cn/baite-rdos/api/health |
  "$python" -c 'import json,sys;x=json.load(sys.stdin);assert x.get("status")=="ok" and x.get("mode")=="production" and x.get("app_release")==sys.argv[1]' "$sha"
stopped=0
trap - ERR INT TERM
echo "RDOS 2.0 $sha is serving at the existing address; maintenance remains controlled by rdos2.env."
