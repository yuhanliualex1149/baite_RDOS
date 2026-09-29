from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Protocol, Tuple

from server import db
from server.rag_sync import FeishuRagSource, _manifest, external_sync_disabled


PROGRESS_FOLDER_NAME = "RDOS 项目进展"
_PROJECT_SYNC_LOCK = threading.Lock()
_EXPORT_LOCK = threading.Lock()


class ProjectSource(Protocol):
    def fetch_project(
        self, folder_token: str
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]: ...

    def download_raw(self, file_token: str) -> bytes: ...


class ProjectWriter(Protocol):
    def ensure_progress_folder(self, parent_token: str) -> Tuple[str, str]: ...

    def write_markdown(self, folder_token: str, filename: str, content: str) -> None: ...


class FeishuProjectWriter:
    """Write only server-generated progress Markdown to the reserved folder."""

    def __init__(self, source: FeishuRagSource | None = None) -> None:
        self.source = source or FeishuRagSource()

    @staticmethod
    def _token(payload: Any) -> str:
        candidates = [payload]
        if isinstance(payload, dict):
            candidates.extend(value for value in payload.values() if isinstance(value, dict))
            data = payload.get("data")
            if isinstance(data, dict):
                candidates.extend(value for value in data.values() if isinstance(value, dict))
        for item in candidates:
            if not isinstance(item, dict):
                continue
            for key in ("token", "folder_token", "file_token"):
                if item.get(key):
                    return str(item[key])
        return ""

    def ensure_progress_folder(self, parent_token: str) -> Tuple[str, str]:
        params = json.dumps({"folder_token": parent_token, "page_size": 200})
        payload = self.source._run(
            [
                "drive",
                "files",
                "list",
                "--as",
                "user",
                "--page-all",
                "--params",
                params,
                "--format",
                "json",
            ]
        )
        matches = [
            item
            for item in self.source._items(payload)
            if str(item.get("type") or item.get("file_type")) == "folder"
            and str(item.get("name") or item.get("title")) == PROGRESS_FOLDER_NAME
        ]
        if len(matches) > 1:
            raise RuntimeError("飞书项目文件夹中存在多个同名 RDOS 项目进展目录")
        if matches:
            token = str(matches[0].get("token") or matches[0].get("file_token") or "")
            if not token:
                raise RuntimeError("无法识别 RDOS 项目进展目录 Token")
            return token, str(matches[0].get("url") or "")
        created = self.source._run(
            [
                "drive",
                "+create-folder",
                "--as",
                "user",
                "--folder-token",
                parent_token,
                "--name",
                PROGRESS_FOLDER_NAME,
            ]
        )
        token = self._token(created)
        if not token:
            raise RuntimeError("飞书已创建进展目录，但返回结果中没有 Token")
        return token, ""

    def write_markdown(self, folder_token: str, filename: str, content: str) -> None:
        if Path(filename).name != filename or not filename.endswith(".md"):
            raise ValueError("进度文件名不安全")
        with tempfile.TemporaryDirectory(prefix="baite-project-progress-") as temporary:
            root = Path(temporary)
            (root / filename).write_text(content, encoding="utf-8")
            self.source._run(
                [
                    "drive",
                    "+push",
                    "--as",
                    "user",
                    "--folder-token",
                    folder_token,
                    "--local-dir",
                    ".",
                    "--if-exists",
                    "overwrite",
                    "--on-duplicate-remote",
                    "fail",
                ],
                cwd=root,
            )


def _project_manifest(
    files: List[Dict[str, Any]], ignored: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], str]:
    manifest, _ = _manifest(files)
    ignored_state = [
        {
            key: (
                ""
                if key == "modified_time"
                and item.get("status") == "reserved_progress_folder"
                else item.get(key, "")
            )
            for key in (
                "source_token",
                "source_type",
                "source_name",
                "source_url",
                "modified_time",
                "status",
            )
        }
        for item in sorted(
            ignored,
            key=lambda value: (
                str(value.get("source_name", "")).casefold(),
                str(value.get("source_token", "")),
            ),
        )
    ]
    encoded = json.dumps(
        {"runtime": manifest, "listed_only": ignored_state},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return manifest, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def sync_project(
    project_id: str, source: ProjectSource | None = None, manual: bool = False
) -> Dict[str, Any]:
    if source is None and external_sync_disabled():
        return {"ok": False, "disabled": True, "error": "外部同步已关闭"}
    if not _PROJECT_SYNC_LOCK.acquire(blocking=False):
        return {"ok": False, "busy": True}
    try:
        if manual:
            db.retry_project_sync(project_id)
        project = db.get_project(project_id)
        if not project:
            raise LookupError("project_not_found")
        actual_source = source or FeishuRagSource()
        files, ignored = actual_source.fetch_project(project["feishu_folder_token"])
        manifest, manifest_hash = _project_manifest(files, ignored)
        result = db.commit_project_snapshot(
            project_id, manifest_hash, manifest, files, ignored
        )
        return {"ok": True, **result}
    except Exception as exc:
        try:
            state = db.record_project_sync_failure(project_id, str(exc))
        except LookupError:
            state = None
        return {"ok": False, "error": str(exc), "project": state}
    finally:
        _PROJECT_SYNC_LOCK.release()


def export_pending_progress(writer: ProjectWriter | None = None) -> List[Dict[str, Any]]:
    if writer is None and external_sync_disabled():
        return [{"ok": False, "disabled": True, "error": "外部同步已关闭"}]
    if not _EXPORT_LOCK.acquire(blocking=False):
        return []
    results: List[Dict[str, Any]] = []
    try:
        actual_writer = writer or FeishuProjectWriter()
        for item in db.pending_project_exports():
            try:
                folder_token = item["progress_folder_token"]
                if not folder_token:
                    folder_token, folder_url = actual_writer.ensure_progress_folder(
                        item["feishu_folder_token"]
                    )
                    db.set_project_progress_folder(
                        item["project_id"], folder_token, folder_url
                    )
                content = db.project_progress_markdown(
                    item["project_id"], item["runner_id"]
                )
                actual_writer.write_markdown(
                    folder_token,
                    f"{item['runner_id']}.md",
                    content,
                )
                db.record_project_export_success(item["project_id"], item["runner_id"])
                results.append({"ok": True, **item})
            except Exception as exc:
                db.record_project_export_failure(
                    item["project_id"], item["runner_id"], str(exc)
                )
                results.append({"ok": False, "error": str(exc), **item})
        return results
    finally:
        _EXPORT_LOCK.release()


def check_workflow_reference(source: ProjectSource | None = None) -> Dict[str, Any]:
    if source is None and external_sync_disabled():
        return {"ok": False, "disabled": True, "error": "外部同步已关闭"}
    state = db.workflow_reference_state()
    try:
        content = (source or FeishuRagSource()).download_raw(state["source_token"])
        return {"ok": True, "state": db.record_workflow_check(hashlib.sha256(content).hexdigest())}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "state": db.record_workflow_check_failure(str(exc))}


async def scheduler(stop: asyncio.Event) -> None:
    while not stop.is_set():
        if os.environ.get("BAITE_DISABLE_EXTERNAL_SYNC", "false").lower() not in (
            "1",
            "true",
            "yes",
        ):
            for project_id in db.due_project_ids():
                await asyncio.to_thread(sync_project, project_id)
            if db.workflow_check_due():
                await asyncio.to_thread(check_workflow_reference)
            if db.pending_project_exports():
                await asyncio.to_thread(export_pending_progress)
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except asyncio.TimeoutError:
            continue
