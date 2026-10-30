"""Node-signed direct task creation, with pinned central identity registration.

Normal enrollment receives only a public key and its possession proof; private
node keys stay on the node. One-time private delivery remains compatibility only.
Signed strings deliberately survive public/internal ID representation mapping.
"""
from __future__ import annotations
import asyncio
import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from server import db, central_store as central, phase3_store as local, phase3_backend as backends, rdos_contract, rdos_projects as projects, rdos_rules as rules
from lab import config

VERSION = 1
_CAPABILITY = object()
_LOCK = threading.RLock()
_AUTOMATIC_ACTIVE = set()
AUTOMATIC_REGISTRAR = 'system:node_registration'
HELPER = Path(__file__).resolve().parents[1] / 'node_skills/rdos-workflow/scripts/peer_crypto.cjs'
BUNDLED_NODE = '/Users/alexliu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'

class PeerError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def crypto(request):
    """Only the frozen helper is executable; guard authorizes this exact stdin."""
    if os.environ.get('RDOS_RUNTIME_MODE') == 'production':
        from server.production_runtime import run_peer_crypto
        try:
            result = run_peer_crypto(request)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise PeerError('runtime_unavailable', '现有 Node 签名功能不可用；未自动安装或修复') from None
        if not isinstance(result, dict) or result.get('error'):
            raise PeerError('invalid_signature', '签名或验签失败')
        return result
    from lab import guard
    binary = BUNDLED_NODE if Path(BUNDLED_NODE).is_file() else shutil.which('node')
    if not binary:
        raise PeerError('runtime_unavailable', '现有 Node 运行时不可用；未自动安装或修复')
    command = [str(Path(binary).resolve()), str(HELPER.resolve())]
    payload = canonical(request)
    permit = getattr(guard, 'peer_crypto_permit', None)
    if permit is None:
        raise PeerError('runtime_unavailable', '签名执行边界尚未配置')
    try:
        with permit(command, payload) as execution_env:
            completed = subprocess.run(command, input=payload, text=True, capture_output=True, timeout=10, check=False,cwd=str(_runtime()),env=execution_env)
            result = json.loads(completed.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # The exact helper's denied/missing execution remains a denied guard
        # event; it disables only this optional capability, never the guard.
        raise PeerError('runtime_unavailable', '现有 Node 签名功能不可用；未自动安装或修复') from None
    if not isinstance(result,dict):
        raise PeerError('runtime_unavailable','现有 Node 未返回有效的签名检查结果')
    if completed.returncode or result.get('error'):
        raise PeerError('invalid_signature', '签名或验签失败')
    return result

def _runtime():
    if os.environ.get('RDOS_RUNTIME_MODE') == 'production':
        from server.production_runtime import require
        return require().database.parent / 'runtime'
    return Path(os.environ['RDOS_LAB_RUNTIME']).resolve()

def _central_keys(*,create=False):
    path = config.checked_path(_runtime() / 'peer_identity.json', _runtime().parent)
    with _LOCK:
        if path.exists():
            if path.stat().st_mode & 0o077:
                raise PeerError('unsafe_identity', '中央身份文件权限过宽')
            return json.loads(path.read_text())
        if not create:raise PeerError('identity_uninitialized','中央签名身份尚未初始化')
        value = crypto({'action':'generate'})
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as output:
            output.write(canonical(value))
        return value

def initialize():
    """Enable only peer dispatch after a bounded actual runtime probe.

    Missing/broken optional Node capability must not take down the Panel. Unsafe
    identity files, database failures and other business errors still propagate.
    """
    try:
        keys=_central_keys(create=True)
        if not isinstance(keys,dict) or not all(isinstance(keys.get(key),str) and keys[key] for key in ('public_key','private_key')):
            raise PeerError('unsafe_identity','中央身份文件格式无效')
        payload=canonical({'component':'peer_tasks','probe_version':1})
        signature=crypto({'action':'sign','payload':payload,'private_key':keys['private_key']}).get('signature')
        if not isinstance(signature,str) or not signature:
            raise PeerError('invalid_signature','签名运行时检查失败')
        result=crypto({'action':'verify','payload':payload,'signature':signature,'public_key':keys['public_key']})
        if result.get('valid') is not True:
            raise PeerError('invalid_signature','签名运行时检查失败')
        state={'enabled':True,'status':'available','error_code':'','message':'节点互派签名能力可用。'}
    except PeerError as exc:
        if exc.code not in ('runtime_unavailable','invalid_signature'):raise
        state={'enabled':False,'status':'unavailable','error_code':('runtime_unavailable' if exc.code=='runtime_unavailable' else 'crypto_probe_failed'),
               'message':'节点互派暂未启用：现有签名运行时不可用。项目、中央对话与维护继续可用；未自动安装或修复。'}
    state['checked_at']=datetime.now(timezone.utc).isoformat()
    local.put('state','peer_runtime',state)
    return state


def runtime_status():
    return copy.deepcopy(local.get('state','peer_runtime') or {'enabled':False,'status':'not_initialized',
        'error_code':'identity_uninitialized','message':'节点互派尚未完成运行时检查。'})


def _require_runtime():
    state=runtime_status()
    if not state.get('enabled'):
        raise PeerError('runtime_unavailable',state['message'])


def public_config():
    path=_runtime()/'peer_identity.json'
    key=_central_keys()['public_key'] if path.exists() else ''
    state=runtime_status()
    return {'version': VERSION, 'central_public_key': key, 'minimum_skill_version':'1.1.0',
            'available':bool(key) and state.get('enabled') is True,'status':state['status'],
            'error_code':state.get('error_code',''),'message':state.get('message','')}

def signed(value, private_key=None):
    _require_runtime()
    text = canonical(value)
    return {'payload_json':text, 'signature':crypto({'action':'sign','payload':text,'private_key':private_key or _central_keys(create=True)['private_key']})['signature']}

def verified(proof, public_key):
    _require_runtime()
    if not isinstance(proof, dict) or set(proof) != {'payload_json','signature'}:
        raise PeerError('invalid_signature','缺少完整的签名依据')
    if not crypto({'action':'verify', 'payload':proof['payload_json'], 'signature':proof['signature'], 'public_key':public_key}).get('valid'):
        raise PeerError('invalid_signature','签名与正文不一致')
    value = json.loads(proof['payload_json'])
    if not isinstance(value, dict):
        raise PeerError('invalid_signature','签名正文格式无效')
    return value

def _node(node_id):
    node = next((item for item in local.node_registry() if node_id in (item['node_id'], item.get('public_id'))), None)
    if not node:
        raise PeerError('unknown_node','节点未登记或已停用')
    return node

def _actor(actor, node=None):
    user = central.user(actor.get('id','')) if isinstance(actor,dict) else None
    if not user or not user.get('enabled') or ('admin' not in user.get('roles',[]) and (node is None or node.get('member_id') != user['id'])):
        raise PeerError('authorization_failed','当前账号不能领取此节点身份')
    return user

def node_status(actor):
    user = central.user(actor.get('id','')) if isinstance(actor,dict) else None
    if not user or not user.get('enabled'): raise PeerError('authorization_failed','当前账号不能查看节点身份')
    values=[]
    state=runtime_status()
    for node in local.node_registry():
        if 'admin' not in user.get('roles',[]) and node.get('member_id') != user['id']: continue
        identity = local.get('peer_identity',node['node_id']) or {}
        pending=[_pending_public(value) for value in local.items('peer_enrollment_request') if value.get('node_id')==node['node_id'] and value.get('status') in ('pending','registry_pending')]
        problem=next((value for value in pending if value.get('automatic_status') in ('conflict','failed')),None)
        bound=identity.get('delivery_status') in ('delivered','registered')
        registration=('conflict' if problem and problem.get('automatic_status')=='conflict' else 'failed' if problem else
                      'registered' if bound else 'registry_pending' if any(value.get('status')=='registry_pending' for value in pending) else 'pending' if pending else 'unbound')
        values.append({'node_id':node.get('public_id',node['node_id']), 'label':node.get('display_name',node.get('label')), 'bound':bound,'delivery_status':identity.get('delivery_status','unbound'),'ready':bool(identity.get('ready')) and state.get('enabled') is True,'runtime_available':state.get('enabled') is True, 'disabled_reason':state.get('message','') if not state.get('enabled') else '', 'runtime_error_code':state.get('error_code',''), 'binding_id':identity.get('binding_id'),'skill_version':identity.get('skill_version'),'pending_requests':pending,
            'registration_status':registration,'registration_error':problem.get('message','') if problem else '',
            'registration_error_code':problem.get('error_code','') if problem else ''})
    return values

def _registry_payload():
    registry=backends.get_backend().registry()
    now=datetime.now(timezone.utc)
    nodes=[]
    for node in local.node_registry():
        identity=local.get('peer_identity',node['node_id'])
        if identity:
            nodes.append({**rdos_contract.public_view({k:identity[k] for k in ('node_id','binding_id','public_key','ready')},registry), 'enabled':True})
    project_values=[]
    for project in projects.list_projects(None,True):
        if project.get('merged_into'):
            continue
        scope=projects.resource_scope(project['id'],registry)
        project_values.append(rdos_contract.public_view({'project_id':project['id'],'status':project['status'],'node_ids':project['node_ids'],'owner_id':project['owner_id'],'zones':scope['zones'],'registered_resources':[{k:v for k,v in document.items() if k in ('id','object_id','revision','content_hash')} for document in scope.get('documents',[])]},registry))
    return {'version':VERSION,'workspace_id':registry.get('workspace_id',rdos_contract.PUBLIC_WORKSPACE_ID),'nodes':nodes,'projects':project_values,'generated_at':now.isoformat(),'expires_at':(now+timedelta(days=8)).isoformat()}

async def publish_registry(operation_id):
    from server.phase3_service import environment
    proof=signed(_registry_payload())
    event={'id':'peer_registry_'+central.digest(operation_id)[:24],**environment(),'project_id':local.PROJECT_ID,'event_type':'node_registry_snapshot','claimed_node_id':'system:peer_registry','task_refs':[],'body':'节点接入范围已更新。','registry_proof':proof,'operation_id':operation_id}
    previous=local.get('peer_registry_operation',operation_id)
    if previous:
        event=previous['event']
    else:
        local.put('peer_registry_operation',operation_id,{'event':event,'dispatched':False})
    matches=[v for v in await asyncio.to_thread(backends.get_backend().list_events) if v.get('id')==event['id']]
    if not matches:
        state=local.get('peer_registry_operation',operation_id)
        if state['dispatched']:
            raise PeerError('write_uncertain','节点登记已发出，继续核对原记录，不重复发布')
        state['dispatched']=True;local.put('peer_registry_operation',operation_id,state)
        try:await asyncio.to_thread(backends.get_backend().append_event,event,operation_id)
        except ValueError as exc:
            if getattr(exc,'code','') not in ('write_uncertain','read_failed','readback_mismatch'):raise
        matches=[v for v in await asyncio.to_thread(backends.get_backend().list_events) if v.get('id')==event['id']]
    from server.peer_records import event_readback_payload
    registry=backends.get_backend().registry()
    if not matches or any(event_readback_payload(v,registry)!=event_readback_payload(event,registry) for v in matches):
        raise PeerError('write_uncertain','节点登记尚未完整独立读回')
    return event['id']

async def issue_identity(node_id, actor, operation_id, rotate=False):
    _require_runtime()
    node=_node(node_id);user=_actor(actor,node)
    with _LOCK:
        previous=local.get('peer_identity',node['node_id'])
        issued=local.get('peer_identity_issue',operation_id)
        if issued:
            raise PeerError('identity_already_issued','此操作的私有接入文件已经领取；丢失时请显式轮换')
        if previous and not rotate:
            raise PeerError('identity_already_bound','节点已绑定；需要更换时请显式轮换')
        pair=crypto({'action':'generate'})
        identity={'node_id':node['node_id'],'binding_id':'binding_'+central.digest([node['node_id'],operation_id])[:24],'public_key':pair['public_key'],'ready':False,'delivery_status':'pending','issued_by':user['id'],'issued_at':central.now() if hasattr(central,'now') else datetime.now(timezone.utc).isoformat()}
        local.put('peer_identity',node['node_id'],identity)
        local.put('peer_identity_issue',operation_id,{'node_id':node['node_id'],'binding_id':identity['binding_id'],'issued_by':user['id']})
    try:
        registry_event=await publish_registry('peer-binding:'+operation_id)
    except BaseException as exc:
        identity.update(delivery_status='not_delivered',ready=False,delivery_error=getattr(exc,'code','delivery_failed'))
        local.put('peer_identity',node['node_id'],identity)
        raise PeerError('identity_not_delivered','身份登记尚未核对，私有文件未交付；修复连接后显式轮换领取，不复用原领取操作') from None
    identity['delivery_status']='delivered';local.put('peer_identity',node['node_id'],identity)
    public=rdos_contract.public_view(identity,backends.get_backend().registry())
    return {'identity':{'version':VERSION,'node_id':public['node_id'],'binding_id':public['binding_id'],'private_key':pair['private_key'],'central_public_key':_central_keys()['public_key'],'workspace_id':_registry_payload()['workspace_id']},'registry_event_id':registry_event}

def wire_task(task):
    value={k:v for k,v in backends.payload(task).items() if k != 'peer_proof'}
    return rdos_contract.encode(value,backends.get_backend().registry())

def validate_task(task):
    if task.get('dispatch_source')!='peer':return False
    proof=task.get('peer_proof') or {}
    issuer=task.get('peer_issuer_node_id')
    identity=local.get('peer_identity',issuer)
    if not identity or not identity.get('ready'):
        raise PeerError('peer_not_ready','发起节点尚未完成新版接入核对')
    actual=wire_task(task)
    if task.get('record_revision')==1:
        expected=verified(proof,identity['public_key'])
        rules.validate_peer_task(expected,_registry_payload(),rdos_contract.public_view(issuer,backends.get_backend().registry()), verify_ready=True)
    else:
        expected=verified(proof,_central_keys()['public_key'])
    if expected != actual:
        raise PeerError('task_tampered','互派任务正文、范围或版本与签名不符')
    return True

def sign_task_update(task):
    if task.get('dispatch_source')=='peer':
        registry=projects.resource_scope(task['project_id'],backends.get_backend().registry())
        task['peer_task_scope']={'input_refs':copy.deepcopy(task.get('input_refs',[])),'output_zone':task['output_zone'],'output_folder_id':registry['zones'][task['output_zone']]['folder_id']}
        task['peer_proof']=signed(wire_task(task))
    return task

def accept_task(task):
    """First verified node creation, never a fabricated administrator click."""
    validate_task(task)
    taskid=task['task_id']; digest=central.digest(backends.payload(task))
    canonical_row=local.get('peer_canonical',taskid)
    if canonical_row and canonical_row['record_id']!=task.get('record_id'):
        if task.get('record_revision')==1 and canonical_row['creation_hash']==digest:
            return False
        raise PeerError('task_conflict','互派任务出现不同物理记录或异内容')
    if not canonical_row:
        if task['record_revision']!=1:
            raise PeerError('task_unadopted','未接纳的互派任务不能从后续修订开始')
        local.immutable('peer_canonical',taskid,{'record_id':task.get('record_id'),'creation_hash':digest,'operation_id':task['operation_id']})
    existing_proof=local.get('task_authority',taskid+'@'+str(task['record_revision']))
    if existing_proof:
        if existing_proof['hash']!=digest:raise PeerError('task_tampered','已核对版本出现不同正文')
    else:local.immutable('task_authority',taskid+'@'+str(task['record_revision']),{'hash':digest,'operation_id':task['operation_id']})
    local.put('grant',taskid+'@'+str(task['requirements_version']),{'operation_id':task['operation_id'],'requirements_hash':task['requirements_hash'],'owner_id':task['owner_id'],'kind':'signed_peer_task'})
    local.confirm_task_head(task,digest)
    return True

def ingest_system_event(raw,*,tasks=None):
    from server.phase3_service import _check_scope
    _check_scope(raw)
    if raw.get('event_type')=='peer_enrollment_request':
        return _ingest_enrollment(raw,tasks or {})
    if not isinstance(raw.get('id'),str) or not raw['id'] or raw.get('task_refs')!=[]:
        raise PeerError('invalid_event','身份登记记录格式无效')
    if raw.get('event_type')=='node_registry_snapshot':
        _registry_snapshot_claim(raw)
        return local.index_event(raw,backends.payload(raw))
    if raw.get('event_type')=='peer_capability_ack':
        node,identity,claim=_capability_ack_claim(raw)
        with _LOCK,db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute("SELECT payload FROM phase3_local WHERE kind='peer_identity' AND id=?",(node['node_id'],)).fetchone()
            current=json.loads(row[0]) if row else {}
            if current.get('binding_id')!=identity.get('binding_id') or current.get('public_key')!=identity.get('public_key'):
                raise PeerError('identity_expired','节点绑定已变化，未用旧能力回执启用新身份')
            indexed=local.index_event(raw,backends.payload(raw),connection=conn)
            current.update(ready=True,skill_version=claim['skill_version'])
            if current.get('binding_source')=='approved_node_public_key':
                current['delivery_status']='registered'
                request_id=current.get('enrollment_request_id','')
                row=conn.execute("SELECT payload FROM phase3_local WHERE kind='peer_enrollment_request' AND id=?",(request_id,)).fetchone()
                pending=json.loads(row[0]) if row else None
                if pending and pending.get('status')=='registry_pending':
                    _transaction_put(conn,'peer_enrollment_request',request_id,{**pending,'status':'approved'})
            _transaction_put(conn,'peer_identity',node['node_id'],current)
            return indexed
    return None

# Root includes this router; no unauthenticated node HTTP task channel.
from server.central_api import current_user
from server.lab_routes import SanitizedLabRoute
router=APIRouter(prefix='/api/central/peer',route_class=SanitizedLabRoute)
class IdentityRequest(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)
    operation_id:str=Field(min_length=8,max_length=200)
    rotate:bool=False

@router.get('/nodes')
def nodes(actor:dict=Depends(current_user)):
    try:return {'nodes':node_status(actor),'runtime':runtime_status()}
    except PeerError as exc:raise HTTPException(403,{'code':exc.code,'message':str(exc)})

@router.post('/nodes/{node_id}/identity')
async def identity(node_id:str,payload:IdentityRequest,actor:dict=Depends(current_user)):
    try:return await issue_identity(node_id,actor,payload.operation_id,payload.rotate)
    except PeerError as exc:raise HTTPException(409 if exc.code!='authorization_failed' else 403,{'code':exc.code,'message':str(exc)})


from server.peer_records import select_task_rows


async def renew_registry_if_due():
    if not local.items('peer_identity'):return {'skipped':'no_bound_nodes'}
    now=datetime.now(timezone.utc)
    payload=_registry_payload()
    business={k:v for k,v in payload.items() if k not in ('generated_at','expires_at')}
    fingerprint=central.digest(business)
    previous=local.get('state','peer_registry_published') or {}
    due=not previous or previous.get('fingerprint')!=fingerprint
    if not due:
        try:due=(now-datetime.fromisoformat(previous['at'])).total_seconds()>=86400
        except (ValueError,KeyError,TypeError):due=True
    if not due:return {'skipped':'current'}
    operation_id='peer_registry_renew:'+fingerprint[:20]+':'+now.strftime('%Y%m%d')
    identifier=await publish_registry(operation_id)
    local.put('state','peer_registry_published',{'fingerprint':fingerprint,'at':now.isoformat(),'event_id':identifier})
    return {'event_id':identifier,'readback_verified':True}


def validate_result_origin(event,task):
    """Shared Feishu accounts cannot forge a bound peer's completion receipt."""
    identity=local.get('peer_identity',event.get('claimed_node_id',''))
    if not identity or not identity.get('ready'):
        raise PeerError('peer_not_ready','执行节点尚未完成身份绑定')
    expected={k:v for k,v in backends.payload(event).items() if k!='peer_result_proof'}
    actual=rdos_contract.encode(expected,backends.get_backend().registry())
    signed=verified(event.get('peer_result_proof'),identity['public_key'])
    if signed!=actual:
        raise PeerError('invalid_signature','执行结果与节点签名不一致')
    return True


def _pending_public(value):
    return rdos_contract.public_view({key:value.get(key) for key in ('request_id','request_hash','node_id','task_id','requirements_version',
        'public_key_fingerprint','confirmation_code','requested_at','status','automatic_status','error_code','message')},backends.get_backend().registry()) | {'resume_operation_id':value.get('approved_operation_id')}


def _enrollment_task(task,node_id,version, *, allow_completed=False):
    from server.phase3_service import _validate_task
    completed = (isinstance(task,dict) and allow_completed and task.get('business_status')=='closed'
        and task.get('close_reason') in ('completed','manual_completed'))
    if (not isinstance(task,dict) or (task.get('business_status')!='active' and not completed) or task.get('requirements_version')!=version
            or node_id not in task.get('executor_node_ids',[]) or task.get('dispatch_source')=='peer'
            or rules.task_type(task)!='action'):
        raise PeerError('enrollment_not_authorized','接入申请需要当前有效、分配给本节点的中央 action 任务')
    _validate_task(task)
    projects.require_active(task['project_id'])
    return task


def _enrollment_claim(raw):
    """Verify possession and immutable request references without admission."""
    if not isinstance(raw.get('id'),str) or not raw['id']:
        raise PeerError('invalid_enrollment','接入申请标识无效')
    proof=raw.get('enrollment_proof')
    if not isinstance(proof,dict) or not isinstance(proof.get('payload_json'),str):
        raise PeerError('invalid_enrollment','接入申请缺少完整持钥证明')
    try:untrusted=json.loads(proof['payload_json'])
    except (ValueError,TypeError):raise PeerError('invalid_enrollment','接入申请正文无效') from None
    if not isinstance(untrusted,dict) or not isinstance(untrusted.get('public_key'),str) or len(untrusted['public_key'])>2000:
        raise PeerError('invalid_enrollment','接入申请公钥无效')
    try:
        lines=untrusted['public_key'].strip().splitlines()
        if lines[0]!='-----BEGIN PUBLIC KEY-----' or lines[-1]!='-----END PUBLIC KEY-----':raise ValueError()
        der=base64.b64decode(''.join(lines[1:-1]),validate=True)
        if len(der)!=44 or not der.startswith(bytes.fromhex('302a300506032b6570032100')):raise ValueError()
    except (ValueError,IndexError):
        raise PeerError('invalid_enrollment','接入申请须使用当前支持的节点公钥类型') from None
    claim=verified(proof,untrusted['public_key'])
    required={'version','workspace_id','request_id','node_id','public_key','task_ref','requested_at'}
    if set(claim)!=required or claim.get('version')!=1 or claim.get('workspace_id')!=_registry_payload()['workspace_id']:
        raise PeerError('invalid_enrollment','接入申请协议或范围无效')
    node=_node(raw.get('claimed_node_id'))
    registry=backends.get_backend().registry()
    if claim['node_id']!=rdos_contract.public_view(node['node_id'],registry):
        raise PeerError('invalid_enrollment','申请公钥不能声明其他节点')
    request_id=claim.get('request_id');ref=claim.get('task_ref')
    if (not isinstance(request_id,str) or not 8<=len(request_id)<=150 or not isinstance(ref,dict)
            or set(ref)!={'task_id','requirements_version'} or not isinstance(ref.get('task_id'),str)
            or type(ref.get('requirements_version')) is not int or ref['requirements_version']<1
            or not isinstance(claim['requested_at'],str) or len(claim['requested_at'])>80):
        raise PeerError('invalid_enrollment','申请任务引用或稳定标识无效')
    references=rdos_contract.internal_input([ref],registry)
    if raw.get('task_refs')!=references:
        raise PeerError('invalid_enrollment','申请任务引用与持钥证明不一致')
    request_hash=central.digest(claim)
    prior=local.get('peer_enrollment_request',request_id)
    if prior and (prior.get('request_hash')!=request_hash or prior.get('node_id')!=node['node_id']):
        raise PeerError('operation_conflict','相同接入申请标识出现不同内容')
    if prior and raw.get('project_id')!=prior.get('project_id'):
        raise PeerError('out_of_scope','接入申请不能借用其他项目任务')
    return claim,node,references,prior


def _ingest_enrollment(raw,tasks):
    claim,node,references,prior=_enrollment_claim(raw)
    request_id=claim['request_id'];request_hash=central.digest(claim)
    admission=None
    if not prior:
        internal_ref=references[0]
        task=tasks.get(internal_ref['task_id']) or local.get('task_cache',internal_ref['task_id'])
        _enrollment_task(task,node['node_id'],internal_ref['requirements_version'])
        if raw['project_id']!=task['project_id']:
            raise PeerError('out_of_scope','接入申请不能借用其他项目任务')
        admission={'requirements_version':task['requirements_version'],'record_revision':task['record_revision'],
            'content_hash':central.digest(backends.payload(task))}
        fingerprint=hashlib.sha256(claim['public_key'].encode()).hexdigest()
        prior={'request_id':request_id,'request_hash':request_hash,'node_id':node['node_id'],'public_key':claim['public_key'],
            'task_id':task['task_id'],'requirements_version':internal_ref['requirements_version'],'project_id':task['project_id'],
            'public_key_fingerprint':fingerprint,'confirmation_code':'-'.join(fingerprint[index:index+4].upper() for index in (0,4,8)),
            'requested_at':claim['requested_at'],'status':'pending','source_event_id':raw['id'],
            'source_event_hash':central.digest(backends.payload(raw))}
    with _LOCK,db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute("SELECT payload FROM phase3_local WHERE kind='peer_enrollment_request' AND id=?",(request_id,)).fetchone()
        current=json.loads(row[0]) if row else None
        if current and (current.get('request_hash')!=request_hash or current.get('node_id')!=node['node_id']):
            raise PeerError('operation_conflict','相同接入申请标识出现不同内容')
        if current and raw.get('project_id')!=current.get('project_id'):
            raise PeerError('out_of_scope','接入申请不能借用其他项目任务')
        if not current:
            # An active read before taking the database lock is insufficient:
            # task completion or cancellation may have committed in between.
            row=conn.execute("SELECT payload FROM phase3_local WHERE kind='task_head' AND id=?",(prior['task_id'],)).fetchone()
            head=json.loads(row[0]) if row else {}
            if admission is None or any(head.get(key)!=value for key,value in admission.items()):
                raise PeerError('version_conflict','维护任务已变化，未用旧状态保存新的接入申请')
        indexed=local.index_event(raw,backends.payload(raw),connection=conn)
        if not current:
            _transaction_put(conn,'peer_enrollment_request',request_id,prior)
        return indexed


def _registry_snapshot_claim(raw):
    claim=verified(raw.get('registry_proof'),_central_keys()['public_key'])
    if claim.get('workspace_id')!=backends.get_backend().registry().get('workspace_id',rdos_contract.PUBLIC_WORKSPACE_ID):
        raise PeerError('out_of_scope','身份登记不属于此工作空间')
    if (claim.get('version')!=VERSION or not isinstance(claim.get('nodes'),list)
            or not isinstance(claim.get('projects'),list)
            or not isinstance(claim.get('generated_at'),str) or not isinstance(claim.get('expires_at'),str)):
        raise PeerError('invalid_registry','中央签名内容不是有效的节点登记快照')
    return claim


def _capability_ack_claim(raw):
    node=_node(raw.get('claimed_node_id'));identity=local.get('peer_identity',node['node_id'])
    if not identity:raise PeerError('peer_not_bound','节点尚未绑定')
    claim=verified(raw.get('peer_ack'),identity['public_key'])
    public=rdos_contract.public_view(node['node_id'],backends.get_backend().registry())
    if claim.get('skill_version') not in ('1.1.0','1.1.1','1.1.2','1.1.3','1.2.0') or claim != {'node_id':public,'binding_id':identity['binding_id'],'capability':'peer_tasks_v1','skill_version':claim.get('skill_version')}:
        raise PeerError('invalid_ack','节点能力回执与当前绑定不一致')
    return node,identity,claim


def observe_system_event(raw, *, tasks=None):
    """Verify a system event for observation; never index, admit or enable it.

    Historical approved requests remain evidence after task completion or a
    requirements update. Observing an unprocessed request verifies its original
    assigned task version, but does not grant current execution authority.
    """
    from server.phase3_service import _check_scope
    _check_scope(raw)
    if (not isinstance(raw.get('id'),str) or not raw['id'] or not isinstance(raw.get('body'),str)):
        raise PeerError('invalid_event','身份登记记录格式无效')
    original_hash=local.event_fingerprint(raw['id'])
    if original_hash and original_hash!=central.digest(backends.payload(raw)):
        raise PeerError('event_conflict','已核对的系统记录出现同一标识的不同正文')
    kind=raw.get('event_type')
    if kind=='peer_enrollment_request':
        claim,node,references,prior=_enrollment_claim(raw)
        if not prior:
            ref=references[0]
            candidates=local.history(ref['task_id'])
            current=local.get('task_cache',ref['task_id']) or (tasks or {}).get(ref['task_id'])
            history=next((item for item in candidates if item.get('requirements_version')==ref['requirements_version']),None)
            task=None
            if current and current.get('dispatch_source')!='peer':
                from server.phase3_service import _validate_task
                current=_validate_task(current)
                # Version history contains requirements, not project identity.
                # Scope comes from the verified task with the same stable ID.
                task={**current,**history} if history else current
            if (not task or task.get('requirements_version')!=ref['requirements_version']
                    or task.get('project_id')!=raw['project_id'] or node['node_id'] not in task.get('executor_node_ids',[])
                    or task.get('dispatch_source')=='peer' or rules.task_type(task)!='action'):
                raise PeerError('enrollment_not_authorized','未找到申请对应的中央任务版本与节点范围')
        return {'kind':kind,'verification':'signature_and_task_reference_verified',
                'registration_applied':False,'node_id':node['node_id']}
    if raw.get('task_refs')!=[]:
        raise PeerError('invalid_event','身份登记记录任务引用无效')
    if kind=='node_registry_snapshot':
        if raw.get('claimed_node_id')!='system:peer_registry' or raw.get('project_id')!=local.PROJECT_ID:
            raise PeerError('invalid_registry','中央登记记录的来源或项目不一致')
        _registry_snapshot_claim(raw)
        return {'kind':kind,'verification':'central_signature_verified','registration_applied':False}
    if kind=='peer_capability_ack':
        node,identity,claim=_capability_ack_claim(raw)
        return {'kind':kind,'verification':'node_signature_and_binding_verified',
                'node_id':node['node_id'],'skill_version':claim['skill_version'],'capability_applied':False}
    raise PeerError('invalid_event','未知的节点系统记录类型')


def _transaction_put(conn,kind,identifier,value):
    conn.execute('INSERT INTO phase3_local VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at',
        (kind,identifier,json.dumps(value,ensure_ascii=False),db.utc_now()))


def _automatic_authority(pending,node,connection=None):
    """Only published setup capabilities can bootstrap a node without review.

    A title, ordinary action task or model-produced approval is not a setup
    capability. Existing confirmed operations retain their original authority.
    """
    if pending.get('approved_operation_id'):
        return 'saved_confirmation'
    def value(table,kind,identifier):
        if connection is None:
            return (local if table=='phase3_local' else central).get(kind,identifier)
        row=connection.execute('SELECT payload FROM '+table+' WHERE kind=? AND id=?',(kind,identifier)).fetchone()
        return json.loads(row[0]) if row else None
    plan=value('phase3_local','node_onboarding',node['node_id'])
    if plan and (plan.get('node_id'),plan.get('task_id'),plan.get('project_id')) == (node['node_id'],pending.get('task_id'),pending.get('project_id')):
        operation=value('central_records','operation',plan.get('operation_id','')) or {}
        if operation.get('status')=='succeeded':return 'panel_node_onboarding'
    # These already published setup tasks predate node_onboarding records.
    # Keep the explicit bootstrap allowlist independent of deployment tooling.
    fixed={'RDOS-2-UPGRADE-YUHAN':'node_yuhan',
           'RDOS-2-UPGRADE-XIAOFEI':'node_xiaofei',
           'RDOS-2-UPGRADE-DUODUO':'node_duoduo',
           'task_p3_8ccef411212183445378':'node_yuhan',
           'task_p3_d76092c54635179dff35':'node_499saukYcMK0PbxO',
           'task_p3_4f77781acbfaeb64bbbd':'node_workbuddy'}
    registry=backends.get_backend().registry()
    public=rdos_contract.public_view({'node_id':node['node_id'],'project_id':pending.get('project_id')},registry)
    if fixed.get(pending.get('task_id'))==public['node_id'] and public['project_id']=='project_rdos':
        return 'registered_legacy_node_setup'
    raise PeerError('setup_authority_missing','此任务没有节点接入配置授权，未自动登记')


def _automatic_member(node,connection=None):
    if connection is None:
        member=central.user(node.get('member_id',''))
        enabled=member and member.get('enabled') and set(member.get('roles',[])) & {'admin','researcher'}
    else:
        row=connection.execute('SELECT enabled,roles_json FROM central_users WHERE id=?',(node.get('member_id',''),)).fetchone()
        enabled=row and row['enabled'] and set(json.loads(row['roles_json'])) & {'admin','researcher'}
    if not enabled:raise PeerError('authorization_failed','节点所属账号未启用，未自动登记')


def _automatic_competitors(connection,pending,node):
    """Recheck competing admitted setup claims at the identity commit point."""
    rows=connection.execute("SELECT payload FROM phase3_local WHERE kind='peer_enrollment_request'").fetchall()
    for row in rows:
        other=json.loads(row[0])
        if (other.get('node_id')!=node['node_id'] or other.get('request_id')==pending['request_id']
                or other.get('public_key')==pending['public_key'] or other.get('status') not in ('pending','registry_pending')):
            continue
        try:_automatic_authority(other,node,connection)
        except PeerError:continue
        project_row=connection.execute("SELECT payload FROM central_records WHERE kind='project' AND id=?",(other.get('project_id'),)).fetchone()
        if not project_row or json.loads(project_row[0]).get('status')!='active':continue
        head_row=connection.execute("SELECT payload FROM phase3_local WHERE kind='task_head' AND id=?",(other.get('task_id'),)).fetchone()
        head=json.loads(head_row[0]) if head_row else {}
        if head.get('requirements_version')!=other.get('requirements_version'):continue
        cached_row=connection.execute("SELECT payload FROM phase3_local WHERE kind='task_cache' AND id=?",(other.get('task_id'),)).fetchone()
        cached=json.loads(cached_row[0]) if cached_row else {}
        if cached and (cached.get('business_status') not in ('active','closed') or
                cached.get('business_status')=='closed' and cached.get('close_reason') not in ('completed','manual_completed')):
            continue
        raise PeerError('identity_already_bound','同一节点存在不同公钥的有效接入申请，未自动选择身份')


async def approve_enrollment(node_id,actor,operation_id,request_id,expected_request_hash,rotate=False):
    """Compatibility entry for old authenticated clients, not the normal flow."""
    return await _confirm_enrollment(node_id,actor,operation_id,request_id,expected_request_hash,rotate)


async def _confirm_enrollment(node_id,actor,operation_id,request_id,expected_request_hash,rotate=False,*,automatic=False):
    _require_runtime()
    node=_node(node_id)
    user={'id':AUTOMATIC_REGISTRAR} if automatic else _actor(actor,node)
    if automatic:_automatic_member(node)
    pending=local.get('peer_enrollment_request',request_id)
    if not pending or pending.get('node_id')!=node['node_id'] or pending.get('request_hash')!=expected_request_hash:
        raise PeerError('version_conflict','接入申请已变化，请重新核对当前申请')
    authority=_automatic_authority(pending,node) if automatic else None
    request={'node_id':node['node_id'],'request_id':request_id,'request_hash':expected_request_hash,'actor_id':user['id'],'rotate':rotate}
    if automatic:request.update(source='automatic_node_registration',authority=authority)
    requested_operation=local.get('peer_enrollment_operation',operation_id)
    canonical_id=pending.get('approved_operation_id')
    if canonical_id:
        # A saved approval is being reconciled, not approved or rotated again.
        # The current caller was authenticated above; retain the original
        # approval's actor and rotation decision rather than deriving them from
        # the now-changed card state. An operation belonging to any other
        # application must still be rejected before following the canonical ID.
        stable_keys=('node_id','request_id','request_hash')
        if requested_operation and any((requested_operation.get('request') or {}).get(key)!=request.get(key)
                                       for key in stable_keys):
            raise PeerError('operation_conflict','同一确认操作不能改用其他接入申请')
        operation_id=canonical_id
        operation=local.get('peer_enrollment_operation',operation_id)
        original=(operation or {}).get('request',{})
        if any(original.get(key)!=request.get(key) for key in stable_keys):
            raise PeerError('operation_conflict','原归属确认依据不完整，未重新生成绑定')
        request=original
    else:
        if requested_operation and requested_operation.get('request')!=request:
            raise PeerError('operation_conflict','同一确认操作不能改用其他接入申请')
        operation=requested_operation
    if operation and operation.get('state')=='verified':
        identity=local.get('peer_identity',node['node_id']) or {}
        if identity.get('binding_id')!=operation.get('binding_id'):
            raise PeerError('identity_expired','节点已被后续确认轮换，不重放旧绑定')
        return operation['result']
    if not operation:
        if pending.get('status') not in ('pending','registry_pending'):
            if pending.get('status')=='approved':
                current=local.get('peer_identity',node['node_id']) or {}
                if current.get('enrollment_request_id')==request_id:
                    return {'node_id':node.get('public_id',node['node_id']),'binding_id':current['binding_id'],'status':'registered','readback_verified':True,'already_confirmed':True}
            raise PeerError('invalid_enrollment','此申请不再等待确认')
        from server.phase3_service import _read_task
        current_task=await _read_task(pending['task_id'])
        # This immutable, independently ingested request was authorized while the
        # task was active. Finishing the node's setup must not revoke its
        # already requested enrollment; cancellation still forbids new approval.
        continuation = (current_task.get('business_status') == 'closed'
            and current_task.get('close_reason') in ('completed', 'manual_completed')
            and current_task.get('requirements_version') == pending['requirements_version'])
        if continuation:
            _enrollment_task(current_task,node['node_id'],pending['requirements_version'],allow_completed=True)
        else:
            _enrollment_task(current_task,node['node_id'],pending['requirements_version'])
        raw=[value for value in await asyncio.to_thread(backends.get_backend().list_events) if value.get('id')==pending['source_event_id']]
        if not raw or any(central.digest(backends.payload(value))!=pending['source_event_hash'] for value in raw):
            raise PeerError('version_conflict','接入申请原文尚未独立核对或已经变化')
        with _LOCK,db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not automatic:
                user_row=conn.execute('SELECT enabled,roles_json FROM central_users WHERE id=?',(user['id'],)).fetchone()
                if not user_row or not user_row['enabled'] or ('admin' not in json.loads(user_row['roles_json']) and node.get('member_id')!=user['id']):
                    raise PeerError('authorization_failed','当前账号不能确认此节点归属')
            node_row=conn.execute("SELECT payload FROM phase3_local WHERE kind='node' AND id=?",(node['node_id'],)).fetchone()
            current_node=json.loads(node_row[0]) if node_row else {}
            if not current_node or not current_node.get('enabled',True):
                raise PeerError('authorization_failed','节点已经停用，未确认新接入')
            if automatic:
                _automatic_member(current_node,conn)
            head_row=conn.execute("SELECT payload FROM phase3_local WHERE kind='task_head' AND id=?",(pending['task_id'],)).fetchone()
            head=json.loads(head_row[0]) if head_row else {}
            if (head.get('requirements_version')!=current_task['requirements_version'] or head.get('record_revision')!=current_task['record_revision']
                    or head.get('content_hash')!=central.digest(backends.payload(current_task))):
                raise PeerError('version_conflict','维护任务已变化，请重新核对接入申请')
            project_row=conn.execute("SELECT payload FROM central_records WHERE kind='project' AND id=?",(pending['project_id'],)).fetchone()
            current_project=json.loads(project_row[0]) if project_row else {}
            if not current_project or current_project.get('status')!='active':
                raise PeerError('out_of_scope','任务项目已经停用，未确认新接入')
            if automatic and node['node_id'] not in current_project.get('node_ids',[]):
                raise PeerError('out_of_scope','节点已不在配置任务的项目中，未自动登记')
            latest_row=conn.execute("SELECT payload FROM phase3_local WHERE kind='peer_enrollment_request' AND id=?",(request_id,)).fetchone()
            latest=json.loads(latest_row[0]) if latest_row else {}
            if latest.get('approved_operation_id') and latest['approved_operation_id']!=operation_id:
                raise PeerError('enrollment_confirmation_in_progress','另一归属确认已保存，请刷新后继续核对原申请')
            if latest.get('request_hash')!=expected_request_hash or latest.get('status') not in ('pending','registry_pending'):
                raise PeerError('version_conflict','接入申请已由另一操作处理')
            if automatic:_automatic_authority(latest,current_node,conn)
            prior_row=conn.execute("SELECT payload FROM phase3_local WHERE kind='peer_identity' AND id=?",(node['node_id'],)).fetchone()
            previous=json.loads(prior_row[0]) if prior_row else None
            if previous and previous.get('public_key')!=pending['public_key'] and not rotate:
                raise PeerError('identity_already_bound','节点已有其他绑定；请确认是否轮换')
            if automatic and not previous:_automatic_competitors(conn,pending,node)
            if automatic and previous:
                identity=copy.deepcopy(previous)
            else:
                identity={'node_id':node['node_id'],'binding_id':'binding_'+central.digest([node['node_id'],request_id,operation_id])[:24],
                    'public_key':pending['public_key'],'ready':False,'binding_source':'approved_node_public_key','enrollment_request_id':request_id,
                    'delivery_status':'registry_pending','issued_by':user['id'],'issued_at':db.utc_now()}
                if automatic:identity['registration_source']='automatic_node_registration'
            operation={'request':request,'binding_id':identity['binding_id'],'state':'registry_pending'}
            _transaction_put(conn,'peer_identity',node['node_id'],identity)
            saved={**latest,'status':'registry_pending','approved_by':user['id'],'approved_operation_id':operation_id}
            if automatic:saved.update(confirmation_source='automatic_node_registration',automatic_status='registry_pending',error_code='',message='正在自动完成登记。')
            _transaction_put(conn,'peer_enrollment_request',request_id,saved)
            _transaction_put(conn,'peer_enrollment_operation',operation_id,operation)
    else:
        identity=local.get('peer_identity',node['node_id']) or {}
        if identity.get('binding_id')!=operation.get('binding_id'):
            raise PeerError('identity_expired','节点已被后续确认轮换，不重放旧绑定')
    try:identifier=await publish_registry('peer-enrollment:'+operation_id)
    except ValueError as exc:
        return {'node_id':node.get('public_id',node['node_id']),'binding_id':operation['binding_id'],'status':'registry_pending',
            'readback_verified':False,'resume_operation_id':operation_id,'error_code':getattr(exc,'code','registry_unavailable'),
            'message':'归属确认已保存，登记尚未核对完成；稍后核对原申请，不需要重新生成身份。'}
    # A remote publication is not a license to overwrite a newer binding. Its
    # local settlement is atomic even if another thread rotates or storage fails.
    with _LOCK,db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute("SELECT payload FROM phase3_local WHERE kind='peer_identity' AND id=?",(node['node_id'],)).fetchone()
        identity=json.loads(row[0]) if row else {}
        if identity.get('binding_id')!=operation['binding_id']:
            raise PeerError('identity_expired','节点绑定已变化，未覆盖新绑定')
        row=conn.execute("SELECT payload FROM phase3_local WHERE kind='peer_enrollment_request' AND id=?",(request_id,)).fetchone()
        latest=json.loads(row[0]) if row else {}
        if (latest.get('request_hash')!=expected_request_hash or latest.get('node_id')!=node['node_id']
                or latest.get('approved_operation_id')!=operation_id):
            raise PeerError('operation_conflict','原登记申请已经变化，未覆盖其处理结果')
        row=conn.execute("SELECT payload FROM phase3_local WHERE kind='peer_enrollment_operation' AND id=?",(operation_id,)).fetchone()
        current_operation=json.loads(row[0]) if row else {}
        if (current_operation.get('binding_id')!=operation['binding_id']
                or current_operation.get('request')!=operation.get('request')):
            raise PeerError('operation_conflict','原登记操作已经变化，未覆盖其处理结果')
        identity['delivery_status']='registered'
        completed={**latest,'status':'approved','registry_event_id':identifier}
        if automatic:completed.update(automatic_status='registered',error_code='',message='登记完成，互派就绪由节点回执确认。')
        result={'node_id':node.get('public_id',node['node_id']),'binding_id':identity['binding_id'],'status':'registered',
            'registry_event_id':identifier,'readback_verified':True,'resume_operation_id':operation_id,'message':'节点归属已确认，等待节点自动回执。'}
        _transaction_put(conn,'peer_identity',node['node_id'],identity)
        _transaction_put(conn,'peer_enrollment_request',request_id,completed)
        _transaction_put(conn,'peer_enrollment_operation',operation_id,{**current_operation,'state':'verified','result':result})
    return result


def _automatic_observation(request_id,status,code='',message=''):
    with _LOCK,db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute("SELECT payload FROM phase3_local WHERE kind='peer_enrollment_request' AND id=?",(request_id,)).fetchone()
        if not row:return
        value=json.loads(row[0])
        _transaction_put(conn,'peer_enrollment_request',request_id,{**value,'automatic_status':status,
            'error_code':code,'message':message,'automatic_checked_at':db.utc_now()})


def _automatic_error(exc):
    code=getattr(exc,'code','registration_unavailable')
    messages={'setup_authority_missing':'此任务不属于已登记的接入配置，未自动登记。',
        'authorization_failed':'节点所属账号或节点已停用，登记未执行。',
        'enrollment_not_authorized':'配置任务当前不允许登记，已保留原申请。',
        'version_conflict':'配置任务或申请已变化，登记未执行。',
        'identity_already_bound':'出现不同公钥的申请，原节点身份保持不变。',
        'identity_expired':'原节点身份已经更换，未恢复旧身份。',
        'out_of_scope':'节点项目当前不允许登记，已保留原申请。',
        'operation_conflict':'登记申请或原操作存在冲突，未覆盖。'}
    return code,messages.get(code,'自动登记尚未完成，下次同步将继续核对。')


async def register_enrollments(project_id):
    """Trusted sync stage; no model/HTTP argument can supply its authority.

    Full table ingestion occurs first. Only configured setup capabilities are
    eligible, so a batch never lets the first of competing keys win by order.
    """
    from server.phase3_service import _read_task
    candidates=[];results=[];issues=0
    for pending in local.items('peer_enrollment_request'):
        if pending.get('project_id')!=project_id:continue
        operation=local.get('peer_enrollment_operation',pending.get('approved_operation_id','')) or {}
        if pending.get('status') not in ('pending','registry_pending') and not (
                pending.get('status')=='approved' and operation and operation.get('state')!='verified'):
            continue
        try:
            node=_node(pending['node_id']);_automatic_member(node)
            _automatic_authority(pending,node)
            if not pending.get('approved_operation_id'):
                task=await _read_task(pending['task_id'])
                continuation=(task.get('business_status')=='closed' and task.get('close_reason') in ('completed','manual_completed'))
                _enrollment_task(task,node['node_id'],pending['requirements_version'],allow_completed=continuation)
            candidates.append((pending,node))
        except (ValueError,KeyError,TypeError) as exc:
            code,message=_automatic_error(exc);_automatic_observation(pending['request_id'],'failed',code,message)
            results.append({'request_id':pending['request_id'],'status':'failed','error_code':code});issues+=1
    grouped={}
    for pending,node in candidates:grouped.setdefault(node['node_id'],[]).append(pending)
    for pending,node in candidates:
        key=node['node_id']
        with _LOCK:
            if key in _AUTOMATIC_ACTIVE:
                results.append({'request_id':pending['request_id'],'status':'in_progress'});continue
            _AUTOMATIC_ACTIVE.add(key)
        try:
            identity=local.get('peer_identity',key)
            keys={value['public_key'] for value in grouped[key]}
            if (not identity and len(keys)>1) or (identity and identity.get('public_key')!=pending['public_key']):
                raise PeerError('identity_already_bound','不同公钥的登记申请冲突')
            opid=pending.get('approved_operation_id') or 'auto-enrollment:'+central.digest([key,pending['request_id'],pending['request_hash']])[:32]
            result=await _confirm_enrollment(key,None,opid,pending['request_id'],pending['request_hash'],automatic=True)
            state='registered' if result['readback_verified'] else 'registry_pending'
            _automatic_observation(pending['request_id'],state,result.get('error_code',''),
                '登记完成，互派就绪由节点回执确认。' if result['readback_verified'] else '正在核对自动登记结果，无需人工审核。')
            results.append({'request_id':pending['request_id'],'status':state,'readback_verified':result['readback_verified']})
            if not result['readback_verified']:issues+=1
        except (ValueError,KeyError,TypeError) as exc:
            code,message=_automatic_error(exc)
            state='conflict' if code in ('identity_already_bound','operation_conflict','identity_expired') else 'failed'
            _automatic_observation(pending['request_id'],state,code,message)
            results.append({'request_id':pending['request_id'],'status':state,'error_code':code});issues+=1
        finally:
            with _LOCK:_AUTOMATIC_ACTIVE.discard(key)
    return {'items':results,'issue_count':issues}


class EnrollmentApproval(IdentityRequest):
    request_id:str=Field(min_length=8,max_length=150)
    expected_request_hash:str=Field(min_length=64,max_length=64,pattern=r'^[0-9a-f]{64}$')


@router.post('/nodes/{node_id}/approve-enrollment')
async def approve(node_id:str,payload:EnrollmentApproval,actor:dict=Depends(current_user)):
    try:return await approve_enrollment(node_id,actor,payload.operation_id,payload.request_id,payload.expected_request_hash,payload.rotate)
    except PeerError as exc:raise HTTPException(403 if exc.code=='authorization_failed' else 409,{'code':exc.code,'message':str(exc)})
