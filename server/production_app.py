"""Formal RDOS 2.0 ASGI entry; importing this module fails closed on bad config."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from server.production_runtime import activate

FORMAL = activate()
from server import db, central_store, central_service, phase3_feishu
from server.central_api import router as central_router
from server.phase3_api import router as phase3_router, onboard_router
from server.central_accounts import router as account_router
from server.shared_knowledge import router as knowledge_router
from server.central_settings import router as settings_router
from server.central_maintenance import router as maintenance_router
from server.peer_tasks import router as peer_router

STATIC = Path(__file__).with_name('static')


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    central_store.init()
    from cutover.formal_migration import require_installed_marker
    require_installed_marker(FORMAL.database)
    phase3_feishu.configure_formal(FORMAL)
    from server import phase3_service, central_memory, central_settings, central_maintenance, phase1_runtime, peer_tasks
    phase1_runtime.initialize()
    phase3_service.initialize()
    peer_tasks.initialize()
    from server.central_api import _sync_registered_projects
    _sync_registered_projects()
    central_memory.initialize()
    central_settings.initialize()
    from server import next_release_migration
    next_release_migration.initialize()
    await central_maintenance.start(enabled=os.environ.get('RDOS_MAINTENANCE_ENABLED') == '1')
    try:
        yield
    finally:
        await central_maintenance.stop()
        await central_service.shutdown()


app = FastAPI(title='百特教育研发协同工作台 RDOS', docs_url=None,
              redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount('/static', StaticFiles(directory=STATIC), name='static')
for router in (central_router, phase3_router, onboard_router, account_router,
               knowledge_router, settings_router, maintenance_router, peer_router):
    app.include_router(router)


def _deprecated(path: str, method: str) -> bool:
    if path.startswith(('/api/lab/', '/lab/', '/dev')) or path in {'/dev', '/legacy'}:
        return True
    if path.startswith(('/api/central/onboarding', '/api/central/proposals/',
                        '/api/central/task-drafts/', '/api/central/phase3/developer',
                        '/api/central/phase3/migrate-workflow')):
        return True
    if path == '/api/central/sync' or path.startswith('/api/central/tasks'):
        return True
    if path.startswith('/api/central/sources') and method not in {'GET', 'HEAD'}:
        return True
    return False


@app.middleware('http')
async def formal_boundary(request: Request, call_next):
    path = request.scope['path']
    root = request.scope.get('root_path', '').rstrip('/')
    if root and path.startswith(root + '/'):
        path = path[len(root):]
    if _deprecated(path, request.method):
        return JSONResponse({'detail': '旧入口已停用，请使用当前协作与任务入口'}, status_code=410)
    if path.startswith(('/api/central/', '/api/auth/')) and request.method not in {'GET','HEAD','OPTIONS'}:
        if request.headers.get('origin') != 'https://yjmt.cn':
            return JSONResponse({'detail': '管理操作必须来自本控制台'}, status_code=403)
    response = await call_next(request)
    if path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
        "frame-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
    return response


@app.get('/')
def index():
    # Browser requests stay under the existing reverse-proxy subpath.  The
    # bundled lab page uses a root base for localhost; do not alter that file
    # or let a production browser send login requests to the site root.
    html = (STATIC / 'central.html').read_text(encoding='utf-8')
    html = html.replace('<base href="/" />', '<base href="/baite-rdos/" />', 1)
    html = html.replace('<aside class="lab-banner" role="status"><strong>RDOS 实验环境</strong><span id="lab-environment" class="developer-only"></span><p id="lab-capability" class="developer-only"></p></aside>', '', 1)
    return HTMLResponse(html, headers={'Cache-Control': 'no-store'})


@app.get('/api/runtime/info')
def runtime_info():
    from server import lab_panel
    return {**lab_panel.environment(), 'project_id': 'project_rdos',
            'runtime': lab_panel.runtime_status(), 'mode': 'production'}


@app.get('/api/health')
def health():
    return {'status': 'ok', 'app_release': os.environ.get('BAITE_APP_RELEASE', 'unverified'),
            'mode': 'production'}
