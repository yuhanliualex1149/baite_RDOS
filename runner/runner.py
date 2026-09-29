from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo


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
    if not all(config.values()):
        raise ValueError("Runner 配置字段不能为空")
    workspace = Path(config["workspace"]).expanduser()
    if not workspace.is_absolute():
        raise ValueError("workspace 必须是绝对路径")
    config["workspace"] = str(workspace.resolve())
    return config


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def safe_filename(name: str, fallback: str) -> str:
    if Path(name).name != name:
        raise ValueError(f"不安全的文件名：{name}")
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


def _switch_shared_link(workspace: Path, shared_directory: Path) -> None:
    current = workspace / "shared"
    if current.exists() and not current.is_symlink():
        legacy = workspace / ".runner" / f"legacy-shared-{int(time.time())}"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        os.replace(current, legacy)
    temporary = workspace / ".shared-next"
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    os.symlink(shared_directory, temporary, target_is_directory=True)
    os.replace(temporary, current)


def apply_snapshot(workspace: Path, runner_id: str, snapshot: Dict[str, Any]) -> Path:
    revision = int(snapshot["revision"])
    releases = workspace / ".runner" / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"revision-{revision}-", dir=releases))
    try:
        shared = staging / "shared"
        (shared / "skills").mkdir(parents=True)
        (shared / "selected_rag").mkdir(parents=True)
        write_atomic(shared / "global_rules.md", snapshot["global_rules"]["content"])

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
        final = releases / f"revision-{revision}-{int(time.time() * 1000)}"
        os.replace(staging, final)
        _switch_shared_link(workspace, final / "shared")
        write_atomic(
            workspace / "control" / "collaboration.md",
            render_collaborations(runner_id, snapshot["collaborations"]),
        )
        write_atomic(
            workspace / "control" / "skill_proposals.md",
            render_skill_proposals(snapshot.get("skill_proposals", [])),
        )
        write_atomic(
            workspace / "control" / "sync.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
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
    project_id = str(project["id"])
    root = _project_path(workspace, project_id)
    control = root / "control"
    work = root / "work"
    files_root = work / "files"
    releases = workspace / ".runner" / "project-releases" / project_id
    control.mkdir(parents=True, exist_ok=True)
    files_root.mkdir(parents=True, exist_ok=True)
    releases.mkdir(parents=True, exist_ok=True)

    staging = Path(
        tempfile.mkdtemp(prefix=f"revision-{project['revision']}-", dir=releases)
    )
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
        final = releases / f"revision-{project['revision']}-{int(time.time() * 1000)}"
        os.replace(staging, final)
        _switch_shared_link(root, final / "shared")

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
        ended = control / "membership_ended.md"
        if ended.exists():
            ended.unlink()
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
                root / "control" / "membership_ended.md",
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
            if not response.get("ack"):
                raise RuntimeError("Control Panel 未返回 ACK")
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


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_project_files(project_root: Path, notes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    files_root = project_root / "work" / "files"
    if files_root.is_symlink():
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
        names[:] = [name for name in names if not (base / name).is_symlink()]
        for filename in filenames:
            path = base / filename
            if path.is_symlink() or not path.is_file():
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
    report_basis = {
        "runner_id": config["runner_id"],
        "project_id": project["id"],
        "date": _project_local_date(project),
        "status_hash": status_payload["source_hash"],
        "file_manifest": manifest,
    }
    event_id = "project-" + hashlib.sha256(
        json.dumps(report_basis, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    payload = {
        "event_id": event_id,
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
    response = api_request(config, "/api/runner/events", "POST", payload)
    if not response.get("ack"):
        raise RuntimeError("项目日报未获得 Control Panel ACK")
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
    return response


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
    state = _load_state(workspace)
    state.setdefault("project_token", "")
    state.setdefault("active_projects", [])
    state.setdefault("project_status_hashes", {})
    state.setdefault("project_manifest_hashes", {})
    state.setdefault("project_daily_reports", {})
    project_cache: List[Dict[str, Any]] = []
    # Always fetch the complete project assignment once after process start.
    known_project_token = ""
    print(
        f"[{config['runner_id']}] Runner 已启动，Workspace: {workspace}",
        flush=True,
    )
    while True:
        try:
            api_request(config, "/api/runner/heartbeat", "POST")
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
                incoming = project_snapshot.get("projects", [])
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

            for project in project_cache:
                project_root = _project_path(workspace, project["id"])
                current_status = load_project_status(project_root)
                status_changed = (
                    current_status["source_hash"] != "missing"
                    and state["project_status_hashes"].get(project["id"])
                    != current_status["source_hash"]
                )
                if status_changed or project_daily_report_due(project, state):
                    try:
                        submit_project_report(config, workspace, project, state)
                        print(
                            f"[{config['runner_id']}] 已回传项目日报：{project['name']}",
                            flush=True,
                        )
                    except urllib.error.HTTPError as exc:
                        detail = exc.read().decode("utf-8", errors="replace")
                        print(
                            f"[{config['runner_id']}] 项目日报失败：HTTP {exc.code} {detail}",
                            file=sys.stderr,
                            flush=True,
                        )
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Baite AI R&D OS Local Runner")
    parser.add_argument("--config", type=Path, required=True, help="Runner 配置 JSON")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()
    try:
        run(args.config, args.poll_seconds)
    except (ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
    except KeyboardInterrupt:
        print("\nRunner 已停止", flush=True)


if __name__ == "__main__":
    main()
