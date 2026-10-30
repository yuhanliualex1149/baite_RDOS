"""Direct node dispatch. Transport is the existing Feishu CLI, not a new service."""
from __future__ import annotations
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess

PEER_SKILL_VERSION='1.2.0'

class PeerMixin:
    def _peer_error(self,code,message):
        raise self.error_type(code,message)

    def _peer_failure(self,error):
        """Keep recovery evidence, never subprocess text, arguments or secrets."""
        import re
        outcome=getattr(error,'write_outcome',None)
        if outcome not in ('not_sent','rejected'):outcome='unknown'
        code=getattr(error,'code','transport_error')
        if not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',code):code='transport_error'
        details={}
        metadata=getattr(error,'details',None)
        for key,value in (metadata if isinstance(metadata,dict) else {}).items():
            if key not in ('category','errno','exit_code','remote_code'):continue
            if type(value) is int or isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}',value):details[key]=value
        return {'code':code,'write_outcome':outcome,'details':details}

    def _peer_event_fields(self,event):
        from rdos_node import canonical
        return {'操作ID':event['operation_id'],'事件ID':event['id'],'类型':event['event_type'],
            '自报节点':event['claimed_node_id'],'任务引用':canonical(event['task_refs']),'记录数据':canonical(event)}

    def _peer_event_rows(self,event):
        """Find by either projection or immutable JSON, then fetch every match."""
        expected=self._peer_event_fields(event);matches=[]
        for row in self.transport.records(self.manifest['events_table']):
            if not isinstance(row,dict) or not isinstance(row.get('fields'),dict):continue
            fields=row.get('fields',{})
            try:body=json.loads(self.cell(fields.get('记录数据')))
            except (ValueError,TypeError):body={}
            if not isinstance(body,dict):body={}
            candidate=(self.cell(fields.get('操作ID'))==event['operation_id']
                or self.cell(fields.get('事件ID'))==event['id']
                or body.get('operation_id')==event['operation_id'] or body.get('id')==event['id'])
            if not candidate:continue
            identifier=row.get('record_id')
            if not identifier:self._peer_error('readback_mismatch','原协作记录缺少可独立读取的标识')
            actual=self.transport.get(self.manifest['events_table'],identifier)
            if not isinstance(actual,dict) or actual.get('record_id')!=identifier or not isinstance(actual.get('fields'),dict):
                self._peer_error('readback_mismatch','独立读回返回了其他记录，未更新原操作')
            actual_fields=actual.get('fields',{})
            try:content=json.loads(self.cell(actual_fields.get('记录数据')))
            except (ValueError,TypeError):self._peer_error('operation_conflict','原操作记录正文不可核对，未覆盖或重发')
            if content!=event:self._peer_error('operation_conflict','相同原操作标识出现不同内容，未覆盖或重发')
            # A missing projection is recoverable from the complete signed JSON;
            # a populated contradictory projection remains a conflict.
            if any(self.cell(actual_fields.get(key)) not in ('',value)
                   for key,value in expected.items() if key!='记录数据'):
                self._peer_error('operation_conflict','原操作可读字段与正文冲突，未覆盖或重发')
            matches.append(identifier)
        return sorted(set(matches))

    def _peer_saved_context(self,operation_id,operation,*,write=False,current_task=None):
        """Validate saved evidence against current scope without deriving keys."""
        event=operation.get('desired',{})
        if (not isinstance(event,dict) or event.get('operation_id')!=operation_id or event.get('workspace_id')!=self.manifest['workspace_id']
                or event.get('claimed_node_id')!=self.manifest['node_id'] or event.get('protocol_version')!=2):
            self._peer_error('out_of_scope','原协作操作不属于当前节点或工作空间')
        project=self.project(event.get('project_id'))
        context={'project_id':event.get('project_id'),'project_status':project['status']}
        if event.get('event_type')=='peer_enrollment_request':
            claim=self._peer_verify(event.get('enrollment_proof'),operation.get('public_key'))
            ref={'task_id':operation.get('task_id'),'requirements_version':operation.get('requirements_version')}
            if (claim.get('node_id')!=self.manifest['node_id'] or claim.get('workspace_id')!=self.manifest['workspace_id']
                    or claim.get('request_id')!=operation.get('request_id') or claim.get('task_ref')!=ref
                    or claim.get('public_key')!=operation.get('public_key') or event.get('task_refs')!=[ref]
                    or event.get('id')!='evt_'+str(operation.get('request_id'))):
                self._peer_error('operation_conflict','原接入申请与持钥证明不一致')
            task=current_task if current_task is not None else self.task(operation['task_id'])
            if (task.get('task_id')!=operation.get('task_id') or task.get('requirements_version')!=operation.get('requirements_version')
                    or task.get('project_id')!=event.get('project_id')
                    or self.manifest['node_id'] not in task.get('executor_node_ids',[])
                    or task.get('dispatch_source')=='peer' or task.get('task_type','action')!='action'):
                self._peer_error('version_conflict','原申请对应任务、版本或执行节点已变化')
            context.update(task_id=task['task_id'],requirements_version=task['requirements_version'],
                record_revision=task['record_revision'],business_status=task['business_status'])
            if write and task['business_status']!='active':
                self._peer_error('enrollment_not_authorized','任务当前不在执行中，只核对原申请，不再次写入')
        elif event.get('event_type')=='peer_capability_ack':
            identity=self._peer_identity();snapshot=self._peer_registry()
            registered=next((entry for entry in snapshot['nodes'] if entry.get('node_id')==self.manifest['node_id']
                and entry.get('binding_id')==identity.get('binding_id') and entry.get('enabled') is True),None)
            if not registered:self._peer_error('identity_expired','当前节点登记已变化，不重放旧能力回执')
            claim=self._peer_verify(event.get('peer_ack'),registered['public_key'])
            if (claim.get('node_id')!=self.manifest['node_id'] or claim.get('binding_id')!=identity.get('binding_id')
                    or claim.get('capability')!='peer_tasks_v1'):
                self._peer_error('identity_expired','原能力回执与当前节点身份不一致')
            context.update(binding_id=identity['binding_id'],skill_version=claim.get('skill_version'))
        else:self._peer_error('operation_missing','此入口只恢复本节点已保存的申请或能力回执')
        if write and project['status']!='active':self._peer_error('out_of_scope','项目已停用，只核对原记录，不重新写入')
        return context

    def _peer_write_saved(self,operation_id,operation,*,explicit_retry=False):
        """Caller owns the state lock. Unknown writes are never retried here."""
        event=operation['desired'];matches=self._peer_event_rows(event)
        target='requested' if event['event_type']=='peer_enrollment_request' else 'verified'
        if not matches:
            failure=operation.get('last_failure') or {}
            retryable=operation.get('state')=='retryable' and failure.get('write_outcome') in ('not_sent','rejected')
            if operation.get('state')!='prepared' and not retryable and not explicit_retry:
                self._peer_error('write_uncertain','原操作尚未找到；请先预览 recover-peer 恢复计划，不换标识或密钥重发')
            self._peer_saved_context(operation_id,operation,write=True)
            attempts=operation.setdefault('attempts',[])
            attempts.append({'number':len(attempts)+1,'started_at':datetime.now(timezone.utc).isoformat(),
                'write_outcome':'unknown','explicit_recovery':bool(explicit_retry)})
            operation['state']='sent';self.state.put('operations',operation_id,operation);self.state.flush()
            error=None
            try:self.transport.append(self._peer_event_fields(event))
            except ValueError as exc:
                error=exc;failure=self._peer_failure(exc)
                operation['last_failure']=failure;attempts[-1].update(failure)
                if failure['write_outcome'] in ('not_sent','rejected'):operation['state']='retryable'
            else:attempts[-1]['response_received']=True
            self.state.put('operations',operation_id,operation);self.state.flush()
            try:matches=self._peer_event_rows(event)
            except ValueError as exc:
                operation['last_read_error']=self._peer_failure(exc)
                self.state.put('operations',operation_id,operation)
                raise
            if not matches:
                if error and operation['state']=='retryable':
                    raise self.error_type(operation['last_failure']['code'],
                        '原操作确定未完成发送；保留原申请，连接恢复后可继续原步骤',
                        write_outcome=operation['last_failure']['write_outcome'],details=operation['last_failure']['details'])
                self._peer_error('write_uncertain','原操作尚未独立读回；保留原身份和操作，不自动再次写入')
        operation.update(state=target,readback={'record_ids':matches,'logical_events':1,
            'verified_at':datetime.now(timezone.utc).isoformat()})
        self.state.put('operations',operation_id,operation)
        return {'readback_verified':True,'record_ids':matches,'logical_events':1,'physical_rows':len(matches)}

    def recover_peer(self,operation_id,expected_hash=None,confirm_retry=False):
        """Preview by default; an exceptional retransmission needs this exact plan."""
        from rdos_node import sha
        with self.state.locked():
            operation=self.state.get('operations',operation_id)
            if not operation:self._peer_error('operation_missing','本节点没有该原操作检查点')
            context=self._peer_saved_context(operation_id,operation)
            event=operation['desired'];matches=self._peer_event_rows(event)
            if matches:
                result=self._peer_write_saved(operation_id,operation)
                return {**result,'operation_id':operation_id,'status':'verified',
                    'message':'原协作记录已独立核对，无需再次写入。'}
            plan={'version':1,'operation_id':operation_id,'workspace_id':self.manifest['workspace_id'],
                'node_id':self.manifest['node_id'],'event_type':event['event_type'],'desired_hash':sha(event),
                'checkpoint_state':operation.get('state'),'attempt_count':len(operation.get('attempts',[])),**context}
            plan_hash=sha(plan)
            if not confirm_retry:
                operation['recovery_plan']={'hash':plan_hash,'plan':plan,'consumed':False}
                self.state.put('operations',operation_id,operation)
                return {'operation_id':operation_id,'status':'recovery_confirmation_required','readback_verified':False,
                    'plan_hash':plan_hash,'plan':plan,'message':'尚未找到原记录。确认恢复后仅重放同一申请与身份；飞书可能保留相同重复行，但只算一个业务事件。'}
            saved=operation.get('recovery_plan') or {}
            if not expected_hash or expected_hash!=plan_hash or saved.get('hash')!=plan_hash or saved.get('consumed'):
                self._peer_error('version_conflict','恢复计划已变化、已使用或未经预览，请重新核对原操作')
            self._peer_saved_context(operation_id,operation,write=True)
            saved['consumed']=True;operation['recovery_plan']=saved
            self.state.put('operations',operation_id,operation);self.state.flush()
            result=self._peer_write_saved(operation_id,operation,explicit_retry=True)
            return {**result,'operation_id':operation_id,'status':'verified',
                'message':'已恢复原协作记录并独立核对，未更换身份或新建申请。'}

    def _peer_crypto(self,request):
        candidates=[shutil.which('node'),str(Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node')]
        helper=Path(__file__).with_name('peer_crypto.cjs')
        for binary in dict.fromkeys(candidates):
            if not binary or not Path(binary).is_file():continue
            try:
                process=subprocess.run([binary,str(helper)],input=json.dumps(request,ensure_ascii=False),text=True,capture_output=True,timeout=10,check=False)
                value=json.loads(process.stdout)
                if process.returncode==0 and not value.get('error'):return value
            except (OSError,ValueError,subprocess.TimeoutExpired):continue
        self._peer_error('runtime_unavailable','现有 Node 签名功能不可用；未安装依赖、服务或修复环境')

    def _peer_verify(self,proof,key):
        if not isinstance(proof,dict) or set(proof)!= {'payload_json','signature'}:
            self._peer_error('invalid_signature','任务或登记缺少完整签名')
        if not self._peer_crypto({'action':'verify','public_key':key,'payload':proof['payload_json'],'signature':proof['signature']}).get('valid'):
            self._peer_error('invalid_signature','任务正文或登记签名不一致')
        value=json.loads(proof['payload_json'])
        if not isinstance(value,dict):self._peer_error('invalid_signature','签名正文无效')
        return value

    def _peer_sign(self,value,identity):
        text=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        return {'payload_json':text,'signature':self._peer_crypto({'action':'sign','private_key':identity['private_key'],'payload':text})['signature']}

    def _peer_registry(self):
        config=self.manifest.get('peer') or {}
        key=config.get('central_public_key')
        if not key:self._peer_error('peer_not_enabled','请先安装当前节点的新版 Skill 并完成身份绑定')
        snapshots=[];acks=[];quarantined=0
        for row in self.transport.records(self.manifest['events_table']):
            try:value=json.loads(self.cell(row.get('fields',{}).get('记录数据')))
            except (ValueError,TypeError):continue
            if value.get('workspace_id')!=self.manifest['workspace_id']:continue
            if value.get('event_type')=='node_registry_snapshot':
                try:snapshot=self._peer_verify(value.get('registry_proof'),key)
                except ValueError:
                    quarantined+=1;continue
                if snapshot.get('workspace_id')==self.manifest['workspace_id']:snapshots.append(snapshot)
            elif value.get('event_type')=='peer_capability_ack':acks.append(value)
        if not snapshots:self._peer_error('peer_registry_missing','尚未读到中央签名的节点登记')
        snapshot=max(snapshots,key=lambda item:item.get('generated_at',''))
        try:expired=datetime.fromisoformat(snapshot['expires_at'])<=datetime.now(timezone.utc)
        except (KeyError,ValueError,TypeError):expired=True
        if expired:self._peer_error('identity_expired','节点登记已过期，请先更新接入登记')
        result=copy.deepcopy(snapshot)
        for node in result['nodes']:
            for event in acks:
                if event.get('claimed_node_id')!=node['node_id']:continue
                try:claim=self._peer_verify(event.get('peer_ack'),node['public_key'])
                except ValueError:continue
                if claim.get('skill_version') in ('1.1.0','1.1.1','1.1.2','1.1.3','1.2.0') and claim=={'node_id':node['node_id'],'binding_id':node['binding_id'],'capability':'peer_tasks_v1','skill_version':claim.get('skill_version')}:
                    node['ready']=True
        self.peer_registry_issues=quarantined
        return result

    def enroll_peer(self,task_id):
        """Explicitly requested setup; a normal check never generates a key."""
        from rdos_node import sha,canonical
        task=self.task(task_id)
        pointer=self.state.get('operations','peer_enrollment_current') or {}
        original=self.state.get('operations',pointer.get('operation_id',''))
        same_request=bool(original and original.get('kind')=='peer_enrollment' and original.get('task_id')==task_id
            and original.get('requirements_version')==task.get('requirements_version'))
        continuing=bool(same_request and task.get('business_status')=='closed' and task.get('close_reason') in ('completed','manual_completed'))
        if (self.manifest['node_id'] not in task.get('executor_node_ids',[]) or (task.get('business_status')!='active' and not continuing)
                or task.get('dispatch_source')=='peer' or task.get('task_type','action')!='action'):
            self._peer_error('enrollment_not_authorized','接入需要当前分配给本节点的有效中央维护任务')
        key=(self.manifest.get('peer') or {}).get('central_public_key')
        if not key:self._peer_error('peer_not_enabled','当前 Skill 未包含固定中央身份，请先更新接入配置')
        # A requirements revision does not create a new cryptographic identity.
        # Reuse the original key, while a new effective task/version needs its
        # own immutable authorization reference instead of borrowing an old one.
        request_id=original['request_id'] if same_request else 'enroll_'+sha([self.manifest['workspace_id'],self.manifest['node_id'],task_id,task['requirements_version']])[:32]
        opid=pointer['operation_id'] if same_request else 'peer_enrollment_'+request_id
        with self.state.locked():
            operation=self.state.get('operations',opid)
            if operation is None:
                installed=self.state.get('operations','peer_identity')
                pair=installed if installed and installed.get('private_key') and installed.get('public_key') else original if original and original.get('private_key') and original.get('public_key') else self._peer_crypto({'action':'generate'})
                if not pair.get('public_key') or not pair.get('private_key'):
                    self._peer_error('runtime_unavailable','本机签名功能未返回有效身份')
                claim={'version':1,'workspace_id':self.manifest['workspace_id'],'request_id':request_id,'node_id':self.manifest['node_id'],
                    'public_key':pair['public_key'],'task_ref':{'task_id':task_id,'requirements_version':task['requirements_version']},
                    'requested_at':datetime.now(timezone.utc).isoformat()}
                event={'protocol_version':2,'workspace_id':self.manifest['workspace_id'],'project_id':task['project_id'],
                    'id':'evt_'+request_id,'operation_id':opid,'claimed_node_id':self.manifest['node_id'],
                    'event_type':'peer_enrollment_request','task_refs':[claim['task_ref']],'body':'申请启用节点工作协作能力。',
                    'enrollment_proof':self._peer_sign(claim,pair)}
                operation={'kind':'peer_enrollment','request_id':request_id,'task_id':task_id,'requirements_version':task['requirements_version'],
                    'public_key':pair['public_key'],'private_key':pair['private_key'],'desired':event,'state':'prepared'}
                self.state.put('operations',opid,operation)
                self.state.put('operations','peer_enrollment_current',{'operation_id':opid})
                self.state.flush()
            self._peer_saved_context(opid,operation)
            self._peer_write_saved(opid,operation)
        fingerprint=sha(operation['public_key'])
        try:automatic=self.continue_peer_enrollment()
        except ValueError as exc:automatic={'status':'recovery_pending','error_code':getattr(exc,'code','peer_recovery_failed')}
        return {'request_id':request_id,'status':automatic['status'],'readback_verified':True,'registration_mode':'automatic',
            'confirmation_code':'-'.join(fingerprint[index:index+4].upper() for index in (0,4,8)),
            'task_completion_allowed':True,'message':('自动登记与节点回执已完成。' if automatic['status']=='ready' else '申请已写回并核对，中央随既有同步自动登记，无需人工审核；后续检查会自动核对，不重复申请或搬运密钥。')}

    def ensure_peer_enrollment_completion(self,task_id,current_task=None):
        pointer=self.state.get('operations','peer_enrollment_current')
        operation=self.state.get('operations',pointer.get('operation_id')) if pointer else None
        if not operation or operation.get('task_id')!=task_id:return
        if operation.get('state') not in ('requested','approved'):
            self._peer_error('write_uncertain','本节点接入申请尚未独立读回，请继续核对原申请')
        self._peer_saved_context(pointer['operation_id'],operation,current_task=current_task)
        # The submitted request is the node-owned setup result. Central approval
        # and capability readiness proceed independently through later checks.


    def continue_peer_enrollment(self):
        pointer=self.state.get('operations','peer_enrollment_current')
        if not pointer:return {'status':'not_requested'}
        operation=self.state.get('operations',pointer.get('operation_id'))
        if not operation or operation.get('kind')!='peer_enrollment':
            self._peer_error('invalid_enrollment','本机接入检查点无效')
        if operation.get('state') not in ('requested','approved'):
            matches=self._peer_event_rows(operation['desired'])
            if not matches:
                return {'status':'request_unverified','request_id':operation['request_id'],
                    'operation_id':pointer['operation_id'],'task_completion_allowed':False,
                    'recovery_required':True,'error_code':(operation.get('last_failure') or {}).get('code','write_uncertain'),
                    'message':'本节点申请尚未读回，中央尚未收到可自动登记的申请；请核对原操作的恢复计划。'}
            with self.state.locked():
                operation['state']='requested';self.state.put('operations',pointer['operation_id'],operation)
        try:registry=self._peer_registry()
        except ValueError as exc:
            if getattr(exc,'code','') in ('peer_registry_missing','identity_expired'):
                return {'status':'pending_confirmation','request_id':operation['request_id'],
                    'registration_mode':'automatic','message':'申请已写回，等待中央随既有同步自动登记，无需人工审核。'}
            raise
        registered=next((entry for entry in registry['nodes'] if entry.get('node_id')==self.manifest['node_id']
            and entry.get('public_key')==operation['public_key']),None)
        if registered is None:return {'status':'pending_confirmation','request_id':operation['request_id'],
            'registration_mode':'automatic','message':'申请已写回，等待中央随既有同步自动登记，无需人工审核。'}
        identity={'version':1,'node_id':self.manifest['node_id'],'binding_id':registered['binding_id'],
            'private_key':operation['private_key'],'public_key':operation['public_key'],
            'central_public_key':self.manifest['peer']['central_public_key'],'workspace_id':self.manifest['workspace_id']}
        with self.state.locked():
            self.state.put('operations','peer_identity',identity)
            self.state.put('operations',pointer['operation_id'],{**operation,'state':'approved','binding_id':registered['binding_id']})
        receipt=self.peer_ack()
        return {'status':'ready','request_id':operation['request_id'],'binding_id':registered['binding_id'],
            'readback_verified':receipt['readback_verified']}

    def bind_peer_identity(self,path):
        private_path=Path(path).expanduser()
        if private_path.is_symlink() or not private_path.is_file():self._peer_error('invalid_identity','私有接入文件必须是普通文件')
        os.chmod(private_path,0o600)
        value=json.loads(private_path.read_text())
        if 'identity' in value:value=value['identity']
        if (value.get('node_id')!=self.manifest['node_id'] or value.get('workspace_id')!=self.manifest['workspace_id']
                or value.get('central_public_key')!=(self.manifest.get('peer') or {}).get('central_public_key')):
            self._peer_error('out_of_scope','私有接入文件与当前节点或固定中央身份不符')
        # Runtime probe also proves the delivered key is usable, without installing.
        self._peer_sign({'probe':'peer_tasks_v1'},value)
        snapshot=self._peer_registry()
        registered=next((n for n in snapshot['nodes'] if n['node_id']==value['node_id'] and n['binding_id']==value.get('binding_id')),None)
        if not registered:self._peer_error('identity_expired','接入文件已被轮换或未登记')
        with self.state.locked():self.state.put('operations','peer_identity',value)
        return self.peer_ack()

    def _peer_identity(self):
        identity=self.state.get('operations','peer_identity')
        if not identity:self._peer_error('peer_not_bound','请按当前维护任务申请节点接入，等待一次归属确认')
        return identity

    def peer_ack(self):
        identity=self._peer_identity();claim={'node_id':self.manifest['node_id'],'binding_id':identity['binding_id'],'capability':'peer_tasks_v1','skill_version':PEER_SKILL_VERSION}
        from rdos_node import sha,canonical
        opid='peer_ack_'+sha(claim)[:32]
        event={'id':'evt_'+opid,'operation_id':opid,'protocol_version':2,'workspace_id':self.manifest['workspace_id'],'project_id':self.manifest['project_id'],
            'event_type':'peer_capability_ack','claimed_node_id':self.manifest['node_id'],'task_refs':[],'body':'已启用当前工作协作方式。','peer_ack':self._peer_sign(claim,identity)}
        with self.state.locked():
            previous=self.state.get('operations',opid)
            if previous and previous['desired']!=event:self._peer_error('operation_conflict','能力回执内容冲突')
            operation=previous or {'kind':'peer_capability_ack','desired':event,'state':'prepared'}
            self._peer_saved_context(opid,operation)
            self._peer_write_saved(opid,operation)
        return {'bound':True,'capability':'peer_tasks_v1','readback_verified':True}

    def _peer_project(self,task):
        if task.get('dispatch_source')!='peer':return None
        registry=self._peer_registry()
        own=self.manifest['node_id']
        if own not in task.get('executor_node_ids',[]) and own!=task.get('peer_issuer_node_id'):
            self._peer_error('unregistered_project','互派任务未分配给当前节点')
        identity=next((n for n in registry['nodes'] if n['node_id']==task.get('peer_issuer_node_id')),None)
        if not identity:self._peer_error('peer_not_ready','发起节点未登记')
        key=identity['public_key'] if task.get('record_revision')==1 else self.manifest['peer']['central_public_key']
        signed=self._peer_verify(task.get('peer_proof'),key)
        expected={k:v for k,v in task.items() if k!='peer_proof'}
        if signed!=expected:self._peer_error('task_tampered','任务正文、版本或交付范围与签名不一致')
        from rdos_node import rules
        if task.get('record_revision')==1:project=rules.validate_peer_task(signed,registry,task['peer_issuer_node_id'])
        else:
            project=next((p for p in registry['projects'] if p['project_id']==task['project_id']),None)
            if not project:self._peer_error('out_of_scope','任务项目未登记')
        grant=task['peer_task_scope']
        if hasattr(self.transport,'peer_folders'):self.transport.peer_folders.add(grant['output_folder_id'])
        return {'id':task['project_id'],'status':project['status'],'node_ids':[own],
                'zones':{grant['output_zone']:{'folder_id':grant['output_folder_id']}},'registered_resources':[],'task_only':True}

    def dispatch_peer(self,request,operation_key):
        """The caller prepares this only from the researcher's current request."""
        from rdos_node import sha,canonical,rules
        identity=self._peer_identity();registry=self._peer_registry()
        if not isinstance(request,dict) or set(request)-set(rules.LEGACY_TASK_FIELDS+rules.BUSINESS_TASK_FIELDS+('project_id','recipient_node_id')):
            self._peer_error('invalid_input','互派内容包含不支持字段')
        project=next((p for p in registry['projects'] if p['project_id']==request.get('project_id')),None)
        if not project:self._peer_error('out_of_scope','请选择当前节点参与的项目')
        opid='peer_dispatch_'+sha([self.manifest['workspace_id'],self.manifest['node_id'],operation_key])[:32]
        task={'protocol_version':2,'workspace_id':self.manifest['workspace_id'],'id':'task_'+sha(opid)[:24],'task_id':'task_'+sha(opid)[:24],
            'project_id':project['project_id'],'owner_id':project['owner_id'],'title':request.get('title',''),'goal':request.get('goal',''),
            'deliverable':request.get('deliverable',''),'executor_node_ids':[request.get('recipient_node_id')],
            'input_refs':request.get('input_refs',[]),'editable_document_ids':[],'output_zone':request.get('output_zone','project'),
            'dispatch_to':['agent'],'return_to':['agent'],'completion_rule':{'kind':'receipt_recorded'},
            'task_type':request.get('task_type','action'),'deliverable_audience':request.get('deliverable_audience',['researcher']),
            'due_at':request.get('due_at',''),'business_status':'active','requirements_version':1,'record_revision':1,'operation_id':opid,
            'dispatch_source':'peer','peer_issuer_node_id':self.manifest['node_id'],'peer_binding_id':identity['binding_id']}
        task['peer_task_scope']={'input_refs':task['input_refs'],'output_zone':task['output_zone'],
            'output_folder_id':project.get('zones',{}).get(task['output_zone'],{}).get('folder_id')}
        task['requirements_hash']=rules.digest(rules.requirements(task))
        rules.validate_peer_task(task,registry,self.manifest['node_id'])
        task['peer_proof']=self._peer_sign(task,identity)
        fields={'操作ID':opid,'任务ID':task['task_id'],'标题':task['title'],'状态':'active','执行节点':', '.join(task['executor_node_ids']),
            '要求版本':'1','记录修订':'1','记录数据':canonical(task)}
        with self.state.locked():
            previous=self.state.get('operations',opid)
            if previous and previous['desired']!=task:self._peer_error('operation_conflict','同一派发操作不能改用不同内容')
            rows=self.transport.records(self.manifest['tasks_table'])
            matches=[r for r in rows if self.cell(r.get('fields',{}).get('任务ID'))==task['task_id']]
            if not matches:
                if previous:self._peer_error('write_uncertain','原派发可能已生效，只核对原操作，不再次创建')
                self.state.put('operations',opid,{'kind':'peer_dispatch','desired':task,'state':'sent'});self.state.flush()
                try:self.transport.append_task(fields)
                except ValueError:pass
                matches=[r for r in self.transport.records(self.manifest['tasks_table']) if self.cell(r.get('fields',{}).get('任务ID'))==task['task_id']]
            if not matches:self._peer_error('write_uncertain','任务尚未独立读回；保留原操作继续核对')
            actual=[]
            for row in matches:
                item,rid=self.decode(self.transport.get(self.manifest['tasks_table'],row['record_id']),'task');actual.append(item)
            effective=rules.select_verified_task_versions(actual)
            if effective!=task:
                if not previous or effective.get('record_revision',0)<=1:self._peer_error('task_conflict','同一任务有不同创建内容')
                return {'task_id':task['task_id'],'business_status':effective['business_status'],'requirements_version':effective['requirements_version'],'readback_verified':True,'already_published':True,'message':'原任务已经发布，当前状态已更新。'}
            self.state.put('operations',opid,{'kind':'peer_dispatch','desired':task,'state':'verified'})
        return {'task_id':task['task_id'],'business_status':'active','readback_verified':True,'message':'已发布，等待接收节点检查。'}

    def peer_progress(self,task_id):
        task=self.task(task_id)
        if task.get('peer_issuer_node_id')!=self.manifest['node_id']:self._peer_error('out_of_scope','只能查询自己发起的互派任务')
        rows,issues=self.read_rows('event')
        return {'task_id':task_id,'business_status':task['business_status'],'results':[value for value,rid in rows if any(ref.get('task_id')==task_id for ref in value.get('task_refs',[]))],'quarantined_records':issues}
