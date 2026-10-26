"""Fail-closed validation of the formal RDOS 2.0 resource manifest.

This module only reads configuration and file metadata. Importing it cannot
enable production mode, contact Feishu, or mutate the existing database.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


PUBLIC_URL = "https://yjmt.cn/baite-rdos"
BASE_TOKEN = "E34dbYDEXaCRnDsMQDBcMvyMnjb"
TASK_TABLE = "tblvJU3CwPt6eAaw"
TASK_VIEW = "vew9KuRw2Q"
HUMAN_FOLDER = "ZBQXfgblvlE3NrdrZ2JcNNhfnxh"
AGENT_FOLDER = "VCAHfzSZsl9iSmdrmnGcTCkWntg"
SELECTED_RAG_FOLDER = "KQjyf6KrxlZ24MdkHirc9TP0n2d"
SHARED_SKILLS_FOLDER = "ImWqfzWcvl5LWUdFmxKcLIfdntp"
DEFAULT_PROJECT = "project_rdos"
LEGACY_AI_PROJECT = "prj_0d9b2a0ecec486cdf8"
FINANCE_PROJECT = "rdos-self"
FINANCE_FOLDER = "NC02ftI7DlejwedufJ9cYVednzh"
FINANCE_URL = "https://shbebetter.feishu.cn/drive/folder/" + FINANCE_FOLDER
LIVE_DATABASE = Path("/var/lib/baite-rdos/baite.db")
# The npm .bin entry on the current server is a symlink. Pin the resolved
# package script itself so replacement of the link cannot redirect execution.
LIVE_CLI = Path("/var/lib/baite-rdos/feishu-cli/node_modules/@larksuite/cli/scripts/run.js")
LIVE_NODE = Path('/www/server/nodejs/v26.1.0/bin/node')
LIVE_MANIFEST = Path("/etc/baite-rdos/rdos2-resources.json")
MODEL_URL = "https://ark.cn-beijing.volces.com/api/plan/v3"
MODEL_NAME = "deepseek-v4-pro"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{4,100}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_FIELDS = frozenset({
    "schema_version", "mode", "base_token", "task_table", "task_view",
    "event_table", "default_project_id", "human_folder", "agent_folder",
    "selected_rag_folder", "shared_skills_folder", "cli_path", "cli_sha256",
    "node_members", "account_fingerprint", "agent_md", "memory_md",
    "finance_project_id", "finance_folder",
})


class ConfigurationError(ValueError):
    """An incomplete formal configuration is a deployment stop, not a mock fallback."""


@dataclass(frozen=True)
class FormalConfig:
    resources: dict
    database: Path
    cli: Path
    agent_md: Path
    memory_md: Path


def _plain_file(path: Path, *, owner_only: bool = False) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ConfigurationError("正式配置文件必须是现有绝对普通文件")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ConfigurationError("正式配置文件不能是特殊文件或硬链接")
    if owner_only and (info.st_mode & 0o037 or ((info.st_mode & 0o040) and info.st_gid != os.getgid())):
        raise ConfigurationError("正式资源登记权限过宽")
    return path


def _token(value: object, label: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ConfigurationError(f"正式资源缺少有效{label}")
    return value


def validate_resources(value: object, *, cli_path: Path = LIVE_CLI,
                       state_root: Path = LIVE_DATABASE.parent) -> dict:
    """Validate the exact formal Base and directories; reject lab resources."""
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ConfigurationError("正式资源登记字段缺失或包含未知项")
    if value["schema_version"] != 1 or value["mode"] != "production":
        raise ConfigurationError("正式资源登记模式无效")
    exact = {"base_token": BASE_TOKEN, "task_table": TASK_TABLE,
             "task_view": TASK_VIEW, "default_project_id": DEFAULT_PROJECT,
             "human_folder": HUMAN_FOLDER, "agent_folder": AGENT_FOLDER,
             "selected_rag_folder": SELECTED_RAG_FOLDER,
             "shared_skills_folder": SHARED_SKILLS_FOLDER,
             "finance_project_id": FINANCE_PROJECT,
             "finance_folder": FINANCE_FOLDER}
    for name, expected in exact.items():
        if value[name] != expected:
            raise ConfigurationError(f"正式资源 {name} 与已批准入口不一致")
    event_table = _token(value["event_table"], "协作记录表")
    if not event_table.startswith("tbl") or event_table == TASK_TABLE:
        raise ConfigurationError("协作记录表必须是原 Base 内独立的新表")
    if value["cli_path"] != str(cli_path) or not _SHA.fullmatch(str(value["cli_sha256"])):
        raise ConfigurationError("正式 CLI 路径或校验值不符合固定配置")
    if not _SHA.fullmatch(str(value["account_fingerprint"])):
        raise ConfigurationError("正式飞书调用账号未固定")
    members = value["node_members"]
    if not isinstance(members, dict) or not members:
        raise ConfigurationError("缺少正式节点与成员映射")
    for node, member in members.items():
        _token(node, "节点"); _token(member, "成员")
    if len(set(members.values())) != len(members):
        raise ConfigurationError("多个节点不能冒充同一成员")
    state_root = state_root.resolve()
    for key, basename in (("agent_md", "agent.md"), ("memory_md", "memory.md")):
        path = Path(value[key])
        if (not path.is_absolute() or path.is_symlink()
                or path.resolve(strict=False) != state_root / "central" / basename):
            raise ConfigurationError("中央职责与记忆文件必须在受限运行目录")
    return dict(value)


def load(manifest: Path = LIVE_MANIFEST, *, environ: dict | None = None,
         database: Path = LIVE_DATABASE, cli: Path = LIVE_CLI,
         state_root: Path | None = None) -> FormalConfig:
    env = os.environ if environ is None else environ
    if env.get("RDOS_RUNTIME_MODE") != "production":
        raise ConfigurationError("正式启动须显式选择 production 模式")
    if env.get("BAITE_PUBLIC_URL", "").rstrip("/") != PUBLIC_URL:
        raise ConfigurationError("正式公开地址不符合保留入口")
    if env.get("BAITE_DB_PATH") != str(database) or not database.is_absolute():
        raise ConfigurationError("正式数据库路径不符合既有位置")
    if len(env.get("BAITE_SESSION_SECRET", "")) < 24:
        raise ConfigurationError("正式会话密钥未配置")
    if not (env.get("BAITE_LLM_API_KEY") or env.get("ARK_API_KEY")):
        raise ConfigurationError("正式模型密钥未配置")
    if env.get("BAITE_LLM_BASE_URL", "").rstrip("/") != MODEL_URL or env.get("BAITE_LLM_MODEL") != MODEL_NAME:
        raise ConfigurationError("正式模型地址或名称不符合已核对配置")
    if env.get("BAITE_LLM_THINKING") != "enabled" or env.get("BAITE_LLM_REASONING_EFFORT") != "high":
        raise ConfigurationError("正式模型推理参数不符合已核对配置")
    if urlsplit(PUBLIC_URL).path != "/baite-rdos":
        raise ConfigurationError("正式子路径无效")
    manifest = _plain_file(manifest, owner_only=True)
    value = json.loads(manifest.read_text(encoding="utf-8"))
    state_root = database.parent if state_root is None else state_root
    resources = validate_resources(value, cli_path=cli, state_root=state_root)
    cli = _plain_file(cli)
    if hashlib.sha256(cli.read_bytes()).hexdigest() != resources["cli_sha256"]:
        raise ConfigurationError("正式 CLI 文件与登记校验值不一致")
    agent_md = _plain_file(Path(resources["agent_md"]), owner_only=True)
    if not agent_md.read_text(encoding="utf-8").strip():
        raise ConfigurationError("中央职责文件为空")
    memory_md = Path(resources["memory_md"])
    if memory_md.exists():
        _plain_file(memory_md, owner_only=True)
    return FormalConfig(resources=resources, database=database, cli=cli,
                        agent_md=agent_md, memory_md=memory_md)
