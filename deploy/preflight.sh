#!/usr/bin/env bash
set -euo pipefail
id
uname -a
test ! -f /etc/os-release || sed -n '1,12p' /etc/os-release
getconf _NPROCESSORS_ONLN
free -m
df -h / /var
ss -lntp
systemctl --version | head -1
command -v python3.12 || true
if ss -lnt | awk '{print $4}' | grep -Eq '(^|:)8010$'; then
    printf 'STOP: port 8010 is already occupied; inspect its owner before deployment.\n' >&2
    exit 1
fi
nginx_bin=/www/server/nginx/sbin/nginx
test -x "$nginx_bin" || nginx_bin=/usr/sbin/nginx
sudo "$nginx_bin" -t
sudo "$nginx_bin" -T 2>&1 | grep -E '(^# configuration file|server_name|listen |include |ssl_certificate |proxy_pass)' || true
for url in https://yjmt.cn/ https://yjmt.cn/yxxz/ https://yjmt.cn/fuquelai/ https://yjmt.cn/fuquelai/api/health; do
    curl --max-time 20 -sS -o /dev/null -w '%{url_effective} %{http_code}\n' "$url"
done
