"""Direct dispatch uses actual Ed25519 and existing task/event services."""
import copy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from tests.phase1_support import run_guarded
from tests.test_rdos_skill import node,task,manifest,row,Transport


class PeerTransport(Transport):
    def __init__(self):
        super().__init__(task());self.tasks=[];self.task_writes=0;self.peer_folders=set();self.lost=False
    def records(self,table):
        return copy.deepcopy(self.tasks if table=='tbl_tasks' else self.events)
    def get(self,table,rid):
        return copy.deepcopy(next(v for v in (self.tasks if table=='tbl_tasks' else self.events) if v['record_id']==rid))
    def append_task(self,fields):
        self.task_writes+=1;self.tasks.append({'record_id':'peer_task_'+str(self.task_writes),'fields':copy.deepcopy(fields)})
        if self.lost:raise node.WorkflowError('connection_failed','lost after commit')


class PeerSkillTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.transport=PeerTransport()
        self.workflow=node.Workflow(manifest(),self.transport,node.State(Path(self.tmp.name)/'sender.sqlite3'))
        self.central=self.workflow._peer_crypto({'action':'generate'})
        self.sender=self.workflow._peer_crypto({'action':'generate'})
        self.receiver=self.workflow._peer_crypto({'action':'generate'})
        self.workflow.manifest['peer']={'version':1,'central_public_key':self.central['public_key']}
        self.registry={'version':1,'workspace_id':'ws_rdos','generated_at':datetime.now(timezone.utc).isoformat(),
            'expires_at':(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),
            'nodes':[{'node_id':'node_codex','binding_id':'binding_sender','public_key':self.sender['public_key'],'enabled':True,'ready':True},
                {'node_id':'node_workbuddy','binding_id':'binding_receiver','public_key':self.receiver['public_key'],'enabled':True,'ready':True}],
            'projects':[{'project_id':'project_rdos','node_ids':['node_codex'],'status':'active','owner_id':'owner_rdos',
                'zones':{'project':{'folder_id':'folder_project'}}}]}
        proof=self.workflow._peer_sign(self.registry,self.central)
        event={'id':'event_registry','operation_id':'op_registry','event_type':'node_registry_snapshot','task_refs':[],
            'claimed_node_id':'system_peer','protocol_version':2,'project_id':'project_rdos','workspace_id':'ws_rdos','registry_proof':proof,'body':'登记'}
        self.transport.events=[row(event,'record_registry','event')]
        with self.workflow.state.locked():self.workflow.state.put('operations','peer_identity',{'node_id':'node_codex','binding_id':'binding_sender','private_key':self.sender['private_key']})
        self.request={'project_id':'project_rdos','recipient_node_id':'node_workbuddy','title':'梳理资料','goal':'核对指定资料','deliverable':'一段清楚的结论','input_refs':[]}
    def tearDown(self):
        self.workflow.state.db.close();self.tmp.cleanup()
    def test_direct_publish_lost_response_readback_and_repeat_only_once(self):
        self.transport.lost=True
        result=self.workflow.dispatch_peer(self.request,'dispatch-one')
        self.assertTrue(result['readback_verified']);self.assertEqual(self.transport.task_writes,1)
        self.assertEqual(self.workflow.dispatch_peer(self.request,'dispatch-one'),result)
        self.assertEqual(self.transport.task_writes,1)
        with self.assertRaises(node.WorkflowError):self.workflow.dispatch_peer({**self.request,'goal':'变更'},'dispatch-one')
        self.assertEqual(self.transport.task_writes,1)
    def test_receiver_can_prepare_without_central_sync_but_no_other_project_data(self):
        result=self.workflow.dispatch_peer(self.request,'dispatch-one')
        other_manifest=manifest();other_manifest['node_id']='node_workbuddy';other_manifest['projects']=[]
        other_manifest['peer']=self.workflow.manifest['peer']
        receiver=node.Workflow(other_manifest,self.transport,node.State(Path(self.tmp.name)/'receiver.sqlite3'))
        try:
            ready=receiver.prepare(result['task_id'])
            self.assertEqual(ready['plan']['reason'],'ready')
            self.assertTrue(receiver.scope(ready['task'])['task_only'])
            with self.assertRaises(node.WorkflowError):receiver.project('project_rdos')
            actual=json.loads(self.transport.tasks[0]['fields']['记录数据']);actual['goal']='篡改'
            self.transport.tasks[0]=row(actual,'peer_task_1')
            with self.assertRaises(node.WorkflowError):receiver.prepare(result['task_id'])
        finally:receiver.state.db.close()
    def test_no_ready_receiver_no_write_and_forged_registry_rejected(self):
        self.registry['nodes'][1]['ready']=False
        event=json.loads(self.transport.events[0]['fields']['记录数据']);event['registry_proof']=self.workflow._peer_sign(self.registry,self.central)
        self.transport.events[0]=row(event,'record_registry','event')
        with self.assertRaises(ValueError):self.workflow.dispatch_peer(self.request,'not-ready')
        self.assertEqual(self.transport.task_writes,0)
        rogue=self.workflow._peer_crypto({'action':'generate'})
        event['registry_proof']=self.workflow._peer_sign(self.registry,rogue);self.transport.events[0]=row(event,'record_registry','event')
        with self.assertRaises(node.WorkflowError):self.workflow.dispatch_peer(self.request,'forged')
        # A forged row does not suppress a still valid signed registration.
        self.registry['nodes'][1]['ready']=True
        valid=copy.deepcopy(event);valid['id']='event_good_registry';valid['operation_id']='op_good_registry'
        valid['registry_proof']=self.workflow._peer_sign(self.registry,self.central)
        self.transport.events.append(row(valid,'good_registry','event'))
        self.assertTrue(self.workflow.dispatch_peer(self.request,'valid-alongside-forgery')['readback_verified'])
        self.assertEqual(self.workflow.peer_registry_issues,1)
    def test_valid_signature_does_not_authorize_bad_business_fields(self):
        result=self.workflow.dispatch_peer(self.request,'dispatch-one')
        valid=json.loads(self.transport.tasks[0]['fields']['记录数据'])
        mutations=[{'requirements_hash':'fake'},{'executor_node_ids':'node_workbuddy'},{'dispatch_to':'agent'},
            {'input_refs':[{'document_id':'outside','revision':1,'content_hash':'known'}]},
            {'quality_status':'accepted'},{'goal':''},{'business_status':'closed'},{'editable_document_ids':['other_doc']}]
        for changes in mutations:
            value={k:v for k,v in valid.items() if k!='peer_proof'};value.update(changes)
            if 'requirements_hash' not in changes:value['requirements_hash']=node.rules.digest(node.rules.requirements(value))
            value['peer_proof']=self.workflow._peer_sign(value,self.sender)
            self.transport.tasks=[row(value,'peer_task_1')]
            with self.assertRaises(ValueError,msg=str(changes)):self.workflow.task(result['task_id'])

    def test_old_creation_duplicate_does_not_block_current_central_revision(self):
        result=self.workflow.dispatch_peer(self.request,'dispatch-version')
        original=json.loads(self.transport.tasks[0]['fields']['记录数据'])
        current={k:v for k,v in original.items() if k!='peer_proof'}
        current.update(record_revision=2,business_status='paused',operation_id='central-pause')
        current['peer_proof']=self.workflow._peer_sign(current,self.central)
        self.transport.tasks.append(row(current,'canonical_current'))
        self.assertEqual(self.workflow.task(result['task_id'])['record_revision'],2)
        repeated=self.workflow.dispatch_peer(self.request,'dispatch-version')
        self.assertTrue(repeated['already_published']);self.assertEqual(repeated['business_status'],'paused')
        self.assertEqual(self.transport.task_writes,1)

    def test_old_manifest_cannot_receive_peer_task(self):
        result=self.workflow.dispatch_peer(self.request,'dispatch-one')
        old=node.Workflow(manifest(),self.transport,node.State(Path(self.tmp.name)/'old.sqlite3'))
        try:
            checked=old.check();self.assertFalse(checked['tasks']);self.assertEqual(checked['state'],'attention_required')
        finally:old.state.db.close()


SETUP='''
from server import phase3_service as service, phase3_store as local, peer_tasks as peer, rdos_contract
from server import phase1_runtime, central_store as central
phase1_runtime.initialize();service.initialize();peer.initialize()
async def bind(nodeid,op):
    delivery=await peer.issue_identity(nodeid,admin,op)
    identity=delivery['identity'];internal=rdos_contract.internal_input(identity['node_id'])
    value=local.get('peer_identity',internal);value['ready']=True;local.put('peer_identity',internal,value)
    return identity
'''

class PeerCentralTest(unittest.TestCase):
    def check(self,body,restart=None):
        result=run_guarded(SETUP+'\n'+body,restart_body=None if restart is None else SETUP+'\n'+restart,timeout=60)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
    def test_binding_private_once_rotation_and_signed_registry(self):
        self.check('''
async def scenario():
    delivery=await peer.issue_identity('node_codex',admin,'binding-first')
    assert 'private_key' in delivery['identity']
    saved=local.get('peer_identity','node_lab_phase3_codex')
    assert 'private_key' not in saved and not saved['ready']
    try:await peer.issue_identity('node_codex',admin,'binding-first')
    except peer.PeerError as exc:assert exc.code=='identity_already_issued'
    else:raise AssertionError('private key was reissued')
    event=next(e for e in service.backends.get_backend().list_events() if e['id']==delivery['registry_event_id'])
    payload=peer.verified(event['registry_proof'],peer.public_config()['central_public_key'])
    assert payload['nodes'][0]['binding_id']==saved['binding_id']
    rotated=await peer.issue_identity('node_codex',admin,'binding-rotate',True)
    assert rotated['identity']['binding_id']!=saved['binding_id']
asyncio.run(scenario())
''')
    def test_exact_crypto_permit_does_not_allow_arbitrary_process(self):
        self.check('''
import subprocess
pair=peer.crypto({'action':'generate'})
proof=peer.signed({'body':'可靠正文'},pair['private_key'])
assert peer.verified(proof,pair['public_key'])=={'body':'可靠正文'}
try:subprocess.run(['/bin/echo','not allowed'])
except PermissionError:pass
else:raise AssertionError('unregistered process escaped')
''')

    def test_peer_task_adopt_close_and_central_update_signed(self):
        self.check('''
from server import rdos_rules as rules
async def scenario():
    sender=await bind('node_codex','peer-bind-sender')
    receiver=await bind('node_workbuddy','peer-bind-receiver')
    registry=peer._registry_payload();project=registry['projects'][0]
    # Receiver is not a participant; task-only grant must still work.
    saved=central.get('project',local.PROJECT_ID);saved['node_ids']=['node_lab_phase3_codex'];central.put('project',local.PROJECT_ID,saved)
    registry=peer._registry_payload();project=registry['projects'][0]
    wire={'id':'task_peer_example','task_id':'task_peer_example','protocol_version':2,'workspace_id':registry['workspace_id'],
        'project_id':project['project_id'],'owner_id':project['owner_id'],'executor_node_ids':['node_workbuddy'],
        'title':'核对资料','goal':'形成清楚结论','deliverable':'一段结论','input_refs':[],'editable_document_ids':[],
        'output_zone':'project','dispatch_to':['agent'],'return_to':['agent'],'completion_rule':{'kind':'receipt_recorded'},
        'task_type':'action','deliverable_audience':['researcher'],'due_at':'','business_status':'active',
        'requirements_version':1,'record_revision':1,'operation_id':'peer-create-example','dispatch_source':'peer',
        'peer_issuer_node_id':'node_codex','peer_binding_id':sender['binding_id'],
        'peer_task_scope':{'input_refs':[],'output_zone':'project','output_folder_id':project['zones']['project']['folder_id']}}
    wire['requirements_hash']=rules.digest(rules.requirements(wire));wire['peer_proof']=peer.signed(wire,sender['private_key'])
    registry_config={**service.backends.get_backend().registry(),**service.environment(),'workspace_id':registry['workspace_id']}
    internal=rdos_contract.decode(wire,registry_config)
    backend3=service.backends.get_backend();backend3.write_task(internal,wire['operation_id'])
    view=await service.refresh(admin,'peer-ingest')
    current=view['tasks'][0]
    assert current['task_id']=='task_peer_example' and not current.get('integrity_error'),current
    assert local.get('peer_canonical',current['task_id'])['record_id']=='mock_task_peer_example'
    paused=await service.apply_task_action(current['task_id'],'pause',{},admin,'peer-central-pause',1,1)
    assert peer.validate_task(backend3.read_task(current['task_id'])) and paused['business_status']=='paused'
    resumed=await service.apply_task_action(current['task_id'],'resume',{},admin,'peer-central-resume',2,1)
    draft=await service.prepare_task({'task_id':current['task_id'],'goal':'核对后形成简短结论','change_reason':'调整交付要求'},admin,'peer-central-update-draft')
    edited=await service.edit_draft(draft['id'],{'goal':'核对后保留一段结论'},admin,draft['draft_hash'])
    current=await service.apply_task_action(current['task_id'],'update',{'draft_id':edited['id'],'expected_hash':edited['draft_hash']},admin,'peer-central-update',3,1)
    assert current['requirements_version']==2 and peer.validate_task(backend3.read_task(current['task_id']))
    other=service.make_node_actor('node_lab_phase3_workbuddy')
    assert other['node_id'] not in service._node_ids(local.PROJECT_ID)
    forged={**service.environment(),'project_id':local.PROJECT_ID,'id':'forged_peer_receipt','operation_id':'forged_peer_receipt_op','event_type':'submission','body':'冒名完成','claimed_node_id':other['node_id'],'task_refs':[{'task_id':current['task_id'],'requirements_version':2}],'result_status':'completed','artifact_refs':[]}
    try:service._ingest_event(forged,{current['task_id']:current})
    except peer.PeerError as exc:assert exc.code=='invalid_signature'
    else:raise AssertionError('unsigned completion was accepted')
    real_event={**forged,'id':'real_peer_receipt','operation_id':'real_peer_receipt_op','body':'已给出清楚结论'}
    real_event['peer_result_proof']=peer.signed(rdos_contract.encode(real_event,registry_config),receiver['private_key'])
    backend3.append_event(real_event,real_event['operation_id'])
    result=await service.refresh(admin,'peer-close')
    final=result['tasks'][0]
    assert final['business_status']=='closed',final
    assert final['record_revision']==5 and peer.validate_task(backend3.read_task(final['task_id']))
    assert final.get('decision') is None and 'quality_status' not in final
asyncio.run(scenario())
''')

    def test_read_only_config_does_not_create_key_and_failed_delivery_requires_rotation(self):
        self.check('''
from pathlib import Path
import os
async def scenario():
    path=Path(os.environ['RDOS_LAB_RUNTIME'])/'peer_identity.json'
    original=path.read_text();path.unlink()
    assert peer.public_config()['available'] is False and not path.exists()
    peer.initialize()
    backend3=service.backends.get_backend();backend3.fault('read_events')
    try:await peer.issue_identity('node_codex',admin,'peer-failed-delivery')
    except peer.PeerError as exc:assert exc.code=='identity_not_delivered'
    else:raise AssertionError('failure must remain visible')
    saved=local.get('peer_identity','node_lab_phase3_codex')
    assert saved['delivery_status']=='not_delivered' and 'private_key' not in saved
    assert peer.node_status(admin)[0]['bound'] is False
    try:await peer.issue_identity('node_codex',admin,'peer-failed-delivery')
    except peer.PeerError as exc:assert exc.code=='identity_already_issued'
    else:raise AssertionError('private key was reissued')
    repaired=await peer.issue_identity('node_codex',admin,'peer-explicit-rotation',True)
    assert repaired['identity']['binding_id']!=saved['binding_id']
    assert local.get('peer_identity','node_lab_phase3_codex')['delivery_status']=='delivered'
asyncio.run(scenario())
''')

    def test_missing_optional_node_runtime_disables_only_peer_and_can_recover(self):
        self.check('''
from unittest.mock import patch
import os
from pathlib import Path
async def scenario():
    path=Path(os.environ['RDOS_LAB_RUNTIME'])/'peer_identity.json'
    original=path.read_bytes()
    with patch.object(peer,'BUNDLED_NODE',str(root/'no-optional-node')),patch.dict(os.environ,{'PATH':''}):
        state=peer.initialize()
    assert state['enabled'] is False and state['error_code']=='runtime_unavailable',state
    assert path.read_bytes()==original
    assert peer.public_config()['available'] is False
    assert peer.public_config()['central_public_key']
    assert all(not value['ready'] and value['runtime_available'] is False for value in peer.node_status(admin))
    assert all('private_key' not in value for value in local.items('state'))
    try:await peer.issue_identity('node_codex',admin,'unavailable-identity')
    except peer.PeerError as exc:assert exc.code=='runtime_unavailable'
    else:raise AssertionError('disabled feature issued identity')
    assert not local.items('peer_identity')
    # Existing ordinary business service still works with the real guard.
    fields={'title':'正常项目任务','goal':'核对资料','deliverable':'清楚结论','executor_node_ids':['node_lab_phase3_codex']}
    draft=await service.prepare_task(fields,admin,'ordinary-draft-node-unavailable')
    task=await service.apply_task_action(draft['task_id'],'start',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'ordinary-start-node-unavailable',0,0)
    assert task['business_status']=='active'
    assert peer.initialize()['enabled'] is True
    assert peer.public_config()['available'] is True
asyncio.run(scenario())
''')

    def test_existing_key_is_probed_helper_failure_is_visible_without_disabling_guard(self):
        self.check('''
from unittest.mock import patch
import subprocess
from pathlib import Path
import os
path=Path(os.environ['RDOS_LAB_RUNTIME'])/'peer_identity.json'
original=path.read_bytes()
real_run=peer.subprocess.run
calls=[]
def unavailable_helper(command,**options):
    assert str(peer.HELPER.resolve())==command[1]
    assert options['cwd']==str(peer._runtime()) and options['env'].get('PATH')=='/usr/bin:/bin'
    calls.append(json.loads(options['input'])['action'])
    return subprocess.CompletedProcess(command,1,stdout='{"error":"cryptography_failed"}',stderr='')
with patch.object(peer.subprocess,'run',side_effect=unavailable_helper):
    state=peer.initialize()
assert calls==['sign'] and state['enabled'] is False and state['error_code']=='crypto_probe_failed',state
assert path.read_bytes()==original and peer.public_config()['available'] is False
assert local.get('state','peer_runtime')['status']=='unavailable'
assert 'private_key' not in json.dumps(local.get('state','peer_runtime'))
try:subprocess.run(['/bin/echo','guard remains active'])
except PermissionError:pass
else:raise AssertionError('runtime failure disabled process guard')
assert peer.initialize()['enabled'] is True
# Unrelated identity safety errors still fail rather than masquerade as success.
path.chmod(0o644)
try:peer.initialize()
except peer.PeerError as exc:assert exc.code=='unsafe_identity'
else:raise AssertionError('unsafe identity file was silently swallowed')
path.chmod(0o600)
''')
