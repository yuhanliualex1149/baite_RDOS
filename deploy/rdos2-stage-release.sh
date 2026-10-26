#!/usr/bin/env bash
# Stage and test one exact commit from the RDOS 2.0 release-preparation branch.
# This script does not stop or replace the running Panel.
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
[[ $(id -u) == 0 && $# == 1 && $1 =~ ^[0-9a-f]{40}$ ]] || exit 64
sha=$1
branch=codex/rdos2-release-prep
base=/opt/baite-rdos
repo=$base/repository
release=$base/releases/$sha
python=$base/tools/bin/python3.12
build_user=baite-rdos-deploy
exec 9>/run/lock/baite-rdos-deploy.lock
flock -n 9 || { echo 'RDOS release operation already running' >&2; exit 75; }
[[ -x $python && -d $repo && -L $base/current ]] || { echo 'Existing release prerequisites missing' >&2; exit 1; }
git -C "$repo" fetch --no-tags origin "$branch"
actual=$(git -C "$repo" rev-parse FETCH_HEAD)
[[ $actual == "$sha" ]] || { echo 'Requested SHA is not the published preparation branch head' >&2; exit 1; }
[[ ! -e $release ]] || { echo 'This exact release directory already exists; inspect it before reuse' >&2; exit 1; }
install -d -m 0755 "$release"
git -C "$repo" archive "$sha" | tar -x -C "$release"
[[ -f $release/cutover/check_release.py && -f $release/server/production_app.py ]] || {
  echo 'Commit lacks formal entry or release suite' >&2; exit 1;
}
chown -R "$build_user:$build_user" "$release"
# Build/test receives neither the live database nor production credentials.
systemd-run --quiet --wait --pipe --collect --unit="rdos2-stage-$sha" \
  -p Type=oneshot -p User="$build_user" -p NoNewPrivileges=yes \
  -p KillMode=control-group -p ProtectHome=yes -p ProtectSystem=strict \
  -p "ReadWritePaths=$release" -p PrivateTmp=yes -- env -i \
  PATH="$PATH" HOME="$release" PYTHONDONTWRITEBYTECODE=1 \
  bash -c '
    set -Eeuo pipefail
    cd "$1"
    "$2" -m venv .venv
    .venv/bin/python -m pip install -r requirements.lock
    .venv/bin/python -m pip check
    .venv/bin/python -B -m cutover.check_release
    .venv/bin/python -B -m compileall -q cutover server lab node_skills
    bash -n deploy/rdos2-stage-release.sh
  ' _ "$release" "$python"
printf 'BAITE_APP_RELEASE=%s\n' "$sha" > "$release/release.env"
printf '%s\n' "$sha" > "$release/.verified"
chown -R root:root "$release"
chmod -R go-w "$release"
chmod 0644 "$release/release.env" "$release/.verified"
echo "Staged and verified $sha; current service was not changed."
