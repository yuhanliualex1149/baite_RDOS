"""Read-only formal startup gate. Never prints credentials or CLI identity."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

from cutover import production_config as formal
from cutover.formal_migration import require_installed_marker
from server.phase3_feishu import account_fingerprint


def read_environment(path: Path) -> dict[str, str]:
    result = {}
    if path.is_symlink() or not path.is_file():
        raise formal.ConfigurationError('正式环境文件不是普通文件')
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        parts = shlex.split(line, comments=False, posix=True)
        if len(parts) != 1 or '=' not in parts[0]:
            raise formal.ConfigurationError('正式环境文件包含无法识别的配置行')
        key, value = parts[0].split('=', 1)
        if not key.isidentifier() or not value or key in result:
            raise formal.ConfigurationError('正式环境文件配置键重复或为空')
        result[key] = value
    return result


def check(old_env=Path('/etc/baite-rdos/rdos.env'),
          overlay=Path('/etc/baite-rdos/rdos2.env')):
    if os.geteuid() != 0:
        raise formal.ConfigurationError('正式预检须由服务器管理员执行')
    values = {**read_environment(old_env), **read_environment(overlay)}
    configured = formal.load(environ=values)
    require_installed_marker(configured.database)
    readable = subprocess.run(['/usr/sbin/runuser', '-u', 'baite-rdos', '--',
                               '/usr/bin/test', '-r', str(formal.LIVE_MANIFEST)],
                              capture_output=True, timeout=10)
    if readable.returncode:
        raise formal.ConfigurationError('正式服务用户不能读取资源登记，未切换服务')
    command = ['/usr/sbin/runuser', '-u', 'baite-rdos', '--', '/usr/bin/env',
               'HOME=/var/lib/baite-rdos', 'PATH='+str(formal.LIVE_NODE.parent)+':/usr/bin:/bin',
               str(configured.cli), 'auth', 'status', '--json', '--verify']
    response = subprocess.run(command, capture_output=True, text=True, timeout=40)
    if response.returncode:
        raise formal.ConfigurationError('既有服务用户的飞书登录预检失败')
    try:
        actual = account_fingerprint(json.loads(response.stdout))
    except (ValueError, KeyError) as exc:
        raise formal.ConfigurationError('飞书身份响应无法核对') from exc
    if actual != configured.resources['account_fingerprint']:
        raise formal.ConfigurationError('飞书身份与固定资源登记不一致')
    return {'status': 'ready', 'original_base': configured.resources['base_token'],
            'task_table': configured.resources['task_table'],
            'event_table': configured.resources['event_table'],
            'node_count': len(configured.resources['node_members'])}


if __name__ == '__main__':
    print(json.dumps(check(), ensure_ascii=False, sort_keys=True))
