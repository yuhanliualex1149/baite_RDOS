#!/usr/bin/env python3
"""RDOS business execution checkpoints. Uses an existing Feishu CLI connection."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys

try:
    import rdos_rules as rules
except ImportError:  # source checkout; released packages include this same file
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from server import rdos_rules as rules


class WorkflowError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def fail(code, message):
    raise WorkflowError(code, message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(value):
    return hashlib.sha256((value if isinstance(value, str) else canonical(value)).encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{3,200}", value):
        fail("invalid_identifier", "登记对象标识无效")
    return value


def project_entries(manifest):
    """The old single-project manifest remains a valid registration."""
    if "projects" in manifest:
        return manifest["projects"]
    return [{"id": manifest["project_id"], "status": "active", "node_ids": [manifest["node_id"]],
             "zones": manifest.get("zones", {}), "registered_resources": manifest.get("registered_resources", [])}]


def manifest_value(value):
    if not isinstance(value, dict) or value.get("protocol_version") != 2:
        fail("invalid_manifest", "请从当前接入页取得版本 2 登记文件")
    for key in ("workspace_id", "project_id", "node_id", "base_token"):
        identifier(value.get(key))
    for key in ("tasks_table", "events_table"):
        value[key] = identifier(value.get(key) or value.get(key + "_id"))
    if value["tasks_table"] == value["events_table"]:
        fail("invalid_manifest", "任务登记和协作记录必须是不同的表")
    value.setdefault("registered_resources", value.get("documents", []))
    value.setdefault("zones", {})
    if not isinstance(value["registered_resources"], list) or not isinstance(value["zones"], dict):
        fail("invalid_manifest", "登记资料与产物位置格式无效")
    projects = project_entries(value)
    if not isinstance(projects, list): fail("invalid_manifest", "登记项目格式无效")
    seen = set()
    for project in projects:
        if not isinstance(project, dict): fail("invalid_manifest", "登记项目格式无效")
        project_id = identifier(project.get("id"))
        if project_id in seen: fail("invalid_manifest", "登记项目标识重复")
        seen.add(project_id)
        if project.get("status") not in ("active", "deactivating", "inactive"):
            fail("invalid_manifest", "登记项目状态无效")
        nodes, zones, resources = project.get("node_ids"), project.get("zones"), project.get("registered_resources", [])
        if (not isinstance(nodes, list) or not isinstance(zones, dict) or not isinstance(resources, list)
            or any(not isinstance(item, dict) for item in resources)):
            fail("invalid_manifest", "登记项目的节点、资料或位置格式无效")
        for node_id in nodes: identifier(node_id)
        for zone in zones.values():
            if not isinstance(zone, dict): fail("invalid_manifest", "登记项目位置格式无效")
            identifier(zone.get("folder_id"))
    return value


def installed_cli(binary=None):
    """Reuse the native executable shipped alongside an installed npm wrapper."""
    selected = binary or shutil.which("lark-cli") or shutil.which("feishu-cli")
    if not selected:
        fail("connection_missing", "未找到现有飞书 CLI；请按执行与连接说明完成连接")
    resolved = Path(selected).expanduser().resolve()
    if not resolved.is_file(): fail("connection_missing", "指定的现有飞书 CLI 不存在")
    if resolved.parent.name == "scripts" and resolved.suffix.lower() in (".js", ".cjs", ".mjs"):
        # Inspect only the resolved wrapper's own installed package, never scan
        # other directories, install dependencies or repair the user's Node.
        names = ("lark-cli.exe", "feishu-cli.exe") if os.name == "nt" else ("lark-cli", "feishu-cli")
        for name in names:
            native = resolved.parent.parent / "bin" / name
            if native.is_file() and os.access(native, os.X_OK): return str(native.resolve())
    return str(resolved)


class Feishu:
    """Fixed transport methods. Business decisions live in Workflow and rules."""
    def __init__(self, manifest, binary=None):
        self.manifest = manifest_value(manifest)
        self.binary = installed_cli(binary)
        self.peer_folders = set()

    def run(self, args, content=None):
        try:
            completed = subprocess.run([self.binary, *args], input=content, text=True,
                capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired):
            fail("connection_failed", "飞书操作未完成；写入操作须先核对，不直接重发")
        if completed.returncode:
            fail("connection_failed", "飞书 CLI 失败；请检查当前用户授权和登记对象访问权限")
        try:
            response = json.loads(completed.stdout)
        except ValueError:
            fail("invalid_response", "飞书 CLI 未返回可核对的 JSON")
        if not isinstance(response, dict): fail("invalid_response", "飞书 CLI 返回格式无效")
        if response.get("ok") is False or response.get("code", 0) not in (0, None):
            fail("connection_failed", "飞书拒绝操作；请检查授权，不扩大访问范围")
        data = response.get("data", response)
        if not isinstance(data, dict): fail("invalid_response", "飞书操作缺少可核对的对象正文")
        return data

    def table_args(self, table):
        if table not in (self.manifest["tasks_table"], self.manifest["events_table"]):
            fail("out_of_scope", "表不在登记范围")
        return ["--as", "user", "--base-token", self.manifest["base_token"], "--table-id", table]

    @staticmethod
    def rows(data):
        items = data.get("items", data.get("records"))
        if items is not None:
            if not isinstance(items, list): fail("invalid_response", "记录格式无效")
            return items
        fields = [f.get("name", f.get("field_name", f.get("id"))) if isinstance(f, dict) else f
                  for f in data.get("fields", [])]
        ids, values = data.get("record_id_list", []), data.get("data", [])
        if len(ids) != len(values): fail("incomplete_read", "记录标识与正文数量不一致")
        return [{"record_id": rid, "fields": dict(zip(fields, cells))} for rid, cells in zip(ids, values)]

    def records(self, table):
        result, offset, revision = [], 0, None
        for _ in range(100):
            data = self.run(["base", "+record-list", *self.table_args(table), "--format", "json",
                             "--limit", "200", "--offset", str(offset)])
            current_revision = data.get("rev")
            if revision is not None and current_revision is not None and current_revision != revision:
                fail("incomplete_read", "分页期间表已变化，请重新完整检查")
            revision = current_revision
            rows = self.rows(data)
            result.extend(rows)
            if not data.get("has_more", len(rows) == 200): return result
            if not rows: fail("incomplete_read", "分页未前进，不能宣布无任务")
            offset += len(rows)
        fail("incomplete_read", "分页尚未结束，未推进检查状态")

    def get(self, table, rid):
        data = self.run(["base", "+record-get", *self.table_args(table), "--record-id", identifier(rid), "--format", "json"])
        if data.get("record"): return data["record"]
        if data.get("record_id") and "fields" in data: return data
        rows = self.rows(data)
        if len(rows) != 1 or rows[0].get("record_id") != rid: fail("readback_mismatch", "记录未独立读回")
        return rows[0]

    def append(self, fields):
        return self.run(["base", "+record-upsert", *self.table_args(self.manifest["events_table"]), "--json", canonical(fields)])

    def append_task(self, fields):
        return self.run(["base", "+record-upsert", *self.table_args(self.manifest["tasks_table"]), "--json", canonical(fields)])

    def root(self, folder):
        self.registered_folder(folder)
        result, page = [], ""
        for _ in range(100):
            params = {"folder_token": folder, "page_size": 200}
            if page: params["page_token"] = page
            data = self.run(["drive", "files", "list", "--as", "user", "--format", "json", "--params", canonical(params)])
            result.extend(data.get("files", data.get("items", [])))
            if not data.get("has_more"): return result
            new = data.get("next_page_token") or data.get("page_token")
            if not new or new == page: fail("incomplete_read", "产物目录分页未结束")
            page = new
        fail("incomplete_read", "产物目录分页尚未结束")

    def registered_folder(self, folder):
        allowed = {zone.get("folder_id") for project in project_entries(self.manifest)
            if self.manifest["node_id"] in project["node_ids"] for zone in project["zones"].values()}
        allowed.update(self.peer_folders)
        if folder not in allowed: fail("out_of_scope", "产物位置未登记")

    def fetch(self, document_id):
        data = self.run(["docs", "+fetch", "--api-version", "v2", "--as", "user", "--doc", identifier(document_id),
                         "--format", "json", "--doc-format", "markdown", "--detail", "simple", "--scope", "full"])
        doc = data.get("document", data)
        content, revision = doc.get("content", doc.get("markdown")), doc.get("revision_id", doc.get("revision"))
        if not isinstance(content, str) or revision is None: fail("incomplete_read", "缺少完整文档正文或版本")
        return {"document_id": document_id, "revision": revision, "content_hash": sha(content),
                "content": content, "title": doc.get("title", "")}

    def create(self, folder, title, content):
        self.registered_folder(folder)
        body = "<title>" + html.escape(title, quote=False) + "</title>\n\n" + content
        return self.run(["docs", "+create", "--api-version", "v2", "--as", "user", "--parent-token", folder,
                         "--doc-format", "markdown", "--content", "-"], body)


class State:
    def __init__(self, path):
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=70)
        os.chmod(path, 0o600)
        self.db.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS checkpoints (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        self.db.commit()

    @contextmanager
    def locked(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.commit()  # preserve dispatched evidence, including exceptions
            raise
        else: self.db.commit()

    def get(self, table, key):
        row = self.db.execute("SELECT payload FROM " + table + " WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, table, key, value):
        self.db.execute("INSERT INTO " + table + " VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (key, canonical(value)))

    def flush(self):
        self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")


try:
    from rdos_peer import PeerMixin
except ImportError:
    sys.path.insert(0,str(Path(__file__).parent))
    from rdos_peer import PeerMixin


class Workflow(PeerMixin):
    error_type = WorkflowError
    def __init__(self, manifest, transport, state):
        self.manifest, self.transport, self.state = manifest_value(manifest), transport, state

    def project(self, project_id):
        project = next((p for p in project_entries(self.manifest) if p["id"] == project_id
            and self.manifest["node_id"] in p["node_ids"]), None)
        if project is None: fail("unregistered_project", "该项目未登记为当前节点的工作范围")
        return project

    def scope(self, value):
        if (not isinstance(value, dict) or value.get("protocol_version") != 2
            or value.get("workspace_id") != self.manifest["workspace_id"]):
            fail("out_of_scope", "记录不属于当前登记工作空间和协议")
        identifier(value.get("project_id"))
        if value.get("dispatch_source")=="peer":return self._peer_project(value)
        # Cross-project receipts are scoped to one independently verified task.
        if value.get("task_refs"):
            try:return self.project(value["project_id"])
            except WorkflowError as exc:
                if exc.code!="unregistered_project":raise
                for reference in value["task_refs"]:
                    task=self.task(reference.get("task_id"))
                    if task.get("dispatch_source")!="peer" or task["project_id"]!=value["project_id"]:raise
                    if value.get("claimed_node_id") not in task["executor_node_ids"]:raise
                return self._peer_project(task)
        return self.project(value["project_id"])

    def plan(self, task, checkpoint):
        plan = rules.execution_plan(task, self.manifest["node_id"], checkpoint)
        project = self.scope(task)
        if (project["status"] != "active" and task.get("business_status") not in ("paused", "closed")
            and plan["assigned"]):
            plan.update(reason="project_" + project["status"],
                allowed_actions=["stop_new_work", "verify_existing_operation", "report_blocker"])
        return plan

    @staticmethod
    def cell(value):
        if isinstance(value, list): return "".join(v.get("text", "") if isinstance(v, dict) else str(v) for v in value)
        return "" if value is None else str(value)

    def decode(self, row, kind):
        if not isinstance(row, dict) or not isinstance(row.get("fields"), dict):
            fail("invalid_record", "协作记录格式无效，保留原文待核对")
        fields = row.get("fields", {})
        try: value = json.loads(self.cell(fields.get("记录数据")))
        except (ValueError, TypeError): fail("invalid_record", "协作记录正文格式无效，保留原文待核对")
        if (not isinstance(value, dict) or value.get("protocol_version") != 2
            or value.get("workspace_id") != self.manifest["workspace_id"]):
            fail("out_of_scope", "记录不属于当前登记工作空间和协议")
        identifier(value.get("project_id"))
        if not isinstance(value.get("operation_id"), str): fail("invalid_record", "记录缺少操作标识")
        expected = {"操作ID": value.get("operation_id", "")}
        if kind == "task":
            nodes = value.get("executor_node_ids")
            if (not isinstance(nodes, list) or any(not isinstance(n, str) for n in nodes)
                or not isinstance(value.get("title"), str)
                or type(value.get("requirements_version")) is not int or value["requirements_version"] < 1
                or type(value.get("record_revision")) is not int or value["record_revision"] < 1
                or value.get("business_status") not in ("draft", "active", "pending_review", "paused", "closed")):
                fail("invalid_record", "任务登记字段格式无效")
            identifier(value.get("task_id"))
            expected.update({"任务ID": value.get("task_id", ""), "标题": value.get("title", ""),
                "状态": value.get("business_status", ""), "执行节点": ", ".join(value.get("executor_node_ids", [])),
                "要求版本": str(value.get("requirements_version", "")), "记录修订": str(value.get("record_revision", ""))})
            # The server hash binds its private authoritative representation;
            # public node aliases are intentionally different. Treat it as an
            # opaque version token, never recompute it from the public copy.
            if not isinstance(value.get("requirements_hash"), str) or not value["requirements_hash"]:
                fail("task_tampered", "任务缺少要求版本指纹，停止执行")
        else:
            refs = value.get("task_refs")
            if (not isinstance(refs, list) or any(not isinstance(ref, dict) for ref in refs)
                or not isinstance(value.get("event_type"), str) or not isinstance(value.get("claimed_node_id"), str)):
                fail("invalid_record", "协作事件字段格式无效")
            identifier(value.get("id"))
            expected.update({"事件ID": value.get("id", ""), "类型": value.get("event_type", ""),
                "自报节点": value.get("claimed_node_id", ""), "任务引用": canonical(value.get("task_refs", []))})
        if any(self.cell(fields.get(key)) != desired for key, desired in expected.items()):
            fail("record_conflict", "可读字段与业务正文不一致")
        # A valid record in a different registered workspace project is simply
        # irrelevant here. Malformed rows were checked first and still surface.
        self.scope(value)
        return value, row.get("record_id")

    def read_rows(self, kind):
        table = self.manifest["tasks_table" if kind == "task" else "events_table"]
        valid, invalid = [], []
        for row in self.transport.records(table):
            try: valid.append(self.decode(row, kind))
            except WorkflowError as exc:
                if exc.code != "unregistered_project":
                    invalid.append({"record_id": row.get("record_id") if isinstance(row, dict) else None, "code": exc.code})
        return valid, invalid

    def task(self, task_id):
        rows, invalid = self.read_rows("task")
        matches = [v for v, rid in rows if v.get("task_id") == task_id]
        if not matches:
            fail("task_unavailable", "当前任务不可用；无有效任务不执行")
        try:task = rules.select_verified_task_versions(matches)
        except rules.RuleViolation as exc:fail(exc.code,str(exc))
        prior = self.state.get("checkpoints", task_id)
        if prior and prior.get("project_id", self.manifest["project_id"]) != task["project_id"]:
            fail("task_conflict", "任务标识与原项目检查点不一致")
        if prior and task.get("record_revision", 0) < prior.get("record_revision", 0):
            fail("task_rollback", "任务记录低于已观察版本")
        return task

    def checkpoint(self, task):
        key = task["task_id"]
        old = self.state.get("checkpoints", key) or {}
        period = rules.period_key(task)
        point = {"task_id": key, "project_id": task["project_id"], "requirements_version": task["requirements_version"],
                 "record_revision": task["record_revision"], "completed_steps": [],
                 "uncertain_operations": old.get("uncertain_operations", [])}
        if period is not None:
            point['period_key'] = period
        if (old.get("requirements_version") == task["requirements_version"]
                and (period is None or old.get('period_key') == period)):
            point["completed_steps"] = old.get("completed_steps", [])
            point["uncertain_operations"] = old.get("uncertain_operations", [])
        events, invalid = self.read_rows("event")
        for event, rid in events:
            if (event.get("project_id") == task["project_id"] and event.get("claimed_node_id") == self.manifest["node_id"]
                and {"task_id": key, "requirements_version": task["requirements_version"]} in event.get("task_refs", [])
                and (period is None or event.get('period_key') == period)
                and event.get("result_status") == "completed"):
                try:
                    rules.validate_result(event, task)
                    actual, actual_rid = self.decode(self.transport.get(self.manifest["events_table"], rid), "event")
                    if actual != event: fail("readback_mismatch", "结果记录独立读回已变化")
                    self.completion_evidence(event, task)
                except (rules.RuleViolation, WorkflowError) as exc:
                    point["uncertain_operations"] = sorted(set(point["uncertain_operations"] + [event.get("operation_id", "")]))
                    invalid.append({"record_id": rid, "code": exc.code})
                    continue
                point["completed_steps"] = sorted(set(point["completed_steps"] + ["submission_verified"]))
        return point, invalid

    def prepare(self, task_id, read_inputs=True):
        with self.state.locked():
            task = self.task(task_id)
            old = self.state.get("checkpoints", task_id)
            point, invalid = self.checkpoint(task)
            # Compute change against the previous observed requirement before replacing it.
            plan = self.plan(task, old or point)
            effective = self.plan(task, point)
            plan.update({k: v for k, v in effective.items() if k not in ("changed_requirements", "old_results_policy")})
            materials = []
            if read_inputs and "read_inputs" in plan["allowed_actions"]:
                for ref in task.get("input_refs", []): materials.append(self.document(ref, task, output=False))
            self.state.put("checkpoints", task_id, point)
            return {"task": task, "plan": plan, "materials": materials, "quarantined_records": invalid}

    def check(self):
        enrollment=self.continue_peer_enrollment()
        rows, invalid = self.read_rows("task")
        events, event_invalid = self.read_rows("event")  # fail incomplete reads before processing any task
        unique = {}
        for task, rid in rows:
            task_id = task.get("task_id")
            try:unique[task_id] = rules.select_verified_task_versions([unique[task_id],task]) if task_id in unique else task
            except rules.RuleViolation as exc:fail(exc.code,str(exc))
        results = []
        for task_id, task in unique.items():
            if self.manifest["node_id"] in task.get("executor_node_ids", []): results.append(self.prepare(task_id, False))
        return {"node_id": self.manifest["node_id"], "tasks": results,"peer_enrollment":enrollment,
                "quarantined_records": invalid + event_invalid, "complete_read": True,
                "state": "attention_required" if invalid + event_invalid else "work_available" if results else "no_work"}

    def document(self, ref, task, output=False):
        project = self.scope(task)
        docid = ref.get("document_id") or ref.get("id")
        allowed = {r.get("document_id", r.get("id")) for r in task.get("input_refs", [])}
        allowed.update(r.get("id", r.get("document_id")) for r in project.get("registered_resources", []))
        if output:
            folder = project["zones"].get(task.get("output_zone"), {}).get("folder_id")
            if not folder: fail("out_of_scope", "任务缺少允许的产物位置")
            if docid not in {r.get("token", r.get("id")) for r in self.transport.root(folder) if r.get("type") == "docx"}:
                fail("out_of_scope", "产物不在任务允许位置")
        elif docid not in allowed:
            fail("out_of_scope", "资料不是任务输入或登记对象")
        actual = self.transport.fetch(identifier(docid))
        if str(actual["revision"]) != str(ref.get("revision")) or actual["content_hash"] != ref.get("content_hash"):
            fail("version_conflict", "资料或成果已变化，请重新核对指定版本")
        return actual

    def operation_id(self, task, key, kind):
        if not isinstance(key, str) or not key.strip() or len(key) > 200: fail("invalid_operation", "请使用本次业务步骤的稳定名称")
        period = rules.period_key(task)
        return "rdos_" + sha([self.manifest["workspace_id"], self.manifest["node_id"], task["task_id"], task["requirements_version"], *([period] if period else []), kind, key])[:32]

    def completion_evidence(self, event, task):
        if event.get("result_status") != "completed": return
        rules.validate_result(event, task)
        for reference in event.get("artifact_refs", []): self.document(reference, task, True)
        if rules.task_type(task) == "review":
            for reference in task.get("input_refs", []): self.document(reference, task)

    def existing(self, operation_id, desired):
        rows, invalid = self.read_rows("event")
        matches = [(v, rid) for v, rid in rows if v.get("operation_id") == operation_id or v.get("id") == desired["id"]]
        if not matches: return None
        if any(v != desired for v, rid in matches): fail("operation_conflict", "同一操作或事件标识存在不同正文")
        actual, rid = self.decode(self.transport.get(self.manifest["events_table"], matches[0][1]), "event")
        if actual != desired: fail("readback_mismatch", "独立读回正文不同")
        return {"operation_id": operation_id, "event_id": desired["id"], "record_id": rid, "state": "verified", "readback_verified": True}

    def operation_project(self, op):
        snapshot = op.get("task_snapshot")
        project_id = op.get("project_id") or (snapshot or {}).get("project_id")
        if not project_id and op.get("kind") == "event": project_id = op.get("desired", {}).get("project_id")
        if not project_id:
            # Old artifact checkpoints predate project_id and task snapshots.
            # Resolve their real task; never reinterpret them using a new default.
            snapshot = self.task(op["task_id"])
            project_id = snapshot["project_id"]
        project = self.scope(snapshot) if snapshot else self.project(project_id)
        if snapshot and (snapshot.get("project_id") != project_id
            or self.manifest["node_id"] not in snapshot.get("executor_node_ids", [])):
            fail("out_of_scope", "原操作不属于当前节点与登记项目")
        if op.get("kind") == "artifact":
            if op["desired"].get("folder") not in {z.get("folder_id") for z in project["zones"].values()}:
                fail("out_of_scope", "原成果操作不属于当前登记项目位置")
        elif op.get("desired", {}).get("project_id") != project_id:
            fail("out_of_scope", "原回执操作的项目归属不一致")
        return project

    def verify(self, operation_id):
        with self.state.locked():
            op = self.state.get("operations", operation_id)
            if not op: fail("operation_missing", "本节点没有该操作检查点；请按原任务检查记录，不创建替代操作")
            self.operation_project(op)
            if op["kind"] == "artifact": return self._verify_artifact(operation_id, op)
            result = self.existing(operation_id, op["desired"])
            if not result: fail("write_uncertain", "结果尚未独立读回，只允许继续核对")
            if op.get("task_snapshot"): self.completion_evidence(op["desired"], op["task_snapshot"])
            self._verified(operation_id, op, result)
            return result

    def _verified(self, operation_id, op, result):
        op.update(state="verified", result=result)
        self.state.put("operations", operation_id, op)
        task_id = op["task_id"]
        point = self.state.get("checkpoints", task_id) or {"task_id": task_id, "requirements_version": op["requirements_version"], "completed_steps": []}
        period = rules.period_key(op.get('task_snapshot') or {})
        if (point.get("requirements_version") == op["requirements_version"]
                and (period is None or point.get('period_key') == period)):
            step = "artifact_verified" if op["kind"] == "artifact" else "submission_verified" if op["desired"].get("result_status") == "completed" else None
            if step: point["completed_steps"] = sorted(set(point.get("completed_steps", []) + [step]))
        point["uncertain_operations"] = [v for v in point.get("uncertain_operations", []) if v != operation_id]
        self.state.put("checkpoints", task_id, point)

    def submit(self, task_id, status, summary, key, artifact_refs=None, event_type=None, review_of=None, correction_of=None):
        if status=="completed":self.ensure_peer_enrollment_completion(task_id)
        with self.state.locked():
            task = self.task(task_id)
            point, invalid = self.checkpoint(task)
            plan = self.plan(task, point)
            if not plan["assigned"]: fail("not_assigned", "此任务未分配给当前节点")
            kind = event_type or ("review" if rules.task_type(task) == "review" else "submission")
            required = ("report_paused_ack" if kind == "paused_ack" else "report_closed_ack" if kind == "closed_ack"
                else "correct_previous_record" if kind == "correction"
                else "report_blocker" if kind in ("blocker", "clarification") else "report_progress" if kind == "progress"
                else "submit_existing_result" if "submit_existing_result" in plan["allowed_actions"] else "submit_result")
            if kind == "progress" and task.get("business_status") in ("paused", "closed"):
                required = "archive_late_result"
            if required not in plan["allowed_actions"]:
                # existing same business operation may be queried, but not newly dispatched
                can_dispatch = False
            else: can_dispatch = True
            opid = self.operation_id(task, key, "event")
            event = {"protocol_version": 2, "workspace_id": self.manifest["workspace_id"], "project_id": task["project_id"],
                     "id": "evt_" + opid[5:], "operation_id": opid, "claimed_node_id": self.manifest["node_id"],
                     "event_type": kind, "task_refs": [{"task_id": task_id, "requirements_version": task["requirements_version"]}],
                     "artifact_refs": artifact_refs or [], "body": summary.strip(), "recipients": task.get("return_to", ["agent"]),
                     "origin": "node_report"}
            period = rules.period_key(task)
            if period is not None:
                event['period_key'] = period
            if kind == "review" and status == "completed" and review_of is None:
                if len(task.get("input_refs", [])) != 1:
                    fail("invalid_review", "请明确被审阅的成果版本，不能将多份输入笼统作为审阅对象")
                review_of = task["input_refs"][0]
            if review_of is not None:
                if review_of not in task.get("input_refs", []): fail("invalid_review", "Review 必须引用当前任务的确切输入版本")
                event["review_of"] = review_of
            if kind in ("submission", "review", "check_result"):
                event["result_status"] = status
                rules.validate_result(event, task)
            elif kind not in ("paused_ack", "closed_ack", "progress", "blocker", "clarification", "correction"):
                fail("invalid_event", "不支持的业务回执类型")
            if not event["body"]: fail("invalid_result", "请说明实际结果")
            if correction_of:
                prior_events, prior_invalid = self.read_rows("event")
                target = [item for item, rid in prior_events if item.get("id") == correction_of]
                if (not target or any(item != target[0] for item in target[1:]) or target[0].get("claimed_node_id") != self.manifest["node_id"]
                    or not any(ref.get("task_id") == task_id for ref in target[0].get("task_refs", []))):
                    fail("invalid_correction", "更正须关联本节点在此任务中的原记录")
                event["correction_of"] = correction_of
            elif kind == "correction": fail("invalid_correction", "更正须提供原事件标识")
            if required == "archive_late_result": event["origin"] = "late_result"
            if task.get("dispatch_source")=="peer":event["peer_result_proof"]=self._peer_sign(event,self._peer_identity())
            previous = self.state.get("operations", opid)
            if previous and previous["desired"] != event: fail("operation_conflict", "同一业务步骤对应不同结果，未覆盖旧记录")
            found = self.existing(opid, event)
            if found:
                self.completion_evidence(event, task)
                op = previous or {"kind": "event", "task_id": task_id, "project_id": task["project_id"], "requirements_version": task["requirements_version"], "desired": event, "task_snapshot": task}
                self._verified(opid, op, found)
                return found
            if previous: fail("write_uncertain", "原操作已准备或发出，请核对原操作，不直接重发")
            if not can_dispatch: fail("action_not_allowed", "当前任务不允许该步骤：" + plan["reason"])
            for ref in event["artifact_refs"]: self.document(ref, task, True)
            if kind == "review" and status == "completed":
                for ref in task.get("input_refs", []): self.document(ref, task)
            latest = self.task(task_id)
            if latest != task: fail("version_conflict", "提交前任务已变化，未写入")
            op = {"kind": "event", "task_id": task_id, "project_id": task["project_id"], "requirements_version": task["requirements_version"], "desired": event, "task_snapshot": task, "state": "sent"}
            self.state.put("operations", opid, op)
            point["uncertain_operations"] = sorted(set(point.get("uncertain_operations", []) + [opid]))
            self.state.put("checkpoints", task_id, point)
            self.state.flush()  # record dispatch before crossing the external boundary
            fields = {"事件ID": event["id"], "类型": kind, "自报节点": event["claimed_node_id"],
                      "任务引用": canonical(event["task_refs"]), "操作ID": opid, "记录数据": canonical(event)}
            try: self.transport.append(fields)
            except WorkflowError: pass  # sent-but-lost response must be reconciled, never retried blindly
            found = self.existing(opid, event)
            if not found: fail("write_uncertain", "回执可能已提交，尚未独立读回；请 verify " + opid)
            self._verified(opid, op, found)
            return found

    def create_artifact(self, task_id, title, content, key, revision_of=None, change_reason=None):
        with self.state.locked():
            task = self.task(task_id)
            point, invalid = self.checkpoint(task)
            plan = self.plan(task, point)
            opid = self.operation_id(task, key, "artifact")
            folder = self.scope(task)["zones"].get(task.get("output_zone"), {}).get("folder_id")
            if not folder: fail("out_of_scope", "任务没有已登记产物位置")
            requested = content.replace("\r\n", "\n").strip("\n")
            desired = {"folder": folder, "title": title.strip(), "content": requested}
            if revision_of is not None:
                desired.update(revision_of=revision_of, change_reason=(change_reason or "").strip())
            if not desired["title"] or not content.strip(): fail("invalid_artifact", "成果须有业务标题与正文")
            prior = self.state.get("operations", opid)
            if prior:
                original = dict(prior["desired"])
                original["content"] = self._artifact_expected_body(opid, prior)
                if original != desired: fail("operation_conflict", "同一创建步骤对应不同成果内容")
                return self._verify_artifact(opid, prior)
            if "perform_work" not in plan["allowed_actions"]:
                if "revise_existing_result" not in plan["allowed_actions"]:
                    fail("action_not_allowed", "当前不允许创建新成果：" + plan["reason"])
                if not revision_of or not (change_reason or "").strip():
                    fail("revision_required", "修订须明确引用已核对成果及变更理由，不能重复制作")
                known = [json.loads(row[0]) for row in self.state.db.execute("SELECT payload FROM operations")]
                if not any(op.get("kind") == "artifact" and op.get("state") == "verified" and op.get("task_id") == task_id
                    and op.get("requirements_version") == task["requirements_version"]
                    and op.get("result", {}).get("artifact_ref") == revision_of for op in known):
                    fail("invalid_revision", "修订引用不是本任务当前要求下已核对的成果")
                self.document(revision_of, task, True)
            if self.task(task_id) != task: fail("version_conflict", "创建前任务已变化")
            baseline = [r.get("token", r.get("id")) for r in self.transport.root(folder)]
            prior = {"kind": "artifact", "task_id": task_id, "project_id": task["project_id"], "task_snapshot": task,
                     "requirements_version": task["requirements_version"], "desired": desired,
                     "baseline_document_ids": baseline, "state": "sent"}
            self.state.put("operations", opid, prior)
            point["uncertain_operations"] = sorted(set(point.get("uncertain_operations", []) + [opid]))
            self.state.put("checkpoints", task_id, point)
            self.state.flush()
            try:
                response = self.transport.create(folder, desired["title"], requested)
            except WorkflowError:
                response = None
            # Save the returned identity before any follow-up network request.
            # A document's body is business content, not an attribution channel:
            # Feishu may discard comments while importing Markdown.
            document = response.get("document", {}) if isinstance(response, dict) else {}
            nested_id = document.get("document_id") if isinstance(document, dict) else None
            direct_id = response.get("document_id") if isinstance(response, dict) else None
            if nested_id and direct_id and nested_id != direct_id:
                fail("invalid_response", "创建响应包含不同文档标识，保留原操作待核对")
            response_id = nested_id or direct_id
            if response_id:
                prior["response_document_id"] = identifier(response_id)
                self.state.put("operations", opid, prior)
                self.state.flush()
            return self._verify_artifact(opid, prior)

    @staticmethod
    def _artifact_expected_body(operation_id, op):
        content = op["desired"]["content"]
        suffix = "\n\n<!-- rdos-operation:" + operation_id + " -->"
        return content[:-len(suffix)] if content.endswith(suffix) else content

    @staticmethod
    def _artifact_bodies(content, title):
        """Only unwrap a known exported title; do not normalize body spacing."""
        bodies = [content]
        for prefix in ("<title>" + html.escape(title, quote=False) + "</title>", "# " + title):
            for separator in ("\n\n", "\n"):
                if content.startswith(prefix + separator):
                    bodies.append(content[len(prefix + separator):])
                    break
        return bodies

    def _artifact_readback(self, operation_id, op, document_id):
        desired = op["desired"]
        identifier(document_id)
        if document_id in op.get("baseline_document_ids", []):
            fail("artifact_conflict", "该文档在创建前已经存在，不能绑定为本次新成果")
        candidates = [r for r in self.transport.root(desired["folder"])
            if r.get("type") == "docx" and r.get("token", r.get("id")) == document_id]
        if len(candidates) != 1: fail("out_of_scope", "成果不在原任务登记位置，未绑定")
        if candidates[0].get("name", candidates[0].get("title")) != desired["title"]:
            fail("artifact_conflict", "成果标题与原创建记录不一致")
        actual = self.transport.fetch(document_id)
        if (actual.get("document_id") != document_id or actual.get("revision") is None
            or not isinstance(actual.get("content"), str) or actual.get("content_hash") != sha(actual["content"])):
            fail("readback_mismatch", "成果缺少完整且一致的独立读回")
        expected = self._artifact_expected_body(operation_id, op)
        suffix = "\n\n<!-- rdos-operation:" + operation_id + " -->"
        bodies = self._artifact_bodies(actual["content"], desired["title"])
        # Old operations stored their own marker in desired content. Accept its
        # presence or removal only; other comments and all body spacing matter.
        if not any(body == expected or (desired["content"].endswith(suffix)
            and body == expected + suffix) for body in bodies):
            fail("artifact_conflict", "成果正文与原创建记录不一致，未重复创建")
        return {k: actual[k] for k in ("document_id", "revision", "content_hash")}

    def _verify_artifact(self, operation_id, op):
        self.operation_project(op)
        desired = op["desired"]
        document_id = (op.get("response_document_id") or op.get("reconciliation", {}).get("artifact_ref", {}).get("document_id")
            or op.get("result", {}).get("artifact_ref", {}).get("document_id"))
        if document_id:
            reference = self._artifact_readback(operation_id, op, document_id)
            previous = op.get("result", {}).get("artifact_ref") or op.get("reconciliation", {}).get("artifact_ref")
            if previous and reference != previous:
                fail("version_conflict", "已核对成果版本发生变化，请保留原版本记录")
            return self._artifact_verified(operation_id, op, reference)
        marker = "<!-- rdos-operation:" + operation_id + " -->"
        if not desired["content"].endswith("\n\n" + marker):
            fail("write_uncertain", "创建响应未能保存，请按原操作与已知成果版本核对；不重复创建")
        # Compatibility for old operations whose marker actually survived.
        # Never adopt a same-title document merely because its body matches.
        candidates = [r for r in self.transport.root(desired["folder"]) if r.get("type") == "docx"
            and r.get("token", r.get("id")) not in op.get("baseline_document_ids", [])
            and r.get("name", r.get("title")) == desired["title"]]
        found = []
        for candidate in candidates:
            actual = self.transport.fetch(candidate.get("token", candidate.get("id")))
            if marker not in actual["content"]: continue
            found.append(self._artifact_readback(operation_id, op, candidate.get("token", candidate.get("id"))))
        if len(found) > 1: fail("artifact_conflict", "同一创建操作对应多个文档，待核对")
        if not found: fail("write_uncertain", "成果创建结果待核对，未重复创建；请 verify " + operation_id)
        return self._artifact_verified(operation_id, op, found[0])

    def _artifact_verified(self, operation_id, op, reference):
        previous = op.get("result", {}).get("artifact_ref")
        if previous and previous != reference:
            fail("version_conflict", "已核对成果版本发生变化，请保留原版本记录")
        result = {"operation_id": operation_id, "state": "verified", "artifact_ref": reference, "readback_verified": True}
        self._verified(operation_id, op, result)
        return result

    def reconcile_artifact(self, operation_id, document_id, revision, content_hash):
        """Bind an explicitly identified existing result; never dispatch a write."""
        with self.state.locked():
            op = self.state.get("operations", operation_id)
            if not op or op.get("kind") != "artifact": fail("operation_missing", "没有对应的原成果创建记录")
            self.operation_project(op)
            identifier(document_id)
            if not str(revision).strip() or not re.fullmatch(r"[a-f0-9]{64}", str(content_hash)):
                fail("invalid_reference", "请提供已知成果的确切版本和正文哈希")
            recorded = op.get("response_document_id") or op.get("reconciliation", {}).get("artifact_ref", {}).get("document_id")
            if recorded and recorded != document_id:
                fail("operation_conflict", "原操作已绑定另一文档，不能改绑")
            task = self.task(op["task_id"])
            if (self.manifest["node_id"] not in task.get("executor_node_ids", [])
                or self.scope(task)["zones"].get(task.get("output_zone"), {}).get("folder_id") != op["desired"]["folder"]):
                fail("out_of_scope", "原操作不属于当前节点与登记产物位置")
            first = self._artifact_readback(operation_id, op, document_id)
            if str(first["revision"]) != str(revision) or first["content_hash"] != content_hash:
                fail("version_conflict", "成果与指定版本不同，未绑定")
            second = self._artifact_readback(operation_id, op, document_id)
            if first != second: fail("version_conflict", "核对期间成果已变化，未绑定")
            previous = op.get("result", {}).get("artifact_ref")
            if previous and previous != first: fail("version_conflict", "成果与原核对版本不同，未改绑")
            op["reconciliation"] = {"method": "explicit_existing_result", "artifact_ref": first,
                "requested_reference": {"document_id": document_id, "revision": revision, "content_hash": content_hash},
                "checks": ["registered_folder", "not_in_creation_baseline", "original_title", "original_body", "stable_double_readback"]}
            return self._artifact_verified(operation_id, op, first)


def main(argv=None):
    parser = argparse.ArgumentParser(description="执行 RDOS 任务并核对业务结果")
    parser.add_argument("--manifest", default=str(Path(__file__).resolve().parents[1] / "manifest.json"))
    parser.add_argument("--state")
    parser.add_argument("--cli", help="明确选择已经安装的飞书 CLI 路径，不安装或修复运行环境")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("connection")
    sub.add_parser("check")
    bind=sub.add_parser("bind-peer");bind.add_argument("identity_file")
    sub.add_parser("peer-ack")
    enroll=sub.add_parser("enroll-peer");enroll.add_argument("task_id")
    dispatch=sub.add_parser("dispatch");dispatch.add_argument("--request",required=True);dispatch.add_argument("--operation-key",required=True)
    progress=sub.add_parser("peer-progress");progress.add_argument("task_id")
    prepare = sub.add_parser("prepare"); prepare.add_argument("task_id")
    submit = sub.add_parser("submit"); submit.add_argument("task_id")
    submit.add_argument("--status", choices=rules.RESULT_STATUSES, default="completed")
    submit.add_argument("--summary", required=True); submit.add_argument("--operation-key", required=True)
    submit.add_argument("--event-type"); submit.add_argument("--artifacts"); submit.add_argument("--review-of"); submit.add_argument("--correction-of")
    create = sub.add_parser("create-artifact"); create.add_argument("task_id")
    create.add_argument("--title", required=True); create.add_argument("--content", required=True); create.add_argument("--operation-key", required=True)
    create.add_argument("--revision-of"); create.add_argument("--change-reason")
    verify = sub.add_parser("verify"); verify.add_argument("operation_id")
    reconcile = sub.add_parser("reconcile-artifact"); reconcile.add_argument("operation_id")
    reconcile.add_argument("--document-id", required=True); reconcile.add_argument("--revision", required=True)
    reconcile.add_argument("--content-hash", required=True)
    args = parser.parse_args(argv)
    try:
        manifest = manifest_value(json.loads(Path(args.manifest).read_text()))
        path = args.state or Path.cwd() / ".rdos" / manifest["workspace_id"] / manifest["node_id"] / "state.sqlite3"
        workflow = Workflow(manifest, Feishu(manifest, args.cli), State(path))
        if args.command in ("connection", "check"):
            if args.command == "connection": workflow.transport.run(["auth", "status"])
            result = workflow.check()
        elif args.command == "bind-peer": result=workflow.bind_peer_identity(args.identity_file)
        elif args.command == "peer-ack": result=workflow.peer_ack()
        elif args.command == "enroll-peer": result=workflow.enroll_peer(args.task_id)
        elif args.command == "dispatch": result=workflow.dispatch_peer(json.loads(Path(args.request).read_text()),args.operation_key)
        elif args.command == "peer-progress": result=workflow.peer_progress(args.task_id)
        elif args.command == "prepare": result = workflow.prepare(args.task_id)
        elif args.command == "verify": result = workflow.verify(args.operation_id)
        elif args.command == "reconcile-artifact":
            result = workflow.reconcile_artifact(args.operation_id, args.document_id, args.revision, args.content_hash)
        elif args.command == "create-artifact":
            original = json.loads(Path(args.revision_of).read_text()) if args.revision_of else None
            result = workflow.create_artifact(args.task_id, args.title, Path(args.content).read_text(), args.operation_key, original, args.change_reason)
        else:
            refs = json.loads(Path(args.artifacts).read_text()) if args.artifacts else []
            review = json.loads(Path(args.review_of).read_text()) if args.review_of else None
            result = workflow.submit(args.task_id, args.status, args.summary, args.operation_key, refs, args.event_type, review, args.correction_of)
        print(canonical({"ok": True, "data": result}))
        return 0
    except (WorkflowError, rules.RuleViolation, OSError, ValueError) as exc:
        print(canonical({"ok": False, "code": getattr(exc, "code", "invalid_input"), "message": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
