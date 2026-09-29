#!/usr/bin/env bash
# Linux only. Failed health check leaves the service STOPPED for explicit recovery.
set -euo pipefail
test "$(id -u)" = 0 || { printf 'Run as root.\n' >&2; exit 1; }
revision=${1:?Pass a prepared full commit SHA}
[[ "$revision" =~ ^[a-f0-9]{40}$ ]] || exit 1
exec 9>/run/lock/baite-rdos-deploy.lock
flock -n 9 || { printf 'Another deployment is running.\n' >&2; exit 1; }
release=/opt/baite-rdos/releases/$revision
test -f "$release/.verified"
test -f /etc/baite-rdos/rdos.env
current=/opt/baite-rdos/current
previous=$(readlink -f "$current" || true)
if test -n "$previous" && test -d "$previous"; then
    test "$previous" != "$release" || { printf 'Already selected.\n'; exit 1; }
    systemctl stop baite-rdos
    # Uses the OLD release's environment and records its SHA/schema alongside the DB.
    systemctl start baite-rdos-backup.service
    ln -s "$previous" /opt/baite-rdos/previous.next
    mv -Tf /opt/baite-rdos/previous.next /opt/baite-rdos/previous
elif test -e /var/lib/baite-rdos/baite.db; then
    printf 'Refusing first activation over an existing database. Inspect it first.\n' >&2
    exit 1
fi
ln -s "$release" /opt/baite-rdos/current.next
mv -Tf /opt/baite-rdos/current.next "$current"
systemctl start baite-rdos
for attempt in {1..30}; do
    if curl -fsS --max-time 2 http://127.0.0.1:8010/api/health |
        "$release/.venv/bin/python" -c 'import json,sys; assert json.load(sys.stdin)["app_release"] == sys.argv[1]' "$revision" 2>/dev/null; then
        systemctl enable baite-rdos baite-rdos-backup.timer
        systemctl start baite-rdos-backup.timer
        printf 'Activated %s; public and existing-site checks are still required.\n' "$revision"
        exit 0
    fi
    sleep 1
done
systemctl stop baite-rdos
printf 'Health check failed. Service stopped; use DEPLOYMENT.md recovery procedure. Previous: %s\n' "$previous" >&2
exit 1
