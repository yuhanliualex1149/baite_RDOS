"""Strict configuration and path checks used before importing the Panel."""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PHASE3_CLI = "/opt/homebrew/lib/node_modules/@larksuite/cli/bin/lark-cli"


def checked_path(path: Path, root: Path, *, tree: bool = False) -> Path:
    path, root = Path(path).absolute(), Path(root).absolute()
    if path == root or root not in path.parents:
        raise ValueError("目标必须是实验根目录的明确子路径")
    for item in (root, *reversed(path.relative_to(root).parents)):
        candidate = item if item.is_absolute() else root / item
        if candidate.is_symlink():
            raise ValueError("实验路径不能经过符号链接")
    candidates = [path]
    if tree and path.exists():
        candidates.extend(path.rglob("*"))
    for candidate in candidates:
        if candidate.is_symlink():
            raise ValueError("实验运行目录含符号链接")
        if candidate.exists():
            info = candidate.stat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ValueError("实验运行目录含非普通文件")
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise ValueError("实验文件不能是硬链接")
        if root.resolve() not in candidate.resolve().parents:
            raise ValueError("实验路径越界")
    return path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    # All callers check the enclosing directory before creating a file.
    if path.is_symlink() or (path.is_file() and path.stat().st_nlink != 1):
        raise ValueError("拒绝链接配置文件")
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load(root: Path = ROOT) -> dict:
    root = Path(root).absolute()
    checked_path(root / "config", root, tree=True)
    path = checked_path(root / "config/lab.json", root)
    cfg = read_json(path)
    required = {"schema_version", "environment", "env_id", "root", "port", "backends", "external_resources"}
    if set(cfg) not in (required, required | {"phase3"}):
        raise ValueError("实验配置字段缺失或包含未知配置")
    if cfg["schema_version"] != 1 or cfg["environment"] != "rdos_local_lab":
        raise ValueError("缺少明确实验环境标识")
    if not re.fullmatch(r"rdos2_lab_[0-9a-f]{12}", str(cfg["env_id"])):
        raise ValueError("无效实验环境标识")
    if cfg["root"] != str(root) or root.resolve() != root:
        raise ValueError("实验配置根目录与实际路径不一致")
    if type(cfg["port"]) is not int or not 1024 <= cfg["port"] <= 65535:
        raise ValueError("实验端口无效")
    if cfg["backends"] not in ({"documents": "local_mock", "model": "local_mock"},
                           {"documents": "local_mock", "model": "agent_plan"}):
        raise ValueError("模型后端只能显式选择 local_mock 或 agent_plan")
    if cfg["backends"]["model"] == "agent_plan":
        model_path = checked_path(root / "runtime/model.json", root)
        if not model_path.is_file() or model_path.stat().st_mode & 0o077:
            raise ValueError("真实模型配置缺失或权限过宽")
        model = read_json(model_path)
        if set(model) != {"api_key", "base_url", "model", "thinking", "reasoning_effort"} or not model["api_key"]:
            raise ValueError("Agent Plan 必要配置不完整")
        if model["base_url"] != "https://ark.cn-beijing.volces.com/api/plan/v3" or model["model"] != "deepseek-v4-pro":
            raise ValueError("Agent Plan 地址或模型不在本轮登记范围")
        if model["thinking"] != "enabled" or model["reasoning_effort"] != "high":
            raise ValueError("Agent Plan 推理参数与本轮核定值不一致")
    if cfg["external_resources"] != {"enabled": False, "folder": "", "documents": [], "task_table": "", "feedback": "", "recipient": ""}:
        raise ValueError("旧外部资源必须留空并禁用")
    phase3 = cfg.get("phase3", {"enabled": False})
    if not isinstance(phase3, dict) or type(phase3.get("enabled")) is not bool:
        raise ValueError("阶段3须显式启用或禁用")
    if not phase3["enabled"]:
        if set(phase3) != {"enabled"}:
            raise ValueError("未启用阶段3不能附带出口配置")
    else:
        if set(phase3) != {"enabled", "cli_path", "cli_sha256", "registry_path"}:
            raise ValueError("阶段3出口配置不完整或有未知字段")
        if phase3["cli_path"] != PHASE3_CLI or not re.fullmatch(r"[0-9a-f]{64}", str(phase3["cli_sha256"])):
            raise ValueError("阶段3只允许已固定的原生CLI和SHA256")
        if phase3["registry_path"] != str(root / "runtime/phase3_resources.json"):
            raise ValueError("阶段3资源登记只能位于实验runtime")
        checked_path(Path(phase3["registry_path"]), root)
    checked_path(root / "runtime", root, tree=True)
    checked_path(root / "nodes", root, tree=True)
    return cfg


def instance(cfg: dict) -> dict:
    path = checked_path(Path(cfg["root"]) / "runtime/instance.json", Path(cfg["root"]))
    value = read_json(path)
    if value.get("env_id") != cfg["env_id"] or not re.fullmatch(r"[0-9a-f]{32}", str(value.get("generation", ""))):
        raise ValueError("运行数据的实验标识或代次不匹配")
    return value
