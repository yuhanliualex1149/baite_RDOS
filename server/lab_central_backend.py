"""Explicit, persistent local_mock provider for central phase-1 exercises.

Only the transport boundary is replaced. FeishuTools still performs its normal
write/independent-read verification, and CentralAgent still runs its tool loop.
This module never contacts Feishu, a model provider, a CLI, or node clients.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any

import httpx

from lab.guard import deny, require_active
from server import central_store as store

PROJECT_IDS = ("lab_project_phase0", "lab_project_phase1_b")
_KEY = object()
_configuration: dict | None = None
_lock = threading.RLock()
_registry = {
    PROJECT_IDS[0]: {"document": "lab_doc_intro", "feedback": "lab_feedback_phase1_a", "folder": "lab_folder_phase1_a"},
    PROJECT_IDS[1]: {"document": "lab_doc_phase1_b", "feedback": "lab_feedback_phase1_b", "folder": "lab_folder_phase1_b"},
}


def configure(cfg: dict) -> None:
    """Called by the launcher after config.load, before the audit hook.

    Read back the validated on-disk configuration here so merely passing a
    model argument or an arbitrary mapping cannot enable this provider.
    """
    from lab import config
    root = Path(cfg.get("root", ""))
    checked = config.load(root)
    if checked != cfg:
        raise ValueError("本地模拟配置与已验证配置不一致")
    instance = config.instance(checked)
    global _configuration
    _configuration = {"root": str(root), "runtime": str(root / "runtime"),
                      "env_id": checked["env_id"], "generation": instance["generation"],
                      "base_url": f"http://127.0.0.1:{checked['port']}"}


def _active(channel: str = "legacy_feishu_factory") -> dict:
    try:
        require_active()
    except RuntimeError:
        deny(channel)
    cfg = _configuration
    if not cfg or any(os.environ.get(name) != cfg[key] for name, key in (
        ("RDOS_LAB_ROOT", "root"), ("RDOS_LAB_RUNTIME", "runtime"),
        ("RDOS_LAB_ENV_ID", "env_id"), ("RDOS_LAB_GENERATION", "generation"))):
        deny(channel)
    return cfg


def _error(code: str, message: str, object_id: str = ""):
    from server.feishu_tools import FeishuError
    raise FeishuError(code, message, object_id)


def _project(project_id: str, *, organization: bool = False) -> None:
    if project_id not in PROJECT_IDS and not (organization and project_id == ""):
        _error("out_of_scope", "对象不在登记的合成项目中", project_id)


def _url(object_id: str, kind: str = "docx") -> str:
    return _active()["base_url"] + "/" + kind + "/" + object_id


def seed() -> None:
    """Idempotently register synthetic resources, preserving all remote edits."""
    _active()
    from server.feishu_tools import TASK_FIELDS
    with _lock:
        for index, project_id in enumerate(PROJECT_IDS):
            identifiers = _registry[project_id]
            project = store.get("project", project_id) or {
                "id": project_id, "name": "RDOS 阶段 1 合成项目 B", "status": "active",
                "description": "仅供跨项目隔离验证的纯合成项目。", "identity_mode": "role_test",
                "member_ids": ["mem_lab_codex_0001", "mem_lab_workbuddy_0001"]}
            project.update(feedback_id=identifiers["feedback"], feedback_url=_url(identifiers["feedback"]),
                           folder_token=identifiers["folder"], folder_url=_url(identifiers["folder"], "drive/folder"))
            store.put("project", project_id, project)
            for purpose in ("document", "feedback"):
                object_id = identifiers[purpose]
                title = ("阶段 0 合成介绍材料" if index == 0 else "阶段 1 项目 B 合成材料") if purpose == "document" else "阶段 1 合成反馈 " + ("A" if index == 0 else "B")
                content = ("# " + title + "\n\n这是纯合成 local_mock 材料。只验证中央工具的读取、草稿和确认后写入。\n"
                           "测试约定：明确区分自述、读回证据与真实客户端验收。") if purpose == "document" else (
                           "# " + title + "\n\n## 项目反馈：local_mock 合成样例\n记录类型：project_update\n"
                           f"项目ID：{project_id}\n成员ID：mem_lab_codex_0001\n身份性质：role_test\n"
                           f"反馈ID：lab_seed_feedback_{index}\n本次运行标识：lab_seed_{index}\n"
                           "实际工作：这是一条由模拟后端预置的合成自述，没有运行任何真实客户端。\n"
                           "本期成果：仅提供中央反馈解析的合成样例，不代表成员成果。\n"
                           "问题与需求：现有测试任务需要补充独立读回证据。")
                if not store.get("lab_remote_document", object_id):
                    store.put("lab_remote_document", object_id, {"id": object_id, "project_id": project_id,
                        "kind": "docx", "title": title, "content": content, "revision": 1})
                source = store.get("source", object_id) or {}
                source.update(id=object_id, name=title, kind="docx", project_id=project_id,
                              source_url=_url(object_id), watch=purpose == "document", backend="local_mock",
                              description="已登记纯合成资源；不对应任何正式飞书对象。")
                store.put("source", object_id, source)
            if index == 1 and not store.get("task", "lab_task_phase1_b"):
                store.put("task", "lab_task_phase1_b", {"id": "lab_task_phase1_b", "task_id": "lab_task_phase1_b",
                    "project_id": project_id, "member_ids": ["mem_lab_codex_0001"], "version": "1",
                    "status": "已发布", "nature": "一次性", "task_kind": "work",
                    "instructions": "纯合成 B 项目任务：读取本项目材料并记录证据。"})
        for task in store.items("task"):
            if task.get("project_id") not in PROJECT_IDS:
                continue
            task_id = task.get("task_id") or task["id"]
            if not store.get("lab_remote_task", task_id):
                fields = {name: ",".join(task.get(key, [])) if key == "member_ids" else task.get(key, "")
                          for key, name in TASK_FIELDS.items()}
                store.put("lab_remote_task", task_id, {"id": task_id, "project_id": task["project_id"],
                    "record_id": "rec_" + task_id, "fields": fields})


def _bump(project_id: str, key: str) -> None:
    item = store.get("lab_remote_stats", project_id) or {"id": project_id}
    item[key] = item.get(key, 0) + 1
    store.put("lab_remote_stats", project_id, item)


def stats(project_id: str) -> dict:
    _active()
    _project(project_id, organization=True)
    return store.get("lab_remote_stats", project_id) or {"id": project_id}


def set_fault(project_id: str, point: str, code: str = "remote_failed", count: int = 1,
              *, skip: int = 0) -> None:
    """Trusted test facility, never exposed as an HTTP/model tool.

    read_*_after_write faults activate only after the next matching mutation.
    *_after_commit models a remote commit whose response was lost.
    """
    _active()
    _project(project_id, organization=True)
    allowed = {"read_tasks", "read_document", "write_task", "write_document",
               "write_task_after_commit", "write_document_after_commit",
               "read_tasks_after_write", "read_document_after_write"}
    if point not in allowed or type(count) is not int or count < 0 or type(skip) is not int or skip < 0:
        raise ValueError("无效模拟故障点或次数")
    with _lock:
        store.put("lab_remote_fault", project_id + ":" + point,
                  {"id": project_id + ":" + point, "code": code, "remaining": count, "skip": skip})


def set_delay(project_id: str, point: str, seconds: float, count: int = 1) -> None:
    """Bounded local worker delay for cancellation tests; no network or retry."""
    _active()
    _project(project_id)
    if (point not in {"read_tasks", "read_document", "write_task", "write_document"}
            or type(seconds) not in {int, float} or not 0 < seconds <= 1
            or type(count) is not int or not 0 <= count <= 10):
        raise ValueError("模拟延迟只允许登记读写点、每次一秒以内及有限次数")
    with _lock:
        key = project_id + ":" + point
        store.put("lab_remote_delay", key, {"id": key, "seconds": seconds, "remaining": count})


def _delay(project_id: str, point: str) -> None:
    key = project_id + ":" + point
    item = store.get("lab_remote_delay", key)
    if item and item["remaining"]:
        item["remaining"] -= 1
        store.put("lab_remote_delay", key, item)
        _bump(project_id, point + "_delay_started")
        time.sleep(item["seconds"])


def _fault(project_id: str, point: str) -> None:
    key = project_id + ":" + point
    item = store.get("lab_remote_fault", key)
    if item and item["remaining"]:
        if item.get("skip", 0):
            item["skip"] -= 1
            store.put("lab_remote_fault", key, item)
            return
        item["remaining"] -= 1
        store.put("lab_remote_fault", key, item)
        _error(item["code"], "local_mock 注入故障：" + point, project_id)


def _arm_read_fault(project_id: str, point: str) -> None:
    key = project_id + ":" + point + "_after_write"
    item = store.get("lab_remote_fault", key)
    if item and item["remaining"]:
        store.put("lab_remote_fault", project_id + ":" + point,
                  {**item, "id": project_id + ":" + point})
        store.remove("lab_remote_fault", key)


class _LocalToolsTransport:
    def __init__(self, project_id: str, sources: list[dict] | None, key: object):
        if key is not _KEY:
            deny("untrusted_local_tools_transport")
        self._key, self.project_id = key, project_id
        self.base_token, self.table_id = "lab_task_store", "lab_task_table"
        self.base_url = _active()["base_url"]
        self.feedback_doc = _registry[project_id]["feedback"] if project_id else ""
        # Organization context has no implicit project resources.
        registry = _registry.get(project_id, {})
        self.sources = [{"id": object_id, "kind": "folder" if purpose == "folder" else "docx",
                         "depth": 0, "registered": True,
                         "source_url": _url(object_id, "drive/folder" if purpose == "folder" else "docx")}
                        for purpose, object_id in registry.items()]
        allowed = {source["id"]: source for source in self.sources}
        for source in sources or []:
            object_id = source.get("id") or source.get("object_id")
            if object_id not in allowed or source.get("kind") not in {allowed[object_id]["kind"], "local_mock"}:
                _error("out_of_scope", "不能登记未批准的模拟对象", str(object_id))

    def _document(self, object_id: str) -> dict:
        if object_id not in {_registry.get(self.project_id, {}).get("document"), self.feedback_doc}:
            _error("out_of_scope", "对象不属于本次合成项目", object_id)
        document = store.get("lab_remote_document", object_id)
        if not document or document["project_id"] != self.project_id:
            _error("out_of_scope", "未登记模拟文档", object_id)
        return document

    @staticmethod
    def _arg(args: list[str], name: str, default: str = "") -> str:
        return args[args.index(name) + 1] if name in args else default

    def run(self, args: list[str], object_id: str = "") -> dict:
        _active()
        with _lock:
            command = tuple(args[:2])
            if command in {("base", "+record-list"), ("base", "+field-list"), ("base", "+record-upsert")}:
                if self._arg(args, "--base-token") != self.base_token or self._arg(args, "--table-id") != self.table_id:
                    _error("out_of_scope", "未登记模拟任务表", object_id)
            if command == ("base", "+record-list"):
                _delay(self.project_id, "read_tasks")
                _fault(self.project_id, "read_tasks")
                _bump(self.project_id, "task_reads")
                return {"data": {"items": [{"record_id": t["record_id"], "fields": t["fields"]}
                    for t in store.items("lab_remote_task") if self.project_id and t["project_id"] == self.project_id], "has_more": False}}
            if command == ("base", "+field-list"):
                from server.feishu_tools import TASK_FIELDS
                return {"data": {"items": [{"name": name} for name in TASK_FIELDS.values()], "has_more": False}}
            if command == ("base", "+record-upsert"):
                return self._write_task(args)
            if command == ("docs", "+fetch"):
                if self._arg(args, "--doc") != object_id:
                    _error("out_of_scope", "文档参数与对象不一致", object_id)
                document = self._document(object_id)
                _delay(self.project_id, "read_document")
                _fault(self.project_id, "read_document")
                _bump(self.project_id, "document_reads")
                return {"data": {"document": {"title": document["title"], "content": document["content"], "revision_id": document["revision"]}}}
            if args[:3] == ["drive", "files", "list"]:
                if object_id != _registry.get(self.project_id, {}).get("folder"):
                    _error("out_of_scope", "未登记模拟文件夹", object_id)
                registered_documents = {_registry[self.project_id]["document"], self.feedback_doc}
                return {"data": {"files": [{"token": d["id"], "name": d["title"], "type": "docx", "parent_token": object_id}
                    for d in store.items("lab_remote_document") if d["project_id"] == self.project_id and d["id"] in registered_documents], "has_more": False}}
            _error("unsupported_source", "此模拟后端未实现该外部能力", object_id)

    def _write_task(self, args: list[str]) -> dict:
        _project(self.project_id)
        changes = json.loads(self._arg(args, "--json"))
        record_id = self._arg(args, "--record-id")
        existing = next((t for t in store.items("lab_remote_task") if t["record_id"] == record_id), None) if record_id else None
        if record_id and (not existing or existing["project_id"] != self.project_id):
            _error("out_of_scope", "任务不属于本项目", record_id)
        if changes.get("项目ID", self.project_id) != self.project_id:
            _error("out_of_scope", "不能跨项目写任务", record_id)
        members = [m for m in str(changes.get("适用成员", "")).split(",") if m]
        if any(m not in (store.get("project", self.project_id) or {}).get("member_ids", []) for m in members):
            _error("out_of_scope", "成员不属于本合成项目", record_id)
        task_id = existing["id"] if existing else changes.get("任务ID", "")
        if not task_id or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", task_id):
            _error("invalid_field", "任务标识无效", record_id)
        collision = store.get("lab_remote_task", task_id)
        if not existing and collision:
            _error("operation_conflict", "已有同标识合成任务", task_id)
        if existing and changes.get("任务ID", task_id) != task_id:
            _error("out_of_scope", "不能变更既有任务标识", task_id)
        _delay(self.project_id, "write_task")
        _fault(self.project_id, "write_task")
        row = existing or {"id": task_id, "record_id": "rec_" + task_id, "project_id": self.project_id, "fields": {}}
        row["fields"].update(changes)
        row["fields"]["项目ID"] = self.project_id
        store.put("lab_remote_task", task_id, row)
        _bump(self.project_id, "task_writes")
        _arm_read_fault(self.project_id, "read_tasks")
        _fault(self.project_id, "write_task_after_commit")
        return {"data": {"record_id": row["record_id"]}}

    def write_content(self, args: list[str], content: str, object_id: str) -> None:
        _active()
        with _lock:
            document = self._document(object_id)
            if tuple(args[:2]) != ("docs", "+update") or self._arg(args, "--doc") != object_id:
                _error("unsupported_source", "此模拟后端只允许既有文档更新", object_id)
            if self._arg(args, "--revision-id") != str(document["revision"]):
                _error("source_changed", "模拟文档版本已变化", object_id)
            command = self._arg(args, "--command")
            if command == "append":
                updated = document["content"] + "\n\n" + content
            elif command == "str_replace":
                pattern = self._arg(args, "--pattern")
                if len(re.findall(pattern, document["content"])) != 1:
                    _error("patch_conflict", "修改位置并非唯一", object_id)
                updated = re.sub(pattern, lambda _: content, document["content"], count=1)
            else:
                _error("unsupported_source", "模拟文档更新方式未实现", object_id)
            _delay(self.project_id, "write_document")
            _fault(self.project_id, "write_document")
            document.update(content=updated, revision=document["revision"] + 1)
            store.put("lab_remote_document", object_id, document)
            _bump(self.project_id, "document_writes")
            _arm_read_fault(self.project_id, "read_document")
            _fault(self.project_id, "write_document_after_commit")


def trusted_tools(value: Any) -> bool:
    return type(value) is _LocalToolsTransport and value._key is _KEY


def make_tools(project_id: str, sources: list[dict] | None = None):
    _active()
    _project(project_id, organization=True)
    from server.feishu_tools import FeishuTools
    return FeishuTools(_local_backend=_LocalToolsTransport(project_id, sources, _KEY))


def tool_message(name: str, arguments: dict, call_id: str = "local_mock_call") -> dict:
    return {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}


def model_stage_content(message: dict) -> str:
    """Inspect only the trusted stage block in the complete system message.

    This is a deterministic provider fixture adapter, not an authorization
    parser. User materials cannot become a stage by containing this marker.
    """
    if message.get('role') != 'system' or not isinstance(message.get('content'), str):
        return ''
    text = message['content']
    if '<current_stage>\n' in text and text.endswith('\n</current_stage>'):
        return text.rsplit('<current_stage>\n', 1)[1].rsplit('\n</current_stage>', 1)[0]
    return text


class _LocalModelTransport(httpx.AsyncBaseTransport):
    def __init__(self, script: list | None, key: object):
        if key is not _KEY:
            deny("untrusted_local_model_transport")
        self._key, self.script, self.index = key, copy.deepcopy(script), 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        _active("real_central_model_http")
        if str(request.url) != "http://local-mock.invalid/chat/completions" or request.method != "POST":
            deny("untrusted_local_model_request")
        payload = json.loads(request.content)
        if (self.script is None and any(model_stage_content(m).startswith('RDOS_REQUEST_INTERPRETER_V1')
                                       for m in payload.get('messages', []))):
            # A separate deterministic provider response, not a business-loop
            # step. It grants no task action and must not consume that script.
            return httpx.Response(200, json={'model': 'local-deterministic-mock', 'choices': [{
                'message': {'role': 'assistant', 'content': self._default(payload)},
                'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 0, 'completion_tokens': 0}}, request=request)
        if self.script is None:
            item = self._default(payload)
        else:
            item = self.script[self.index] if self.index < len(self.script) else "local_mock 脚本结束；没有额外执行。"
        self.index += 1
        if isinstance(item, dict) and "http_status" in item:
            if "raw_body" in item:
                return httpx.Response(item["http_status"], content=item["raw_body"], request=request)
            return httpx.Response(item["http_status"], json=item.get("body", {}), request=request)
        if isinstance(item, dict) and "choices" in item:
            return httpx.Response(200, json=item, request=request)
        message = {"role": "assistant", "content": item} if isinstance(item, str) else item
        return httpx.Response(200, json={"model": "local-deterministic-mock", "choices": [{
            "message": message, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0}}, request=request)

    def _default(self, payload: dict) -> dict | str:
        messages = payload.get("messages", [])
        if any(model_stage_content(m).startswith('RDOS_REQUEST_INTERPRETER_V1') for m in messages):
            return json.dumps({'purpose': 'discuss', 'task_actions': [], 'memory_changes': []})
        context: dict = {}
        trusted_context: dict = {}
        for message in messages:
            text = model_stage_content(message) if message.get('role') == 'system' else message.get("content", "")
            if message.get("role") in {"system", "user"} and isinstance(text, str):
                start = text.find("{")
                if start >= 0:
                    try:
                        candidate, _ = json.JSONDecoder().raw_decode(text[start:])
                    except ValueError:
                        continue
                    if isinstance(candidate, dict) and ("project" in candidate or "trusted_context" in candidate):
                        context = candidate.get("trusted_context", candidate)
                    if (message.get("role") == "system" and text.startswith("可信执行属性")
                            and isinstance(candidate, dict)):
                        trusted_context = candidate
        project = context.get("project") or {}
        project_id = trusted_context.get("project_id", project.get("id") or context.get("project_id", ""))
        if project_id in ("lab_project_phase3_collab", "project_rdos"):
            results = [m for m in messages if m.get("role") == "tool"]
            if self.index and results:
                try:
                    latest = json.loads(results[-1].get("content", ""))
                except (ValueError, TypeError):
                    latest = {}
                if not isinstance(latest, dict) or not latest.get("ok"):
                    return "模拟模型读取失败；请查看运行记录，不将失败报告为已同步。"
            if self.index == 0:
                return tool_message("phase3_read_workspace", {}, "mock_phase3_workspace")
            if self.index == 1:
                return tool_message("phase3_list_tasks", {}, "mock_phase3_tasks")
            return ("模拟模型已读取阶段3登记任务与协作缓存。待本人确认的问题：目标读者是谁、两个场景分别解决什么问题、交付格式及验收条件是什么。"
                    "这份问题清单是固定模拟响应，只验证受控工具编排；请在协作与任务页审阅具体要求并确认动作。尚未证明真实模型理解或节点执行。")
        if project_id not in PROJECT_IDS:
            return "当前使用 local_mock 确定性模拟模型。请先选择一个测试项目，再读取测试资料或准备任务草稿。"
        tool_results = [m for m in messages if m.get("role") == "tool"]
        if self.index and tool_results:
            try:
                latest_result = json.loads(tool_results[-1].get("content", ""))
            except (ValueError, TypeError):
                latest_result = {}
            if not isinstance(latest_result, dict) or not latest_result.get("ok"):
                return "local_mock 模拟工具返回失败；请查看运行记录，当前不能报告为已完成或已发布。"
        # Count only this transport's requests; prior conversation tool results
        # cannot skip the current read/prepare sequence.
        prompt = next((str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"), "")
        wants_draft = any(word in prompt for word in ("草稿", "起草", "更新", "调整"))
        if self.index == 0:
            return tool_message("read_feedback", {"project_id": project_id}, "mock_read_feedback")
        if wants_draft and self.index == 1:
            return tool_message("list_tasks", {"project_id": project_id}, "mock_list_tasks")
        if wants_draft and self.index == 2:
            tasks = context.get("tasks", [])
            for result in reversed(tool_results):
                try:
                    value = json.loads(result.get("content", ""))
                except (ValueError, TypeError):
                    continue
                candidate = value.get("items", value.get("tasks", [])) if isinstance(value, dict) else []
                if candidate and isinstance(candidate[0], dict) and candidate[0].get("task_id"):
                    tasks = candidate
                    break
            task = next((t for t in tasks if t.get("project_id") == project_id and t.get("status") == "已发布"), None)
            if task:
                return tool_message("prepare_task", {"project_id": project_id, "task_id": task["task_id"],
                    "draft_action": "update", "instructions": task["instructions"] + "\n\n合成补充要求：独立读回本次结果并记录证据；真实客户端验收仍待验证。",
                    "member_ids": task.get("member_ids", []), "reason": "local_mock：根据合成反馈补充独立读回要求。"}, "mock_prepare_task")
        if not wants_draft and self.index == 1:
            return tool_message("read_material", {"project_id": project_id, "object_id": _registry[project_id]["document"], "kind": "docx"}, "mock_read_material")
        return ("local_mock 确定性模拟：已通过工具读取本项目合成反馈并准备既有任务更新草稿。请在任务草稿中审阅并确认；当前尚未发布，真实客户端仍待验证。" if wants_draft else
                "local_mock 确定性模拟：已通过工具读取本项目合成反馈和测试资料。材料仅供本地核心验证；真实飞书、模型及两个客户端尚未验证。")


def trusted_model(value: Any) -> bool:
    return type(value) is _LocalModelTransport and value._key is _KEY


def make_agent(script: list | None = None):
    _active("real_central_model_run")
    from server.central_agent import CentralAgent
    return CentralAgent(api_key="", base_url="http://local-mock.invalid", model="local-deterministic-mock",
                        transport=_LocalModelTransport(script, _KEY))
