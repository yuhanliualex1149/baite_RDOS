"""Node-signed direct task creation, with pinned central identity registration.

The private key delivered once to a node is never persisted by the server.
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
        values.append({'node_id':node.get('public_id',node['node_id']), 'label':node.get('display_name',node.get('label')), 'bound':identity.get('delivery_status') in ('delivered','registered'),'delivery_status':identity.get('delivery_status','unbound'),'ready':bool(identity.get('ready')) and state.get('enabled') is True,'runtime_available':state.get('enabled') is True, 'disabled_reason':state.get('message','') if not state.get('enabled') else '', 'runtime_error_code':state.get('error_code',''), 'binding_id':identity.get('binding_id'),'skill_version':identity.get('skill_version'),'pending_requests':[_pending_public(value) for value in local.items('peer_enrollment_request') if value.get('node_id')==node['node_id'] and value.get('status') in ('pending','registry_pending')]})
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
    if not matches or any(backends.payload(v)!=backends.payload(event) for v in matches):
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
        payload=verified(raw.get('registry_proof'),_central_keys()['public_key'])
        if payload.get('workspace_id')!=_registry_payload()['workspace_id']:
            raise PeerError('out_of_scope','身份登记不属于此工作空间')
        return local.index_event(raw,backends.payload(raw))
    if raw.get('event_type')=='peer_capability_ack':
        node=_node(raw.get('claimed_node_id'));identity=local.get('peer_identity',node['node_id'])
        if not identity:raise PeerError('peer_not_bound','节点尚未绑定')
        claim=verified(raw.get('peer_ack'),identity['public_key'])
        public=rdos_contract.public_view(node['node_id'],backends.get_backend().registry())
        if claim.get('skill_version') not in ('1.1.0','1.1.1','1.1.2') or claim != {'node_id':public,'binding_id':identity['binding_id'],'capability':'peer_tasks_v1','skill_version':claim.get('skill_version')}:
            raise PeerError('invalid_ack','节点能力回执与当前绑定不一致')
        identity.update(ready=True,skill_version=claim['skill_version'])
        if identity.get('binding_source')=='approved_node_public_key':
            identity['delivery_status']='registered'
            pending=local.get('peer_enrollment_request',identity.get('enrollment_request_id',''))
            if pending and pending.get('status')=='registry_pending':
                local.put('peer_enrollment_request',pending['request_id'],{**pending,'status':'approved'})
        local.put('peer_identity',node['node_id'],identity)
        return local.index_event(raw,backends.payload(raw))
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
        'public_key_fingerprint','confirmation_code','requested_at','status')},backends.get_backend().registry()) | {'resume_operation_id':value.get('approved_operation_id')}


def _enrollment_task(task,node_id,version):
    from server.phase3_service import _validate_task
    if (not isinstance(task,dict) or task.get('business_status')!='active' or task.get('requirements_version')!=version
            or node_id not in task.get('executor_node_ids',[]) or task.get('dispatch_source')=='peer'
            or rules.task_type(task)!='action'):
        raise PeerError('enrollment_not_authorized','接入申请需要当前有效、分配给本节点的中央 action 任务')
    _validate_task(task)
    projects.require_active(task['project_id'])
    return task


def _ingest_enrollment(raw,tasks):
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
    if not prior:
        internal_ref=references[0]
        task=tasks.get(internal_ref['task_id']) or local.get('task_cache',internal_ref['task_id'])
        _enrollment_task(task,node['node_id'],internal_ref['requirements_version'])
        if raw['project_id']!=task['project_id']:
            raise PeerError('out_of_scope','接入申请不能借用其他项目任务')
        fingerprint=hashlib.sha256(claim['public_key'].encode()).hexdigest()
        prior={'request_id':request_id,'request_hash':request_hash,'node_id':node['node_id'],'public_key':claim['public_key'],
            'task_id':task['task_id'],'requirements_version':internal_ref['requirements_version'],'project_id':task['project_id'],
            'public_key_fingerprint':fingerprint,'confirmation_code':'-'.join(fingerprint[index:index+4].upper() for index in (0,4,8)),
            'requested_at':claim['requested_at'],'status':'pending','source_event_id':raw['id'],
            'source_event_hash':central.digest(backends.payload(raw))}
        local.immutable('peer_enrollment_request',request_id,prior)
    return local.index_event(raw,backends.payload(raw))


def _transaction_put(conn,kind,identifier,value):
    conn.execute('INSERT INTO phase3_local VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at',
        (kind,identifier,json.dumps(value,ensure_ascii=False),db.utc_now()))


async def approve_enrollment(node_id,actor,operation_id,request_id,expected_request_hash,rotate=False):
    _require_runtime()
    node=_node(node_id);user=_actor(actor,node)
    pending=local.get('peer_enrollment_request',request_id)
    if not pending or pending.get('node_id')!=node['node_id'] or pending.get('request_hash')!=expected_request_hash:
        raise PeerError('version_conflict','接入申请已变化，请重新核对当前申请')
    request={'node_id':node['node_id'],'request_id':request_id,'request_hash':expected_request_hash,'actor_id':user['id'],'rotate':rotate}
    requested_operation=local.get('peer_enrollment_operation',operation_id)
    if requested_operation and requested_operation.get('request')!=request:
        raise PeerError('operation_conflict','同一确认操作不能改用其他接入申请')
    canonical_id=pending.get('approved_operation_id')
    if canonical_id:
        operation_id=canonical_id
        operation=local.get('peer_enrollment_operation',operation_id)
        original=(operation or {}).get('request',{})
        if any(original.get(key)!=request.get(key) for key in ('node_id','request_id','request_hash')):
            raise PeerError('operation_conflict','原归属确认依据不完整，未重新生成绑定')
        request=original
    else:
        operation=requested_operation
    if operation and operation.get('state')=='verified':
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
        _enrollment_task(current_task,node['node_id'],pending['requirements_version'])
        raw=[value for value in await asyncio.to_thread(backends.get_backend().list_events) if value.get('id')==pending['source_event_id']]
        if not raw or any(central.digest(backends.payload(value))!=pending['source_event_hash'] for value in raw):
            raise PeerError('version_conflict','接入申请原文尚未独立核对或已经变化')
        with _LOCK,db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            user_row=conn.execute('SELECT enabled,roles_json FROM central_users WHERE id=?',(user['id'],)).fetchone()
            if not user_row or not user_row['enabled'] or ('admin' not in json.loads(user_row['roles_json']) and node.get('member_id')!=user['id']):
                raise PeerError('authorization_failed','当前账号不能确认此节点归属')
            node_row=conn.execute("SELECT payload FROM phase3_local WHERE kind='node' AND id=?",(node['node_id'],)).fetchone()
            if not node_row or not json.loads(node_row[0]).get('enabled',True):
                raise PeerError('authorization_failed','节点已经停用，未确认新接入')
            head_row=conn.execute("SELECT payload FROM phase3_local WHERE kind='task_head' AND id=?",(pending['task_id'],)).fetchone()
            head=json.loads(head_row[0]) if head_row else {}
            if head.get('requirements_version')!=current_task['requirements_version'] or head.get('record_revision')!=current_task['record_revision']:
                raise PeerError('version_conflict','维护任务已变化，请重新核对接入申请')
            project_row=conn.execute("SELECT payload FROM central_records WHERE kind='project' AND id=?",(pending['project_id'],)).fetchone()
            if not project_row or json.loads(project_row[0]).get('status')!='active':
                raise PeerError('out_of_scope','任务项目已经停用，未确认新接入')
            latest_row=conn.execute("SELECT payload FROM phase3_local WHERE kind='peer_enrollment_request' AND id=?",(request_id,)).fetchone()
            latest=json.loads(latest_row[0]) if latest_row else {}
            if latest.get('approved_operation_id') and latest['approved_operation_id']!=operation_id:
                raise PeerError('enrollment_confirmation_in_progress','另一归属确认已保存，请刷新后继续核对原申请')
            if latest.get('request_hash')!=expected_request_hash or latest.get('status') not in ('pending','registry_pending'):
                raise PeerError('version_conflict','接入申请已由另一操作处理')
            prior_row=conn.execute("SELECT payload FROM phase3_local WHERE kind='peer_identity' AND id=?",(node['node_id'],)).fetchone()
            previous=json.loads(prior_row[0]) if prior_row else None
            if previous and previous.get('public_key')!=pending['public_key'] and not rotate:
                raise PeerError('identity_already_bound','节点已有其他绑定；请确认是否轮换')
            identity={'node_id':node['node_id'],'binding_id':'binding_'+central.digest([node['node_id'],request_id,operation_id])[:24],
                'public_key':pending['public_key'],'ready':False,'binding_source':'approved_node_public_key','enrollment_request_id':request_id,
                'delivery_status':'registry_pending','issued_by':user['id'],'issued_at':db.utc_now()}
            operation={'request':request,'binding_id':identity['binding_id'],'state':'registry_pending'}
            _transaction_put(conn,'peer_identity',node['node_id'],identity)
            _transaction_put(conn,'peer_enrollment_request',request_id,{**latest,'status':'registry_pending','approved_by':user['id'],'approved_operation_id':operation_id})
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
    identity=local.get('peer_identity',node['node_id']) or {}
    if identity.get('binding_id')!=operation['binding_id']:
        raise PeerError('identity_expired','节点绑定已变化，未覆盖新绑定')
    identity['delivery_status']='registered';local.put('peer_identity',node['node_id'],identity)
    latest=local.get('peer_enrollment_request',request_id)
    local.put('peer_enrollment_request',request_id,{**latest,'status':'approved','registry_event_id':identifier})
    result={'node_id':node.get('public_id',node['node_id']),'binding_id':identity['binding_id'],'status':'registered',
        'registry_event_id':identifier,'readback_verified':True,'resume_operation_id':operation_id,'message':'节点归属已确认，等待节点自动回执。'}
    local.put('peer_enrollment_operation',operation_id,{**operation,'state':'verified','result':result})
    return result


class EnrollmentApproval(IdentityRequest):
    request_id:str=Field(min_length=8,max_length=150)
    expected_request_hash:str=Field(min_length=64,max_length=64,pattern=r'^[0-9a-f]{64}$')


@router.post('/nodes/{node_id}/approve-enrollment')
async def approve(node_id:str,payload:EnrollmentApproval,actor:dict=Depends(current_user)):
    try:return await approve_enrollment(node_id,actor,payload.operation_id,payload.request_id,payload.expected_request_hash,payload.rotate)
    except PeerError as exc:raise HTTPException(403 if exc.code=='authorization_failed' else 409,{'code':exc.code,'message':str(exc)})
