"""Validated formal-runtime capability shared by production service modules.

The launcher must call ``activate`` before importing the application. An
environment variable alone never opens the formal Feishu or model backends.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import threading

from cutover.production_config import FormalConfig, load, LIVE_NODE

_config: FormalConfig | None = None
_PEER_NODE = LIVE_NODE
_peer_snapshot = None
_peer_lock = threading.Lock()


def activate() -> FormalConfig:
    global _config
    if _config is not None:
        return _config
    candidate = load()
    _config = candidate
    return candidate


def require() -> FormalConfig:
    if _config is None:
        raise RuntimeError("正式运行配置尚未验证，拒绝访问正式资源")
    return _config


def active() -> bool:
    return _config is not None


def run_peer_crypto(request: dict) -> dict:
    """Run only the packaged signature helper with a fixed existing Node.

    This capability accepts typed crypto facts, never commands, paths or an
    inherited environment. Hashes are frozen before the first probe and checked
    on every call; replacing either executable disables the capability.
    """
    configured = require()
    fields = {'generate': {'action'}, 'sign': {'action', 'payload', 'private_key'},
              'verify': {'action', 'payload', 'signature', 'public_key'}}
    if (not isinstance(request, dict) or request.get('action') not in fields
            or set(request) != fields[request['action']]
            or any(not isinstance(v, str) for v in request.values())):
        raise ValueError('签名辅助请求无效')
    payload = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if len(payload.encode()) > 2_000_000:
        raise ValueError('签名辅助请求过大')
    helper = Path(__file__).resolve().parents[1] / 'node_skills/rdos-workflow/scripts/peer_crypto.cjs'
    binary = _PEER_NODE.resolve(strict=True)
    paths = (binary, helper)
    if any(p.is_symlink() or not p.is_file() or p.stat().st_mode & 0o022 for p in paths):
        raise ValueError('签名辅助文件不可核对')
    current = tuple((str(p), hashlib.sha256(p.read_bytes()).hexdigest()) for p in paths)
    global _peer_snapshot
    with _peer_lock:
        if _peer_snapshot is None:
            _peer_snapshot = current
        elif current != _peer_snapshot:
            raise ValueError('签名辅助文件已变化')
    runtime = configured.database.parent / 'runtime'
    completed = subprocess.run([str(binary), str(helper)], input=payload, capture_output=True,
        text=True, timeout=10, check=False, cwd=str(runtime),
        env={'HOME': str(runtime), 'PATH': '/usr/bin:/bin', 'TMPDIR': str(runtime / 'tmp')})
    if completed.returncode or len(completed.stdout.encode()) > 2_000_000:
        raise ValueError('签名辅助程序未成功')
    return json.loads(completed.stdout)
