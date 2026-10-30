"""Production-shaped public event round trips, without contacting Feishu."""
import copy
import unittest
from server import peer_records,rdos_contract
from tests.test_phase3_egress import isolated
from tests.test_peer_enrollment import ENROLL_SETUP
from tests.phase1_support import run_guarded
from tests import test_peer_tasks as peer_tests
from tests.test_rdos_skill import task
node,row=peer_tests.node,peer_tests.row


class PeerProtocolReadbackTests(unittest.TestCase):
    def test_only_decoder_top_level_default_is_normalized(self):
        registry={'env_id':'private-env','generation':'private-generation','project_id':'lab_project_phase3_collab',
                  'workspace_id':'ws_rdos','identity_mode':'actual'}
        event={'id':'peer-event','event_type':'node_registry_snapshot','operation_id':'same-operation',
            'env_id':registry['env_id'],'generation':registry['generation'],'project_id':registry['project_id'],
            'task_refs':[],'body':'registered','claimed_node_id':'system:peer_registry',
            'registry_proof':{'payload_json':'{"identity":"same"}','signature':'same-signature'}}
        decoded=rdos_contract.decode(rdos_contract.encode(event,registry),registry)
        self.assertEqual(peer_records.event_readback_payload(event,registry),peer_records.event_readback_payload(decoded,registry))
        for change in ({'body':'changed'},{'identity_mode':'role_test'},{'operation_id':'other-operation'},
                       {'registry_proof':{'payload_json':'changed','signature':'same-signature'}},
                       {'verified_actor':{'id':'person','identity_mode':'role_test'}}):
            self.assertNotEqual(peer_records.event_readback_payload(event,registry),peer_records.event_readback_payload({**decoded,**change},registry))
    def test_actual_adapter_write_and_old_operation_recovery_keep_one_physical_event(self):
        result=isolated('''
backend.resources.update(public_protocol_version=2,workspace_id='ws_rdos',identity_mode='actual',allow_legacy_reads=False)
event={'id':'peer_registry_roundtrip','event_type':'node_registry_snapshot','env_id':cfg['env_id'],
 'generation':'1'*32,'project_id':'lab_project_phase3_collab','task_refs':[],
 'claimed_node_id':'system:peer_registry','body':'节点登记已保存',
 'registry_proof':{'payload_json':'{"scope":"fixed"}','signature':'signature-preserved'}}
with patch.object(fs.subprocess,'run',side_effect=remote):
 first=backend.append_event(event,'peer-registry-original')
 assert first['identity_mode']=='actual'
 again=backend.append_event(event,'peer-registry-original')
 assert first['record_id']==again['record_id']
 assert sum(c[:2]==('base','+record-upsert') for c in calls)==1
 denied(lambda:backend.append_event({**event,'body':'tampered'},'peer-registry-original'))
 assert sum(c[:2]==('base','+record-upsert') for c in calls)==1
''')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
    def test_central_registry_recovery_uses_original_operation_with_actual_decode(self):
        result=run_guarded(ENROLL_SETUP+'''
from unittest.mock import patch
async def scenario():
 original_registry=backend3.registry
 registry={**original_registry(),**service.environment(),'identity_mode':'actual','public_protocol_version':2,'workspace_id':'ws_rdos'}
 original_list=backend3.list_events
 def public_roundtrip():return [rdos_contract.decode(rdos_contract.encode(value,registry),registry) for value in original_list()]
 with patch.object(backend3,'registry',return_value=registry),patch.object(backend3,'list_events',side_effect=public_roundtrip):
  first=await peer.publish_registry('original-registry-operation')
  again=await peer.publish_registry('original-registry-operation')
  assert first==again
 assert len([value for value in original_list() if value.get('event_type')=='node_registry_snapshot'])==1
asyncio.run(scenario())
''',timeout=60)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


class EnrollmentContinuationTests(unittest.TestCase):
    setUp=peer_tests.PeerSkillTest.setUp
    tearDown=peer_tests.PeerSkillTest.tearDown
    def test_requirements_revision_and_closed_completed_task_resume_original_key(self):
        ordinary=task();self.transport.tasks=[row(ordinary,'maintenance_task')]
        self.workflow.state.db.execute("DELETE FROM operations WHERE id='peer_identity'");self.workflow.state.db.commit()
        first=self.workflow.enroll_peer(ordinary['task_id'])
        pointer=self.workflow.state.get('operations','peer_enrollment_current')
        original=self.workflow.state.get('operations',pointer['operation_id'])
        changed=task(requirements_version=2,record_revision=2)
        self.transport.tasks=[row(changed,'maintenance_task')]
        original_crypto=self.workflow._peer_crypto
        def no_new_key(request):
            if request.get('action')=='generate':raise AssertionError('generated second identity')
            return original_crypto(request)
        with unittest.mock.patch.object(self.workflow,'_peer_crypto',side_effect=no_new_key):
            again=self.workflow.enroll_peer(ordinary['task_id'])
        self.assertNotEqual(first['request_id'],again['request_id'])
        revised_pointer=self.workflow.state.get('operations','peer_enrollment_current')
        self.assertEqual(self.workflow.state.get('operations',revised_pointer['operation_id'])['public_key'],original['public_key'])
        self.assertEqual(self.transport.append_count,2)
        closed=task(requirements_version=2,record_revision=3,business_status='closed',close_reason='completed')
        self.transport.tasks=[row(closed,'maintenance_task')]
        again=self.workflow.enroll_peer(ordinary['task_id'])
        self.assertEqual(again['request_id'],self.workflow.state.get('operations',revised_pointer['operation_id'])['request_id']);self.assertEqual(self.transport.append_count,2)
        self.transport.tasks=[row({**closed,'close_reason':'cancelled'},'maintenance_task')]
        with self.assertRaises(node.WorkflowError):self.workflow.enroll_peer(ordinary['task_id'])

class CentralFinishedSetupContinuationTest(unittest.TestCase):
    def test_approved_node_request_survives_its_completed_setup_task(self):
        result=run_guarded(ENROLL_SETUP+'''
async def scenario():
 task=await start('completed-setup',completion_rule={'kind':'receipt_recorded'})
 event,pair=await enrollment(task,'request_completed_setup')
 await service.refresh(admin,'collect-finished-setup-application')
 pending=local.get('peer_enrollment_request','request_completed_setup')
 assert pending['status']=='pending'
 await service.submit_event({'event_type':'submission','task_refs':refs(task),'result_status':'completed',
     'body':'本节点安装、申请及原生定时配置完成'},node,'completed-own-setup')
 await service.refresh(admin,'settle-finished-own-setup')
 closed=backend3.read_task(task['task_id'])
 assert closed['business_status']=='closed' and closed['close_reason']=='completed',closed
 result=await peer.approve_enrollment('node_codex',admin,'approve-finished-setup',pending['request_id'],pending['request_hash'])
 assert result['readback_verified'] and result['status']=='registered',result
 assert local.get('peer_identity','node_lab_phase3_codex')['public_key']==pair['public_key']
 # A newly invented request using the ended task cannot gain authority.
 forged=copy.deepcopy(event);forged['id']='evt_after_closed';forged['operation_id']='request-after-closed'
 claim=json.loads(forged['enrollment_proof']['payload_json']);claim['request_id']='new_request_after_closed'
 forged['enrollment_proof']=peer.signed(claim,pair['private_key'])
 try:service._ingest_event(forged,{task['task_id']:closed})
 except peer.PeerError as exc:assert exc.code=='enrollment_not_authorized'
 else:raise AssertionError('new identity requested through a closed task')
asyncio.run(scenario())
''',timeout=60)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

class CentralRevisedRequestTest(unittest.TestCase):
    def test_revised_task_can_reuse_original_key_with_current_request_reference(self):
        result=run_guarded(ENROLL_SETUP+'''
async def scenario():
 old=await start('revised-enrollment',completion_rule={'kind':'receipt_recorded'})
 event,pair=await enrollment(old,'request_before_revision')
 await service.refresh(admin,'collect-old-enrollment')
 draft=await service.prepare_task({'task_id':old['id'],'goal':'保留原身份，完成新版工作接入','change_reason':'补充日检要求'},admin,'revise-enrollment-draft')
 current=await service.apply_task_action(old['id'],'update',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'revise-enrollment',1,1)
 revised,_=await enrollment(current,'request_current_revision',pair=pair)
 await service.refresh(admin,'collect-current-enrollment')
 pending=local.get('peer_enrollment_request','request_current_revision')
 assert pending['requirements_version']==2 and pending['public_key']==pair['public_key']
 confirmed=await peer.approve_enrollment('node_codex',admin,'approve-revised-enrollment',pending['request_id'],pending['request_hash'])
 assert confirmed['readback_verified']
 assert local.get('peer_identity','node_lab_phase3_codex')['public_key']==pair['public_key']
 assert local.get('peer_enrollment_request','request_before_revision')['status']=='pending'
asyncio.run(scenario())
''',timeout=60)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
