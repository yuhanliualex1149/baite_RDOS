"""Automatic public-key handoff, one authenticated binding, no private-file transfer."""
import copy
from datetime import datetime,timedelta,timezone
import json
import unittest
from unittest.mock import patch
from tests import test_peer_tasks as peer_tests
node,row=peer_tests.node,peer_tests.row
from tests.test_rdos_skill import task
from tests.phase1_support import run_guarded
from tests.test_phase3_core import SETUP as CORE_SETUP


class EnrollmentSkillTest(unittest.TestCase):
    setUp=peer_tests.PeerSkillTest.setUp
    tearDown=peer_tests.PeerSkillTest.tearDown
    def test_explicit_enrollment_then_check_automatically_acknowledges_public_binding(self):
        ordinary=task();self.transport.tasks=[row(ordinary,'maintenance_task')]
        self.workflow.state.db.execute("DELETE FROM operations WHERE id='peer_identity'");self.workflow.state.db.commit()
        before_count=self.transport.append_count
        with patch.object(self.workflow,'_peer_crypto',side_effect=AssertionError('ordinary check created identity')):
            self.assertEqual(self.workflow.check()['peer_enrollment']['status'],'not_requested')
        self.assertEqual(self.transport.append_count,before_count)
        self.transport.lose_response=True
        first=self.workflow.enroll_peer(ordinary['task_id'])
        self.assertEqual(first['status'],'pending_confirmation')
        self.assertFalse(first['task_completion_allowed'])
        with self.assertRaises(node.WorkflowError) as waiting:
            self.workflow.submit(ordinary['task_id'],'completed','已提交公钥申请','premature-completion')
        self.assertEqual(waiting.exception.code,'enrollment_pending')
        self.assertTrue(first['readback_verified'])
        self.assertNotIn('private',json.dumps(first))
        pointer=self.workflow.state.get('operations','peer_enrollment_current')
        operation=self.workflow.state.get('operations',pointer['operation_id'])
        self.assertTrue(operation['private_key'])
        remote=json.dumps(self.transport.events)
        self.assertNotIn(operation['private_key'],remote)
        self.assertEqual(self.transport.append_count,1)
        repeated=self.workflow.enroll_peer(ordinary['task_id'])
        self.assertEqual(repeated['request_id'],first['request_id'])
        self.assertEqual(self.transport.append_count,1)
        self.assertEqual(self.workflow.state.get('operations',pointer['operation_id'])['public_key'],operation['public_key'])
        # Simulate only the transport boundary: central confirms this public key.
        approved=copy.deepcopy(self.registry)
        approved['generated_at']=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()
        approved['nodes'][0].update(public_key=operation['public_key'],binding_id='binding_approved',ready=False)
        registry_event=json.loads(self.transport.events[0]['fields']['记录数据'])
        registry_event['registry_proof']=self.workflow._peer_sign(approved,self.central)
        self.transport.events[0]=row(registry_event,'record_registry','event')
        checked=self.workflow.check()
        self.assertEqual(checked['peer_enrollment']['status'],'ready')
        self.assertEqual(self.transport.append_count,2)
        installed=self.workflow.state.get('operations','peer_identity')
        self.assertEqual(installed['private_key'],operation['private_key'])
        self.assertEqual(installed['binding_id'],'binding_approved')
        ack=[json.loads(item['fields']['记录数据']) for item in self.transport.events if item['fields']['类型']=='peer_capability_ack'][0]
        self.assertEqual(json.loads(ack['peer_ack']['payload_json'])['skill_version'],'1.1.3')
        self.workflow.check();self.assertEqual(self.transport.append_count,2)

    def test_invalid_task_cannot_generate_a_new_key(self):
        self.transport.tasks=[row(task(executor_node_ids=['node_workbuddy']),'other_task')]
        with patch.object(self.workflow,'_peer_crypto',side_effect=AssertionError('unauthorized task generated identity')):
            with self.assertRaises(node.WorkflowError):self.workflow.enroll_peer('task_example')
        self.assertIsNone(self.workflow.state.get('operations','peer_enrollment_current'))


ENROLL_SETUP=CORE_SETUP+'''
import copy
from server import peer_tasks as peer, rdos_contract
peer.initialize()
async def enrollment(task,request_id,pair=None,node_id='node_lab_phase3_codex'):
    pair=pair or peer.crypto({'action':'generate'})
    registry={**backend3.registry(),**service.environment()}
    claim={'version':1,'workspace_id':registry.get('workspace_id','ws_rdos'),'request_id':request_id,
        'node_id':rdos_contract.public_view(node_id,registry),'public_key':pair['public_key'],
        'task_ref':{'task_id':task['task_id'],'requirements_version':task['requirements_version']},'requested_at':'2026-10-24T00:00:00+00:00'}
    event={'id':'evt_'+request_id,'operation_id':'operation_'+request_id,**service.environment(),'project_id':task['project_id'],
        'claimed_node_id':node_id,'event_type':'peer_enrollment_request','task_refs':[claim['task_ref']],
        'body':'申请启用节点工作协作能力。','enrollment_proof':peer.signed(claim,pair['private_key'])}
    backend3.append_event(event,event['operation_id'])
    return event,pair
'''


class EnrollmentCentralTest(unittest.TestCase):
    def check(self,body):
        result=run_guarded(ENROLL_SETUP+'\n'+body,timeout=60)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_competing_requests_stay_pending_and_selected_confirmation_contains_no_private_key(self):
        self.check('''
async def scenario():
 task=await start('enrollment',completion_rule={'kind':'receipt_recorded'})
 project_before=copy.deepcopy(service.projects.get_project(task['project_id']))
 first,first_pair=await enrollment(task,'request_first_claim')
 selected,pair=await enrollment(task,'request_legitimate_claim')
 await service.refresh(admin,'read-enrollment-requests')
 assert not local.items('peer_identity'),'first claimant must never become identity'
 pending=local.get('peer_enrollment_request','request_legitimate_claim')
 assert pending['status']=='pending' and len(peer.node_status(admin)[0]['pending_requests'])==2
 try:await peer.approve_enrollment('node_codex',admin,'approve-wrong-hash','request_legitimate_claim','0'*64)
 except peer.PeerError as exc:assert exc.code=='version_conflict'
 else:raise AssertionError('wrong reviewed hash admitted')
 confirmed=await peer.approve_enrollment('node_codex',admin,'approve-selected-request','request_legitimate_claim',pending['request_hash'])
 assert confirmed['readback_verified'] and confirmed['status']=='registered'
 assert 'private_key' not in json.dumps(confirmed)
 assert 'private_key' not in json.dumps(local.items('peer_identity'))
 identity=local.get('peer_identity','node_lab_phase3_codex')
 assert identity['public_key']==pair['public_key'] and not identity['ready']
 repeated=await peer.approve_enrollment('node_codex',admin,'approve-selected-request','request_legitimate_claim',pending['request_hash'])
 assert repeated==confirmed
 assert service.projects.get_project(task['project_id'])['node_ids']==project_before['node_ids']
 claim={'node_id':'node_codex','binding_id':identity['binding_id'],'capability':'peer_tasks_v1','skill_version':'1.1.1'}
 ack={'id':'evt_auto_ack','operation_id':'auto-ack',**service.environment(),'project_id':local.PROJECT_ID,'claimed_node_id':'node_lab_phase3_codex',
      'event_type':'peer_capability_ack','task_refs':[],'body':'已启用当前工作协作方式。','peer_ack':peer.signed(claim,pair['private_key'])}
 backend3.append_event(ack,'auto-ack')
 await service.refresh(admin,'read-auto-ack')
 assert local.get('peer_identity','node_lab_phase3_codex')['ready'] is True
 assert peer.node_status(admin)[0]['skill_version']=='1.1.1'
asyncio.run(scenario())
''')

    def test_bad_pop_wrong_node_closed_task_and_changed_remote_request_are_not_approved(self):
        self.check('''
async def scenario():
 task=await start('enrollment-bad',completion_rule={'kind':'receipt_recorded'})
 event,pair=await enrollment(task,'request_changed_remote')
 await service.refresh(admin,'collect-request')
 pending=local.get('peer_enrollment_request','request_changed_remote')
 altered=copy.deepcopy(event);altered['body']='Changed after displayed'
 backend3._save('event',altered['id'],altered)
 try:await peer.approve_enrollment('node_codex',admin,'approve-changed-request','request_changed_remote',pending['request_hash'])
 except peer.PeerError as exc:assert exc.code=='version_conflict'
 else:raise AssertionError('changed source approved')
 assert not local.items('peer_identity')
 backend3._save('event',event['id'],event)
 forged=copy.deepcopy(event);forged['id']='evt_forged_request';forged['operation_id']='forged-request'
 forged['enrollment_proof']['signature']='AAAA'
 try:service._ingest_event(forged,{task['task_id']:task})
 except peer.PeerError as exc:assert exc.code in ('invalid_signature','runtime_unavailable')
 else:raise AssertionError('bad PoP admitted')
 await service.apply_task_action(task['task_id'],'cancel',{'reason':'任务已结束'},admin,'cancel-enrollment-task',1,1)
 try:await peer.approve_enrollment('node_codex',admin,'approve-closed-task','request_changed_remote',pending['request_hash'])
 except peer.PeerError as exc:assert exc.code=='enrollment_not_authorized'
 else:raise AssertionError('closed task authorized binding')
 assert not local.items('peer_identity')
asyncio.run(scenario())
''')

    def test_confirmation_lost_registry_readback_recovers_original_operation_once(self):
        self.check('''
from unittest.mock import patch
from server.phase3_backend import BackendError
async def scenario():
 task=await start('enrollment-recovery',completion_rule={'kind':'receipt_recorded'})
 event,pair=await enrollment(task,'request_registry_recovery')
 await service.refresh(admin,'collect-recovery-request')
 pending=local.get('peer_enrollment_request','request_registry_recovery')
 original_append=backend3.append_event
 def persisted_then_lost(value,opid):
  result=original_append(value,opid)
  if value.get('event_type')=='node_registry_snapshot':
   backend3.fault('read_events')
   raise BackendError('write_uncertain','lost after actual commit')
  return result
 with patch.object(backend3,'append_event',side_effect=persisted_then_lost):
  first=await peer.approve_enrollment('node_codex',admin,'confirm-registry-recovery','request_registry_recovery',pending['request_hash'])
 assert first['status']=='registry_pending' and first['readback_verified'] is False,first
 assert not local.get('peer_identity','node_lab_phase3_codex')['ready']
 again=await peer.approve_enrollment('node_codex',admin,'different-op-after-reload','request_registry_recovery',pending['request_hash'])
 assert again['status']=='registered' and again['readback_verified'] is True,again
 assert again['binding_id']==first['binding_id'] and again['resume_operation_id']=='confirm-registry-recovery'
 assert len(local.items('peer_enrollment_operation'))==1
 assert len([entry for entry in backend3.list_events() if entry.get('event_type')=='node_registry_snapshot'])==1
 assert 'private_key' not in json.dumps(local.items('peer_enrollment_operation'))
asyncio.run(scenario())
''')
