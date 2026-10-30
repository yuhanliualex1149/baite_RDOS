from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from server import central_service as service, central_store as store, central_projects as projects, db
from server import lab_panel, phase1_runtime
from server.lab_routes import SanitizedLabRoute


router = APIRouter(prefix="/api/central", route_class=SanitizedLabRoute)
COOKIE = lab_panel.cookie_name("central")
_LOGIN_ATTEMPTS: dict[tuple[str, str], list[float]] = {}
_LOGIN_LOCK = threading.Lock()
_LOGIN_WINDOW_SECONDS = 60
_LOGIN_MAX_ATTEMPTS = 10


def _cookie_path() -> str:
    return urlsplit(os.environ.get("BAITE_PUBLIC_URL", "http://127.0.0.1:8000")).path.rstrip("/") + "/"


def _check_login_limit(request: Request) -> None:
    # The reverse proxy supplies the client address to trusted Uvicorn proxy
    # headers. Never trust a user-supplied X-Forwarded-For header here.
    key = (str(db.db_path()), request.client.host if request.client else "unknown")
    now = time.monotonic()
    with _LOGIN_LOCK:
        recent = [stamp for stamp in _LOGIN_ATTEMPTS.get(key, ()) if stamp > now - _LOGIN_WINDOW_SECONDS]
        if len(recent) >= _LOGIN_MAX_ATTEMPTS:
            retry_after = max(1, int(recent[0] + _LOGIN_WINDOW_SECONDS - now) + 1)
            raise HTTPException(429, "登录尝试过于频繁，请稍后再试。", headers={"Retry-After": str(retry_after)})
        recent.append(now)
        _LOGIN_ATTEMPTS[key] = recent
        if len(_LOGIN_ATTEMPTS) > 1024:
            for old_key, stamps in list(_LOGIN_ATTEMPTS.items()):
                if stamps[-1] <= now - _LOGIN_WINDOW_SECONDS:
                    _LOGIN_ATTEMPTS.pop(old_key, None)
            if len(_LOGIN_ATTEMPTS) > 1024:
                oldest = sorted(_LOGIN_ATTEMPTS, key=lambda item: _LOGIN_ATTEMPTS[item][-1])
                for old_key in oldest[:len(_LOGIN_ATTEMPTS) - 1024]:
                    _LOGIN_ATTEMPTS.pop(old_key, None)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=500)


class Operation(Input):
    operation_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")


class ProjectOperation(Operation):
    project_id: str = Field(default=store.PROJECT_ID, min_length=1, max_length=100)


class ThreadInput(Input):
    project_id: Optional[str] = Field(default=None, max_length=100)
    title: str = Field(default="", max_length=80)


class Chat(Operation):
    message: str = Field(min_length=1, max_length=20000)
    thread_id: str = Field(min_length=1, max_length=120)
    # Empty identifies an organization thread, never an inferred project.
    project_id: Optional[str] = Field(default=None, max_length=100)


class Task(ProjectOperation):
    task_id: str = Field(default="", max_length=100)
    instructions: str = Field(min_length=1, max_length=25000)
    member_ids: list[str] = Field(min_length=1, max_length=30)
    task_kind: Literal["work", "onboarding"] = "work"
    stage: Literal["", "第一阶段", "第二阶段", "第三阶段"] = ""
    nature: Literal["一次性", "周期", "每次接入执行"] = "一次性"
    related_task: str = Field(default="", max_length=100)
    version: str = Field(default="", max_length=30)


class Member(Input):
    username: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=80)
    roles: list[Literal["admin", "researcher"]] = Field(min_length=1, max_length=2)
    password: str = Field(default="", max_length=500, repr=False)
    identity_mode: Literal["actual", "role_test"] = "actual"
    member_id: str = Field(default="", max_length=84, pattern=r"^(?:|mem_[A-Za-z0-9_-]{8,80})$")


class MemberEdit(Input):
    username: Optional[str] = Field(default=None, min_length=1, max_length=80)
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    roles: Optional[list[Literal["admin", "researcher"]]] = None
    identity_mode: Optional[Literal["actual", "role_test"]] = None
    enabled: Optional[StrictBool] = None


class Onboarding(Input):
    project_id: str = Field(default=store.PROJECT_ID, min_length=1, max_length=100)
    work_scope: str = Field(min_length=1, max_length=4000)
    share_scope: str = Field(min_length=1, max_length=4000)
    stage: int = Field(ge=1, le=3)
    evidence: str = Field(min_length=1, max_length=12000)
    run_label: str = Field(min_length=1, max_length=120)


class Runtime(Input):
    paused: StrictBool


class DraftEdit(Input):
    old_text: str = Field(max_length=30000)
    new_text: str = Field(min_length=1, max_length=30000)
    summary: str = Field(min_length=1, max_length=3000)


class Publish(Operation):
    expected_hash: str = Field(min_length=64, max_length=64)


class ProjectCreate(Operation):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    member_ids: list[str] = Field(default_factory=list, max_length=30)
    node_ids: list[str] = Field(default_factory=list, max_length=30)
    folder_url: str = Field(default="", max_length=1000)
    # Admin-only migration from a local trial. Ordinary creation still creates
    # a project feedback document in the selected folder.
    project_id: str = Field(default="", max_length=100, pattern=r"^(?:|prj_[A-Za-z0-9_-]{6,90})$")
    feedback_url: str = Field(default="", max_length=1000)


class ProjectEdit(Operation):
    expected_revision: int = Field(ge=1)
    name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    description: Optional[str] = Field(default=None, max_length=4000)
    member_ids: Optional[list[str]] = Field(default=None, min_length=1, max_length=30)
    node_ids: Optional[list[str]] = Field(default=None, max_length=30)
    folder_url: Optional[str] = Field(default=None, max_length=1000)


class ProjectStateChange(Operation):
    expected_revision: int = Field(ge=1)


class SourceCreate(Operation):
    url: str = Field(min_length=1, max_length=1000)
    name: str = Field(default="", max_length=160)
    watch: StrictBool = False


class Rejection(Input):
    reason: str = Field(default="", max_length=4000)


class TaskDraftEdit(Input):
    instructions: str = Field(min_length=1, max_length=25000)
    member_ids: list[str] = Field(min_length=1, max_length=30)
    task_id: str = Field(default="", max_length=100)
    task_kind: Literal["work", "onboarding"] = "work"
    stage: Literal["", "第一阶段", "第二阶段", "第三阶段"] = ""
    nature: Literal["一次性", "周期", "每次接入执行"] = "一次性"
    related_task: str = Field(default="", max_length=100)
    version: str = Field(default="", max_length=30)
    draft_action: Optional[Literal["create", "update", "withdraw"]] = None
    reason: Optional[str] = Field(default=None, max_length=3000)


def current_user(token: Optional[str] = Cookie(default=None, alias=COOKIE),
                 actor: Optional[str] = Header(default=None, alias="X-RDOS-Actor")) -> dict:
    user = store.session(token or "")
    if not user:
        raise HTTPException(401, "登录已过期，请重新登录。")
    if actor is not None and actor != user["id"]:
        raise HTTPException(409, {"message": "当前账号已变化，请重新登录。", "code": "session_changed"})
    return user


def admin(user: dict = Depends(current_user)) -> dict:
    if "admin" not in user["roles"]:
        raise HTTPException(403, "当前账号不能执行此操作，请使用管理员账号，或联系管理员。")
    return user


def researcher(user: dict = Depends(current_user)) -> dict:
    if "researcher" not in user["roles"]:
        raise HTTPException(403, "当前账号不能登记研究员接入，请使用研究员账号，或联系管理员。")
    return user


def failure(exc: Exception) -> HTTPException:
    parsing = isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)) or getattr(exc, "code", "") == "parse_error"
    status = 404 if isinstance(exc, projects.ProjectNotFoundError) else 403 if isinstance(exc, PermissionError) else 502 if parsing else 409 if isinstance(exc, (ValueError, sqlite3.IntegrityError)) else 502
    code = getattr(exc, "code", "parse_error" if parsing else "project_not_found" if status == 404 else "permission_denied" if status == 403 else "validation_error" if status == 409 else "operation_failed")
    status = getattr(exc, 'status', status)
    return HTTPException(status, {"message": "账号已存在，请换一个登录名。" if isinstance(exc, sqlite3.IntegrityError) else service.human_error(exc),
                                 "code": code, "technical_detail": service.safe_error(exc)})


def safe_display(value):
    """Redact retained records as well as new logs, without changing draft hashes."""
    result = service._safe_record(value)

    def mark(original, displayed):
        if isinstance(original, dict) and isinstance(displayed, dict):
            if original.get("draft_hash"):
                displayed["draft_hash"] = original["draft_hash"]
                body_keys = ("instructions", "reason", "summary", "old_text", "new_text", "evidence")
                if any(original.get(key) != displayed.get(key) for key in body_keys):
                    displayed["display_redacted"] = True
            for key in original.keys() & displayed.keys():
                mark(original[key], displayed[key])
        elif isinstance(original, list) and isinstance(displayed, list):
            for before, after in zip(original, displayed):
                mark(before, after)

    mark(value, result)
    return result


def project_failure(exc: Exception, operation_id: str) -> HTTPException:
    error = failure(exc)
    operation = store.get("operation", operation_id) or {}
    if operation.get("action") == "project" and operation.get("status") == "failed":
        record_id = (operation.get("result") or {}).get("project_id")
        item = store.get("project", record_id) if record_id else None
        if item and item.get("status") == "failed":
            error.detail["project_id"] = record_id
    return error


@router.post("/auth/login")
def login(payload: Login, response: Response, request: Request):
    _check_login_limit(request)
    result = store.login(payload.username.strip(), payload.password)
    if not result:
        raise HTTPException(401, "账号或密码不匹配，请检查后重试。")
    response.set_cookie(COOKIE, result["token"], max_age=12 * 3600, httponly=True,
                        secure=request.url.scheme == "https" or urlsplit(os.environ.get("BAITE_PUBLIC_URL", "")).scheme == "https"
                        or os.environ.get("BAITE_COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
                        samesite="strict", path=_cookie_path())
    return {"user": result["user"]}


@router.get("/auth/me")
def me(user: dict = Depends(current_user)):
    return {"user": user}


@router.post("/auth/logout")
def logout(response: Response, token: Optional[str] = Cookie(default=None, alias=COOKIE)):
    store.logout(token or "")
    response.delete_cookie(COOKIE, path=_cookie_path())
    return {"ok": True}


def visible_run(item: dict, user: dict) -> bool:
    """Manual conversations belong to their initiator, including among admins."""
    return bool(item.get("user_id") == user["id"] or
                ("admin" in user["roles"] and item.get("user_id") == "system:scheduler"
                 and item.get("trigger") == "scheduled"))


def require_run(run_id: str, user: dict) -> dict:
    item = store.get("run", run_id)
    if not item:
        raise HTTPException(404, "找不到这次处理记录，请刷新后重新选择。")
    if not visible_run(item, user):
        raise HTTPException(403, "当前账号不能读取或继续另一账号的手动运行。")
    if item.get("project_id"):
        try:
            projects.project(item["project_id"], user)
        except Exception as exc:
            raise failure(exc) from exc
    return item


@router.post("/threads")
def create_thread(payload: ThreadInput, user: dict = Depends(admin)):
    try:
        return {"item": service.create_thread(user, payload.project_id or "", payload.title)}
    except Exception as exc:
        raise failure(exc) from exc


@router.get("/threads")
def list_threads(project_id: str = "", user: dict = Depends(admin)):
    try:
        return {"items": service.list_threads(user, project_id)}
    except Exception as exc:
        raise failure(exc) from exc


@router.get("/workspace")
def workspace(project_id: str = store.PROJECT_ID, thread_id: Optional[str] = None,
              user: dict = Depends(current_user)):
    if 'admin' not in user['roles']:
        from server import rdos_projects, rdos_contract, phase3_service
        try:
            if thread_id:
                raise PermissionError('研发成员不能读取管理员对话')
            selected = rdos_contract.internal_input(project_id)
            rdos_projects.require_read(selected, user)
            project = _managed_project_view(rdos_projects.get_project(selected, user))
            return safe_display({'user': user, 'project': project,
                'projects': [_managed_project_view(p) for p in rdos_projects.list_projects(user)],
                'users': [user], 'tasks': [], 'feedback': [], 'proposals': [], 'runs': [],
                'task_drafts': [], 'messages': [], 'sources': [], 'runtime': {},
                'project_health': {}, 'onboarding': [], 'read_only': True})
        except Exception as exc:
            raise failure(exc) from exc
    try:
        project = projects.project(project_id, user)
        if thread_id:
            service.get_thread(thread_id, user, project_id)
    except Exception as exc:
        raise failure(exc) from exc
    is_admin = "admin" in user["roles"]
    tasks = [item for item in store.items("task") if projects.belongs(item, project_id) or (not item.get("project_id") and item.get("task_id") in ("ONB-FRAME-01", "ONB-REVIEW-01"))]
    feedback = [item for item in store.items("feedback") if projects.belongs(item, project_id)]
    proposals = [item for item in store.items("proposal") if projects.belongs(item, project_id)
                 or (item.get("target_kind") == "template" and item.get("project_id") == "")]
    runtime = dict(store.get("state", "runtime") or {})
    project_runtime = runtime.get("project_runtime", {}).get(project_id)
    # A team-level chat must not appear as the selected project's latest work.
    for field in ("last_scheduled", "last_manual", "last_read_at", "error", "last_error", "last_read_error", "technical_detail"):
        runtime.pop(field, None)
    runtime["schedule_verified"] = False
    if project_runtime:
        runtime.update(project_runtime)
    runtime["next_due"] = runtime.get("project_next_due", {}).get(project_id, runtime.get("next_due"))
    runtime.update(lab_panel.runtime_status())
    # These filters route relevant work; Feishu's existing organization sharing is unchanged.
    if not is_admin:
        tasks = [task for task in tasks if service.task_applies(task, user["id"])]
        feedback = [item for item in feedback if item.get("member_id") == user["id"]]
        proposals = [item for item in proposals if item["status"] == "published"]
    all_runs = [item for item in store.items("run") if visible_run(item, user)] if is_admin else []
    runs_by_id = {item["id"]: item for item in all_runs}
    messages = []
    if is_admin and thread_id:
        for message in store.items("message"):
            if message.get("thread_id") != thread_id or not projects.belongs(message, project_id):
                continue
            run = runs_by_id.get(message.get("run_id"), {})
            if not run or not visible_run(run, user):
                continue
            entry = {**message, "kind": message.get("kind", run.get("kind", "")),
                     "trigger": message.get("trigger", run.get("trigger", ""))}
            if entry["kind"] != "sync" and entry["trigger"] != "scheduled":
                messages.append(entry)
        # Keep the conversation window independent of the diagnostics window.
        # Source metadata, never message wording, distinguishes periodic checks.
        messages = sorted(messages, key=lambda item: item["created_at"])[-60:]
    return safe_display({"user": user, "project": project, "projects": projects.list_projects(user),
            "users": store.users() if is_admin else [user], "tasks": tasks, "feedback": feedback,
            "proposals": [item for item in proposals if item.get("status") != "deleted"],
            "deleted_proposals": [item for item in proposals if item.get("status") == "deleted"] if is_admin else [],
            "runs": [item for item in all_runs if projects.belongs(item, project_id)][:60],
            "task_drafts": [item for item in store.items("task_draft") if projects.belongs(item, project_id) and item.get("status") != "deleted"] if is_admin else [],
            "deleted_task_drafts": [item for item in store.items("task_draft") if projects.belongs(item, project_id) and item.get("status") == "deleted"] if is_admin else [],
            "messages": messages,
            "sources": projects.sources(project_id), "runtime": runtime,
            "project_health": phase1_runtime.health(project_id),
            "onboarding": [item for item in store.items("onboarding") if projects.belongs(item, project_id) and (is_admin or item["member_id"] == user["id"])]})


@router.post("/chat")
async def chat(payload: Chat, user: dict = Depends(admin)):
    try:
        service.get_thread(payload.thread_id, user, payload.project_id or "")
        run = service.new_run(user, payload.message, payload.operation_id,
                              project_id=payload.project_id or "", thread_id=payload.thread_id)
        if run["status"] == "queued":
            service.launch(run)
        return {"run_id": run["id"]}
    except Exception as exc:
        raise failure(exc) from exc


@router.get("/overview")
def overview(thread_id: Optional[str] = None, user: dict = Depends(admin)):
    """No thread means no chat history; task and health facts remain visible."""
    try:
        if thread_id:
            service.get_thread(thread_id, user)
        result = service.organization_overview(user, thread_id=thread_id)
        result["runs"] = [item for item in result.get("runs", []) if visible_run(item, user)]
        if not thread_id:
            result["messages"] = []
        formal = os.environ.get('RDOS_RUNTIME_MODE') == 'production'
        result.update(source_environment=('feishu_production' if formal else 'local_mock'),
                      client_acceptance=('not_inferred_from_cache' if formal else 'pending'))
        return safe_display(result)
    except Exception as exc:
        raise failure(exc) from exc


@router.get("/diagnostics")
def diagnostics(user: dict = Depends(admin)):
    result = service.runtime_diagnostics(user)
    formal = os.environ.get('RDOS_RUNTIME_MODE') == 'production'
    result.update(source_environment=('feishu_production' if formal else 'local_mock'),
                  client_acceptance=('not_inferred_from_cache' if formal else 'pending'))
    return safe_display(result)


@router.post("/sync")
async def sync(payload: ProjectOperation, user: dict = Depends(admin)):
    try:
        run = service.new_run(user, "检查本地测试资料的必要变化；无变化时不分析，有变化时仅分析或形成本地草稿，不自行发布。", payload.operation_id, "sync", project_id=payload.project_id)
        if run["status"] == "queued":
            service.launch(run)
        return {"run_id": run["id"]}
    except Exception as exc:
        raise failure(exc) from exc


@router.get("/runs")
def run_list(project_id: Optional[str] = None, offset: int = Query(default=0, ge=0),
             limit: int = Query(default=30, ge=1, le=100), user: dict = Depends(admin)):
    if project_id:
        try:
            projects.project(project_id, user)
        except Exception as exc:
            raise failure(exc) from exc
    items = sorted([item for item in store.items("run") if visible_run(item, user)
                    and (project_id is None or item.get("project_id", "") == project_id)],
                   key=lambda item: (item.get("created_at", ""), item["id"]), reverse=True)
    end = offset + limit
    return safe_display({"items": items[offset:end], "total": len(items), "next_offset": end if end < len(items) else None})


@router.get("/runs/{run_id}")
def run_detail(run_id: str, user: dict = Depends(admin)):
    item = require_run(run_id, user)
    return safe_display({"run": item, **item})


@router.post("/runs/{run_id}/retry")
async def retry(run_id: str, user: dict = Depends(admin)):
    require_run(run_id, user)
    try:
        item = service.retry_run(run_id, user)
        if asyncio.iscoroutine(item):
            item = await item
        return {"run_id": item["id"]}
    except Exception as exc:
        raise failure(exc) from exc


@router.post("/tasks")
async def task(payload: Task, user: dict = Depends(admin)):
    try:
        return {"item": await service.save_task(payload.model_dump(exclude={"operation_id"}, exclude_unset=True), payload.operation_id, user)}
    except Exception as exc:
        raise failure(exc) from exc


@router.post("/tasks/{task_id}/withdraw")
async def withdraw(task_id: str, payload: ProjectOperation, user: dict = Depends(admin)):
    try:
        return {"item": await service.save_task({"task_id": task_id, "status": "停用", "project_id": payload.project_id}, payload.operation_id, user)}
    except Exception as exc:
        raise failure(exc) from exc


@router.get("/users")
def list_members(user: dict = Depends(admin)):
    from server.panel_members import users_view
    return users_view(user)


@router.post("/users")
def create_member(payload: Member, user: dict = Depends(admin)):
    try:
        return store.create_user(**payload.model_dump(), actor_id=user['id'])
    except Exception as exc:
        raise failure(exc) from exc


@router.patch("/users/{user_id}")
def edit_member(user_id: str, payload: MemberEdit, user: dict = Depends(admin)):
    try:
        before = store.user(user_id)
        updated = store.update_user(user_id, payload.model_dump(exclude_none=True), actor_id=user['id'])
        relogin = user_id == user['id'] and before is not None and any(
            before.get(key) != updated.get(key) for key in ('username', 'roles', 'enabled'))
        return {"user": updated, 'relogin_required': relogin}
    except Exception as exc:
        raise failure(exc) from exc


@router.post("/onboarding")
async def onboarding(payload: Onboarding, user: dict = Depends(current_user)):
    try:
        project = projects.project(payload.project_id, user)
    except Exception as exc:
        raise failure(exc) from exc
    item = {**payload.model_dump(), "id": "onb_" + store.digest([user["id"], payload.project_id, payload.stage, payload.run_label])[:20],
            "member_id": user["id"], "project_id": payload.project_id, "identity_mode": user["identity_mode"],
            "evidence_status": "研究员自述；本地记忆保存和执行效果仍需实际核对", "created_at": db.utc_now()}
    # Only a matching remote member/run segment proves feedback was readable here.
    try:
        document = await asyncio.to_thread(service.feishu(payload.project_id).read_document, project["feedback_id"])
        entries = service.parse_feedback(document, [task for task in store.items("task") if projects.belongs(task, payload.project_id)], store.users(), payload.project_id)
        matches = [entry for entry in entries if entry["member_id"] == user["id"] and entry.get("run_label") == payload.run_label]
        item.update(remote_feedback_verified=bool(matches), source_url=document["source_url"],
                    feedback_ids=[entry["id"] for entry in matches], read_error="", technical_detail="")
    except Exception as exc:
        item.update(remote_feedback_verified=False, read_error=service.human_error(exc), technical_detail=service.safe_error(exc))
    store.put("onboarding", item["id"], item)
    return {"item": item}


@router.patch("/runtime")
def runtime(payload: Runtime, user: dict = Depends(admin)):
    state = store.get("state", "runtime")
    state.update(paused=payload.paused, next_due=store.future(300) if not payload.paused else state.get("next_due"))
    store.put("state", "runtime", state)
    return {"runtime": state}


@router.post("/proposals/{proposal_id}/edit")
async def edit_proposal(proposal_id: str, payload: DraftEdit, user: dict = Depends(admin)):
    async with service.WRITE_LOCK:
        item = store.get("proposal", proposal_id)
        if not item:
            raise HTTPException(404, "草稿不存在")
        if item["status"] == "published" or item.get("publication_operation_id"):
            raise HTTPException(409, "建议已确认发布，请先核对原操作结果；不能替换已确认内容")
        try:
            if item["target_kind"] == "template":
                from server import central_templates
                material = central_templates.snapshot(item["target_id"])
                if not payload.old_text:
                    raise ValueError("模板修改须指出当前原文中的唯一位置")
            else:
                material = await asyncio.to_thread(service.source_read, item["target_id"], item["target_kind"], item.get("project_id", store.PROJECT_ID))
            if material["content_hash"] != item["base_hash"] or (payload.old_text and material["content"].count(payload.old_text) != 1):
                raise ValueError("来源已变化或原文范围不准确，请重新读取并形成草稿")
            if payload.old_text == payload.new_text:
                raise ValueError("草稿没有改动")
            item.update(**payload.model_dump(), status="draft", edited_by=user["id"], edited_at=db.utc_now())
            item.pop("confirmed_by", None)
            item.pop("confirmed_at", None)
            item.pop("publication_operation_id", None)
            item["draft_hash"] = service.proposal_hash(item)
            store.put("proposal", proposal_id, item)
            return {"item": item}
        except Exception as exc:
            raise failure(exc) from exc


@router.post("/proposals/{proposal_id}/publish")
async def publish(proposal_id: str, payload: Publish, user: dict = Depends(admin)):
    try:
        return {"item": await service.publish_proposal(proposal_id, payload.expected_hash, payload.operation_id, user)}
    except Exception as exc:
        raise failure(exc) from exc


def _managed_project_view(item):
    from server import rdos_contract, phase3_service
    result = rdos_contract.public_view(safe_display(item), {'nodes': phase3_service.node_registry()})
    # Keep existing administrator project IDs compatible with saved threads.
    result['id'] = item['id']
    return result


@router.get("/projects")
def project_list(developer: bool = False, include_inactive: bool = False, user: dict = Depends(current_user)):
    from server import rdos_projects, phase3_service, rdos_contract
    from server.panel_members import visible_nodes
    from server.phase3_api import developer_enabled
    if not (developer and 'admin' in user['roles'] and developer_enabled()):
        try:
            return {'items': [_managed_project_view(p) for p in rdos_projects.list_projects(user, include_inactive)],
                    'nodes': visible_nodes(user),
                    'default_folder_url': rdos_projects.get_project(phase3_service.PROJECT_ID)['resolved_folder_url'] if 'admin' in user['roles'] else ''}
        except Exception as exc:
            raise failure(exc) from exc
    items=projects.list_projects(user)
    from server import phase3_store
    if phase3_store.get('state','business_workflow'):
        from server.phase3_api import developer_enabled
        if not (developer and 'admin' in user['roles'] and developer_enabled()):
            items=[item for item in items if item['id']==phase3_store.PROJECT_ID]
    return {"items": items}


@router.post("/projects")
async def create_project(payload: ProjectCreate, user: dict = Depends(admin)):
    try:
        from server import rdos_projects, rdos_contract, phase3_service
        if payload.project_id or payload.feedback_url or payload.member_ids:
            raise ValueError('请从项目管理登记参与节点与工作文件夹')
        fields = rdos_contract.internal_input(payload.model_dump(include={'name','description','node_ids','folder_url'}),
                                               {'nodes':phase3_service.node_registry()})
        item = rdos_projects.create_project(fields, user, payload.operation_id)
        _sync_registered_projects()
        return {'item': _managed_project_view(item)}
    except Exception as exc:
        raise failure(exc) from exc


def _sync_registered_projects(*, persist=True):
    from server import phase3_backend, rdos_projects
    backend = phase3_backend.get_backend()
    if hasattr(backend, 'register_projects'):
        backend.register_projects(rdos_projects.list_projects(None, include_inactive=True), persist=persist)


async def _legacy_create_project(payload: ProjectCreate, user: dict):
    try:
        fields = payload.model_dump(exclude={"operation_id", "project_id", "feedback_url"})
        if payload.project_id:
            fields["project_id"] = payload.project_id
        if payload.feedback_url:
            fields["feedback_url"] = payload.feedback_url
        return {"item": await projects.save_project(fields, payload.operation_id, user)}
    except Exception as exc:
        raise project_failure(exc, payload.operation_id) from exc


@router.patch("/projects/{project_id}")
async def edit_project(project_id: str, payload: ProjectEdit, user: dict = Depends(admin)):
    try:
        from server import rdos_projects, rdos_contract, phase3_service
        fields = rdos_contract.internal_input(payload.model_dump(exclude={'operation_id','expected_revision'}, exclude_none=True),
                                               {'nodes':phase3_service.node_registry()})
        item = rdos_projects.update_project(project_id, fields, user, payload.operation_id, payload.expected_revision)
        _sync_registered_projects()
        return {'item': _managed_project_view(item)}
    except Exception as exc:
        raise failure(exc) from exc


@router.get('/projects/{project_id}/deactivation-preview')
async def deactivation_preview(project_id: str, user: dict = Depends(admin)):
    from server import rdos_projects
    try:
        return safe_display(await rdos_projects.preview_deactivate(project_id, user))
    except Exception as exc:
        raise failure(exc) from exc


@router.post('/projects/{project_id}/deactivate')
async def deactivate_project(project_id: str, payload: ProjectStateChange, user: dict = Depends(admin)):
    from server import rdos_projects
    try:
        item = await rdos_projects.deactivate_project(project_id, user, payload.operation_id, payload.expected_revision)
        _sync_registered_projects()
        return {'item': _managed_project_view(item)}
    except Exception as exc:
        raise failure(exc) from exc


@router.post('/projects/{project_id}/reactivate')
def reactivate_project(project_id: str, payload: ProjectStateChange, user: dict = Depends(admin)):
    from server import rdos_projects
    try:
        item = rdos_projects.reactivate_project(project_id, user, payload.operation_id, payload.expected_revision)
        _sync_registered_projects()
        return {'item': _managed_project_view(item)}
    except Exception as exc:
        raise failure(exc) from exc


@router.get("/sources")
def source_list(user: dict = Depends(admin)):
    return {"items": [source for source in store.items("source") if not source.get("project_id")]}


@router.post("/sources")
async def register_source(payload: SourceCreate, user: dict = Depends(admin)):
    try:
        return {"item": await projects.register_source(payload.model_dump(exclude={"operation_id"}), payload.operation_id, user)}
    except Exception as exc:
        raise failure(exc) from exc


@router.delete("/sources/{source_id}")
def remove_source(source_id: str, user: dict = Depends(admin)):
    item = store.get("source", source_id)
    if not item or not item.get("removable"):
        raise HTTPException(409, "这个入口是基础接入资料，请保留")
    store.remove("source", source_id)
    return {"ok": True}


@router.post("/task-drafts/{draft_id}/edit")
async def edit_task_draft(draft_id: str, payload: TaskDraftEdit, user: dict = Depends(admin)):
    async with service.WRITE_LOCK:
        old = store.get("task_draft", draft_id)
        if not old:
            raise HTTPException(404, "任务草稿不存在")
        if old["status"] in ("published", "deleted") or old.get("publication_operation_id"):
            raise HTTPException(409, "这份任务已确认发布；请先核对发布结果，再另建修改草稿")
        try:
            fields = {key: old[key] for key in ("instructions", "member_ids", "task_id", "task_kind", "stage", "nature", "related_task", "draft_action", "reason") if key in old}
            changes = payload.model_dump(exclude_unset=True)
            if changes.get("stage") in ("第一阶段", "第二阶段", "第三阶段") and "task_kind" not in changes:
                changes["task_kind"] = "onboarding"
            # The next version always comes from the latest remote task. A
            # stale form must not pin the old number during explicit re-review.
            changes.pop("version", None)
            if (changes.get("draft_action") and changes["draft_action"] != old.get("draft_action")
                    and not changes.get("reason")):
                raise ValueError("改变任务操作时请说明原因")
            fields.update(changes)
            fields["task_id"] = fields.get("task_id") or old["task_id"]
            remote_tasks = (await asyncio.to_thread(service.feishu(old["project_id"]).list_tasks))["items"]
            matches = [item for item in remote_tasks if item.get("task_id") == fields["task_id"]]
            if len(matches) > 1:
                raise ValueError("飞书存在重复任务 ID，请先核对原记录")
            checked = service.prepare_task(fields, {"id": old.get("run_id", ""), "project_id": old["project_id"],
                                                    "operation_id": "edit:" + store.identifier("draft")}, user,
                                           existing_task=matches[0] if matches else {})
            store.remove("task_draft", checked["id"])
            old.update({key: checked[key] for key in ("instructions", "member_ids", "task_id", "task_kind", "stage", "nature", "related_task", "version",
                                                      "draft_action", "reason", "target_status", "base_task_version", "base_task_hash", "base_task_record_id", "base_task_instructions")},
                       status="draft", edited_by=user["id"], edited_at=db.utc_now(), needs_review=False, last_publish_error="")
            old["draft_hash"] = service.task_draft_hash(old)
            store.put("task_draft", draft_id, old)
            return {"item": old}
        except Exception as exc:
            raise failure(exc) from exc


@router.post("/task-drafts/{draft_id}/publish")
async def publish_task_draft(draft_id: str, payload: Publish, user: dict = Depends(admin)):
    try:
        return {"item": await service.publish_task_draft(draft_id, payload.expected_hash, payload.operation_id, user)}
    except Exception as exc:
        raise failure(exc) from exc


@router.post("/task-drafts/{draft_id}/reject")
async def reject_task_draft(draft_id: str, payload: Rejection, user: dict = Depends(admin)):
    async with service.WRITE_LOCK:
        item = store.get("task_draft", draft_id)
        if not item:
            raise HTTPException(404, "任务草稿不存在")
        if item["status"] == "published" or item.get("publication_operation_id"):
            raise HTTPException(409, "任务已确认发布，请先核对发布结果")
        item.update(status="rejected", rejection_reason=payload.reason, rejected_by=user["id"], rejected_at=db.utc_now())
        return {"item": store.put("task_draft", draft_id, item)}


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: str, payload: Rejection, user: dict = Depends(admin)):
    async with service.WRITE_LOCK:
        item = store.get("proposal", proposal_id)
        if not item:
            raise HTTPException(404, "建议不存在")
        if item["status"] == "published" or item.get("publication_operation_id"):
            raise HTTPException(409, "建议已确认发布，请先核对原操作结果")
        item.update(status="rejected", rejection_reason=payload.reason, rejected_by=user["id"], rejected_at=db.utc_now())
        return {"item": store.put("proposal", proposal_id, item)}


@router.post("/task-drafts/{draft_id}/delete")
async def delete_task_draft(draft_id: str, payload: Publish, user: dict = Depends(admin)):
    return await _draft_lifecycle('task_draft', draft_id, 'delete', payload, user)


@router.post("/task-drafts/{draft_id}/restore")
async def restore_task_draft(draft_id: str, payload: Publish, user: dict = Depends(admin)):
    return await _draft_lifecycle('task_draft', draft_id, 'restore', payload, user)


@router.post("/proposals/{proposal_id}/delete")
async def delete_proposal(proposal_id: str, payload: Publish, user: dict = Depends(admin)):
    return await _draft_lifecycle('proposal', proposal_id, 'delete', payload, user)


@router.post("/proposals/{proposal_id}/restore")
async def restore_proposal(proposal_id: str, payload: Publish, user: dict = Depends(admin)):
    return await _draft_lifecycle('proposal', proposal_id, 'restore', payload, user)


async def _draft_lifecycle(kind, identifier, action, payload, user):
    try:
        from server.draft_lifecycle import transition
        return {"item": await transition(kind, identifier, action, user, payload.operation_id, payload.expected_hash)}
    except Exception as exc:
        raise failure(exc) from exc
