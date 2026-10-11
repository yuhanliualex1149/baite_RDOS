"""Small project associations for the central Agent, independent of legacy Runners."""
from __future__ import annotations

import asyncio
import os
import re
from urllib.parse import urlparse

from server import central_store as store, db


class ProjectNotFoundError(ValueError):
    pass


# Both domains are used by Baite's existing Feishu material. Keep the exact
# host allowlist; accepting any *.feishu.cn URL would silently widen scope.
FEISHU_HOSTS = {urlparse(store.FEISHU_HOST).hostname, "shbebetter.feishu.cn"}


def project(record_id: str = store.PROJECT_ID, user: dict | None = None) -> dict:
    item = store.get("project", record_id)
    if not item:
        raise ProjectNotFoundError("项目不存在，请重新选择项目")
    item = dict(item)
    item.setdefault("member_ids", [member["id"] for member in store.users()] if record_id == store.PROJECT_ID else [])
    item.setdefault("feedback_id", store.FEEDBACK_ID if record_id == store.PROJECT_ID else "")
    item["feedback_doc_id"] = item["feedback_id"]
    item.setdefault("feedback_url", store.FEISHU_HOST + "/docx/" + item["feedback_id"] if item["feedback_id"] else "")
    item.setdefault("folder_url", "")
    item.setdefault("status", "active")
    if user and "admin" not in user["roles"] and user["id"] not in item["member_ids"]:
        raise PermissionError("当前账号尚未加入这个项目，请联系管理员添加，或选择已加入的项目。")
    item["member_names"] = {member["id"]: member["display_name"] for member in store.users()
                            if member["id"] in item["member_ids"]}
    return item


def list_projects(user: dict) -> list:
    return [project(item["id"]) for item in store.items("project")
            if "admin" in user["roles"] or user["id"] in project(item["id"])["member_ids"]]


def belongs(item: dict, project_id: str) -> bool:
    return (item.get("project_id") or store.PROJECT_ID) == project_id


def sources(project_id: str = store.PROJECT_ID) -> list:
    item = project(project_id)
    result = [source for source in store.items("source") if (not source.get("project_id") or source["project_id"] == project_id)
              and (project_id == store.PROJECT_ID or source["id"] != store.FEEDBACK_ID)]
    # A project's feedback entry is a project source even before the first read.
    for token, kind, name, url in (
        (item.get("folder_id"), "folder", item["name"] + "资料", item.get("folder_url")),
        (item.get("feedback_id"), "docx", item["name"] + "反馈", item.get("feedback_url")),
    ):
        if token and not any(source["id"] == token for source in result):
            result.append({"id": token, "kind": kind, "name": name, "source_url": url, "project_id": project_id, "global_shared": False})
    return result


def parse_link(url: str, required_kind: str = "") -> dict:
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or parsed.netloc.lower() not in FEISHU_HOSTS:
        raise ValueError("请粘贴已授权的百特飞书链接")
    match = re.fullmatch(r"/(drive/folder|docx|file)/([A-Za-z0-9_-]+)/?", parsed.path)
    if not match:
        raise ValueError("请使用飞书文件夹、文档或文件的直接链接")
    path, token = match.groups()
    kind = {"drive/folder": "folder", "docx": "docx", "file": "file"}[path]
    if required_kind and kind != required_kind:
        raise ValueError("请粘贴飞书文档链接" if required_kind == "docx" else "请粘贴飞书文件夹链接")
    return {"id": token, "kind": kind, "source_url": f"https://{parsed.hostname}/{path}/{token}"}


def member_ids(values: list) -> list:
    ids = list(dict.fromkeys(values))
    enabled = {member["id"] for member in store.users() if member["enabled"]}
    if not ids or any(value not in enabled for value in ids):
        raise ValueError("请选择至少一位已启用的项目成员")
    return ids


async def save_project(fields: dict, operation_id: str, user: dict, project_id: str = "") -> dict:
    from server import central_service as service
    async with service.WRITE_LOCK:
        requested_id = str(fields.get("project_id") or "")
        feedback_url = str(fields.get("feedback_url") or "")
        if project_id and (requested_id or feedback_url):
            raise ValueError("已有项目不能通过创建入口替换反馈文档")
        if requested_id and not re.fullmatch(r"prj_[A-Za-z0-9_-]{6,90}", requested_id):
            raise ValueError("项目标识格式无效")
        if requested_id and not feedback_url:
            raise ValueError("保留项目标识时，请同时提供原项目反馈文档")
        feedback_link = parse_link(feedback_url, "docx") if feedback_url else None
        if feedback_link and "admin" not in user.get("roles", []):
            raise PermissionError("只有管理员可以关联已有项目反馈文档")
        if feedback_link and feedback_link["id"] == store.FEEDBACK_ID:
            raise ValueError("机构接入反馈文档不能作为新项目反馈入口")
        current = project(project_id) if project_id else {}
        record_id = project_id or requested_id or "prj_" + store.digest(operation_id)[:18]
        old = store.get("project", record_id) or current
        if feedback_link and old and old.get("creation_operation_id") != operation_id:
            raise ValueError("该项目标识已被其他项目使用，请核对后再关联")
        if feedback_link and any(item["id"] != record_id and item.get("feedback_id") == feedback_link["id"]
                                 for item in store.items("project")):
            raise ValueError("该反馈文档已关联其他项目，请核对项目入口")
        name = fields.get("name", old.get("name", "")).strip()
        if not name:
            raise ValueError("请填写项目名称")
        members = member_ids(fields.get("member_ids", old.get("member_ids", [])))
        folder_url = fields.get("folder_url", old.get("folder_url", ""))
        link = parse_link(folder_url, "folder") if folder_url else None
        setup = old.get("feedback_setup")
        if setup and not old.get("feedback_id") and (name != old.get("name") or not link or link["id"] != setup["folder_id"]):
            raise ValueError("上次创建反馈文档的结果尚未确认，请先使用原名称和文件夹继续核对，再调整项目。")
        if not link and not old:
            raise ValueError("请指定保存项目资料的飞书文件夹")
        op = store.operation(operation_id, "project", {"project_id": project_id, "fields": fields, "user_id": user["id"]})
        if op["status"] == "succeeded":
            return op["result"]
        item = {**old, "id": record_id, "name": name, "description": fields.get("description", old.get("description", "")),
                "member_ids": members, "created_by": old.get("created_by", user["id"]),
                "created_at": old.get("created_at", db.utc_now()), "updated_at": db.utc_now()}
        if feedback_link:
            item["creation_operation_id"] = operation_id
        try:
            if link:
                extra_sources = [{**link, "name": name + "资料"}]
                if feedback_link:
                    extra_sources.append({**feedback_link, "name": name + "反馈"})
                probe = service.feishu(extra_sources=extra_sources)
                folder_read = await asyncio.to_thread(probe.read_source, link["id"], "folder")
                if feedback_link:
                    if not isinstance(folder_read, dict) or folder_read.get("ok") is not True or folder_read.get("object_id") != link["id"]:
                        raise ValueError("无法读取指定项目文件夹，请检查权限或链接")
                    # A project may later change its material folder while the
                    # original feedback document remains in its old location.
                    document_read = await asyncio.to_thread(probe.read_source, feedback_link["id"], "docx")
                    if not isinstance(document_read, dict) or document_read.get("ok") is not True or document_read.get("object_id") != feedback_link["id"]:
                        raise ValueError("无法读取指定项目反馈文档，请检查权限或链接")
                item.update(folder_id=link["id"], folder_url=link["source_url"])
            # Existing feedback stays in its original location when its project folder changes.
            if not item.get("feedback_id"):
                if not link:
                    raise ValueError("项目缺少反馈入口，请指定飞书文件夹")
                if feedback_link:
                    item.update(feedback_id=feedback_link["id"], feedback_doc_id=feedback_link["id"],
                                feedback_url=feedback_link["source_url"])
                else:
                    item.update(status="preparing", error="", feedback_setup={"folder_id": link["id"], "title": name + " · 工作反馈", "operation_id": "project:" + record_id})
                    store.put("project", record_id, item)
                    created = await asyncio.to_thread(probe.ensure_project_feedback, link["id"], name + " · 工作反馈", "project:" + record_id)
                    item.update(feedback_id=created["object_id"], feedback_doc_id=created["object_id"], feedback_url=created["source_url"])
            item.update(status="active", error="", technical_detail="", checked_at=db.utc_now())
            store.put("project", record_id, item)
            return store.finish_operation(op, item)
        except Exception as exc:
            # Retain a recoverable setup record; do not replace an existing healthy project's settings.
            if not old or old.get("status") in ("preparing", "failed"):
                item.update(status="failed", error=service.human_error(exc), technical_detail=service.safe_error(exc))
                store.put("project", record_id, item)
            store.finish_operation(op, {"error": service.safe_error(exc), "project_id": record_id}, "failed")
            raise


async def register_source(fields: dict, operation_id: str, user: dict) -> dict:
    from server import central_service as service
    async with service.WRITE_LOCK:
        link = parse_link(fields["url"])
        op = store.operation(operation_id, "source", {"fields": fields, "user_id": user["id"]})
        if op["status"] == "succeeded":
            return op["result"]
        existing = store.get("source", link["id"]) or {}
        protected = {*store.ONBOARDING_IDS, store.FEEDBACK_ID,
                     os.environ.get("BAITE_SELECTED_RAG_FOLDER_TOKEN", "KQjyf6KrxlZ24MdkHirc9TP0n2d"),
                     os.environ.get("BAITE_CENTRAL_SKILL_FOLDER_TOKEN", "ImWqfzWcvl5LWUdFmxKcLIfdntp")}
        source = {**existing, **link, "name": fields.get("name") or existing.get("name") or "共享资料",
                  "watch": bool(fields.get("watch", False)), "registered_by": user["id"], "registered_at": db.utc_now(), "global_shared": True,
                  "removable": link["id"] not in protected}
        source.pop("project_id", None)
        # This authenticated admin action registers the exact link; it never changes remote sharing.
        tool = service.feishu(extra_sources=[source])
        result = await asyncio.to_thread(tool.read_source, source["id"], source["kind"])
        source.update(checked_at=db.utc_now(), check_status="readable", name=fields.get("name") or result.get("title") or source["name"])
        store.put("source", source["id"], source)
        return store.finish_operation(op, source)
