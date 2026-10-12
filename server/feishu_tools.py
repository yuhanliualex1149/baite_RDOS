"""Small, scoped Feishu tool layer for the central RDOS trial.

Feishu owns shared content.  These tools read it back after a write and never
retry a mutation blindly.  Role checks and method-draft approval live in the
calling service; the model is not given a generic CLI or arbitrary API tool.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

BASE_TOKEN = "E34dbYDEXaCRnDsMQDBcMvyMnjb"
TABLE_ID = "tblvJU3CwPt6eAaw"
FEEDBACK_DOC = "RoxidRoZXocXSnxR0tlc4aCMnof"
ONBOARDING_DOCS = (
    "A1FrdvwproG7axxh6jtcOnk5nPd",
    "H1TmdAL4Eo3O1zxx1kXctet2nsd",
    "NmEtd50lcopox4xuRG3c3Ovenxd",
)
RAG_FOLDER = "KQjyf6KrxlZ24MdkHirc9TP0n2d"
SKILL_FOLDER = "ImWqfzWcvl5LWUdFmxKcLIfdntp"
DEFAULT_CLI = "/opt/homebrew/lib/node_modules/@larksuite/cli/bin/lark-cli"
FEISHU_ORIGIN = "https://scn33386s7ui.feishu.cn"
TASK_FIELDS = {
    "task_id": "任务ID", "stage": "阶段", "instructions": "任务说明",
    "version": "版本", "status": "发布状态", "nature": "任务性质",
    "project_id": "项目ID", "member_ids": "适用成员", "related_task": "关联任务",
    "operation_id": "写回标识",
    "task_kind": "任务类别",
}
ADDED_TASK_FIELDS = ("项目ID", "适用成员", "关联任务", "写回标识", "任务类别")


class FeishuError(RuntimeError):
    def __init__(self, code: str, message: str, object_id: str = "") -> None:
        self.code, self.message, self.object_id = code, message, object_id
        super().__init__(message)

    def as_dict(self) -> dict:
        return {"ok": False, "code": self.code, "message": self.message,
                "object_id": self.object_id}


def _text(value: Any) -> str:
    if isinstance(value, list):
        return ",".join(_text(item) for item in value)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or value.get("value") or "")
    return "" if value is None else str(value)


def _plain(content: str) -> str:
    # Used only for read-back verification across Markdown -> native doc rendering.
    content = re.sub(r"</?(?:a|b|i|s|u|em|strong|span|text|code)\b[^>]*>", "", content, flags=re.I)
    content = re.sub(r"<[^>]+>", " ", content)
    content = html.unescape(content)
    content = re.sub(r"(?m)^\s{0,3}(?:#{1,6}\s+|[-*+]\s+)", "", content)
    content = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", content)
    return " ".join(content.replace("**", "").replace("`", "").split())


class FeishuTools:
    def __init__(self, executable: str | None = None, timeout: int = 45,
                 sources: list[dict] | None = None, feedback_doc: str | None = None) -> None:
        candidate = executable or os.environ.get("BAITE_LARK_CLI") or DEFAULT_CLI
        self.executable = candidate if Path(candidate).is_file() else shutil.which(candidate)
        if not self.executable and not executable and not os.environ.get("BAITE_LARK_CLI"):
            self.executable = shutil.which("lark-cli")
        if not self.executable:
            raise FeishuError("not_configured", "找不到本机飞书 CLI，请检查 BAITE_LARK_CLI")
        self.timeout = min(max(int(timeout), 1), 45)
        self.base_token = os.environ.get("BAITE_CENTRAL_BASE_TOKEN", BASE_TOKEN)
        self.table_id = os.environ.get("BAITE_CENTRAL_TABLE_ID", TABLE_ID)
        self.feedback_doc = feedback_doc or os.environ.get("BAITE_CENTRAL_FEEDBACK_DOC", FEEDBACK_DOC)
        self.rag_folder = os.environ.get("BAITE_SELECTED_RAG_FOLDER_TOKEN", RAG_FOLDER)
        self.skill_folder = os.environ.get("BAITE_CENTRAL_SKILL_FOLDER_TOKEN", SKILL_FOLDER)
        self._sources = {token: {"kind": "docx", "depth": 0} for token in ONBOARDING_DOCS}
        self._sources[self.feedback_doc] = {"kind": "docx", "depth": 0}
        for token in (self.rag_folder, self.skill_folder):
            self._sources[token] = {"kind": "folder", "depth": 0}
        # These registrations come from trusted project configuration, not a
        # model argument or document instruction.  Each project gets an instance.
        for source in sources or []:
            token = str(source.get("id") or source.get("object_id") or "")
            kind = str(source.get("kind") or "")
            kind = "file" if kind == "markdown" else kind
            if not token or kind not in {"docx", "folder", "file"}:
                raise FeishuError("invalid_source", "登记资料缺少有效对象或类型", token)
            self._sources[token] = {**source, "kind": kind, "depth": 0, "registered": True}
        self._roots = [token for token, source in self._sources.items() if source["kind"] == "folder"]

    @property
    def task_url(self) -> str:
        return f"{FEISHU_ORIGIN}/base/{self.base_token}?table={self.table_id}"

    def _run(self, args: list[str], object_id: str = "", cwd: str | None = None, recover_auth: bool = True) -> dict:
        try:
            result = subprocess.run([self.executable, *args], capture_output=True,
                                    text=True, timeout=self.timeout, check=False, cwd=cwd)
        except subprocess.TimeoutExpired as exc:
            raise FeishuError("timeout", "飞书操作超时；写入操作需要先回读核对", object_id) from exc
        except OSError as exc:
            raise FeishuError("cli_unavailable", "无法启动本机飞书 CLI", object_id) from exc
        try:
            payload = json.loads(result.stdout)
        except (ValueError, TypeError):
            payload = {}
        if result.returncode or not isinstance(payload, dict) or payload.get("ok") is False or payload.get("code", 0) != 0:
            diagnostic = (result.stderr + result.stdout).lower()
            # The CLI writes structured API errors to stderr after a progress
            # line. Keep only the numeric code; never expose the raw response.
            remote_code = None
            for output in (result.stderr, result.stdout):
                start = output.find("{")
                if start < 0:
                    continue
                try:
                    error_data = json.loads(output[start:])
                    remote_code = (error_data.get("error") or {}).get("code") or error_data.get("code")
                except (ValueError, AttributeError, TypeError):
                    continue
                if remote_code:
                    break
            auth_error = any(term in diagnostic for term in ("unauthorized", "forbidden", "oauth", "expired", "999916", "999914", "permission denied"))
            readonly = tuple(args[:2]) in {
                ("docs", "+fetch"), ("markdown", "+fetch"),
                ("base", "+record-list"), ("base", "+field-list"),
            } or args[:3] == ["drive", "files", "list"]
            if auth_error and readonly and recover_auth:
                # Revalidate only the existing grant.  Never log status payloads,
                # expand scopes, or repeat a mutation after an auth error.
                try:
                    status = self._run(["auth", "status", "--verify"], object_id, recover_auth=False)
                    verified = status.get("verified") is True or (isinstance(status.get("data"), dict) and status["data"].get("verified") is True)
                except FeishuError:
                    verified = False
                if verified:
                    return self._run(args, object_id, cwd, recover_auth=False)
            if args[:3] == ["drive", "files", "list"] and remote_code == 1061002:
                raise FeishuError("folder_request_rejected", "飞书拒绝文件夹读取请求（错误码 1061002）", object_id)
            raise FeishuError("authorization_failed" if auth_error else "remote_failed",
                              "飞书授权失效或范围不足" if auth_error else
                              "飞书操作失败" + (f"（错误码 {remote_code}）" if isinstance(remote_code, int) else "") + "，请检查本机授权和对象状态",
                              object_id)
        if not payload:
            raise FeishuError("invalid_response", "飞书没有返回有效 JSON", object_id)
        return payload

    @staticmethod
    def _data(payload: dict) -> dict:
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            raise FeishuError("invalid_response", "飞书返回内容格式不正确")
        return data

    def _check_scope(self, object_id: str, kind: str) -> dict:
        source = self._sources.get(object_id)
        normalized = "docx" if kind in ("document", "doc", "onboarding", "feedback") else kind
        if normalized == "markdown":
            normalized = "file"
        if not source or source["kind"] != normalized:
            raise FeishuError("out_of_scope", "对象不在本轮已登记的资料范围内", object_id)
        return source

    def _base_args(self, command: str) -> list[str]:
        return ["base", command, "--base-token", self.base_token, "--table-id", self.table_id, "--as", "user"]

    @staticmethod
    def _normalize_task(record_id: str, fields: dict, source_url: str) -> dict:
        task = {key: _text(fields.get(name)) for key, name in TASK_FIELDS.items()}
        task["member_ids"] = [item.strip() for item in re.split(r"[,，;；\n]", task["member_ids"]) if item.strip()]
        if task["task_kind"] not in {"work", "onboarding"}:
            task["task_kind"] = "onboarding" if re.match(r"^(?:ONB-|LOCAL-.*-REFRESH-|LOCAL-DEEPSEEK-ONB-)", task["task_id"]) else "work"
        if task["task_kind"] == "work":
            task["stage"] = ""
        task.update(record_id=record_id, source_url=source_url, fields=fields)
        return task

    def list_tasks(self) -> dict:
        items, offset = [], 0
        for _ in range(20):
            data = self._data(self._run(self._base_args("+record-list") + ["--format", "json", "--limit", "200", "--offset", str(offset)], self.table_id))
            rows = data.get("items") or data.get("records")
            if rows is None:
                rows = [{"record_id": record_id, "fields": dict(zip(data.get("fields", []), row))}
                        for record_id, row in zip(data.get("record_id_list", []), data.get("data", []))]
            if not isinstance(rows, list):
                raise FeishuError("invalid_response", "无法识别飞书任务记录", self.table_id)
            items.extend(self._normalize_task(str(row.get("record_id") or row.get("id") or ""), row.get("fields", {}), self.task_url) for row in rows)
            if not data.get("has_more"):
                return {"ok": True, "object_id": self.table_id, "source_url": self.task_url, "items": items}
            if not rows:
                break
            offset += len(rows)
        raise FeishuError("read_limit", "任务表超过本轮读取上限，未完成读取", self.table_id)

    def _field_names(self) -> set[str]:
        data = self._data(self._run(self._base_args("+field-list") + ["--limit", "100"], self.table_id))
        fields = data.get("fields", data.get("items", []))
        if data.get("has_more") or data.get("total", len(fields)) > 100:
            raise FeishuError("read_limit", "任务表字段过多，未完成核对", self.table_id)
        return {str(field.get("name", field.get("field_name", ""))) for field in fields}

    def ensure_task_fields(self) -> dict:
        names, created = self._field_names(), []
        for name in ADDED_TASK_FIELDS:
            if name in names:
                continue
            error = None
            try:
                self._run(self._base_args("+field-create") + ["--json", json.dumps({"name": name, "type": "text"}, ensure_ascii=False)], self.table_id)
            except FeishuError as exc:
                error = exc
            try:
                names = self._field_names()
                if name not in names:
                    # Feishu may expose the newly created schema on its next read.
                    # Re-read only: never repeat the creation after an uncertain response.
                    names = self._field_names()
            except FeishuError as exc:
                raise FeishuError("write_uncertain", "字段写入结果未能回读，请核对后再试", self.table_id) from exc
            if name not in names:
                raise error or FeishuError("write_unverified", "字段写入未通过远端核对", self.table_id)
            created.append(name)
        return {"ok": True, "object_id": self.table_id, "source_url": self.task_url, "created": created}

    def read_document(self, token: str) -> dict:
        source = self._check_scope(token, "docx")
        data = self._data(self._run(["docs", "+fetch", "--api-version", "v2", "--as", "user", "--doc", token, "--format", "json"], token))
        document = data.get("document", data)
        content = document.get("content", document.get("markdown"))
        if not isinstance(content, str):
            raise FeishuError("invalid_response", "文档没有可读正文", token)
        title_match = re.search(r"<title>(.*?)</title>", content, re.S)
        return {"ok": True, "object_id": token, "kind": "docx", "source_url": source.get("source_url") or f"{FEISHU_ORIGIN}/docx/{token}",
                "title": document.get("title") or (html.unescape(title_match[1]) if title_match else ""),
                "content": content, "content_hash": hashlib.sha256(content.encode()).hexdigest(),
                "revision": document.get("revision_id")}

    def read_markdown(self, token: str) -> dict:
        self._check_scope(token, "file")
        source = self._sources[token]
        if not source.get("registered") and not str(source.get("name", "")).lower().endswith(".md"):
            raise FeishuError("unsupported_source", "首版只读取文档和 Markdown 正文", token)
        data = self._data(self._run(["markdown", "+fetch", "--file-token", token, "--as", "user", "--format", "json"], token))
        content = data.get("content")
        if not isinstance(content, str):
            raise FeishuError("invalid_response", "Markdown 文件没有可读正文", token)
        return {"ok": True, "object_id": token, "kind": "markdown", "source_url": f"{FEISHU_ORIGIN}/file/{token}",
                "title": data.get("file_name") or source.get("name", ""), "content": content,
                "content_hash": hashlib.sha256(content.encode()).hexdigest()}

    def list_folder(self, token: str) -> dict:
        source = self._check_scope(token, "folder")
        folder_url = source.get("source_url") or f"{FEISHU_ORIGIN}/drive/folder/{token}"
        origin = folder_url.split("/drive/folder/", 1)[0]
        if source["depth"] >= 3:
            raise FeishuError("read_limit", "只读取已登记文件夹的三层子目录", token)
        data = self._data(self._run(["drive", "files", "list", "--as", "user", "--page-all", "--page-limit", "5",
                                    "--params", json.dumps({"folder_token": token, "page_size": 200}), "--format", "json"], token))
        if data.get("has_more"):
            raise FeishuError("read_limit", "文件夹超过读取上限，未完成读取", token)
        items = []
        for item in data.get("files", data.get("items", [])):
            object_id = str(item.get("token") or item.get("file_token") or "")
            kind = str(item.get("type") or item.get("file_type") or "")
            name = str(item.get("name") or item.get("title") or "")
            if object_id and item.get("parent_token", token) == token:
                path = "drive/folder" if kind == "folder" else "docx" if kind == "docx" else "file"
                child_url = f"{origin}/{path}/{object_id}"
                self._sources[object_id] = {"kind": kind, "name": name, "depth": source["depth"] + 1, "source_url": child_url}
                items.append({"object_id": object_id, "id": object_id, "token": object_id,
                              "kind": kind, "type": kind, "title": name, "name": name,
                              "source_url": child_url, "modified_time": item.get("modified_time")})
        return {"ok": True, "object_id": token, "source_url": folder_url, "items": items}

    def read_source(self, object_id: str, kind: str) -> dict:
        if kind in ("tasks", "task", "table") and object_id == self.table_id:
            return self.list_tasks()
        if object_id not in self._sources:
            self._discover_source(object_id)
        self._check_scope(object_id, kind)
        if kind == "folder":
            return self.list_folder(object_id)
        if kind in ("file", "markdown"):
            return self.read_markdown(object_id)
        return self.read_document(object_id)

    def _discover_source(self, object_id: str) -> None:
        # Restarted workers prove membership using only trusted registered roots.
        folders, seen = list(self._roots), set()
        while folders and len(seen) < 40:
            token = folders.pop(0)
            if token in seen or self._sources[token]["depth"] >= 3:
                continue
            seen.add(token)
            for item in self.list_folder(token)["items"]:
                if item["object_id"] == object_id:
                    return
                if item["kind"] == "folder":
                    folders.append(item["object_id"])
        if folders:
            raise FeishuError("read_limit", "资料范围核对达到四十个文件夹上限", object_id)
        raise FeishuError("out_of_scope", "对象不在本轮已登记的资料范围内", object_id)

    def read_registered_source(self, object_id: str, kind: str) -> dict:
        return self.read_source(object_id, kind)

    def find_project_feedback(self, folder_token: str, title: str, operation_id: str) -> dict | None:
        """Read the registered folder for this operation's feedback document.

        This is deliberately read-only so an uncertain creation can be checked
        without sending another create request.
        """
        self._check_scope(folder_token, "folder")
        self._marker(operation_id)
        title = title.strip()
        if not title or len(title) > 150:
            raise FeishuError("invalid_title", "项目反馈标题须为一至一百五十个字符", folder_token)
        marker = f"[RDOS 项目反馈标识:{operation_id}]"
        remote_title = title + " · " + hashlib.sha256(operation_id.encode()).hexdigest()[:12]
        documents = [item for item in self.list_folder(folder_token)["items"] if item["kind"] == "docx"]
        if len(documents) > 40:
            raise FeishuError("read_limit", "项目目录文档超过四十份，无法完成创建前去重核对", folder_token)
        found = []
        for item in documents:
            document = self.read_document(item["object_id"])
            if marker in document["content"]:
                found.append(document)
            elif item["name"] == remote_title:
                raise FeishuError("operation_conflict", "同名项目反馈缺少本次创建标识，请核对远端", item["object_id"])
        if len(found) > 1:
            raise FeishuError("operation_conflict", "同标识对应多份项目反馈，请核对远端", folder_token)
        return found[0] if found else None

    def ensure_project_feedback(self, folder_token: str, title: str, operation_id: str) -> dict:
        existing = self.find_project_feedback(folder_token, title, operation_id)
        if existing:
            return {**existing, "operation_id": operation_id, "status": "duplicate"}
        title = title.strip()
        marker = f"[RDOS 项目反馈标识:{operation_id}]"
        remote_title = title + " · " + hashlib.sha256(operation_id.encode()).hexdigest()[:12]
        content = (f"<title>{html.escape(remote_title)}</title>\n# {title}\n\n项目工作反馈入口。请保留已有记录并追加本次反馈，注明项目ID、成员ID、"
                   f"关联任务及版本（如有）、本次运行标识和身份性质。区分实际成果、自述与尚未验证。\n\n{marker}")
        error = None
        try:
            self._write_content(["docs", "+create", "--api-version", "v2", "--as", "user",
                                 "--parent-token", folder_token, "--doc-format", "markdown"],
                                "--content", content, folder_token)
        except FeishuError as exc:
            error = exc
        try:
            created = self.find_project_feedback(folder_token, title, operation_id)
            if not created:
                created = self.find_project_feedback(folder_token, title, operation_id)  # Read lag never causes a second creation.
        except FeishuError as exc:
            raise FeishuError("write_uncertain", "项目反馈创建结果无法回读；请先核对远端再重试", folder_token) from exc
        if not created:
            raise error or FeishuError("write_unverified", "项目反馈创建未通过目录和正文核对", folder_token)
        if _plain(content) not in _plain(created["content"]):
            raise FeishuError("write_unverified", "项目反馈正文未通过远端核对", created["object_id"])
        return {**created, "operation_id": operation_id, "status": "verified"}

    @staticmethod
    def _marker(operation_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", operation_id):
            raise FeishuError("invalid_operation", "写回标识只能使用字母、数字及 . _ : -")
        return f"[RDOS 写回标识:{operation_id}]"

    def save_task(self, fields: dict, operation_id: str, record_id: str | None = None) -> dict:
        self._marker(operation_id)
        changes = {}
        for key, value in fields.items():
            name = TASK_FIELDS.get(key, key)
            if name not in TASK_FIELDS.values():
                raise FeishuError("invalid_field", "任务包含未登记字段", self.table_id)
            changes[name] = ",".join(str(item) for item in value) if isinstance(value, list) and name == "适用成员" else value
        if "任务类别" in changes:
            if changes["任务类别"] not in {"work", "onboarding"}:
                raise FeishuError("invalid_task_kind", "任务类别须为 work 或 onboarding", self.table_id)
            if changes["任务类别"] == "work":
                # The live column is select.  New work needs no select value;
                # empty-string writes can create an invalid option remotely.
                changes.pop("阶段", None)
            elif "阶段" in changes and changes["阶段"] not in {"第一阶段", "第二阶段", "第三阶段"}:
                raise FeishuError("invalid_stage", "接入任务须指定第一、第二或第三阶段", self.table_id)
        changes["写回标识"] = operation_id
        before = self.list_tasks()["items"]
        target = next((task for task in before if task["record_id"] == record_id), None) if record_id else None
        if changes.get("任务类别") == "work" and target and target["task_kind"] == "onboarding":
            changes["阶段"] = None  # Explicitly clear only the converted task's stage.
        matching = [task for task in before if task["operation_id"] == operation_id]
        if matching:
            if len(matching) != 1 or any(_text(matching[0]["fields"].get(name)) != _text(value) for name, value in changes.items()):
                raise FeishuError("operation_conflict", "已有同标识任务与本次内容不一致", self.table_id)
            return self._task_result(matching[0], operation_id, "duplicate")
        if record_id and target is None:
            raise FeishuError("out_of_scope", "未找到需要修改的现有任务", record_id)
        args = self._base_args("+record-upsert") + ["--json", json.dumps(changes, ensure_ascii=False)]
        if record_id:
            args += ["--record-id", record_id]
        error = None
        try:
            self._run(args, record_id or self.table_id)
        except FeishuError as exc:
            error = exc
        # The table can briefly return its pre-write snapshot after a successful
        # upsert. Re-read at most twice; never repeat the mutation. The caller's
        # run deadline still bounds how long it waits for these read-only checks.
        for delay in (0, 0.5, 1):
            if delay:
                time.sleep(delay)
            try:
                matching = [task for task in self.list_tasks()["items"] if task["operation_id"] == operation_id]
            except FeishuError as exc:
                raise FeishuError("write_uncertain", "任务写入结果无法回读；请先核对远端再重试", record_id or self.table_id) from exc
            if len(matching) == 1 and all(_text(matching[0]["fields"].get(name)) == _text(value) for name, value in changes.items()):
                return self._task_result(matching[0], operation_id, "verified")
        if not matching:
            raise error or FeishuError("write_unverified", "任务写入未通过远端核对", self.table_id)
        if len(matching) != 1 or any(_text(matching[0]["fields"].get(name)) != _text(value) for name, value in changes.items()):
            raise FeishuError("write_unverified", "任务回读内容与写入不一致", self.table_id)

    @staticmethod
    def _task_result(task: dict, operation_id: str, status: str) -> dict:
        return {"ok": True, "object_id": task["record_id"], "record_id": task["record_id"],
                "source_url": task["source_url"], "operation_id": operation_id, "status": status, "task": task}

    def _write_content(self, args: list[str], flag: str, content: str, object_id: str) -> None:
        # Avoid passing private work content in process arguments or error logs.
        with tempfile.TemporaryDirectory(prefix="baite-central-feishu-") as temporary:
            source = Path(temporary) / "content.md"
            source.write_text(content, encoding="utf-8")
            # CLI file inputs must be relative to its working directory.
            self._run(args + [flag, "@content.md"], object_id, cwd=temporary)

    def append_feedback(self, content: str, operation_id: str) -> dict:
        marker = self._marker(operation_id)
        before = self.read_document(self.feedback_doc)
        if marker in before["content"]:
            if _plain(content) not in _plain(before["content"]):
                raise FeishuError("operation_conflict", "同标识反馈与本次正文不一致", self.feedback_doc)
            return {**before, "operation_id": operation_id, "status": "duplicate"}
        error = None
        try:
            self._write_content(["docs", "+update", "--api-version", "v2", "--as", "user", "--doc", self.feedback_doc,
                                 "--command", "append", "--doc-format", "markdown", "--revision-id", str(before.get("revision") or -1)],
                                "--content", content + "\n\n" + marker, self.feedback_doc)
        except FeishuError as exc:
            error = exc
        try:
            after = self.read_document(self.feedback_doc)
        except FeishuError as exc:
            raise FeishuError("write_uncertain", "反馈写入结果无法回读；请先核对远端再重试", self.feedback_doc) from exc
        if marker not in after["content"]:
            raise error or FeishuError("write_unverified", "反馈写回标识未能回读", self.feedback_doc)
        if _plain(content) not in _plain(after["content"]) or _plain(before["content"]) not in _plain(after["content"]):
            raise FeishuError("write_unverified", "反馈正文或已有内容未通过远端核对", self.feedback_doc)
        return {**after, "operation_id": operation_id, "status": "verified"}

    def publish_patch(self, kind: str, object_id: str, base_hash: str, old_text: str, new_text: str, operation_id: str) -> dict:
        marker = self._marker(operation_id)
        before = self.read_source(object_id, kind)
        content = before.get("content", "")
        if marker in content:
            if _plain(new_text + "\n\n" + marker) not in _plain(content):
                raise FeishuError("operation_conflict", "同标识资料修改与本次正文不一致", object_id)
            return {**before, "operation_id": operation_id, "status": "duplicate"}
        if before.get("content_hash") != base_hash:
            raise FeishuError("source_changed", "来源正文已变化，请重读并重新审阅草稿", object_id)
        if old_text and content.count(old_text) != 1:
            raise FeishuError("patch_conflict", "修改位置必须在当前正文中精确出现一次", object_id)
        replacement = new_text + "\n\n" + marker
        expected = content.replace(old_text, replacement, 1) if old_text else content + "\n\n" + replacement
        error = None
        try:
            if before["kind"] == "markdown":
                self._write_content(["markdown", "+overwrite", "--as", "user", "--file-token", object_id, "--format", "json"], "--content", expected, object_id)
            elif before["kind"] == "docx":
                args = ["docs", "+update", "--api-version", "v2", "--as", "user", "--doc", object_id,
                        "--doc-format", "markdown", "--revision-id", str(before.get("revision") or -1)]
                args += ["--command", "str_replace", "--pattern", re.escape(old_text)] if old_text else ["--command", "append"]
                self._write_content(args, "--content", replacement, object_id)
            else:
                raise FeishuError("unsupported_source", "当前对象不支持正文修改", object_id)
        except FeishuError as exc:
            error = exc
        try:
            after = self.read_source(object_id, kind)
        except FeishuError as exc:
            raise FeishuError("write_uncertain", "资料发布结果无法回读；请先核对远端再重试", object_id) from exc
        if marker not in after.get("content", ""):
            raise error or FeishuError("write_unverified", "资料修改未能回读确认", object_id)
        expected_matches = after["content"] == expected if before["kind"] == "markdown" else _plain(after["content"]) == _plain(expected)
        if not expected_matches:
            raise FeishuError("write_unverified", "回读资料与确认的修改不一致，请检查远端正文", object_id)
        return {**after, "operation_id": operation_id, "status": "verified"}
