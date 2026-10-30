"""Panel-only phase-3 actions and a deliberately small public onboarding page.

Nodes exchange business data through the registered Feishu test resources.
This module provides no localhost node read/write API.
"""
from __future__ import annotations

from html import escape
from typing import Any, Literal
from urllib.parse import urlsplit
from pathlib import Path
import hashlib
import json
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from server import central_api, phase3_service as service, rdos_contract, phase3_backend
from server.lab_routes import SanitizedLabRoute
from lab.build_node_skill import SKILL_VERSION


router = APIRouter(prefix="/api/central/phase3", tags=["phase3-panel"], route_class=SanitizedLabRoute)
onboard_router = APIRouter(tags=["phase3-readonly-instructions"], route_class=SanitizedLabRoute)


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")


class Draft(Operation):
    payload: dict[str, Any]
    project_id: str | None = None


class DraftEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    payload: dict[str, Any]


class Action(Operation):
    expected_revision: int = Field(ge=0)
    expected_requirements_version: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


def _failure(exc: Exception) -> HTTPException:
    result = central_api.failure(exc)
    # Business errors may distinguish a missing task from stale requirements.
    status = getattr(exc, "status", None)
    if isinstance(status, int) and status in (400, 403, 404, 409, 422, 503):
        result.status_code = status
    return result


def _display(value):
    displayed = central_api.safe_display(value)

    def mark(original, safe):
        if isinstance(original, dict) and isinstance(safe, dict):
            if original.get("draft_hash") and any(original.get(key) != safe.get(key) for key in ("payload", "change_reason")):
                safe["display_redacted"] = True
            for key in original.keys() & safe.keys():
                mark(original[key], safe[key])
        elif isinstance(original, list) and isinstance(safe, list):
            for before, after in zip(original, safe):
                mark(before, after)

    mark(value, displayed)
    return rdos_contract.public_view(displayed, {'nodes': service.node_registry()})


def _project(project_id):
    return rdos_contract.internal_input(project_id or rdos_contract.PUBLIC_PROJECT_ID)


@router.get("/workspace")
def workspace(project_id: str | None = None, actor: dict = Depends(central_api.current_user)):
    try:
        from server import rdos_projects
        rdos_projects.require_read(_project(project_id), actor)
        return _display(service.business_workspace(actor, _project(project_id)) if project_id is not None else service.business_workspace(actor))
    except Exception as exc:
        raise _failure(exc) from exc


@router.get("/tasks/{task_id}/history")
def task_history(task_id: str, actor: dict = Depends(central_api.current_user)):
    try:
        if 'admin' in actor['roles']:
            result = service.task_history(task_id, actor)
            current = service.get_task(task_id, actor)
        else:
            from server import phase3_store
            current = service.readable_task(task_id, actor)
            # A history list needs no admin action evidence or remote read.
            result = {'versions': phase3_store.history(task_id),
                      'events': [e for e in phase3_store.events() if e.get('project_id') == current['project_id']
                                 and any(ref.get('task_id') == task_id for ref in e.get('task_refs', []))]}
        items = result if isinstance(result, list) else [*result.get('versions',[]), *result.get('events',[])]
        return _display({'items': [{key: item[key] for key in ('requirements_version','record_revision','event_type','created_at','updated_at','close_reason') if key in item} | {'title': current['title']} for item in items]})
    except Exception as exc:
        raise _failure(exc) from exc


@router.post("/drafts")
async def prepare_draft(payload: Draft, project_id: str | None = None, actor: dict = Depends(central_api.admin)):
    try:
        if payload.project_id and project_id and _project(payload.project_id) != _project(project_id):
            raise PermissionError('草稿项目不一致')
        scope = {'project_id':_project(project_id or payload.project_id)} if project_id or payload.project_id else {}
        item = await service.prepare_task(rdos_contract.internal_input(payload.payload, {'nodes': service.node_registry()}), actor, payload.operation_id, **scope)
        return {"item": _display(item)}
    except Exception as exc:
        raise _failure(exc) from exc


@router.post("/drafts/{draft_id}/edit")
async def edit_draft(draft_id: str, payload: DraftEdit, project_id: str | None = None, actor: dict = Depends(central_api.admin)):
    try:
        from server import phase3_store
        draft = phase3_store.get('draft', draft_id)
        if project_id and draft and draft['project_id'] != _project(project_id):
            raise PermissionError('草稿不属于当前项目')
        item = await service.edit_draft(draft_id, rdos_contract.internal_input(payload.payload, {'nodes': service.node_registry()}), actor, payload.expected_hash)
        return {"item": _display(item)}
    except Exception as exc:
        raise _failure(exc) from exc


class DraftLifecycle(Operation):
    expected_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


@router.post("/drafts/{draft_id}/{action}")
async def draft_lifecycle(draft_id: str, action: Literal["delete", "restore"], payload: DraftLifecycle,
                          actor: dict = Depends(central_api.admin)):
    try:
        from server.draft_lifecycle import transition
        item = await transition('draft', draft_id, action, actor, payload.operation_id, payload.expected_hash)
        return {"item": _display(service.business_draft(item))}
    except Exception as exc:
        raise _failure(exc) from exc


@router.post("/tasks/{task_id}/actions/{action}")
async def task_action(task_id: str, action: Literal["start", "update", "pause", "resume", "submit", "manual_complete", "cancel"],
                      payload: Action, project_id: str | None = None, actor: dict = Depends(central_api.admin)):
    try:
        from server import phase3_store
        candidate = phase3_store.get('draft', payload.payload.get('draft_id', '')) if action in ('start','update') else service.get_task(task_id, actor)
        if project_id is not None and candidate and candidate.get('project_id') != _project(project_id):
            raise PermissionError('任务不属于当前项目')
        item = await service.apply_task_action(
            task_id, action, rdos_contract.internal_input(payload.payload, {'nodes': service.node_registry()}), actor, payload.operation_id,
            payload.expected_revision, payload.expected_requirements_version,
            **({'project_id':_project(project_id)} if project_id is not None else {}))
        return {"item": _display(item)}
    except Exception as exc:
        raise _failure(exc) from exc


@router.post("/tasks/{task_id}/migrate-completion")
async def migrate_completion(task_id: str, payload: Action, actor: dict = Depends(central_api.admin)):
    try:
        return {"item": _display(service.business_task(await service.migrate_legacy_quality_task(
            task_id, actor, payload.operation_id, payload.expected_revision, payload.expected_requirements_version)))}
    except Exception as exc:
        raise _failure(exc) from exc


@router.post("/sync")
async def sync(payload: Operation, project_id: str | None = None, actor: dict = Depends(central_api.admin)):
    try:
        await service.refresh(actor, payload.operation_id, project_id=_project(project_id))
        return _display(service.business_workspace(actor, _project(project_id)))
    except Exception as exc:
        raise _failure(exc) from exc


def _root():
    from lab.config import ROOT
    return ROOT


def developer_enabled():
    path = _root() / 'runtime/developer_ui.json'
    try:
        return json.loads(path.read_text()).get('enabled') is True
    except (OSError, ValueError, AttributeError):
        return False


@onboard_router.get('/dev')
def developer_page(actor: dict = Depends(central_api.admin)):
    if not developer_enabled():
        raise HTTPException(404, '开发入口未启用')
    return FileResponse(Path(__file__).parent / 'static/central.html')


@router.get('/developer')
def developer_workspace(actor: dict = Depends(central_api.admin)):
    if not developer_enabled():
        raise HTTPException(404, '开发入口未启用')
    return central_api.safe_display(service.overview(actor))


@router.post('/migrate-workflow')
async def migrate_workflow(payload: Operation, actor: dict = Depends(central_api.admin)):
    if not developer_enabled():
        raise HTTPException(404, '开发入口未启用')
    path = _root() / 'runtime/workflow_migration.json'
    try:
        manifest = json.loads(path.read_text())
        allowed = {'task_p3_7b4e31512aedc14334b2', 'task_p3_cf53fbf0cd16681b732e'}
        if set(item['task_id'] for item in manifest['tasks']) != allowed:
            raise ValueError('迁移清单不符合本次配置任务范围')
        results = []
        for item in manifest['tasks']:
            current = service.get_task(item['task_id'], actor)
            if current['requirements_version'] == 1 and current['requirements_hash'] != item['requirements_hash']:
                raise ValueError('原要求已变化，停止迁移')
            result = await service.migrate_receipt_completion(item['task_id'],item['event_id'],actor,
                payload.operation_id+':'+item['node'],item['record_revision'],item['requirements_version'],
                fields=item['fields'],expected_event_hash=item['event_hash'])
            results.append(result)
        return _display({'items':results})
    except Exception as exc:
        raise _failure(exc) from exc


def _feishu_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        allowed = (parsed.scheme == "https" and not parsed.username and not parsed.password and not parsed.port
                   and parsed.hostname and parsed.hostname.endswith(".feishu.cn")
                   and parsed.path.startswith(("/docx/", "/base/", "/drive/folder/")))
    except ValueError:
        return None
    if not allowed:
        return None
    return value


def _node_info(node_id):
    node = next((n for n in service.node_registry() if node_id in (n['node_id'], n['public_id'])), None)
    if not node:
        raise HTTPException(404, '节点未登记')
    return node


def _business_title(document):
    names = {'DGLldWdxSogyEOx7ByMchS5anPh':'项目需求',
             'Q143dE1kJo6fRrx7haJcwQhsnjd':'项目说明',
             'KBYmdq9hbo8EzzxHn9mcDaDZnRt':'工作说明'}
    return document.get('business_title') or names.get(document['id']) or document.get('title', '协作资料')


@onboard_router.get('/lab/onboard/{node_id}')
def legacy_onboard(node_id: str, request: Request):
    node = _node_info(node_id)
    # Keep an already registered read-only URL usable during client upgrades.
    # It serves only current instructions, never the retired protocol.
    return node_start(node['public_id'], request)


@onboard_router.get('/nodes/{node_id}/skill.zip')
def skill_package(node_id: str):
    node=_node_info(node_id)
    from lab.build_node_skill import package_bytes
    content, _ = package_bytes(_manifest_data(node))
    return Response(content, media_type='application/zip', headers={
        'Content-Disposition':'attachment; filename="rdos-workflow.zip"', 'Cache-Control':'no-store'})


@router.post('/nodes/{node_id}/onboarding')
async def prepare_node_onboarding(node_id: str, user: dict = Depends(central_api.admin)):
    try:
        from server import node_onboarding
        return _display(await node_onboarding.prepare(node_id, user))
    except Exception as exc:
        raise _failure(exc) from exc


@onboard_router.get('/nodes/{node_id}/manifest')
def node_manifest(node_id: str, request: Request):
    node = _node_info(node_id)
    from lab.build_node_skill import package_bytes
    manifest = _manifest_data(node)
    _, metadata = package_bytes(manifest)
    return {**manifest, 'skill': {'name':'rdos-workflow','version':SKILL_VERSION,
        'url':str(request.base_url)+'nodes/'+node['public_id']+'/skill.zip','sha256':metadata['sha256']}}


def _manifest_data(node):
    registry = phase3_backend.get_backend().registry()
    if not all(registry.get(key) for key in ('base_token','tasks_table','events_table')):
        raise HTTPException(503, '工作接口尚未登记，请联系管理员')
    from server import phase3_store, rdos_projects
    resources=[]
    for document in registry.get('documents', []):
        if document.get('retired'):
            continue
        cached=phase3_store.get('document_cache', document['id']) or {}
        resources.append({'id':document['id'], 'title':_business_title(document),
            'project_id':rdos_contract.public_view(document.get('project_id', service.PROJECT_ID)),
            'revision':cached.get('revision'), 'content_hash':cached.get('content_hash'),
            'zone':'project' if document.get('zone')=='human' else document.get('zone'),
            'source_url':document.get('source_url','')})
    project_values = []
    for project in rdos_projects.list_projects(None, include_inactive=True):
        if project.get('merged_into'):
            continue
        if node['node_id'] not in project['node_ids']:
            continue
        public_id = rdos_contract.public_view(project['id'])
        document_ids = {d['id'] for d in rdos_projects.resource_scope(project['id'], registry).get('documents', [])}
        project_values.append({'id':public_id, 'name':project['name'], 'status':project['status'],
            'node_ids':rdos_contract.public_view(project['node_ids']),
            'zones': {'project':{'folder_id':project['resolved_folder_id']}, 'agent':{'folder_id':registry.get('agent_folder','')}},
            'registered_resources':[r for r in resources if r['id'] in document_ids]})
    default_assigned = any(project['id'] == 'project_rdos' for project in project_values)
    legacy_project_root = registry.get('human_folder', '') if default_assigned else ''
    # Shared transport registration is not project membership. Explicitly empty
    # projects is understood by the current Skill and must not grant Default.
    common_agent_ids = {d['id'] for d in registry.get('documents', [])
                        if d.get('zone') == 'agent' and not d.get('task_id')
                        and d.get('project_id', service.PROJECT_ID) == service.PROJECT_ID}
    legacy_resources = [r for r in resources if r['id'] in common_agent_ids
                        or (default_assigned and r['project_id'] == 'project_rdos')]
    from server.peer_tasks import public_config
    from server.phase3_feishu import KNOWLEDGE_ROOTS
    return {'protocol_version':2, 'shared_skills':{'root_folder_id':KNOWLEDGE_ROOTS['shared_skills']}, 'peer':public_config(), 'workspace_id':registry.get('workspace_id','ws_rdos'),
        'project_id':'project_rdos','node_id':node['public_id'], 'node_name':node.get('label',node['public_id']),
        'base_token':registry.get('base_token',''), 'tasks_table_id':registry.get('tasks_table',''),
        'events_table_id':registry.get('events_table',''), 'registered_resources':legacy_resources,
        'projects': project_values,
        'allowed_output_roots':{'project':legacy_project_root, 'agent':registry.get('agent_folder','') if project_values else ''},
        'zones':{'project':{'folder_id':legacy_project_root},'agent':{'folder_id':registry.get('agent_folder','')}},
        'task_table_url':registry.get('task_table_url',''),'event_table_url':registry.get('event_table_url','')}


@onboard_router.get('/nodes/{node_id}/start', response_class=HTMLResponse)
def node_start(node_id: str, request: Request):
    node=_node_info(node_id)
    registry=phase3_backend.get_backend().registry()
    public_id=node['public_id']
    label=escape(node.get('label',public_id))
    root = request.scope.get('root_path', '').rstrip('/')
    manifest_href = escape(root + '/nodes/' + public_id + '/manifest', quote=True)
    package_href = escape(root + '/nodes/' + public_id + '/skill.zip', quote=True)
    links=''.join('<li><a href="'+escape(url,quote=True)+'">'+name+'</a></li>'
        for key,name in [('task_table_url','任务登记'),('event_table_url','协作记录')]
        if (url:=_feishu_url(registry.get(key))))
    text=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RDOS 工作接入 · {label}</title><style>body{{font:16px/1.8 system-ui;max-width:800px;padding:24px;margin:auto;color:#222}}a{{overflow-wrap:anywhere}}</style>
<main><h1>RDOS 工作接入 · {label}</h1><p>节点：<code>{escape(public_id)}</code>。通过飞书领取当前任务、读取资料并提交结果。</p>
<h2>启用执行规范</h2><p>读取<a href="{manifest_href}">节点配置与 Skill 校验信息</a>，下载并按客户端原生 Skill 机制持久安装、启用 <a href="{package_href}">RDOS 执行 Skill</a>，确认后续会话可以调用；只在临时目录运行不算安装完成。保留已有检查点，不修改全局或其他项目规则。规范、业务校验脚本和 JSON 模板均在包内，复用已有飞书连接。项目参与范围调整后，重新取得本节点当前配置，保留工作检查点。</p>
<h2>节点互派</h2><p>研究员明确要求向其他节点派发工作时，由 Skill 核对条件、直接发布到原任务表并独立读回。首次启用时，Skill 按接入任务在节点本地生成密钥，写回公钥与持有证明。中央随既有同步自动核验并完成登记，不需要人工审核；节点下次检查自动核对签名登记并回执。私钥不离开节点，无需传递文件；登记完成与互派就绪分别显示，等待期间继续原有任务流程。已发布任务的调整、暂停、恢复与取消仍交中央办理。</p>
<h2>开始工作</h2><ol><li>使用 Skill 检查分配给本节点的当前有效任务。无任务就报告无任务。</li><li>按任务指定的目标、资料、交付对象和完成方式执行；在关键操作前重新核对要求版本与状态。</li><li>暂停或取消后停止新增工作；要求变化时保留旧成果，按变更说明处理。</li><li>进展、问题、Review 和回执写入同一张协作记录表；长篇成果引用确切版本。有效完成回执经中央核对后按任务规则收尾。</li></ol>
<h2>飞书入口</h2><ul>{links}</ul>
<h2>无法读取飞书时</h2><p>先复用已有飞书工具或 CLI，核对登录及指定对象读取能力。已有 lark-cli 可执行 <code>lark-cli auth status --verify</code>；需要重新授权时通过官方登录页由使用者核对账号和权限。不要索取密码、验证码或 Token。</p><p>若工具缺失，报告具体缺项及<a href="https://github.com/larksuite/cli">飞书官方 CLI 说明</a>，由使用者决定接入。成功标准是完整读到登记资料和任务；写回另以有效结果及独立读回核对。</p>
<p>新节点首次接入包含 Skill 安装、每日检查、每周反馈和接入回执。管理员复制接入指令时系统自动准备配置任务，不需另行起草；按该任务配置客户端原生安排，已有安排只原位更新。接入配置完成不代表首次到点运行已验证。通过飞书交换业务，不读取 Panel 文件或其他节点目录。</p></main></html>'''
    return HTMLResponse(text,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
