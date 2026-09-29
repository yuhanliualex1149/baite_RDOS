from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import tempfile
import threading
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Protocol, Tuple

from server import db


class _LarkMarkdownParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.cell: List[str] | None = None
        self.row: List[str] = []
        self.table_row_index = 0

    def _append(self, value: str) -> None:
        (self.cell if self.cell is not None else self.parts).append(value)

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, str | None]]) -> None:
        if tag == "title":
            self._append("\n# ")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._append(f"\n{'#' * int(tag[1])} ")
        elif tag in {"p", "div"} and self.cell is None:
            self._append("\n")
        elif tag == "br":
            self._append("\n")
        elif tag in {"b", "strong"}:
            self._append("**")
        elif tag in {"i", "em"}:
            self._append("*")
        elif tag == "li":
            self._append("\n- ")
        elif tag == "table":
            self._append("\n")
            self.table_row_index = 0
        elif tag == "tr":
            self.row = []
        elif tag in {"td", "th"}:
            self.cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"b", "strong"}:
            self._append("**")
        elif tag in {"i", "em"}:
            self._append("*")
        elif tag in {"td", "th"} and self.cell is not None:
            text = " ".join("".join(self.cell).split()).replace("|", "\\|")
            self.row.append(text)
            self.cell = None
        elif tag == "tr" and self.row:
            self.parts.append("| " + " | ".join(self.row) + " |\n")
            if self.table_row_index == 0:
                self.parts.append("| " + " | ".join("---" for _ in self.row) + " |\n")
            self.table_row_index += 1
        elif tag in {"title", "h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "table"}:
            self._append("\n")

    def handle_data(self, data: str) -> None:
        self._append(data)

    def markdown(self) -> str:
        lines = [line.rstrip() for line in "".join(self.parts).splitlines()]
        compact: List[str] = []
        for line in lines:
            if line or not compact or compact[-1]:
                compact.append(line)
        return "\n".join(compact).strip() + "\n"


def lark_content_to_markdown(content: str) -> str:
    if "<" not in content or ">" not in content:
        return content.rstrip() + "\n"
    parser = _LarkMarkdownParser()
    parser.feed(content)
    return parser.markdown()


class RagSource(Protocol):
    def fetch(self, folder_token: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]: ...


class FeishuRagSource:
    """Read the configured Feishu folder without modifying it."""

    def __init__(self, executable: str = "lark-cli") -> None:
        resolved = shutil.which(executable)
        if not resolved:
            raise RuntimeError("找不到 lark-cli，无法读取飞书文件夹")
        self.executable = resolved

    def _run(self, args: List[str], cwd: Path | None = None) -> Any:
        completed = subprocess.run(
            [self.executable, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=cwd,
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            if "auth" in message.lower() or "token" in message.lower():
                raise RuntimeError(f"飞书 OAuth 需要重新登录：{message[:1000]}")
            raise RuntimeError(f"飞书读取失败：{message[:1000]}")
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("飞书命令没有返回有效 JSON") from exc

    @staticmethod
    def _items(payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        data = payload.get("data", payload)
        if isinstance(data, dict):
            for key in ("files", "items"):
                if isinstance(data.get(key), list):
                    return data[key]
        for key in ("files", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
        return []

    @staticmethod
    def _doc_markdown(payload: Any) -> str:
        if isinstance(payload, str):
            return lark_content_to_markdown(payload)
        if not isinstance(payload, dict):
            raise RuntimeError("飞书文档转换结果格式未知")
        data = payload.get("data", payload)
        if isinstance(data, str):
            return lark_content_to_markdown(data)
        if isinstance(data, dict):
            for key in ("markdown", "content"):
                value = data.get(key)
                if isinstance(value, str):
                    return lark_content_to_markdown(value)
            document = data.get("document")
            if isinstance(document, dict) and isinstance(document.get("content"), str):
                return lark_content_to_markdown(document["content"])
        for key in ("markdown", "content"):
            value = payload.get(key)
            if isinstance(value, str):
                return lark_content_to_markdown(value)
        raise RuntimeError("飞书文档没有可用的 Markdown 内容")

    @staticmethod
    def _source_url(item: Dict[str, Any]) -> str:
        return str(item.get("url") or item.get("source_url") or "")

    def _download_markdown(self, token: str, name: str) -> str:
        with tempfile.TemporaryDirectory(prefix="baite-rag-") as temporary:
            destination = Path(temporary) / Path(name).name
            self._run(
                [
                    "drive",
                    "+download",
                    "--as",
                    "user",
                    "--file-token",
                    token,
                    "--output",
                    destination.name,
                    "--overwrite",
                ],
                cwd=Path(temporary),
            )
            if not destination.is_file():
                raise RuntimeError(f"飞书文件下载后不存在：{name}")
            return destination.read_text(encoding="utf-8")

    def _fetch_docx(self, token: str) -> str:
        payload = self._run(
            [
                "docs",
                "+fetch",
                "--api-version",
                "v2",
                "--as",
                "user",
                "--doc",
                token,
                "--format",
                "json",
            ]
        )
        return self._doc_markdown(payload)

    def fetch(self, folder_token: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        return self._fetch_folder(folder_token, allow_empty=False)

    def fetch_project(
        self, folder_token: str
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        return self._fetch_folder(
            folder_token,
            allow_empty=True,
            reserved_folder_name="RDOS 项目进展",
        )

    def download_raw(self, file_token: str) -> bytes:
        with tempfile.TemporaryDirectory(prefix="baite-workflow-") as temporary:
            destination = Path(temporary) / "workflow-source"
            self._run(
                [
                    "drive",
                    "+download",
                    "--as",
                    "user",
                    "--file-token",
                    file_token,
                    "--output",
                    destination.name,
                    "--overwrite",
                ],
                cwd=Path(temporary),
            )
            if not destination.is_file():
                raise RuntimeError("飞书工作流来源下载后不存在")
            return destination.read_bytes()

    def _fetch_folder(
        self,
        folder_token: str,
        allow_empty: bool,
        reserved_folder_name: str = "",
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        params = json.dumps(
            {
                "folder_token": folder_token,
                "page_size": 200,
                "order_by": "EditedTime",
                "direction": "DESC",
            }
        )
        payload = self._run(
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
        listed = self._items(payload)
        if not listed:
            if allow_empty:
                return [], []
            raise RuntimeError("飞书文件夹为空，或当前账号无权读取")

        files: List[Dict[str, Any]] = []
        ignored: List[Dict[str, Any]] = []
        runtime_names: set[str] = set()
        for item in listed:
            source_type = str(item.get("type") or item.get("file_type") or "unknown")
            source_name = str(item.get("name") or item.get("title") or "未命名")
            token = str(item.get("token") or item.get("file_token") or "")
            base = {
                "source_token": token,
                "source_type": source_type,
                "source_name": source_name,
                "source_url": self._source_url(item),
                "modified_time": str(
                    item.get("modified_time")
                    or item.get("edited_time")
                    or item.get("modified_at")
                    or ""
                ),
            }
            if source_type == "folder":
                status = (
                    "reserved_progress_folder"
                    if reserved_folder_name and source_name == reserved_folder_name
                    else "subfolder_ignored"
                )
                ignored.append({**base, "status": status})
                continue
            if source_type == "file" and source_name.lower().endswith(".md"):
                runtime_name = Path(source_name).name
                content = self._download_markdown(token, source_name)
            elif source_type == "docx":
                runtime_name = f"{Path(source_name).name}.md"
                content = self._fetch_docx(token)
            else:
                ignored.append({**base, "status": "unsupported"})
                continue
            normalized_name = runtime_name.casefold()
            if normalized_name in runtime_names:
                raise RuntimeError(f"Runtime 文件重名冲突：{runtime_name}")
            runtime_names.add(normalized_name)
            content_bytes = content.encode("utf-8")
            files.append(
                {
                    **base,
                    "runtime_name": runtime_name,
                    "content_hash": hashlib.sha256(content_bytes).hexdigest(),
                    "content": content,
                    "size_bytes": len(content_bytes),
                }
            )
        if not files and not allow_empty:
            raise RuntimeError("文件夹中没有可进入 Runtime 的 Markdown 或飞书文档")
        return files, ignored


_SYNC_LOCK = threading.Lock()


def _manifest(files: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str]:
    manifest = []
    runtime_names: set[str] = set()
    for item in sorted(files, key=lambda value: value["runtime_name"].casefold()):
        runtime_name = str(item["runtime_name"])
        normalized = runtime_name.casefold()
        if Path(runtime_name).name != runtime_name:
            raise RuntimeError(f"Runtime 文件名不安全：{runtime_name}")
        if normalized in runtime_names:
            raise RuntimeError(f"Runtime 文件重名冲突：{runtime_name}")
        runtime_names.add(normalized)
        encoded = str(item["content"]).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != item["content_hash"]:
            raise RuntimeError(f"内容 Hash 异常：{runtime_name}")
        if len(encoded) != int(item["size_bytes"]):
            raise RuntimeError(f"内容大小异常：{runtime_name}")
        manifest.append(
            {
                key: item[key]
                for key in (
                    "source_token",
                    "source_type",
                    "source_name",
                    "runtime_name",
                    "source_url",
                    "modified_time",
                    "content_hash",
                    "size_bytes",
                )
            }
        )
    content_state = [
        {
            "source_token": item["source_token"],
            "source_type": item["source_type"],
            "runtime_name": item["runtime_name"],
            "content_hash": item["content_hash"],
        }
        for item in manifest
    ]
    encoded = json.dumps(
        content_state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return manifest, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def sync_selected_rag(source: RagSource | None = None, manual: bool = False) -> Dict[str, Any]:
    if not _SYNC_LOCK.acquire(blocking=False):
        return {"ok": False, "busy": True, "state": db.rag_state()}
    try:
        if manual:
            db.reset_rag_retry()
        state = db.rag_state()
        folder_token = str(state.get("folder_token") or "").strip()
        if not folder_token:
            raise RuntimeError("未配置 BAITE_SELECTED_RAG_FOLDER_TOKEN")
        files, ignored = (source or FeishuRagSource()).fetch(folder_token)
        manifest, manifest_hash = _manifest(files)
        result = db.commit_rag_snapshot(
            folder_token, manifest_hash, manifest, files, ignored
        )
        return {"ok": True, **result}
    except Exception as exc:
        state = db.record_rag_failure(str(exc))
        return {"ok": False, "error": str(exc), "state": state}
    finally:
        _SYNC_LOCK.release()


async def scheduler(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            if db.rag_sync_due():
                await asyncio.to_thread(sync_selected_rag)
        except Exception:
            # The sync function records actionable errors; the scheduler must stay alive.
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=15)
        except asyncio.TimeoutError:
            continue
