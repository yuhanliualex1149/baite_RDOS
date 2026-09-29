#!/usr/bin/env bash
# Creates a new release; does not switch the live service or change Nginx.
set -euo pipefail
test "$(id -u)" = 0 || { printf 'Run as root.\n' >&2; exit 1; }
revision=${1:?Pass the verified full 40-character commit SHA}
[[ "$revision" =~ ^[a-f0-9]{40}$ ]] || { printf 'Invalid SHA\n' >&2; exit 1; }
python_bin=$(command -v python3.12 || true)
test -n "$python_bin" || python_bin=/opt/baite-rdos/tools/bin/python3.12
test -x "$python_bin"
if ! id baite-rdos >/dev/null 2>&1; then
    useradd --system --create-home --home-dir /var/lib/baite-rdos --shell /usr/sbin/nologin baite-rdos
fi
install -d -m 0755 /opt/baite-rdos /opt/baite-rdos/releases
install -d -m 0700 -o baite-rdos -g baite-rdos /var/lib/baite-rdos /var/lib/baite-rdos/backups /var/log/baite-rdos
install -d -m 0750 -o root -g baite-rdos /etc/baite-rdos
repo=/opt/baite-rdos/repository
if test ! -d "$repo/.git"; then
    git clone https://github.com/yuhanliualex1149/baite_RDOS.git "$repo"
else
    git -C "$repo" fetch origin
fi
git -C "$repo" cat-file -e "$revision^{commit}"
release=/opt/baite-rdos/releases/$revision
test ! -e "$release" || { printf 'Release already exists: %s\n' "$release" >&2; exit 1; }
install -d -m 0755 "$release"
git -C "$repo" archive "$revision" | tar -x -C "$release"
"$python_bin" -m venv "$release/.venv"
"$release/.venv/bin/python" -m pip install -r "$release/requirements.lock"
printf 'BAITE_APP_RELEASE=%s\n' "$revision" > "$release/release.env"
cd "$release"
PYTHONDONTWRITEBYTECODE=1 BAITE_DISABLE_EXTERNAL_SYNC=true "$release/.venv/bin/python" -m unittest discover -v
PYTHONDONTWRITEBYTECODE=1 BAITE_DISABLE_EXTERNAL_SYNC=true "$release/.venv/bin/python" tests/e2e_demo.py
"$release/.venv/bin/python" -m pip check
touch "$release/.verified"
printf 'Prepared and tested release: %s\n' "$release"
