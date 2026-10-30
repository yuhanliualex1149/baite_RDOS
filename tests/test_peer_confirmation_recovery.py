"""Resume a saved approval without treating live card flags as new authority."""
import unittest

from tests.phase1_support import run_guarded
from tests.test_peer_enrollment import ENROLL_SETUP


SETUP = ENROLL_SETUP + '''
from unittest.mock import patch
from server.phase3_backend import BackendError

async def pending_confirmation(*, rotate=False):
 task=await start('confirmation-recovery',completion_rule={'kind':'receipt_recorded'})
 event,pair=await enrollment(task,'request_confirmation_recovery')
 await service.refresh(admin,'read-confirmation-application')
 pending=local.get('peer_enrollment_request','request_confirmation_recovery')
 if rotate:
  previous=peer.crypto({'action':'generate'})
  local.put('peer_identity','node_lab_phase3_codex',{'node_id':'node_lab_phase3_codex',
   'binding_id':'prior-binding','public_key':previous['public_key'],
   'delivery_status':'registered','ready':True})
 original_append=backend3.append_event
 def commit_then_lose_readback(value,opid):
  result=original_append(value,opid)
  if value.get('event_type')=='node_registry_snapshot':
   backend3.fault('read_events')
   raise BackendError('write_uncertain','synthetic response lost after actual commit')
  return result
 with patch.object(backend3,'append_event',side_effect=commit_then_lose_readback):
  first=await peer.approve_enrollment('node_codex',admin,'original-confirmation',
   pending['request_id'],pending['request_hash'],rotate=rotate)
 assert first['status']=='registry_pending' and not first['readback_verified']
 return task,pending,first

def another_admin():
 return central.create_user('recovery-admin','另一管理员',['admin'],
  'isolated-recovery-test-password',actor_id=admin['id'])['user']

async def rejected(code,call):
 try:await call
 except peer.PeerError as exc:assert exc.code==code,(exc.code,str(exc))
 else:raise AssertionError('expected '+code)
'''


class PeerConfirmationRecoveryTest(unittest.TestCase):
    def check(self, body):
        result = run_guarded(SETUP + body, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_pending_rotation_flag_change_reuses_binding_and_remote_record(self):
        self.check('''
async def scenario():
 task,pending,first=await pending_confirmation(rotate=True)
 assert not next(n for n in peer.node_status(admin) if n['node_id']=='node_codex')['bound']
 identity=copy.deepcopy(local.get('peer_identity','node_lab_phase3_codex'))
 events=copy.deepcopy(backend3.list_events())
 original=copy.deepcopy(local.get('peer_enrollment_operation','original-confirmation'))
 result=await peer.approve_enrollment('node_codex',admin,'original-confirmation',
  pending['request_id'],pending['request_hash'],rotate=False)
 assert result['readback_verified'] and result['binding_id']==first['binding_id']
 saved=local.get('peer_enrollment_operation','original-confirmation')
 assert saved['request']==original['request'] and saved['request']['rotate'] is True
 assert len(local.items('peer_enrollment_operation'))==1 and backend3.list_events()==events
 current=local.get('peer_identity','node_lab_phase3_codex')
 for key in ('node_id','binding_id','public_key','issued_by','issued_at'):
  assert current[key]==identity[key]
 assert current['delivery_status']=='registered' and not current['ready']
 assert len([e for e in events if e['event_type']=='node_registry_snapshot'])==1
asyncio.run(scenario())
''')

    def test_another_authorized_admin_recovers_original_attribution(self):
        self.check('''
async def scenario():
 task,pending,first=await pending_confirmation()
 operator=another_admin()
 original=copy.deepcopy(local.get('peer_enrollment_operation','original-confirmation'))
 events=copy.deepcopy(backend3.list_events())
 result=await peer.approve_enrollment('node_codex',operator,'original-confirmation',
  pending['request_id'],pending['request_hash'],rotate=True)
 assert result['readback_verified'] and result['binding_id']==first['binding_id']
 assert local.get('peer_enrollment_operation','original-confirmation')['request']==original['request']
 assert local.get('peer_identity','node_lab_phase3_codex')['issued_by']==admin['id']
 assert backend3.list_events()==events
asyncio.run(scenario())
''')

    def test_unprivileged_or_disabled_caller_cannot_resume_or_replay(self):
        self.check('''
async def scenario():
 task,pending,first=await pending_confirmation()
 events=copy.deepcopy(backend3.list_events())
 stranger=central.user('mem_lab_workbuddy_0001')
 await rejected('authorization_failed',peer.approve_enrollment('node_codex',stranger,
  'original-confirmation',pending['request_id'],pending['request_hash']))
 assert backend3.list_events()==events
 operator=another_admin()
 result=await peer.approve_enrollment('node_codex',operator,'original-confirmation',
  pending['request_id'],pending['request_hash'])
 central.update_user(admin['id'],{'enabled':False},actor_id=operator['id'])
 await rejected('authorization_failed',peer.approve_enrollment('node_codex',admin,
  'original-confirmation',pending['request_id'],pending['request_hash']))
 assert result['readback_verified'] and backend3.list_events()==events
 assert local.get('peer_enrollment_operation','original-confirmation')['request']['actor_id']==admin['id']
asyncio.run(scenario())
''')

    def test_other_application_node_and_hash_cannot_borrow_confirmation_operation(self):
        self.check('''
async def scenario():
 task,pending,first=await pending_confirmation()
 competing,pair=await enrollment(task,'request_competing_claim')
 second=await start('other-node',executor_node_ids=[other['node_id']],
  completion_rule={'kind':'receipt_recorded'})
 other_event,other_pair=await enrollment(second,'request_other_node',node_id=other['node_id'])
 await service.refresh(admin,'collect-other-requests')
 foreign=local.get('peer_enrollment_request','request_other_node')
 await peer.approve_enrollment('node_workbuddy',admin,'other-confirmation',
  foreign['request_id'],foreign['request_hash'])
 before=copy.deepcopy(local.items('peer_identity'))
 operations=copy.deepcopy(local.items('peer_enrollment_operation'))
 events=copy.deepcopy(backend3.list_events())
 same_node=local.get('peer_enrollment_request','request_competing_claim')
 await rejected('operation_conflict',peer.approve_enrollment('node_codex',admin,
  'original-confirmation',same_node['request_id'],same_node['request_hash']))
 await rejected('operation_conflict',peer.approve_enrollment('node_codex',admin,
  'other-confirmation',pending['request_id'],pending['request_hash']))
 await rejected('operation_conflict',peer.approve_enrollment('node_workbuddy',admin,
  'original-confirmation',foreign['request_id'],foreign['request_hash']))
 wrong_hash='0'*64 if pending['request_hash']!='0'*64 else '1'*64
 await rejected('version_conflict',peer.approve_enrollment('node_codex',admin,
  'original-confirmation',pending['request_id'],wrong_hash))
 assert local.items('peer_identity')==before
 assert local.items('peer_enrollment_operation')==operations
 assert backend3.list_events()==events
asyncio.run(scenario())
''')

    def test_verified_operation_replay_ignores_card_flags_and_keeps_original_operation(self):
        self.check('''
async def scenario():
 task,pending,first=await pending_confirmation()
 result=await peer.approve_enrollment('node_codex',admin,'original-confirmation',
  pending['request_id'],pending['request_hash'])
 assert result['readback_verified']
 identity=copy.deepcopy(local.get('peer_identity','node_lab_phase3_codex'))
 operation=copy.deepcopy(local.get('peer_enrollment_operation','original-confirmation'))
 events=copy.deepcopy(backend3.list_events())
 operator=another_admin()
 for opid in ('original-confirmation','fresh-ui-operation'):
  again=await peer.approve_enrollment('node_codex',operator,opid,pending['request_id'],
   pending['request_hash'],rotate=True)
  assert again==result
 assert local.get('peer_identity','node_lab_phase3_codex')==identity
 assert local.get('peer_enrollment_operation','original-confirmation')==operation
 assert len(local.items('peer_enrollment_operation'))==1 and backend3.list_events()==events
asyncio.run(scenario())
''')

    def test_old_verified_confirmation_cannot_claim_registration_after_identity_rotation(self):
        self.check('''
async def scenario():
 task=await start('identity-rotation',completion_rule={'kind':'receipt_recorded'})
 first_event,first_pair=await enrollment(task,'request_before_rotation')
 await service.refresh(admin,'collect-first-identity')
 first=local.get('peer_enrollment_request','request_before_rotation')
 old=await peer.approve_enrollment('node_codex',admin,'approval-before-rotation',
  first['request_id'],first['request_hash'])
 assert old['readback_verified']
 second_event,second_pair=await enrollment(task,'request_after_rotation')
 await service.refresh(admin,'collect-second-identity')
 second=local.get('peer_enrollment_request','request_after_rotation')
 new=await peer.approve_enrollment('node_codex',admin,'approval-after-rotation',
  second['request_id'],second['request_hash'],rotate=True)
 assert new['readback_verified'] and new['binding_id']!=old['binding_id']
 identity=copy.deepcopy(local.get('peer_identity','node_lab_phase3_codex'))
 operations=copy.deepcopy(local.items('peer_enrollment_operation'))
 events=copy.deepcopy(backend3.list_events())
 await rejected('identity_expired',peer.approve_enrollment('node_codex',admin,
  'approval-before-rotation',first['request_id'],first['request_hash'],rotate=True))
 assert local.get('peer_identity','node_lab_phase3_codex')==identity
 assert local.items('peer_enrollment_operation')==operations and backend3.list_events()==events
 assert identity['binding_id']==new['binding_id'] and identity['public_key']==second_pair['public_key']
 assert len([e for e in events if e['event_type']=='node_registry_snapshot'])==2
 again=await peer.approve_enrollment('node_codex',admin,'approval-after-rotation',
  second['request_id'],second['request_hash'],rotate=False)
 assert again==new and backend3.list_events()==events
asyncio.run(scenario())
''')
