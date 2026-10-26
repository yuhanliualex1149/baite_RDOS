"""Validated formal-runtime capability shared by production service modules.

The launcher must call ``activate`` before importing the application. An
environment variable alone never opens the formal Feishu or model backends.
"""
from __future__ import annotations

from cutover.production_config import FormalConfig, load

_config: FormalConfig | None = None


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
