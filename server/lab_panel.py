"""Exact phase-1 Panel boundary; node communication remains disabled."""
from __future__ import annotations

import os
import re
import hashlib

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from lab.guard import require_active

router = APIRouter()
PROJECT_ID = "lab_project_phase0"


def environment() -> dict:
    if os.environ.get('RDOS_RUNTIME_MODE') == 'production':
        from server.production_runtime import require
        resources = require().resources
        generation = hashlib.sha256((resources['base_token'] + ':' + resources['task_table']).encode()).hexdigest()[:32]
        return {'env_id': 'rdos_formal', 'generation': generation}
    require_active()
    names = ("RDOS_LAB_ENV_ID", "RDOS_LAB_GENERATION")
    values = [os.environ.get(name, "") for name in names]
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value) for value in values):
        raise RuntimeError("实验环境标识或运行代次缺失/无效")
    return {"env_id": values[0], "generation": values[1]}


def cookie_name(kind: str) -> str:
    if os.environ.get('RDOS_RUNTIME_MODE') == 'production':
        from server.production_runtime import require
        require()
        if kind not in {'admin', 'central'}:
            raise ValueError('未知会话类别')
        return 'rdos2_' + kind
    info = environment()
    return f"rdos_lab_{kind}_{info['env_id']}_{info['generation']}"


def runtime_status() -> dict:
    if os.environ.get('RDOS_RUNTIME_MODE') == 'production':
        from server.production_runtime import require
        require()
        from server import central_store
        paused = bool((central_store.get('state', 'runtime') or {}).get('paused'))
        return {'paused': paused, 'schedule_verified': False,
                'model_configured': True, 'model': 'deepseek-v4-pro',
                'model_mode': 'agent_plan', 'feishu_configured': True,
                'backend': 'feishu_production', 'scheduler_enabled': False,
                'external_enabled': True, 'phase3_enabled': True,
                'phase3_backend': 'feishu_production',
                'phase3_node_transport': 'feishu_only', 'phase': 3}
    phase3_live = os.environ.get('RDOS_LAB_PHASE3_ENABLED') == '1'
    model_live = os.environ.get('RDOS_LAB_MODEL_MODE') == 'agent_plan'
    return {"paused": False, "schedule_verified": False,
            "model_configured": model_live, "model": "deepseek-v4-pro" if model_live else "mock",
            "model_mode": "agent_plan" if model_live else "mock",
            "feishu_configured": False, "backend": "local_mock",
            "scheduler_enabled": False, "external_enabled": phase3_live,
            "phase3_enabled": phase3_live, "phase3_backend": "feishu_test" if phase3_live else "local_mock",
            "phase3_node_transport": "feishu_only", "phase": 3 if phase3_live else 1}


@router.get("/api/lab/info")
def info() -> dict:
    return {**environment(), "project_id": PROJECT_ID,
            "label": "RDOS 本地实验环境", "runtime": runtime_status(),
            "verification_boundary": ("阶段3仅登记的飞书测试对象可读写；模型配置为Agent Plan，实际调用与两个真实客户端需单独验收。"
                if os.environ.get('RDOS_LAB_PHASE3_ENABLED') == '1' else
                "中央编排只使用本地测试后端；节点连接、真实模型及飞书均未启用。")}


READ_PATHS = frozenset({
    "/", "/api/health", "/api/central/auth/me", "/api/central/projects",
    "/api/central/workspace", "/api/central/overview", "/api/central/sources",
    "/api/central/threads", "/api/central/runs", "/api/central/diagnostics", "/api/lab/info",
    "/api/central/users", "/api/central/knowledge",
    "/api/central/settings/agent", "/api/central/settings/memories", "/api/central/settings/memory-preview",
    "/api/central/maintenance/state", "/api/central/maintenance/runs", "/api/central/peer/nodes",
    "/static/central.html", "/static/central.js", "/static/central.css", "/dev",
})
POST_PATHS = frozenset({"/api/central/auth/login", "/api/central/auth/logout",
    "/api/central/auth/change-password", "/api/central/projects",
    "/api/central/threads", "/api/central/chat", "/api/central/sync",
    "/api/central/users", "/api/central/knowledge/sync", "/api/central/settings/memories",
    "/api/central/maintenance/refresh", "/api/central/maintenance/inspect"})
OBJECT_ID = r"[A-Za-z0-9_.:-]{1,150}"
READ_OBJECTS = re.compile(r"/api/central/runs/" + OBJECT_ID)
POST_OBJECTS = re.compile(r"/api/central/(?:runs/" + OBJECT_ID + r"/retry|(?:task-drafts|proposals)/" +
                          OBJECT_ID + r"/(?:edit|publish|reject|delete|restore)|projects/" + OBJECT_ID +
                          r"/(?:deactivate|reactivate)|users/" + OBJECT_ID + r"/reset-password)")
PROJECT_READ = re.compile(r"/api/central/projects/" + OBJECT_ID + r"/deactivation-preview")
PROJECT_EDIT = re.compile(r"/api/central/projects/" + OBJECT_ID)
USER_EDIT = re.compile(r"/api/central/users/" + OBJECT_ID)
KNOWLEDGE_READ = re.compile(r"/api/central/knowledge/items/" + OBJECT_ID)
MAINTENANCE_READ = re.compile(r"/api/central/maintenance/runs/" + OBJECT_ID)
PEER_BIND = re.compile(r"/api/central/peer/nodes/" + OBJECT_ID + r"/identity")
PEER_APPROVE = re.compile(r"/api/central/peer/nodes/" + OBJECT_ID + r"/approve-enrollment")
PHASE3_READ = re.compile(r"(?:/api/central/phase3/(?:workspace|developer|tasks/" + OBJECT_ID +
    r"/history)|/lab/onboard/(?:node_lab_phase3_codex|node_lab_phase3_workbuddy)|/nodes/"+OBJECT_ID+r"/(?:start|manifest|skill\.zip))")
PHASE3_POST = re.compile(r"/api/central/phase3/(?:drafts|sync|migrate-workflow|drafts/" + OBJECT_ID +
    r"/(?:edit|delete|restore)|tasks/" + OBJECT_ID + r"/migrate-completion|nodes/" + OBJECT_ID + r"/onboarding|tasks/" + OBJECT_ID + r"/actions/(?:start|update|pause|resume|submit|manual_complete|cancel))")


async def boundary(request: Request, call_next):
    """Deny unreviewed legacy routes, including GET routes that return live instructions."""
    require_active()
    path = request.scope["path"]
    allowed = ((request.method in {"GET", "HEAD"} and (path in READ_PATHS or READ_OBJECTS.fullmatch(path) or PHASE3_READ.fullmatch(path) or PROJECT_READ.fullmatch(path) or KNOWLEDGE_READ.fullmatch(path) or MAINTENANCE_READ.fullmatch(path)))
               or (request.method == "POST" and (path in POST_PATHS or POST_OBJECTS.fullmatch(path) or PHASE3_POST.fullmatch(path) or PEER_BIND.fullmatch(path) or PEER_APPROVE.fullmatch(path)))
               or (request.method == 'PATCH' and (PROJECT_EDIT.fullmatch(path) or USER_EDIT.fullmatch(path) or path=='/api/central/maintenance/schedule'))
               or (request.method == 'PUT' and path=='/api/central/settings/agent'))
    if not allowed:
        return JSONResponse({"detail": {"code": "lab_route_disabled",
            "message": "当前入口未启用。"}}, status_code=503)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
        "frame-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
    return response
