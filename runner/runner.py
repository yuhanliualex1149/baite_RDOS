from __future__ import annotations

import argparse
import hashlib
import json
import logging
from logging.handlers import TimedRotatingFileHandler
import os
import platform
import re
import shutil
import stat
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rdos_protocol import content_hash, verify_snapshot
from rdos_contract import agent_projection, contract_hash, render_rules, validate_contract
from rdos_agent_integration import ENTRY_VERSION, ROOT_ENTRY, render_read_first
from runner.platform_support import (WINDOWS, is_linklike, owned_release, portable_filename,
                                     reject_link_ancestors, workspace_lock)


def api_request(
    config: Dict[str, str],
    path: str,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    body = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {config['runner_token']}",
    }
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        config["control_url"].rstrip("/") + path,
        data=body,
        method=method,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def load_config(path: Path) -> Dict[str, str]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Runner 配置不存在：{path}")
    if os.name != "nt":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            path.chmod(0o600)
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = {"control_url", "runner_id", "runner_token", "workspace"}
    missing = expected - payload.keys()
    if missing:
        raise ValueError(f"Runner 配置缺少字段：{', '.join(sorted(missing))}")
    config = {key: str(payload[key]).strip() for key in expected}
    for optional in ("installation_id", "bootstrapper_version", "protocol_version"):
        if payload.get(optional):
            config[optional] = str(payload[optional]).strip()
    if not all(config.values()):
        raise ValueError("Runner 配置字段不能为空")
    workspace = Path(config["workspace"]).expanduser()
    if not workspace.is_absolute():
        raise ValueError("workspace 必须是绝对路径")
    config["workspace"] = str(workspace.resolve())
    return config


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def safe_filename(name: str, fallback: str) -> str:
    name = portable_filename(unicodedata.normalize("NFC", name))
    if name.lower().endswith(".md"):
        return name
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{slug or fallback}.md"


def render_skill(item: Dict[str, Any]) -> str:
    return (
        f"<!-- Shared Skill: {item['name']} -->\n"
        f"<!-- Version: {item['version']} -->\n"
        f"<!-- Updated: {item['updated_at']} -->\n\n"
        f"{item['content'].rstrip()}\n"
    )


def render_collaborations(runner_id: str, items: List[Dict[str, Any]]) -> str:
    lines = [
        "# 协作状态",
        "",
        "此文件由 Local Runner 同步。Local Agent 自行决定具体工作方法。",
        "",
        "## 等待当前节点处理",
        "",
    ]
    incoming = [
        item
        for item in items
        if item["to_runner_id"] == runner_id
        and item["status"] == "pending"
        and not item["requires_admin"]
    ]
    if not incoming:
        lines.append("暂无。")
    for item in incoming:
        lines.extend(
            [
                f"### #{item['id']}｜{item['from_name']}：{item['topic']}",
                "",
                item["summary"],
                "",
                f"参考：{item['reference'] or '无'}",
                "处理方式：Confirm / Return / Escalate",
                "",
            ]
        )
    lines.extend(["## 已发出或已处理", ""])
    history = [
        item
        for item in items
        if item["from_runner_id"] == runner_id
        or (item["to_runner_id"] == runner_id and item not in incoming)
    ]
    if not history:
        lines.append("暂无。")
    for item in history:
        lines.append(
            f"- #{item['id']}｜{item['from_name']} → {item['to_name']}｜"
            f"{item['topic']}｜{item['status']}"
            + (f"｜{item['feedback']}" if item["feedback"] else "")
        )
    return "\n".join(lines) + "\n"


def render_skill_proposals(items: List[Dict[str, Any]]) -> str:
    lines = [
        "# Shared Skill Proposal 状态",
        "",
        "此文件只同步当前节点提交的 Proposal 结果与管理员反馈。",
        "",
    ]
    if not items:
        lines.append("暂无。")
    for item in items:
        lines.extend(
            [
                f"## {item['skill_name']}｜{item['proposed_version']}",
                "",
                f"状态：{item['status']}",
                f"说明：{item['summary']}",
                f"管理员反馈：{item['feedback'] or '暂无'}",
                f"更新时间：{item['updated_at']}",
                "",
            ]
        )
    return "\n".join(lines) + "\n"


def render_project(project: Dict[str, Any]) -> str:
    mode_labels = {
        "new_product": "新产品 / 无可靠母版",
        "existing_new_funder": "既有母版 / 新资方或年度",
        "existing_new_context": "既有母版 / 新人群或场域",
        "known_issue_iteration": "旧版问题明确 / 迭代",
    }
    lines = [
        f"# {project['name']}",
        "",
        project.get("description") or "（暂无项目说明）",
        "",
        "## 协作边界",
        "",
        "Control Panel 汇总项目协作信息；Local Agent 自主决定具体工作方法。",
        "",
        "## 项目设置",
        "",
        f"- 项目 ID：`{project['id']}`",
        f"- 生命周期：{project['status']}",
        f"- 系统计算状态：{project['computed_status']}",
        f"- 周期：{project['start_date']} 至 {project['end_date']}",
        f"- 工作日上报截止：{project['daily_cutoff']}（{project['timezone']}）",
        f"- 研发模式：{mode_labels.get(project['development_mode'], project['development_mode'])}",
        f"- 飞书来源：{project['feishu_folder_url']}",
        "",
        "## 当前工作流节点",
        "",
    ]
    nodes = [node for node in project.get("workflow_nodes", []) if node["status"] != "not_started"]
    if not nodes:
        lines.append("暂无结构化节点状态。")
    for node in nodes:
        evidence = "、".join(node.get("evidence", [])) or "未提供"
        lines.append(f"- {node['node_code']}：{node['status']}；证据：{evidence}")
    lines.extend(
        [
            "",
            "## Local Agent 上报方式",
            "",
            "请维护 `../work/project_status.json`；Runner 会同步状态，并只扫描 `../work/files/` 的文件元数据。",
            "",
        ]
    )
    return "\n".join(lines)


def render_project_todos(items: List[Dict[str, Any]]) -> str:
    lines = ["# Shared TODO", ""]
    if not items:
        lines.append("暂无。")
    for item in items:
        lines.append(
            f"- [{item['status']}] {item['title']}｜责任人：{item.get('owner') or '待明确'}"
            + (f"｜截止：{item['due_date']}" if item.get("due_date") else "")
            + f"｜ID：`{item['todo_id']}`"
        )
    return "\n".join(lines) + "\n"


def render_project_gates(items: List[Dict[str, Any]]) -> str:
    lines = [
        "# Gate 记录",
        "",
        "这里记录 Gate 是否被报告通过及管理员核实结果。它不会阻断 Local Agent 工作。",
        "",
    ]
    if not items:
        lines.append("暂无。")
    for item in items:
        evidence = "、".join(item.get("evidence", [])) or "未提供"
        lines.extend(
            [
                f"## {item['gate_code']}｜{item['status']}",
                "",
                f"证据：{evidence}",
                f"说明：{item.get('note') or '无'}",
                f"管理员反馈：{item.get('feedback') or '暂无'}",
                "",
            ]
        )
    return "\n".join(lines) + "\n"


def _validate_content(item: Dict[str, Any]) -> bytes:
    content = str(item["content"])
    encoded = content.encode("utf-8")
    actual_hash = hashlib.sha256(encoded).hexdigest()
    if actual_hash != item["content_hash"]:
        raise ValueError(f"内容 Hash 校验失败：{item['runtime_name']}")
    if len(encoded) != int(item["size_bytes"]):
        raise ValueError(f"内容大小校验失败：{item['runtime_name']}")
    return encoded


def _contract_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    operating = snapshot.get("global_contract")
    if not isinstance(operating, dict) or set(operating) != {
        "contract", "contract_revision", "contract_hash", "updated_at", "shared_revision"
    }:
        raise ValueError("共享快照缺少 Operating Contract")
    contract = validate_contract(operating["contract"])
    if operating["contract_hash"] != contract_hash(contract):
        raise ValueError("Operating Contract Hash 不匹配")
    if (type(operating["contract_revision"]) is not int or operating["contract_revision"] < 1
            or operating["shared_revision"] != snapshot["revision"]):
        raise ValueError("Operating Contract revision 不匹配")
    if snapshot.get("global_rules", {}).get("content") != render_rules(contract):
        raise ValueError("Global Rules 与 Operating Contract 不一致")
    return operating


def _activate_release(root: Path, release: Path) -> None:
    """Shared content and control documents switch through one pointer."""
    release = owned_release(root, release.resolve())
    snapshot = _validate_release(release)
    entry = {"layout_version": 1, "snapshot_dir": str(release), "revision": snapshot["revision"]}
    if "global_contract" in snapshot:
        operating = _contract_from_snapshot(snapshot)
        entry.update(contract_version=operating["contract"]["contract_version"],
                     contract_revision=operating["contract_revision"], contract_hash=operating["contract_hash"],
                     shared_revision=snapshot["revision"])
        if (release / "manifest.json").is_file():
            manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
            if manifest.get("agent_integration_version") == ENTRY_VERSION:
                entry["agent_integration_version"] = ENTRY_VERSION
    # Windows has no link aliases. Replacing a file is the only publication step.
    if WINDOWS:
        write_atomic(root / "runtime.json", json.dumps(entry, ensure_ascii=False, indent=2) + "\n")
        return
    internal = root / ".runner"
    internal.mkdir(parents=True, exist_ok=True)
    for name in ("shared", "control"):
        public = root / name
        target = internal / "current" / name
        if public.is_symlink() and os.readlink(public) == str(target):
            continue
        if public.exists() and not public.is_symlink():
            os.replace(public, internal / f"legacy-{name}-{time.time_ns()}")
        temporary = root / f".{name}-next"
        temporary.unlink(missing_ok=True)
        os.symlink(target, temporary, target_is_directory=True)
        os.replace(temporary, public)
    temporary = internal / ".current-next"
    temporary.unlink(missing_ok=True)
    os.symlink(release, temporary, target_is_directory=True)
    previous = os.readlink(internal / "current") if (internal / "current").is_symlink() else None
    os.replace(temporary, internal / "current")
    try:
        write_atomic(root / "runtime.json", json.dumps(entry, ensure_ascii=False, indent=2) + "\n")
    except OSError:
        if previous:
            os.symlink(previous, temporary, target_is_directory=True)
            os.replace(temporary, internal / "current")
        else:
            (internal / "current").unlink(missing_ok=True)
        raise


def runtime_directory(root: Path) -> Path:
    """Read once per batch; callers keep this path until all batch reads finish."""
    for attempt in range(5):
        try:
            entry = json.loads((root / "runtime.json").read_text(encoding="utf-8"))
            break
        except PermissionError:
            # NTFS can briefly deny new opens while another process replaces the entry.
            if not WINDOWS or attempt == 4:
                raise
            time.sleep(0.02)
    if entry.get("layout_version") != 1:
        raise ValueError("不支持的本地布局版本")
    return owned_release(root, Path(entry["snapshot_dir"]))


def _store_release(staging: Path, snapshot: dict) -> None:
    write_atomic(staging / "snapshot.json", json.dumps(snapshot, ensure_ascii=False))
    files = {path.relative_to(staging).as_posix(): _file_sha256(path)
             for folder in ("shared", "control") for path in (staging / folder).rglob("*") if path.is_file()}
    files["manifest.json"] = _file_sha256(staging / "manifest.json")
    write_atomic(staging / "local-manifest.json", json.dumps(files, sort_keys=True))


def _validate_release(release: Path) -> dict:
    for name in ("snapshot.json", "local-manifest.json"):
        if is_linklike(release / name):
            raise ValueError("快照清单不能是重解析点或符号链接")
    snapshot = json.loads((release / "snapshot.json").read_text(encoding="utf-8"))
    verify_snapshot(snapshot)
    expected = json.loads((release / "local-manifest.json").read_text(encoding="utf-8"))
    actual = {}
    if "manifest.json" in expected:
        actual["manifest.json"] = _file_sha256(release / "manifest.json")
    for folder in ("shared", "control"):
        if is_linklike(release / folder):
            raise ValueError("快照中不允许重解析点或符号链接")
        for directory, dirs, files in os.walk(release / folder, followlinks=False):
            for name in dirs + files:
                path = Path(directory) / name
                if is_linklike(path):
                    raise ValueError("快照中不允许重解析点或符号链接")
                if name in files and path.relative_to(release).as_posix() != "control/membership_ended.md":
                    actual[path.relative_to(release).as_posix()] = _file_sha256(path)
    if actual != expected:
        raise ValueError("本地快照文件不完整或已损坏")
    if "global_contract" in snapshot:
        operating = _contract_from_snapshot(snapshot)
        for relative in operating["contract"]["runtime"]["required_read_order"]:
            if not (release / relative).is_file():
                raise ValueError(f"Operating Contract 入口文件缺失：{relative}")
        stored = json.loads((release / "shared/global_contract.json").read_text(encoding="utf-8"))
        agent = json.loads((release / "control/agent_contract.json").read_text(encoding="utf-8"))
        if stored != operating["contract"] or agent["contract_hash"] != operating["contract_hash"]:
            raise ValueError("本地 Operating Contract 内容不一致")
        manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
        if any(manifest.get(key) != value for key, value in {
            "revision": snapshot["revision"],
            "contract_version": operating["contract"]["contract_version"],
            "contract_revision": operating["contract_revision"],
            "contract_hash": operating["contract_hash"],
        }.items()):
            raise ValueError("快照 Manifest 与 Operating Contract 不一致")
        if manifest.get("agent_integration_version") == ENTRY_VERSION:
            if agent != agent_projection(operating["contract"], snapshot["revision"],
                                         agent["generated_at"], operating["contract_revision"]):
                raise ValueError("Agent Contract 投影内容不一致")
            if (release / "control/READ_FIRST.md").read_text(encoding="utf-8") != render_read_first(agent):
                raise ValueError("Agent 入口与 Contract 不一致")
        elif "agent_integration_version" in manifest:
            raise ValueError("不支持的 Agent Integration 版本")
    return snapshot


def ensure_agent_entries(workspace: Path) -> list[str]:
    """Create stable locators only; never overwrite a user's existing entry."""
    conflicts = []
    for filename in ("AGENTS.md", "CODEBUDDY.md"):
        path = workspace / filename
        if path.exists() or is_linklike(path):
            try:
                managed = not is_linklike(path) and path.is_file() and path.read_text(encoding="utf-8") == ROOT_ENTRY
            except (OSError, UnicodeError):
                managed = False
            if not managed:
                conflicts.append(filename)
            continue
        try:
            with path.open("x", encoding="utf-8", newline="\n") as output:
                output.write(ROOT_ENTRY)
        except OSError:
            conflicts.append(filename)
    if conflicts:
        print(f"Agent 入口冲突，未覆盖已有文件：{', '.join(conflicts)}", file=sys.stderr, flush=True)
    return conflicts


def recover_release(root: Path, releases: Path) -> Optional[dict]:
    current = root / ".runner" / "current"
    candidates = []
    try:
        candidates.append(runtime_directory(root))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if current.is_symlink():
        active = current.resolve()
        if active.parent == releases.resolve():
            candidates.append(active)
    candidates.extend(sorted(releases.glob("revision-*"), key=lambda path: path.stat().st_mtime_ns, reverse=True))
    for candidate in candidates:
        if not candidate.is_dir() or is_linklike(candidate):
            continue
        try:
            candidate = owned_release(root, candidate.resolve())
            snapshot = _validate_release(candidate)
            _activate_release(root, candidate)
            if "global_contract" in snapshot and (candidate / "control/READ_FIRST.md").is_file():
                ensure_agent_entries(root)
            return snapshot
        except (OSError, ValueError, KeyError):
            continue
    return None


def apply_snapshot(workspace: Path, runner_id: str, snapshot: Dict[str, Any]) -> Path:
    verify_snapshot(snapshot)
    operating = _contract_from_snapshot(snapshot)
    revision = int(snapshot["revision"])
    releases = workspace / ".runner" / "releases"
    reject_link_ancestors(releases)
    releases.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"revision-{revision}-", dir=releases))
    try:
        shared = staging / "shared"
        (shared / "skills").mkdir(parents=True)
        (shared / "selected_rag").mkdir(parents=True)
        write_atomic(shared / "global_contract.json", json.dumps(operating["contract"], ensure_ascii=False, indent=2) + "\n")
        write_atomic(shared / "global_rules.md", render_rules(operating["contract"]))
        agent = agent_projection(operating["contract"], revision, datetime.now(timezone.utc).isoformat(),
                                 operating["contract_revision"])
        write_atomic(staging / "control" / "agent_contract.json",
                     json.dumps(agent, ensure_ascii=False, indent=2) + "\n")
        write_atomic(staging / "control" / "READ_FIRST.md", render_read_first(agent))

        used_skill_names: set[str] = set()
        for item in snapshot["shared_skills"]:
            filename = safe_filename(str(item["name"]), f"skill-{item['id']}")
            if filename.casefold() in used_skill_names:
                raise ValueError(f"Shared Skill 文件名冲突：{filename}")
            used_skill_names.add(filename.casefold())
            write_atomic(shared / "skills" / filename, render_skill(item))

        rag_manifest = []
        used_rag_names: set[str] = set()
        for item in snapshot["selected_rag"]:
            filename = safe_filename(str(item["runtime_name"]), "rag")
            if filename.casefold() in used_rag_names:
                raise ValueError(f"Selected RAG 文件名冲突：{filename}")
            used_rag_names.add(filename.casefold())
            encoded = _validate_content(item)
            destination = shared / "selected_rag" / filename
            destination.write_bytes(encoded)
            rag_manifest.append(
                {
                    "source_token": item["source_token"],
                    "runtime_name": filename,
                    "content_hash": item["content_hash"],
                    "size_bytes": item["size_bytes"],
                }
            )

        manifest = {
            "revision": revision,
            "shared_revision": revision,
            "contract_version": operating["contract"]["contract_version"],
            "contract_revision": operating["contract_revision"],
            "contract_hash": operating["contract_hash"],
            "agent_integration_version": ENTRY_VERSION,
            "rag_snapshot": snapshot["rag_snapshot"],
            "selected_rag": rag_manifest,
            "shared_skills": [
                {"name": item["name"], "version": item["version"]}
                for item in snapshot["shared_skills"]
            ],
        }
        write_atomic(
            staging / "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
        write_atomic(
            staging / "control" / "collaboration.md",
            render_collaborations(runner_id, snapshot["collaborations"]),
        )
        write_atomic(
            staging / "control" / "skill_proposals.md",
            render_skill_proposals(snapshot.get("skill_proposals", [])),
        )
        write_atomic(
            staging / "control" / "sync.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
        _store_release(staging, snapshot)
        final = releases / f"revision-{revision}-{time.time_ns()}"
        os.replace(staging, final)
        _activate_release(workspace, final)
        ensure_agent_entries(workspace)
        return final
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _project_path(workspace: Path, project_id: str) -> Path:
    if not re.fullmatch(r"prj_[A-Za-z0-9_-]+", project_id):
        raise ValueError(f"项目 ID 不安全：{project_id}")
    return workspace / "projects" / project_id


def apply_project_snapshot(workspace: Path, project: Dict[str, Any]) -> Path:
    verify_snapshot(project)
    project_id = str(project["id"])
    root = _project_path(workspace, project_id)
    control = root / "control"
    work = root / "work"
    files_root = work / "files"
    releases = workspace / ".runner" / "project-releases" / project_id
    reject_link_ancestors(root)
    reject_link_ancestors(releases)
    reject_link_ancestors(files_root)
    files_root.mkdir(parents=True, exist_ok=True)
    releases.mkdir(parents=True, exist_ok=True)

    staging = Path(
        tempfile.mkdtemp(prefix=f"revision-{project['revision']}-", dir=releases)
    )
    control = staging / "control"
    try:
        shared = staging / "shared"
        shared.mkdir(parents=True)
        manifest = []
        used_names: set[str] = set()
        for item in project.get("content_files", []):
            filename = safe_filename(str(item["runtime_name"]), "project-file")
            if filename.casefold() in used_names:
                raise ValueError(f"项目资料文件名冲突：{filename}")
            used_names.add(filename.casefold())
            encoded = _validate_content(item)
            (shared / filename).write_bytes(encoded)
            manifest.append(
                {
                    "source_token": item["source_token"],
                    "runtime_name": filename,
                    "content_hash": item["content_hash"],
                    "size_bytes": item["size_bytes"],
                }
            )
        write_atomic(
            staging / "manifest.json",
            json.dumps(
                {
                    "project_id": project_id,
                    "project_revision": project["revision"],
                    "snapshot_id": project.get("active_snapshot_id"),
                    "files": manifest,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        public_project = json.loads(json.dumps(project, ensure_ascii=False))
        for item in public_project.get("content_files", []):
            item.pop("content", None)
        write_atomic(control / "project.md", render_project(public_project))
        write_atomic(
            control / "project.json",
            json.dumps(public_project, ensure_ascii=False, indent=2),
        )
        write_atomic(
            control / "todos.md", render_project_todos(project.get("todos", []))
        )
        write_atomic(
            control / "gate_records.md",
            render_project_gates(project.get("gate_records", [])),
        )
        _store_release(staging, project)
        final = releases / f"revision-{project['revision']}-{time.time_ns()}"
        os.replace(staging, final)
        _activate_release(root, final)
        return final
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def mark_removed_projects(workspace: Path, previous: set[str], current: set[str]) -> None:
    for project_id in previous - current:
        root = _project_path(workspace, project_id)
        if root.exists():
            write_atomic(
                root / "membership_ended.md" if WINDOWS else root / "control" / "membership_ended.md",
                "# 项目成员关系已结束\n\nRunner 不再接收本项目更新；历史文件不会自动删除。\n",
            )


def prepare_workspace(workspace: Path) -> None:
    for directory in (
        workspace / "control",
        workspace / "work",
        workspace / "outbox",
        workspace / "outbox" / "sent",
        workspace / "outbox" / "rejected",
        workspace / ".runner" / "releases",
        workspace / ".runner" / "project-releases",
        workspace / "projects",
    ):
        if not directory.is_symlink():
            directory.mkdir(parents=True, exist_ok=True)


def archive_acknowledged(path: Path, sent: Path) -> None:
    destination = sent / path.name
    if destination.exists():
        destination = sent / f"{path.stem}-{int(time.time() * 1000)}{path.suffix}"
    os.replace(path, destination)


def reject_event(path: Path, rejected: Path, reason: str) -> None:
    destination = rejected / path.name
    if destination.exists():
        destination = rejected / f"{path.stem}-{int(time.time() * 1000)}{path.suffix}"
    os.replace(path, destination)
    write_atomic(destination.with_suffix(destination.suffix + ".error.txt"), reason)


def submit_outbox(config: Dict[str, str], workspace: Path) -> None:
    outbox = workspace / "outbox"
    sent = outbox / "sent"
    rejected = outbox / "rejected"
    for event_path in sorted(outbox.glob("*.json")):
        try:
            payload = json.loads(event_path.read_text(encoding="utf-8"))
            response = api_request(config, "/api/runner/events", "POST", payload)
            if response.get("ack") is not True or response.get("event_id") != payload.get("event_id"):
                raise RuntimeError("Control Panel 未返回匹配当前事件的 ACK")
            archive_acknowledged(event_path, sent)
            print(f"[{config['runner_id']}] 已回传 {event_path.name}", flush=True)
        except json.JSONDecodeError as exc:
            reject_event(event_path, rejected, f"JSON 无法解析：{exc}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code in (400, 409, 422):
                reject_event(event_path, rejected, f"HTTP {exc.code}: {detail}")
            else:
                print(
                    f"[{config['runner_id']}] 回传失败：HTTP {exc.code} {detail}",
                    file=sys.stderr,
                    flush=True,
                )
        except (urllib.error.URLError, TimeoutError, ConnectionError, RuntimeError) as exc:
            print(f"[{config['runner_id']}] 事件保留待重试：{exc}", file=sys.stderr, flush=True)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_project_files(project_root: Path, notes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    files_root = project_root / "work" / "files"
    try:
        reject_link_ancestors(files_root)
    except ValueError:
        return []
    files_root.mkdir(parents=True, exist_ok=True)
    note_map: Dict[str, Dict[str, Any]] = {}
    for note in notes:
        raw_path = str(note.get("path") or "")
        path = Path(raw_path)
        if path.is_absolute() or ".." in path.parts:
            continue
        normalized = path.as_posix()
        note_map[normalized] = note

    manifest: List[Dict[str, Any]] = []
    for directory, names, filenames in os.walk(files_root, followlinks=False):
        base = Path(directory)
        names[:] = [name for name in names if not is_linklike(base / name)]
        for filename in filenames:
            path = base / filename
            if is_linklike(path) or not path.is_file():
                continue
            relative = (Path("files") / path.relative_to(files_root)).as_posix()
            stat_result = path.stat()
            note = note_map.get(relative, {})
            manifest.append(
                {
                    "path": relative,
                    "size_bytes": stat_result.st_size,
                    "modified_at": datetime.fromtimestamp(
                        stat_result.st_mtime
                    ).astimezone().isoformat(),
                    "sha256": _file_sha256(path),
                    "status": str(note.get("status") or ""),
                    "summary": str(note.get("summary") or ""),
                }
            )
    return sorted(manifest, key=lambda item: item["path"])


def load_project_status(project_root: Path) -> Dict[str, Any]:
    status_path = project_root / "work" / "project_status.json"
    empty = {
        "summary": "",
        "workflow_nodes": [],
        "todos": [],
        "file_notes": [],
        "issues": [],
        "gate_claim": None,
        "complete": False,
        "source_hash": "missing",
    }
    try:
        reject_link_ancestors(status_path)
    except ValueError:
        return {**empty, "issues": ["project_status.json 路径含重解析点或符号链接，未读取"], "source_hash": "invalid:link"}
    if not status_path.is_file():
        return empty
    try:
        raw = status_path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("project_status.json 必须是 JSON 对象")
        result = {
            "summary": str(payload.get("summary") or ""),
            "workflow_nodes": payload.get("workflow_nodes") or [],
            "todos": payload.get("todos") or [],
            "file_notes": payload.get("file_notes") or [],
            "issues": payload.get("issues") or [],
            "gate_claim": payload.get("gate_claim"),
            "complete": bool(str(payload.get("summary") or "").strip()),
            "source_hash": hashlib.sha256(raw).hexdigest(),
        }
        for key in ("workflow_nodes", "todos", "file_notes", "issues"):
            if not isinstance(result[key], list):
                raise ValueError(f"project_status.json 的 {key} 必须是数组")
        return result
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return {
            **empty,
            "issues": [f"project_status.json 无法读取：{exc}"],
            "source_hash": f"invalid:{status_path.stat().st_mtime_ns}",
        }


def mark_stale_project_status(
    project_root: Path, project: Dict[str, Any], status: Dict[str, Any]
) -> Dict[str, Any]:
    """Mark yesterday's Agent status as incomplete without discarding file metadata."""
    status_path = project_root / "work" / "project_status.json"
    if not status_path.is_file() or status["source_hash"].startswith("invalid:"):
        return status
    try:
        zone = ZoneInfo(project.get("timezone") or "Asia/Shanghai")
    except Exception:
        zone = ZoneInfo("Asia/Shanghai")
    modified_date = datetime.fromtimestamp(status_path.stat().st_mtime, zone).date()
    if modified_date >= datetime.now(zone).date():
        return status
    return {
        **status,
        "complete": False,
        "issues": [
            *status["issues"],
            f"project_status.json 已过期（最后修改：{modified_date.isoformat()}）",
        ],
    }


def _project_local_date(project: Dict[str, Any]) -> str:
    try:
        zone = ZoneInfo(project.get("timezone") or "Asia/Shanghai")
    except Exception:
        zone = ZoneInfo("Asia/Shanghai")
    return datetime.now(zone).date().isoformat()


def project_daily_report_due(project: Dict[str, Any], state: Dict[str, Any]) -> bool:
    try:
        zone = ZoneInfo(project.get("timezone") or "Asia/Shanghai")
    except Exception:
        zone = ZoneInfo("Asia/Shanghai")
    now = datetime.now(zone)
    today = now.date().isoformat()
    if project.get("status") != "active" or now.weekday() >= 5:
        return False
    if not (project["start_date"] <= today <= project["end_date"]):
        return False
    if project.get("runner_today_reported"):
        return False
    return state.get("project_daily_reports", {}).get(project["id"]) != today


def submit_project_report(
    config: Dict[str, str], workspace: Path, project: Dict[str, Any], state: Dict[str, Any]
) -> Dict[str, Any]:
    root = _project_path(workspace, project["id"])
    status_payload = mark_stale_project_status(
        root, project, load_project_status(root)
    )
    manifest = scan_project_files(root, status_payload["file_notes"])
    payload = {
        "type": "project_update",
        "project_id": project["id"],
        "summary": status_payload["summary"],
        "complete": status_payload["complete"],
        "workflow_nodes": status_payload["workflow_nodes"],
        "todos": status_payload["todos"],
        "issues": status_payload["issues"],
        "file_manifest": manifest,
        "gate_claim": status_payload["gate_claim"],
    }
    event_id = "project-" + content_hash({"runner_id": config["runner_id"], "date": _project_local_date(project), "payload": payload})
    payload["event_id"] = event_id
    event_path = workspace / "outbox" / f"{event_id}.json"
    if not event_path.exists() and not (workspace / "outbox" / "sent" / event_path.name).exists():
        write_atomic(event_path, json.dumps(payload, ensure_ascii=False))
    state.setdefault("project_status_hashes", {})[project["id"]] = status_payload[
        "source_hash"
    ]
    state.setdefault("project_manifest_hashes", {})[project["id"]] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode("utf-8")
    ).hexdigest()
    state.setdefault("project_daily_reports", {})[project["id"]] = _project_local_date(
        project
    )
    _save_state(workspace, state)
    return {"queued": True, "event_id": event_id}


def queue_project_reports(config: dict, workspace: Path, projects: list, state: dict) -> None:
    for project in projects:
        root = _project_path(workspace, project["id"])
        status = load_project_status(root)
        changed = status["source_hash"] != "missing" and state["project_status_hashes"].get(project["id"]) != status["source_hash"]
        if changed or project_daily_report_due(project, state):
            submit_project_report(config, workspace, project, state)


def _load_state(workspace: Path) -> Dict[str, Any]:
    path = workspace / ".runner" / "state.json"
    if not path.is_file():
        return {"revision": -1, "failure_count": 0, "retry_nonce": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"revision": -1, "failure_count": 0, "retry_nonce": 0}


def _save_state(workspace: Path, state: Dict[str, Any]) -> None:
    write_atomic(
        workspace / ".runner" / "state.json",
        json.dumps(state, ensure_ascii=False, indent=2),
    )


def report_sync(config: Dict[str, str], state: Dict[str, Any], health: str, error: str = "") -> None:
    api_request(
        config,
        "/api/runner/sync-status",
        "POST",
        {
            "revision": int(state["revision"]),
            "health": health,
            "failure_count": int(state["failure_count"]),
            "error": error,
        },
    )


def run(config_path: Path, poll_seconds: float) -> None:
    config = load_config(config_path)
    workspace = Path(config["workspace"])
    prepare_workspace(workspace)
    try:
        process_lock = workspace_lock(workspace / ".runner" / "runner.lock")
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    release_file = Path(__file__).resolve().parents[1] / "release.json"
    release = json.loads(release_file.read_text(encoding="utf-8"))["release"] if release_file.exists() else "development"
    runtime_info = {"platform": "windows" if WINDOWS else platform.system().lower(),
                    "runner_version": release, "actual_workspace": str(workspace)}
    for optional in ("installation_id", "bootstrapper_version", "protocol_version"):
        if config.get(optional):
            runtime_info[optional] = config[optional]
    state = _load_state(workspace)
    recovered = recover_release(workspace, workspace / ".runner" / "releases")
    state["revision"] = int(recovered["revision"]) if recovered else -1
    state.setdefault("project_token", "")
    state.setdefault("active_projects", [])
    state.setdefault("project_status_hashes", {})
    state.setdefault("project_manifest_hashes", {})
    state.setdefault("project_daily_reports", {})
    project_cache: List[Dict[str, Any]] = []
    for project_id in state["active_projects"]:
        root = _project_path(workspace, project_id)
        recovered_project = recover_release(root, workspace / ".runner" / "project-releases" / project_id)
        if recovered_project:
            project_cache.append(recovered_project)
    # Always fetch the complete project assignment once after process start.
    known_project_token = ""
    print(
        f"[{config['runner_id']}] Runner 已启动，Workspace: {workspace}",
        flush=True,
    )
    while True:
        try:
            queue_project_reports(config, workspace, project_cache, state)
            api_request(config, "/api/runner/heartbeat", "POST", runtime_info)
            query = urllib.parse.urlencode({"known_revision": int(state["revision"])})
            snapshot = api_request(config, f"/api/runner/sync?{query}")
            retry_nonce = int(snapshot.get("retry_nonce", state.get("retry_nonce", 0)))
            if retry_nonce != int(state.get("retry_nonce", 0)):
                state["failure_count"] = 0
                state["retry_nonce"] = retry_nonce
                _save_state(workspace, state)
            paused = int(state.get("failure_count", 0)) >= 10
            if snapshot.get("changed") and not paused:
                try:
                    apply_snapshot(workspace, config["runner_id"], snapshot)
                    state.update(
                        {
                            "revision": int(snapshot["revision"]),
                            "failure_count": 0,
                            "retry_nonce": retry_nonce,
                        }
                    )
                    _save_state(workspace, state)
                    report_sync(config, state, "healthy")
                    print(
                        f"[{config['runner_id']}] 已切换至 shared revision {state['revision']}",
                        flush=True,
                    )
                except Exception as exc:
                    state["failure_count"] = int(state.get("failure_count", 0)) + 1
                    _save_state(workspace, state)
                    health = "needs_admin" if state["failure_count"] >= 10 else "recovering"
                    report_sync(config, state, health, str(exc))
                    print(
                        f"[{config['runner_id']}] 本地快照校验失败：{exc}",
                        file=sys.stderr,
                        flush=True,
                    )
            project_query = urllib.parse.urlencode(
                {"known_token": known_project_token}
            )
            project_snapshot = api_request(
                config, f"/api/runner/projects/sync?{project_query}"
            )
            if project_snapshot.get("changed"):
                verify_snapshot(project_snapshot)
                incoming = project_snapshot.get("projects", [])
                for project in incoming:
                    verify_snapshot(project)
                for project in incoming:
                    apply_project_snapshot(workspace, project)
                previous = set(state.get("active_projects", []))
                current = {str(project["id"]) for project in incoming}
                mark_removed_projects(workspace, previous, current)
                project_cache = incoming
                known_project_token = str(project_snapshot["sync_token"])
                state["project_token"] = known_project_token
                state["active_projects"] = sorted(current)
                _save_state(workspace, state)
                print(
                    f"[{config['runner_id']}] 已同步 {len(incoming)} 个项目",
                    flush=True,
                )

            queue_project_reports(config, workspace, project_cache, state)
            submit_outbox(config, workspace)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            print(
                f"[{config['runner_id']}] Control Panel 拒绝请求：HTTP {exc.code} {detail}",
                file=sys.stderr,
                flush=True,
            )
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            print(
                f"[{config['runner_id']}] 网络暂不可用，将自动重连：{exc}",
                file=sys.stderr,
                flush=True,
            )
        except Exception as exc:
            print(f"[{config['runner_id']}] Runner 错误：{exc}", file=sys.stderr, flush=True)
        time.sleep(poll_seconds)


class _LogStream:
    def __init__(self, logger: logging.Logger, level: int):
        self.logger, self.level = logger, level

    def write(self, message: str) -> int:
        if message.strip():
            self.logger.log(self.level, message.rstrip())
        return len(message)

    def flush(self) -> None:
        for handler in self.logger.handlers:
            handler.flush()


def main() -> None:
    os.umask(0o077)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if getattr(sys, "frozen", False):
        import certifi
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    parser = argparse.ArgumentParser(description="Baite AI R&D OS Local Runner")
    parser.add_argument("--config", type=Path, required=True, help="Runner 配置 JSON")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--log-directory", type=Path, help="按日轮转日志，保留 30 天")
    args = parser.parse_args()
    if args.poll_seconds <= 0:
        parser.error("poll-seconds 必须大于 0")
    if args.log_directory:
        args.log_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        logger = logging.getLogger("rdos.runner")
        logger.setLevel(logging.INFO)
        handler = TimedRotatingFileHandler(args.log_directory / "runner.log", when="midnight", backupCount=30, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        sys.stdout, sys.stderr = _LogStream(logger, logging.INFO), _LogStream(logger, logging.ERROR)
    try:
        run(args.config, args.poll_seconds)
    except (ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
    except KeyboardInterrupt:
        print("\nRunner 已停止", flush=True)


if __name__ == "__main__":
    main()
