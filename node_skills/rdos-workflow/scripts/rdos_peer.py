"""Direct node dispatch. Transport is the existing Feishu CLI, not a new service."""
from __future__ import annotations
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess

PEER_SKILL_VERSION='1.1.3'

class PeerMixin:
    def _peer_error(self,code,message):
        raise self.error_type(code,message)

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
                if claim.get('skill_version') in ('1.1.0','1.1.1','1.1.2','1.1.3') and claim=={'node_id':node['node_id'],'binding_id':node['binding_id'],'capability':'peer_tasks_v1','skill_version':claim.get('skill_version')}:
                    node['ready']=True
        self.peer_registry_issues=quarantined
        return result

    def enroll_peer(self,task_id):
        """Explicitly requested setup; a normal check never generates a key."""
        from rdos_node import sha,canonical
        task=self.task(task_id)
        if (self.manifest['node_id'] not in task.get('executor_node_ids',[]) or task.get('business_status')!='active'
                or task.get('dispatch_source')=='peer' or task.get('task_type','action')!='action'):
            self._peer_error('enrollment_not_authorized','接入需要当前分配给本节点的有效中央维护任务')
        key=(self.manifest.get('peer') or {}).get('central_public_key')
        if not key:self._peer_error('peer_not_enabled','当前 Skill 未包含固定中央身份，请先更新接入配置')
        request_id='enroll_'+sha([self.manifest['workspace_id'],self.manifest['node_id'],task_id,task['requirements_version']])[:32]
        opid='peer_enrollment_'+request_id
        with self.state.locked():
            operation=self.state.get('operations',opid)
            if operation is None:
                pair=self._peer_crypto({'action':'generate'})
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
            event=operation['desired']
            matches=[r for r in self.transport.records(self.manifest['events_table']) if self.cell(r.get('fields',{}).get('操作ID'))==opid]
            if not matches:
                if operation['state']!='prepared':self._peer_error('write_uncertain','原接入申请可能已提交，只核对原记录，不重新申请')
                operation['state']='sent';self.state.put('operations',opid,operation);self.state.flush()
                fields={'操作ID':opid,'事件ID':event['id'],'类型':event['event_type'],'自报节点':event['claimed_node_id'],
                    '任务引用':canonical(event['task_refs']),'记录数据':canonical(event)}
                try:self.transport.append(fields)
                except ValueError:pass
                matches=[r for r in self.transport.records(self.manifest['events_table']) if self.cell(r.get('fields',{}).get('操作ID'))==opid]
            if not matches:self._peer_error('write_uncertain','接入申请尚未独立读回；保留本机身份，不生成替代密钥')
            for row in matches:
                actual=self.transport.get(self.manifest['events_table'],row['record_id'])
                if json.loads(self.cell(actual['fields']['记录数据']))!=event:self._peer_error('operation_conflict','接入申请原文出现冲突')
            operation['state']='requested';self.state.put('operations',opid,operation)
        fingerprint=sha(operation['public_key'])
        automatic=self.continue_peer_enrollment()
        return {'request_id':request_id,'status':automatic['status'],'readback_verified':True,
            'confirmation_code':'-'.join(fingerprint[index:index+4].upper() for index in (0,4,8)),
            'task_completion_allowed':automatic['status']=='ready','message':('接入确认与自动回执已完成。' if automatic['status']=='ready' else '接入申请已提交，等待接入确认；保持本配置任务进行中，暂不提交完成。确认后会自动回执，无需搬运密钥文件。')}

    def ensure_peer_enrollment_completion(self,task_id):
        pointer=self.state.get('operations','peer_enrollment_current')
        operation=self.state.get('operations',pointer.get('operation_id')) if pointer else None
        if not operation or operation.get('task_id')!=task_id:return
        current=self.task(task_id)
        if operation.get('requirements_version')!=current['requirements_version']:return
        status=self.continue_peer_enrollment()
        if status.get('status')!='ready':
            self._peer_error('enrollment_pending','等待接入确认；当前配置任务保持进行中，确认与自动回执后才提交完成')

    def continue_peer_enrollment(self):
        pointer=self.state.get('operations','peer_enrollment_current')
        if not pointer:return {'status':'not_requested'}
        operation=self.state.get('operations',pointer.get('operation_id'))
        if not operation or operation.get('kind')!='peer_enrollment':
            self._peer_error('invalid_enrollment','本机接入检查点无效')
        try:registry=self._peer_registry()
        except ValueError as exc:
            if getattr(exc,'code','') in ('peer_registry_missing','identity_expired'):
                return {'status':'pending_confirmation','request_id':operation['request_id']}
            raise
        registered=next((entry for entry in registry['nodes'] if entry.get('node_id')==self.manifest['node_id']
            and entry.get('public_key')==operation['public_key']),None)
        if registered is None:return {'status':'pending_confirmation','request_id':operation['request_id']}
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
        fields={'事件ID':event['id'],'操作ID':opid,'类型':event['event_type'],'自报节点':event['claimed_node_id'],'任务引用':'[]','记录数据':canonical(event)}
        with self.state.locked():
            previous=self.state.get('operations',opid)
            if previous and previous['desired']!=event:self._peer_error('operation_conflict','能力回执内容冲突')
            matches=[r for r in self.transport.records(self.manifest['events_table']) if self.cell(r.get('fields',{}).get('操作ID'))==opid]
            if not matches:
                if previous:self._peer_error('write_uncertain','能力回执已经发出；继续核对，不重复提交')
                self.state.put('operations',opid,{'desired':event,'state':'sent'});self.state.flush()
                try:self.transport.append(fields)
                except ValueError:pass
                matches=[r for r in self.transport.records(self.manifest['events_table']) if self.cell(r.get('fields',{}).get('操作ID'))==opid]
            if not matches:self._peer_error('write_uncertain','能力回执尚未独立读回')
            for row in matches:
                actual=self.transport.get(self.manifest['events_table'],row['record_id'])
                if json.loads(self.cell(actual['fields']['记录数据']))!=event:self._peer_error('readback_mismatch','能力回执读回不符')
            self.state.put('operations',opid,{'desired':event,'state':'verified'})
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
