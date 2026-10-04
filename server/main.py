from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Literal, Optional, Type
from urllib.parse import urlsplit

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, field_validator

from server import db
from server.onboarding import instructions, validate_client_workspace
from server.project_sync import (
    export_pending_progress,
    scheduler as project_scheduler,
    sync_project,
)
from server.rag_sync import external_sync_disabled, scheduler, sync_selected_rag
from server.workflow_reference import (
    DELIVERY_SCALES,
    DEVELOPMENT_MODES,
    NODE_STATUSES,
    PRODUCT_TYPES,
    TODO_STATUSES,
    catalog as workflow_catalog,
)


STATIC_DIR = Path(__file__).resolve().parent / "static"
SESSION_COOKIE = "baite_admin_session"
SESSION_HOURS = int(os.environ.get("BAITE_SESSION_HOURS", "12"))
ONLINE_WINDOW_SECONDS = float(os.environ.get("BAITE_ONLINE_WINDOW_SECONDS", "60"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(scheduler(stop)),
        asyncio.create_task(project_scheduler(stop)),
    ]
    try:
        yield
    finally:
        stop.set()
        await asyncio.gather(*tasks)


app = FastAPI(
    title="Baite AI R&D OS",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def admin_origin_guard(request: Request, call_next):
    path = request.scope["path"]
    root_path = request.scope.get("root_path", "").rstrip("/")
    if root_path and path.startswith(root_path + "/"):
        path = path[len(root_path):]
    protected = path.startswith(("/api/admin/", "/api/auth/"))
    if protected and request.method not in ("GET", "HEAD", "OPTIONS"):
        public = urlsplit(_public_url())
        if request.headers.get("origin") != f"{public.scheme}://{public.netloc}":
            return JSONResponse({"detail": "管理操作必须来自本控制台"}, status_code=403)
    response = await call_next(request)
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def require_external_sync() -> None:
    if external_sync_disabled():
        raise HTTPException(status_code=503, detail="外部同步已关闭，请先配置云端飞书授权并启用同步")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginInput(StrictModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=500)


class PasswordInput(StrictModel):
    current_password: str = Field(min_length=1, max_length=500)
    new_password: str = Field(min_length=10, max_length=500)


class RulesInput(StrictModel):
    content: str = Field(min_length=1, max_length=100_000)


class ContractInput(StrictModel):
    organization_guidance: str = Field(min_length=1, max_length=100_000)
    selected_rag_required: StrictBool
    shared_skill_policy: Literal["recommended", "optional"]
    expected_contract_revision: StrictInt = Field(ge=1)


class ContractRestoreInput(StrictModel):
    expected_contract_revision: StrictInt = Field(ge=1)


class RunnerCreateInput(StrictModel):
    display_name: str = Field(min_length=1, max_length=100)
    workspace: str = Field(default="", max_length=2000)
    delivery: Literal["config", "enrollment"] = "config"


class RunnerHeartbeatInput(StrictModel):
    platform: Optional[Literal["windows", "darwin", "linux", "macos"]] = None
    runner_version: Optional[str] = Field(default=None, max_length=100)
    actual_workspace: Optional[str] = Field(default=None, max_length=2000)
    installation_id: Optional[str] = Field(default=None, min_length=16, max_length=100)
    bootstrapper_version: Optional[str] = Field(default=None, max_length=100)
    protocol_version: Optional[str] = Field(default=None, max_length=100)

    @field_validator("actual_workspace")
    @classmethod
    def workspace_path(cls, value):
        return validate_client_workspace(value) if value is not None else None


class RunnerUpdateInput(StrictModel):
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    enabled: Optional[bool] = None


class ProposalDecisionInput(StrictModel):
    decision: Literal["publish", "return"]
    feedback: str = Field(default="", max_length=5000)


class CollaborationDecisionInput(StrictModel):
    decision: Literal["confirm", "return"]
    feedback: str = Field(default="", max_length=5000)


class SyncStatusInput(StrictModel):
    revision: int = Field(ge=-1)
    health: Literal["healthy", "recovering", "needs_admin"]
    failure_count: int = Field(ge=0, le=10_000)
    error: str = Field(default="", max_length=5000)


class EventBase(StrictModel):
    event_id: str = Field(min_length=8, max_length=200)


class InstallationSelfTestEvent(EventBase):
    type: Literal["installation_self_test"]
    installation_id: str = Field(min_length=16, max_length=100)
    shared_revision: int = Field(ge=0)
    project_count: int = Field(ge=0)


class BootstrapClaimInput(StrictModel):
    runner_id: str = Field(pattern=r"^rnr_[A-Za-z0-9_-]{12,100}$")
    enrollment_code: str = Field(min_length=32, max_length=200)
    installation_id: str = Field(min_length=16, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    candidate_token: str = Field(min_length=40, max_length=150, pattern=r"^brt_[A-Za-z0-9_-]+$")


class ProgressEvent(EventBase):
    type: Literal["progress"]
    current_focus: str = Field(min_length=1, max_length=500)
    status: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=5000)
    needs_collaboration: str = Field(default="", max_length=5000)


class ActivityEvent(EventBase):
    type: Literal["activity_record"]
    kind: Literal["skill", "workflow", "method"]
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(default="", max_length=100)
    purpose: str = Field(min_length=1, max_length=1000)
    recorded_at: str = Field(min_length=1, max_length=100)


class SkillProposalEvent(EventBase):
    type: Literal["skill_change_proposal"]
    skill_name: str = Field(min_length=1, max_length=200)
    base_version: str = Field(default="", max_length=100)
    proposed_version: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=5000)
    content: str = Field(min_length=1, max_length=2_000_000)


class CollaborationRequestEvent(EventBase):
    type: Literal["collaboration_request"]
    to_runner_id: str = Field(min_length=1, max_length=200)
    category: Literal["ordinary", "risk", "external_release", "irreversible"]
    topic: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=5000)
    reference: str = Field(default="", max_length=2000)


class CollaborationResponseEvent(EventBase):
    type: Literal["collaboration_response"]
    collaboration_id: int = Field(gt=0)
    action: Literal["confirm", "return", "escalate"]
    note: str = Field(default="", max_length=5000)


class ProjectWorkflowNode(StrictModel):
    code: Literal["C0", "C1", "C2", "C3", "C4", "C5", "C6", "C7"]
    status: Literal[
        "not_started", "in_progress", "complete", "blocked", "not_applicable"
    ]
    evidence: list[str] = Field(default_factory=list, max_length=50)


class ProjectTodo(StrictModel):
    todo_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._-]+$")
    title: str = Field(min_length=1, max_length=500)
    owner: str = Field(default="", max_length=200)
    status: Literal["open", "in_progress", "blocked", "done"]
    due_date: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class ProjectFileMeta(StrictModel):
    path: str = Field(min_length=1, max_length=2000)
    size_bytes: int = Field(ge=0)
    modified_at: str = Field(min_length=1, max_length=100)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: str = Field(default="", max_length=100)
    summary: str = Field(default="", max_length=1000)

    @field_validator("path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts or not value.startswith("files/"):
            raise ValueError("文件路径必须位于项目 work/files 下")
        return candidate.as_posix()


class ProjectGateClaim(StrictModel):
    code: Literal["G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7"]
    claimed_passed: bool
    evidence: list[str] = Field(default_factory=list, max_length=50)
    note: str = Field(default="", max_length=5000)


class ProjectUpdateEvent(EventBase):
    type: Literal["project_update"]
    project_id: str = Field(min_length=5, max_length=100)
    summary: str = Field(default="", max_length=5000)
    complete: bool = False
    workflow_nodes: list[ProjectWorkflowNode] = Field(default_factory=list, max_length=8)
    todos: list[ProjectTodo] = Field(default_factory=list, max_length=200)
    issues: list[str] = Field(default_factory=list, max_length=100)
    file_manifest: list[ProjectFileMeta] = Field(default_factory=list, max_length=20_000)
    gate_claim: Optional[ProjectGateClaim] = None


EVENT_MODELS: Dict[str, Type[EventBase]] = {
    "installation_self_test": InstallationSelfTestEvent,
    "progress": ProgressEvent,
    "activity_record": ActivityEvent,
    "skill_change_proposal": SkillProposalEvent,
    "collaboration_request": CollaborationRequestEvent,
    "collaboration_response": CollaborationResponseEvent,
    "project_update": ProjectUpdateEvent,
}


class ProjectCreateInput(StrictModel):
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=10_000)
    participant_runner_ids: list[str] = Field(min_length=1, max_length=100)
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    daily_cutoff: str = Field(default="18:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=100)
    feishu_folder_url: str = Field(min_length=8, max_length=2000)
    development_mode: Literal[
        "new_product", "existing_new_funder", "existing_new_context", "known_issue_iteration"
    ]
    product_types: list[
        Literal[
            "course", "picture_book", "game", "camp_pbl", "large_event",
            "space_system", "digital_remote", "video", "venue_study", "compliance_trust",
            "other"
        ]
    ] = Field(default_factory=list)
    delivery_scales: list[
        Literal["single_site", "multi_site", "external_supplier", "outcome_evidence"]
    ] = Field(default_factory=list)
    status: Literal["active", "paused", "completed"] = "active"


class ProjectUpdateInput(StrictModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=300)
    description: Optional[str] = Field(default=None, max_length=10_000)
    participant_runner_ids: Optional[list[str]] = Field(default=None, min_length=1, max_length=100)
    start_date: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    daily_cutoff: Optional[str] = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: Optional[str] = Field(default=None, min_length=1, max_length=100)
    feishu_folder_url: Optional[str] = Field(default=None, min_length=8, max_length=2000)
    development_mode: Optional[
        Literal["new_product", "existing_new_funder", "existing_new_context", "known_issue_iteration"]
    ] = None
    product_types: Optional[list[str]] = None
    delivery_scales: Optional[list[str]] = None
    status: Optional[Literal["active", "paused", "completed"]] = None


class ProjectTodoInput(StrictModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=500)
    owner: Optional[str] = Field(default=None, max_length=200)
    status: Optional[Literal["open", "in_progress", "blocked", "done"]] = None
    due_date: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class ProjectGateDecisionInput(StrictModel):
    decision: Literal["confirm", "return"]
    feedback: str = Field(default="", max_length=5000)


def _cookie_secure() -> bool:
    return os.environ.get("BAITE_COOKIE_SECURE", "false").lower() in ("1", "true", "yes")


def _public_url() -> str:
    return os.environ.get("BAITE_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")


def _cookie_path() -> str:
    return urlsplit(_public_url()).path.rstrip("/") + "/"


def _online(last_seen_at: Optional[str]) -> bool:
    if not last_seen_at:
        return False
    try:
        last_seen = datetime.fromisoformat(last_seen_at)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - last_seen).total_seconds() <= ONLINE_WINDOW_SECONDS


def _runners_with_presence() -> list[Dict[str, Any]]:
    runners = db.list_runners()
    for runner in runners:
        runner["online"] = bool(runner["enabled"] and _online(runner["last_seen_at"]))
        runner["self_test"] = db.installation_status(runner["id"])["self_test"] if runner["installation_id"] else None
    return runners


def require_admin(
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
) -> Dict[str, Any]:
    if not session_token or not db.admin_session_valid(session_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="需要系统管理员登录")
    return db.admin_profile()


def require_runner(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 Runner Token")
    runner = db.authenticate_runner(authorization[7:].strip())
    if not runner:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Runner Token 无效或节点已停用")
    return runner


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "app_release": os.environ.get("BAITE_APP_RELEASE", "development")}


@app.get("/api/bootstrap/manifest")
def bootstrap_manifest() -> Dict[str, Any]:
    path = os.environ.get("BAITE_BOOTSTRAP_MANIFEST_PATH", "")
    if not path:
        raise HTTPException(status_code=503, detail="安装包尚未发布")
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="安装清单不可用") from exc
    return manifest


@app.post("/api/bootstrap/claim")
def bootstrap_claim(payload: BootstrapClaimInput) -> Dict[str, str]:
    try:
        return db.claim_enrollment(payload.runner_id, payload.enrollment_code,
                                   payload.installation_id, payload.candidate_token)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/auth/login")
def login(payload: LoginInput, response: Response) -> Dict[str, Any]:
    if not db.authenticate_admin(payload.username.strip(), payload.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    token = db.create_admin_session(SESSION_HOURS)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_HOURS * 3600,
        httponly=True,
        secure=_cookie_secure(),
        samesite="strict",
        path=_cookie_path(),
    )
    return {"admin": db.admin_profile()}


@app.post("/api/auth/logout")
def logout(
    response: Response,
    _: Dict[str, Any] = Depends(require_admin),
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
) -> Dict[str, bool]:
    if session_token:
        db.delete_admin_session(session_token)
    response.delete_cookie(SESSION_COOKIE, path=_cookie_path())
    return {"ok": True}


@app.get("/api/auth/me")
def me(admin: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    return {"admin": admin}


@app.post("/api/auth/change-password")
def change_password(
    payload: PasswordInput,
    response: Response,
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, bool]:
    try:
        db.change_admin_password(payload.current_password, payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="当前密码不正确") from exc
    response.delete_cookie(SESSION_COOKIE, path=_cookie_path())
    return {"ok": True, "login_required": True}


@app.get("/api/admin/overview")
def admin_overview(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    result = db.overview()
    result["runners"] = _runners_with_presence()
    result["offline_after_seconds"] = ONLINE_WINDOW_SECONDS
    return result


@app.get("/api/admin/work-queue")
def admin_work_queue(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    return db.work_queue()


@app.get("/api/admin/runners")
def admin_runners(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    return {"items": _runners_with_presence(), "offline_after_seconds": ONLINE_WINDOW_SECONDS}


@app.post("/api/admin/runners", status_code=201)
def create_runner(
    payload: RunnerCreateInput, _: Dict[str, Any] = Depends(require_admin)
) -> Dict[str, Any]:
    try:
        workspace = validate_client_workspace(payload.workspace)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    item = db.create_runner(payload.display_name.strip(), workspace, _public_url())
    if payload.delivery == "enrollment":
        item.pop("runner_token", None)
        item.pop("config", None)
        return {"item": item, "enrollment": db.issue_enrollment(item["id"]), "token_visible_once": False}
    return {"item": item, "token_visible_once": True}


@app.post("/api/admin/runners/{runner_id}/enrollment")
def issue_runner_enrollment(runner_id: str, _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    try:
        return db.issue_enrollment(runner_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Runner 不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/api/admin/runners/{runner_id}/enrollment")
def revoke_runner_enrollment(runner_id: str, _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, bool]:
    try:
        db.revoke_enrollment(runner_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Runner 不存在") from exc
    return {"ok": True}


@app.get("/api/admin/runners/{runner_id}/installation")
def admin_installation_status(runner_id: str, _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    try:
        return db.installation_status(runner_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Runner 不存在") from exc


@app.get("/api/admin/runners/{runner_id}/onboarding")
def runner_onboarding(
    runner_id: str, platform: Literal["macos", "windows"] = "macos",
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    if not db.get_runner(runner_id):
        raise HTTPException(status_code=404, detail="Runner 不存在")
    return instructions(runner_id, platform, _public_url())


@app.patch("/api/admin/runners/{runner_id}")
def edit_runner(
    runner_id: str,
    payload: RunnerUpdateInput,
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    try:
        item = db.update_runner(
            runner_id,
            payload.display_name.strip() if payload.display_name is not None else None,
            payload.enabled,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Runner 不存在") from exc
    item["online"] = bool(item["enabled"] and _online(item.get("last_seen_at")))
    return {"item": item}


@app.post("/api/admin/runners/{runner_id}/rotate-token")
def rotate_runner(
    runner_id: str, _: Dict[str, Any] = Depends(require_admin)
) -> Dict[str, Any]:
    try:
        token = db.rotate_runner_token(runner_id)
        runner = db.get_runner(runner_id)
        if not runner:
            raise LookupError
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Runner 不存在") from exc
    config = {
        "control_url": _public_url(),
        "runner_id": runner_id,
        "runner_token": token,
        "workspace": runner["workspace_path"],
    }
    return {"runner_token": token, "config": config, "token_visible_once": True}


@app.post("/api/admin/runners/{runner_id}/retry-sync")
def retry_runner(runner_id: str, _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    try:
        return {"item": db.retry_runner_sync(runner_id)}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Runner 不存在") from exc


@app.get("/api/admin/global-rules")
def get_rules(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    return db.get_rules()


@app.put("/api/admin/global-rules")
def publish_rules(payload: RulesInput, _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    raise HTTPException(status_code=409, detail="Global Rules 是 Operating Contract 生成视图，请使用 /api/admin/global-contract")


@app.get("/api/admin/global-contract")
def global_contract(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    return db.get_global_contract()


@app.put("/api/admin/global-contract")
def publish_global_contract(payload: ContractInput, _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    try:
        return db.update_global_contract(payload.organization_guidance, payload.selected_rag_required,
                                         payload.shared_skill_policy, payload.expected_contract_revision)
    except ValueError as exc:
        raise HTTPException(status_code=409 if str(exc) == "stale_contract_revision" else 400,
                            detail=str(exc)) from exc


@app.get("/api/admin/global-contract/history")
def global_contract_history(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    return {"items": db.list_global_contract_history()}


@app.post("/api/admin/global-contract/history/{contract_revision}/restore")
def restore_global_contract(contract_revision: int, payload: ContractRestoreInput,
                            _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    try:
        return db.restore_global_contract(contract_revision, payload.expected_contract_revision)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="历史版本不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409 if str(exc) == "stale_contract_revision" else 400,
                            detail=str(exc)) from exc


@app.get("/api/admin/shared-skills")
def shared_skills(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    return {"items": db.list_skills(include_content=True)}


@app.get("/api/admin/skill-proposals")
def skill_proposals(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    return {"items": db.list_skill_proposals()}


@app.post("/api/admin/skill-proposals/{proposal_id}/decision")
def decide_skill_proposal(
    proposal_id: int,
    payload: ProposalDecisionInput,
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    if payload.decision == "return" and not payload.feedback.strip():
        raise HTTPException(status_code=400, detail="退回时请填写反馈")
    try:
        item = (
            db.publish_skill_proposal(proposal_id)
            if payload.decision == "publish"
            else db.return_skill_proposal(proposal_id, payload.feedback.strip())
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Proposal 不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="Proposal 已处理") from exc
    return {"item": item}


@app.get("/api/admin/selected-rag")
def selected_rag(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    return {"state": db.rag_state(), "items": db.active_rag_files(include_content=False)}


@app.post("/api/admin/selected-rag/sync")
async def synchronize_rag(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    require_external_sync()
    result = await asyncio.to_thread(sync_selected_rag, None, True)
    if not result.get("ok"):
        code = 409 if result.get("busy") else 502
        raise HTTPException(status_code=code, detail=result.get("error", "同步正在进行"))
    return result


@app.get("/api/admin/activities")
def activities(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    return {"items": db.list_activities()}


@app.get("/api/admin/collaborations")
def collaborations(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    return {"items": db.list_collaborations()}


@app.post("/api/admin/collaborations/{collaboration_id}/decision")
def decide_collaboration(
    collaboration_id: int,
    payload: CollaborationDecisionInput,
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    if payload.decision == "return" and not payload.feedback.strip():
        raise HTTPException(status_code=400, detail="退回时请填写反馈")
    try:
        item = db.decide_collaboration(
            collaboration_id, payload.decision, payload.feedback.strip()
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="协作请求不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="该请求不等待管理员处理") from exc
    return {"item": item}


@app.get("/api/admin/projects")
def admin_projects(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    return {"items": db.list_projects()}


@app.post("/api/admin/projects", status_code=201)
def create_project(
    payload: ProjectCreateInput, _: Dict[str, Any] = Depends(require_admin)
) -> Dict[str, Any]:
    try:
        item = db.create_project(payload.model_dump())
    except ValueError as exc:
        messages = {
            "invalid_feishu_folder": "请输入有效的飞书文件夹链接或 Token",
            "participants_required": "至少选择一个参与 Runner",
            "invalid_project_dates": "项目结束日期不能早于开始日期",
            "participant_runner_not_found": "参与 Runner 不存在或已停用",
            "folder_already_in_use": "该飞书文件夹已绑定其他未完成项目",
        }
        raise HTTPException(status_code=409, detail=messages.get(str(exc), str(exc))) from exc
    return {"item": item, "source_sync_queued": True}


@app.get("/api/admin/projects/{project_id}")
def admin_project(
    project_id: str, _: Dict[str, Any] = Depends(require_admin)
) -> Dict[str, Any]:
    item = db.get_project(project_id, include_history=True)
    if not item:
        raise HTTPException(status_code=404, detail="项目不存在")
    return {"item": item}


@app.patch("/api/admin/projects/{project_id}")
def edit_project(
    project_id: str,
    payload: ProjectUpdateInput,
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    data = payload.model_dump(exclude_unset=True)
    allowed_products = {item["value"] for item in PRODUCT_TYPES}
    allowed_scales = {item["value"] for item in DELIVERY_SCALES}
    if "product_types" in data and not set(data["product_types"]).issubset(allowed_products):
        raise HTTPException(status_code=422, detail="包含未知产品类型")
    if "delivery_scales" in data and not set(data["delivery_scales"]).issubset(allowed_scales):
        raise HTTPException(status_code=422, detail="包含未知交付规模")
    try:
        return {"item": db.update_project(project_id, data)}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="项目不存在") from exc
    except ValueError as exc:
        messages = {
            "invalid_feishu_folder": "请输入有效的飞书文件夹链接或 Token",
            "participants_required": "至少选择一个参与 Runner",
            "invalid_project_dates": "项目结束日期不能早于开始日期",
            "participant_runner_not_found": "参与 Runner 不存在或已停用",
            "folder_already_in_use": "该飞书文件夹已绑定其他未完成项目",
        }
        raise HTTPException(status_code=409, detail=messages.get(str(exc), str(exc))) from exc


@app.post("/api/admin/projects/{project_id}/sync")
async def synchronize_project(
    project_id: str, _: Dict[str, Any] = Depends(require_admin)
) -> Dict[str, Any]:
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    require_external_sync()
    result = await asyncio.to_thread(sync_project, project_id, None, True)
    if not result.get("ok"):
        code = 409 if result.get("busy") else 502
        raise HTTPException(status_code=code, detail=result.get("error", "项目同步正在进行"))
    return result


@app.patch("/api/admin/projects/{project_id}/todos/{todo_id}")
def edit_project_todo(
    project_id: str,
    todo_id: str,
    payload: ProjectTodoInput,
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    try:
        return {
            "item": db.update_project_todo(
                project_id, todo_id, payload.model_dump(exclude_unset=True)
            )
        }
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="项目不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="TODO 标题不能为空") from exc


@app.post("/api/admin/projects/{project_id}/gate-records/{record_id}/decision")
def decide_project_gate(
    project_id: str,
    record_id: int,
    payload: ProjectGateDecisionInput,
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    if payload.decision == "return" and not payload.feedback.strip():
        raise HTTPException(status_code=400, detail="退回核实时请填写反馈")
    try:
        return {
            "item": db.decide_project_gate(
                project_id, record_id, payload.decision, payload.feedback.strip()
            )
        }
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Gate 记录不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="Gate 记录已核实") from exc


@app.post("/api/admin/projects/{project_id}/retry-writeback")
async def retry_project_writeback(
    project_id: str,
    runner_id: Optional[str] = None,
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    require_external_sync()
    try:
        db.retry_project_export(project_id, runner_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="项目或写回状态不存在") from exc
    results = await asyncio.to_thread(export_pending_progress)
    return {"ok": all(item.get("ok") for item in results), "items": results}


@app.get("/api/admin/workflow-reference")
def workflow_reference(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    return {"catalog": workflow_catalog(), "state": db.workflow_reference_state()}


@app.post("/api/runner/heartbeat")
def runner_heartbeat(payload: Optional[RunnerHeartbeatInput] = None,
                     runner: Dict[str, Any] = Depends(require_runner)) -> Dict[str, bool]:
    try:
        db.heartbeat(runner["id"], payload.model_dump(exclude_none=True) if payload else None)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/runner/installation-status")
def runner_installation_status(runner: Dict[str, Any] = Depends(require_runner)) -> Dict[str, Any]:
    return db.installation_status(runner["id"])


@app.get("/api/runner/projects/sync")
def runner_projects_sync(
    known_token: str = "", runner: Dict[str, Any] = Depends(require_runner)
) -> Dict[str, Any]:
    snapshot = db.runner_project_snapshot(runner["id"])
    current = snapshot["sync_token"]
    if known_token and known_token == current:
        return {"changed": False, "sync_token": current}
    snapshot["changed"] = True
    return snapshot


@app.get("/api/runner/sync")
def runner_sync(
    known_revision: int = -1, runner: Dict[str, Any] = Depends(require_runner)
) -> Dict[str, Any]:
    revision = db.current_revision()
    current = db.get_runner(runner["id"]) or runner
    if known_revision == revision:
        return {
            "changed": False,
            "revision": revision,
            "retry_nonce": current["retry_nonce"],
        }
    snapshot = db.shared_snapshot(runner["id"])
    snapshot["changed"] = True
    return snapshot


@app.post("/api/runner/events")
def runner_event(
    payload: Dict[str, Any], runner: Dict[str, Any] = Depends(require_runner)
) -> Dict[str, Any]:
    event_type = str(payload.get("type") or "")
    model = EVENT_MODELS.get(event_type)
    if not model:
        raise HTTPException(status_code=422, detail="不支持的事件类型")
    try:
        validated = model.model_validate(payload).model_dump()
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    try:
        result, duplicate = db.process_runner_event(runner["id"], validated)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ack": True, "event_id": validated["event_id"], "duplicate": duplicate, "result": result}


@app.post("/api/runner/sync-status")
def runner_sync_status(
    payload: SyncStatusInput, runner: Dict[str, Any] = Depends(require_runner)
) -> Dict[str, Any]:
    item = db.update_runner_sync_status(
        runner["id"],
        payload.revision,
        payload.health,
        payload.failure_count,
        payload.error,
    )
    return {"ok": True, "runner": item}
