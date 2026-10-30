"""Peer event conflicts and saved-enrollment lifecycle, with isolated core services."""
import unittest

from tests.phase1_support import run_guarded
from tests.test_peer_enrollment import ENROLL_SETUP


class PeerLifecycleRecoveryTest(unittest.TestCase):
    def check(self, body):
        result = run_guarded(ENROLL_SETUP + body, timeout=70)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_first_request_cannot_be_saved_after_validated_task_changes(self):
        self.check('''
from unittest.mock import patch
import threading
async def scenario():
 task=await start('first-request-task-race',completion_rule={'kind':'receipt_recorded'})
 event,pair=await enrollment(task,'request_task_changed_before_save')
 original=peer._enrollment_task
 failures=[]
 def validate_then_complete(value,node_id,version,**options):
  result=original(value,node_id,version,**options)
  def complete():
   try:asyncio.run(service.apply_task_action(task['task_id'],'manual_complete',{},admin,'complete-during-request-validation',1,1))
   except BaseException as error:failures.append(error)
  worker=threading.Thread(target=complete)
  worker.start();worker.join(timeout=15)
  assert not worker.is_alive() and not failures,failures
  assert backend3.read_task(task['task_id'])['business_status']=='closed'
  return result
 with patch.object(peer,'_enrollment_task',side_effect=validate_then_complete):
  try:service._ingest_event(event,{task['task_id']:task})
  except peer.PeerError as error:assert error.code=='version_conflict',error.code
  else:raise AssertionError('new pending request was saved from a stale active task')
 assert not local.get('peer_enrollment_request','request_task_changed_before_save')
 assert not local.event_fingerprint(event['id']) and not local.items('peer_identity')
asyncio.run(scenario())
''')

    def test_conflicting_event_id_cannot_leave_a_new_pending_application(self):
        self.check('''
async def scenario():
 task=await start('enrollment-index-conflict',completion_rule={'kind':'receipt_recorded'})
 first,pair=await enrollment(task,'request_first_event_id')
 service._ingest_event(first,{task['task_id']:task})
 incoming,_=await enrollment(task,'request_second_event_id',pair=pair)
 incoming['id']=first['id']
 before=copy.deepcopy(local.items('peer_enrollment_request'))
 indexed=copy.deepcopy(local.events())
 try:service._ingest_event(incoming,{task['task_id']:task})
 except ValueError:pass
 else:raise AssertionError('same event ID admitted different claim')
 assert local.items('peer_enrollment_request')==before,'conflicting event changed pending applications'
 assert local.events()==indexed and not local.items('peer_identity')
asyncio.run(scenario())
''')

    def test_conflicting_ack_event_cannot_enable_a_rotated_binding(self):
        self.check('''
async def scenario():
 task=await start('ack-index-conflict',completion_rule={'kind':'receipt_recorded'})
 first,pair=await enrollment(task,'request_ack_first_identity')
 await service.refresh(admin,'collect-ack-first-identity')
 pending=local.get('peer_enrollment_request','request_ack_first_identity')
 await peer.approve_enrollment('node_codex',admin,'approve-ack-first-identity',pending['request_id'],pending['request_hash'])
 def acknowledgement(identity,key):
  claim={'node_id':'node_codex','binding_id':identity['binding_id'],'capability':'peer_tasks_v1','skill_version':'1.2.0'}
  return {'id':'evt_conflicting_capability','operation_id':'capability-original-operation',**service.environment(),
   'project_id':local.PROJECT_ID,'event_type':'peer_capability_ack','claimed_node_id':'node_lab_phase3_codex',
   'task_refs':[],'body':'已启用工作协作方式。','peer_ack':peer.signed(claim,key['private_key'])}
 old_identity=local.get('peer_identity','node_lab_phase3_codex')
 old_ack=acknowledgement(old_identity,pair)
 service._ingest_event(old_ack,{})
 assert local.get('peer_identity','node_lab_phase3_codex')['ready'] is True
 second,second_pair=await enrollment(task,'request_ack_second_identity')
 await service.refresh(admin,'collect-ack-second-identity')
 pending=local.get('peer_enrollment_request','request_ack_second_identity')
 await peer.approve_enrollment('node_codex',admin,'approve-ack-second-identity',pending['request_id'],pending['request_hash'],rotate=True)
 current=copy.deepcopy(local.get('peer_identity','node_lab_phase3_codex'))
 assert not current['ready'] and current['binding_id']!=old_identity['binding_id']
 new_ack=acknowledgement(current,second_pair)
 before=copy.deepcopy(local.events())
 try:service._ingest_event(new_ack,{})
 except ValueError:pass
 else:raise AssertionError('conflicting ACK event ID admitted')
 assert local.get('peer_identity','node_lab_phase3_codex')==current,'rejected ACK enabled new identity'
 assert local.events()==before
asyncio.run(scenario())
''')

    def test_identical_physical_replays_keep_one_event_and_binding(self):
        self.check('''
async def scenario():
 task=await start('duplicate-registration-events',completion_rule={'kind':'receipt_recorded'})
 event,pair=await enrollment(task,'request_duplicate_physical_rows')
 service._ingest_event({**event,'record_id':'physical-first'},{task['task_id']:task})
 service._ingest_event({**event,'record_id':'physical-second'},{task['task_id']:task})
 pending=local.get('peer_enrollment_request','request_duplicate_physical_rows')
 assert len(local.items('peer_enrollment_request'))==1
 assert len([value for value in local.events() if value['id']==event['id']])==1
 confirmed=await peer.approve_enrollment('node_codex',admin,'approve-duplicate-physical',pending['request_id'],pending['request_hash'])
 identity=copy.deepcopy(local.get('peer_identity','node_lab_phase3_codex'))
 claim={'node_id':'node_codex','binding_id':identity['binding_id'],'capability':'peer_tasks_v1','skill_version':'1.2.0'}
 ack={'id':'evt_duplicate_physical_ack','operation_id':'duplicate-physical-ack',**service.environment(),
  'project_id':local.PROJECT_ID,'event_type':'peer_capability_ack','claimed_node_id':'node_lab_phase3_codex',
  'task_refs':[],'body':'当前工作协作已启用。','peer_ack':peer.signed(claim,pair['private_key'])}
 service._ingest_event({**ack,'record_id':'physical-ack-first'},{})
 service._ingest_event({**ack,'record_id':'physical-ack-second'},{})
 assert len([value for value in local.events() if value['id']==ack['id']])==1
 assert local.get('peer_identity','node_lab_phase3_codex')['binding_id']==identity['binding_id']
 assert local.get('peer_identity','node_lab_phase3_codex')['ready'] is True
 assert len([value for value in backend3.list_events() if value['event_type']=='node_registry_snapshot'])==1
 assert confirmed['readback_verified']
asyncio.run(scenario())
''')

    def test_only_a_saved_active_request_can_continue_after_completed_task(self):
        self.check('''
async def scenario():
 task=await start('saved-before-close',completion_rule={'kind':'receipt_recorded'})
 saved,pair=await enrollment(task,'request_saved_before_completion')
 service._ingest_event(saved,{task['task_id']:task})
 pending=local.get('peer_enrollment_request','request_saved_before_completion')
 unseen,_=await enrollment(task,'request_unseen_before_completion',pair=pair)
 await service.apply_task_action(task['task_id'],'manual_complete',{},admin,'finish-own-setup',1,1)
 closed=backend3.read_task(task['task_id'])
 assert closed['business_status']=='closed' and closed['close_reason']=='manual_completed'
 try:service._ingest_event(unseen,{task['task_id']:closed})
 except peer.PeerError as error:assert error.code=='enrollment_not_authorized'
 else:raise AssertionError('unseen request borrowed a completed task')
 assert not local.get('peer_enrollment_request','request_unseen_before_completion')
 result=await peer.approve_enrollment('node_codex',admin,'approve-saved-after-close',pending['request_id'],pending['request_hash'])
 assert result['readback_verified'] and local.get('peer_identity','node_lab_phase3_codex')['public_key']==pair['public_key']
 assert not local.get('peer_identity','node_lab_phase3_codex')['ready']
asyncio.run(scenario())
''')

    def test_sibling_nodes_do_not_share_application_or_capability_state(self):
        self.check('''
async def scenario():
 first=await start('first-workstation',completion_rule={'kind':'receipt_recorded'})
 second=await start('second-workstation',executor_node_ids=[other['node_id']],completion_rule={'kind':'receipt_recorded'})
 first_event,first_pair=await enrollment(first,'request_workstation_one')
 second_event,second_pair=await enrollment(second,'request_workstation_two',node_id=other['node_id'])
 await service.refresh(admin,'collect-two-workstations')
 pending=local.get('peer_enrollment_request','request_workstation_one')
 try:await peer.approve_enrollment('node_workbuddy',admin,'wrong-workstation-approval',pending['request_id'],pending['request_hash'])
 except peer.PeerError as error:assert error.code=='version_conflict'
 else:raise AssertionError('first workstation request bound sibling node')
 confirmed=await peer.approve_enrollment('node_codex',admin,'approve-first-workstation',pending['request_id'],pending['request_hash'])
 status={value['node_id']:value for value in peer.node_status(admin)}
 assert status['node_codex']['bound'] and not status['node_codex']['ready']
 assert not status['node_workbuddy']['bound'] and not status['node_workbuddy']['ready']
 assert len(status['node_workbuddy']['pending_requests'])==1
 assert confirmed['readback_verified'] and not local.get('peer_identity',other['node_id'])
asyncio.run(scenario())
''')

    def test_transaction_failures_roll_back_event_and_business_updates_together(self):
        self.check('''
from unittest.mock import patch
async def scenario():
 task=await start('atomic-enrollment-and-ack',completion_rule={'kind':'receipt_recorded'})
 event,pair=await enrollment(task,'request_atomic_event_state')
 original_put=peer._transaction_put
 def save_then_fail(conn,kind,identifier,value):
  original_put(conn,kind,identifier,value)
  raise RuntimeError('synthetic interruption within transaction')
 before=copy.deepcopy(local.events())
 with patch.object(peer,'_transaction_put',side_effect=save_then_fail):
  try:service._ingest_event(event,{task['task_id']:task})
  except RuntimeError:pass
  else:raise AssertionError('injected persistence interruption did not occur')
 assert not local.get('peer_enrollment_request','request_atomic_event_state')
 assert local.events()==before
 service._ingest_event(event,{task['task_id']:task})
 pending=local.get('peer_enrollment_request','request_atomic_event_state')
 await peer.approve_enrollment('node_codex',admin,'approve-atomic-event-state',pending['request_id'],pending['request_hash'])
 identity=copy.deepcopy(local.get('peer_identity','node_lab_phase3_codex'))
 claim={'node_id':'node_codex','binding_id':identity['binding_id'],'capability':'peer_tasks_v1','skill_version':'1.2.0'}
 ack={'id':'evt_atomic_capability','operation_id':'atomic-capability-operation',**service.environment(),
  'project_id':local.PROJECT_ID,'event_type':'peer_capability_ack','claimed_node_id':'node_lab_phase3_codex',
  'task_refs':[],'body':'当前协作方式已启用。','peer_ack':peer.signed(claim,pair['private_key'])}
 before=copy.deepcopy(local.events())
 with patch.object(peer,'_transaction_put',side_effect=save_then_fail):
  try:service._ingest_event(ack,{})
  except RuntimeError:pass
  else:raise AssertionError('ACK persistence interruption did not occur')
 assert local.get('peer_identity','node_lab_phase3_codex')==identity
 assert local.events()==before and not local.event_fingerprint(ack['id'])
 service._ingest_event(ack,{})
 assert local.get('peer_identity','node_lab_phase3_codex')['ready']
asyncio.run(scenario())
''')

    def test_binding_is_rechecked_after_signature_verification(self):
        self.check('''
from unittest.mock import patch
async def scenario():
 task=await start('binding-changed-after-verification',completion_rule={'kind':'receipt_recorded'})
 event,pair=await enrollment(task,'request_binding_race')
 service._ingest_event(event,{task['task_id']:task})
 pending=local.get('peer_enrollment_request','request_binding_race')
 await peer.approve_enrollment('node_codex',admin,'approve-binding-race',pending['request_id'],pending['request_hash'])
 identity=copy.deepcopy(local.get('peer_identity','node_lab_phase3_codex'))
 claim={'node_id':'node_codex','binding_id':identity['binding_id'],'capability':'peer_tasks_v1','skill_version':'1.2.0'}
 ack={'id':'evt_binding_race','operation_id':'binding-race-ack',**service.environment(),
  'project_id':local.PROJECT_ID,'event_type':'peer_capability_ack','claimed_node_id':'node_lab_phase3_codex',
  'task_refs':[],'body':'已启用。','peer_ack':peer.signed(claim,pair['private_key'])}
 next_identity={**identity,'binding_id':'subsequently-rotated-binding','ready':False}
 original_verify=peer.verified
 def verified_then_rotate(proof,key):
  value=original_verify(proof,key)
  local.put('peer_identity','node_lab_phase3_codex',next_identity)
  return value
 with patch.object(peer,'verified',side_effect=verified_then_rotate):
  try:service._ingest_event(ack,{})
  except peer.PeerError as error:assert error.code=='identity_expired'
  else:raise AssertionError('old proof changed the subsequently rotated identity')
 assert local.get('peer_identity','node_lab_phase3_codex')==next_identity
 assert not local.event_fingerprint(ack['id'])
asyncio.run(scenario())
''')
