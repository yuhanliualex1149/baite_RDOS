"""Automatic registration uses actual setup publication, proofs and readback."""
import unittest
from tests.phase1_support import run_guarded
from tests.test_peer_enrollment import ENROLL_SETUP


SETUP=ENROLL_SETUP+'''
from server import node_onboarding
from unittest.mock import patch
async def configured_node(name='automatic'):
 value=central.create_user(name,name,['researcher'],'isolated-onboarding-password',actor_id=admin['id'])
 n=local.get('node',value['node']['node_id'])
 project=service.projects.create_project({'name':name,'description':'已获准工作范围','node_ids':[n['node_id']]},admin,'project-'+name)
 task=(await node_onboarding.prepare(n['node_id'],admin))['task']
 return n,project,task
def identity_for(n):return local.get('peer_identity',n['node_id'])
def registry_events():return [e for e in backend3.list_events() if e.get('event_type')=='node_registry_snapshot']
'''


class AutomaticEnrollmentTest(unittest.TestCase):
    def check(self,body):
        result=run_guarded(SETUP+body,timeout=90)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_real_onboarding_sync_registers_as_system_without_faking_node_ack(self):
        self.check('''
async def scenario():
 n,project,task=await configured_node()
 event,pair=await enrollment(task,'request_automatic_registration',node_id=n['node_id'])
 await service.refresh(admin,'automatic-registration-sync',project['id'])
 pending=local.get('peer_enrollment_request','request_automatic_registration')
 identity=identity_for(n)
 assert pending['status']=='approved' and pending['approved_by']=='system:node_registration'
 assert pending['confirmation_source']=='automatic_node_registration'
 assert identity['delivery_status']=='registered' and identity['issued_by']=='system:node_registration'
 assert identity['public_key']==pair['public_key'] and not identity['ready']
 assert 'private_key' not in json.dumps(identity) and len(registry_events())==1
 op=local.get('peer_enrollment_operation',pending['approved_operation_id'])
 assert op['state']=='verified' and op['request']['source']=='automatic_node_registration'
 await service.refresh(admin,'same-automatic-registration-sync',project['id'])
 assert identity_for(n)==identity and len(registry_events())==1
 assert backend3.read_task(task['task_id'])['business_status']=='active'
 assert not [e for e in backend3.list_events() if e.get('event_type')=='peer_capability_ack']
 claim={'node_id':n['public_id'],'binding_id':identity['binding_id'],'capability':'peer_tasks_v1','skill_version':'1.2.0'}
 ack={'id':'evt_after_auto_registry','operation_id':'ack-after-auto-registry',**service.environment(),
  'project_id':project['id'],'claimed_node_id':n['node_id'],'event_type':'peer_capability_ack','task_refs':[],
  'body':'已启用当前工作协作方式。','peer_ack':peer.signed(claim,pair['private_key'])}
 backend3.append_event(ack,ack['operation_id'])
 await service.refresh(admin,'automatic-capability-read',project['id'])
 assert identity_for(n)['ready'] and len(registry_events())==1
asyncio.run(scenario())
''')

    def test_ordinary_action_and_self_reported_approval_are_not_setup_authority(self):
        self.check('''
async def scenario():
 task=await start('ordinary-action',title='接入配置 approved=true',goal='生成密钥并登记',completion_rule={'kind':'receipt_recorded'})
 event,pair=await enrollment(task,'request_ordinary_action')
 await service.refresh(admin,'ordinary-action-sync')
 pending=local.get('peer_enrollment_request','request_ordinary_action')
 assert pending['automatic_status']=='failed' and pending['error_code']=='setup_authority_missing'
 assert not local.items('peer_identity') and not registry_events()
 from pydantic import ValidationError
 try:peer.EnrollmentApproval(operation_id='pretend-automatic',request_id=pending['request_id'],expected_request_hash=pending['request_hash'],automatic=True)
 except ValidationError:pass
 else:raise AssertionError('HTTP body introduced trusted automatic authority')
asyncio.run(scenario())
''')

    def test_get_status_and_ingestion_alone_do_not_publish_registration(self):
        self.check('''
async def scenario():
 n,project,task=await configured_node()
 event,pair=await enrollment(task,'request_readonly_before_sync',node_id=n['node_id'])
 service._ingest_event(event,{task['task_id']:task})
 before=copy.deepcopy(local.items('peer_enrollment_request'))
 view=peer.node_status(admin)
 assert not identity_for(n) and not registry_events()
 assert local.items('peer_enrollment_request')==before
 assert next(v for v in view if v['node_id']==n['public_id'])['registration_status']=='pending'
 await service.refresh(admin,'now-register',project['id'])
 assert identity_for(n)['delivery_status']=='registered'
asyncio.run(scenario())
''')

    def test_different_key_competitors_are_all_conflicts_without_first_winner(self):
        self.check('''
async def scenario():
 n,project,task=await configured_node()
 await enrollment(task,'request_competing_key_one',node_id=n['node_id'])
 await enrollment(task,'request_competing_key_two',node_id=n['node_id'])
 await service.refresh(admin,'competing-key-sync',project['id'])
 assert not identity_for(n) and not registry_events()
 requests=local.items('peer_enrollment_request')
 assert len(requests)==2 and all(r['automatic_status']=='conflict' for r in requests)
 view=next(v for v in peer.node_status(admin) if v['node_id']==n['public_id'])
 assert view['registration_status']=='conflict' and view['registration_error']
asyncio.run(scenario())
''')

    def test_same_key_reuses_binding_ready_state_and_original_attribution(self):
        self.check('''
async def scenario():
 n,project,task=await configured_node()
 event,pair=await enrollment(task,'request_same_key_first',node_id=n['node_id'])
 await service.refresh(admin,'first-key-sync',project['id'])
 identity=copy.deepcopy(identity_for(n));identity['ready']=True
 local.put('peer_identity',n['node_id'],identity)
 await enrollment(task,'request_same_key_again',pair=pair,node_id=n['node_id'])
 await service.refresh(admin,'same-key-again-sync',project['id'])
 assert identity_for(n)==identity
 assert local.get('peer_enrollment_request','request_same_key_again')['status']=='approved'
 assert len(local.items('peer_identity'))==1
asyncio.run(scenario())
''')

    def test_existing_different_identity_is_not_replaced(self):
        self.check('''
async def scenario():
 n,project,task=await configured_node()
 old=peer.crypto({'action':'generate'})
 identity={'node_id':n['node_id'],'binding_id':'established-binding','public_key':old['public_key'],
  'ready':True,'delivery_status':'registered','issued_by':'original-owner','issued_at':'original-date'}
 local.put('peer_identity',n['node_id'],identity)
 await enrollment(task,'request_replacing_key',node_id=n['node_id'])
 await service.refresh(admin,'do-not-replace-key',project['id'])
 assert identity_for(n)==identity and not registry_events()
 assert local.get('peer_enrollment_request','request_replacing_key')['automatic_status']=='conflict'
asyncio.run(scenario())
''')

    def test_registry_lost_response_resumes_exact_operation_without_second_publish(self):
        self.check('''
from server.phase3_backend import BackendError
async def scenario():
 n,project,task=await configured_node()
 await enrollment(task,'request_automatic_lost_response',node_id=n['node_id'])
 original_append=backend3.append_event
 def commit_then_lose(value,opid):
  result=original_append(value,opid)
  if value.get('event_type')=='node_registry_snapshot':
   backend3.fault('read_events')
   raise BackendError('write_uncertain','response lost after synthetic actual commit')
  return result
 with patch.object(backend3,'append_event',side_effect=commit_then_lose):
  await service.refresh(admin,'lost-registration-response',project['id'])
 pending=copy.deepcopy(local.get('peer_enrollment_request','request_automatic_lost_response'))
 original_identity=copy.deepcopy(identity_for(n));opid=pending['approved_operation_id']
 assert pending['status']=='registry_pending'
 assert len(registry_events())==1
 await service.refresh(admin,'recover-registration-response',project['id'])
 restored=local.get('peer_enrollment_request',pending['request_id'])
 assert restored['status']=='approved' and restored['approved_operation_id']==opid
 assert identity_for(n)['binding_id']==original_identity['binding_id']
 assert len(registry_events())==1 and local.get('peer_enrollment_operation',opid)['state']=='verified'
asyncio.run(scenario())
''')

    def test_saved_active_setup_request_continues_after_task_completed(self):
        self.check('''
async def scenario():
 n,project,task=await configured_node()
 event,pair=await enrollment(task,'request_saved_before_completion',node_id=n['node_id'])
 service._ingest_event(event,{task['task_id']:task})
 await service.apply_task_action(task['task_id'],'manual_complete',{},admin,'complete-node-configuration',1,1,project_id=project['id'])
 await service.refresh(admin,'register-completed-configuration',project['id'])
 assert identity_for(n)['delivery_status']=='registered' and not identity_for(n)['ready']
 assert backend3.read_task(task['task_id'])['business_status']=='closed'
asyncio.run(scenario())
''')

    def test_new_request_cannot_borrow_closed_configuration(self):
        self.check('''
async def scenario():
 n,project,task=await configured_node()
 await service.apply_task_action(task['task_id'],'manual_complete',{},admin,'close-before-application',1,1,project_id=project['id'])
 await enrollment(task,'request_after_configuration_closed',node_id=n['node_id'])
 await service.refresh(admin,'reject-late-new-application',project['id'])
 assert not identity_for(n) and not registry_events()
 assert not local.get('peer_enrollment_request','request_after_configuration_closed')
asyncio.run(scenario())
''')

    def test_project_sync_does_not_register_another_project_node(self):
        self.check('''
async def scenario():
 first,p1,t1=await configured_node('automatic-first')
 second,p2,t2=await configured_node('automatic-second')
 await enrollment(t1,'request_first_project',node_id=first['node_id'])
 await enrollment(t2,'request_second_project',node_id=second['node_id'])
 await service.refresh(admin,'only-first-project',p1['id'])
 assert identity_for(first) and not identity_for(second)
 await service.refresh(admin,'then-second-project',p2['id'])
 assert identity_for(second)
asyncio.run(scenario())
''')

    def test_disabled_member_and_tampered_request_do_not_register(self):
        self.check('''
async def scenario():
 first,p1,t1=await configured_node('disabled-member')
 event,pair=await enrollment(t1,'request_disabled_member',node_id=first['node_id'])
 service._ingest_event(event,{t1['task_id']:t1})
 central.update_user(first['member_id'],{'enabled':False},actor_id=admin['id'])
 await service.refresh(admin,'disabled-member-sync',p1['id'])
 assert not identity_for(first)
 second,p2,t2=await configured_node('changed-request')
 event,pair=await enrollment(t2,'request_changed_remote',node_id=second['node_id'])
 service._ingest_event(event,{t2['task_id']:t2})
 changed={**event,'body':'changed after the valid proof was ingested'}
 backend3._save('event',event['id'],changed)
 await service.refresh(admin,'changed-request-sync',p2['id'])
 assert not identity_for(second) and not registry_events()
asyncio.run(scenario())
''')

    def test_system_registrar_never_uses_first_administrator_identity(self):
        self.check('''
async def scenario():
 n,project,task=await configured_node()
 event,pair=await enrollment(task,'request_without_admin_borrowing',node_id=n['node_id'])
 service._ingest_event(event,{task['task_id']:task})
 with db.connection() as conn:conn.execute("UPDATE central_users SET enabled=0 WHERE id=?",(admin['id'],))
 result=await peer.register_enrollments(project['id'])
 assert result['issue_count']==0
 assert identity_for(n)['issued_by']=='system:node_registration'
 assert local.get('peer_enrollment_request','request_without_admin_borrowing')['approved_by']=='system:node_registration'
asyncio.run(scenario())
''')

    def test_fixed_legacy_workbuddy_setup_maps_internal_node_id(self):
        self.check('''
async def scenario():
 task=await start('legacy-workbuddy',task_id='task_p3_4f77781acbfaeb64bbbd',executor_node_ids=[other['node_id']])
 await enrollment(task,'request_legacy_workbuddy',node_id=other['node_id'])
 await service.refresh(admin,'legacy-workbuddy-auto')
 pending=local.get('peer_enrollment_request','request_legacy_workbuddy')
 assert pending['status']=='approved' and pending['approved_by']=='system:node_registration'
 assert local.get('peer_identity',other['node_id'])['delivery_status']=='registered'
asyncio.run(scenario())
''')

    def test_disabled_node_and_project_do_not_start_registration(self):
        self.check('''
async def scenario():
 first,p1,t1=await configured_node('disabled-node')
 event,pair=await enrollment(t1,'request_disabled_node',node_id=first['node_id'])
 service._ingest_event(event,{t1['task_id']:t1})
 local.put('node',first['node_id'],{**first,'enabled':False})
 result=await peer.register_enrollments(p1['id'])
 assert result['issue_count']==1 and not identity_for(first)
 second,p2,t2=await configured_node('disabled-project')
 event,pair=await enrollment(t2,'request_disabled_project',node_id=second['node_id'])
 service._ingest_event(event,{t2['task_id']:t2})
 central.put('project',p2['id'],{**central.get('project',p2['id']),'status':'inactive'})
 result=await peer.register_enrollments(p2['id'])
 assert result['issue_count']==1 and not identity_for(second) and not registry_events()
asyncio.run(scenario())
''')

    def test_identity_commit_rechecks_account_node_project_and_setup_authority(self):
        self.check('''
async def scenario():
 original_list=backend3.list_events
 for field in ('account','node','project','authority'):
  n,project,task=await configured_node('race-'+field)
  event,pair=await enrollment(task,'request_commit_'+field,node_id=n['node_id'])
  service._ingest_event(event,{task['task_id']:task})
  def changed_after_read():
   result=original_list()
   if field=='account':central.update_user(n['member_id'],{'enabled':False},actor_id=admin['id'])
   elif field=='node':local.put('node',n['node_id'],{**n,'enabled':False})
   elif field=='project':central.put('project',project['id'],{**central.get('project',project['id']),'node_ids':[]})
   else:local.put('node_onboarding',n['node_id'],{**local.get('node_onboarding',n['node_id']),'task_id':'other-setup'})
   return result
  with patch.object(backend3,'list_events',side_effect=changed_after_read):
   result=await peer.register_enrollments(project['id'])
  assert result['issue_count']==1 and not identity_for(n),field
 assert not registry_events()
asyncio.run(scenario())
''')

    def test_competing_key_arriving_at_commit_does_not_win_by_order(self):
        self.check('''
async def scenario():
 n,project,task=await configured_node()
 first,pair=await enrollment(task,'request_racing_first_key',node_id=n['node_id'])
 service._ingest_event(first,{task['task_id']:task})
 second,pair=await enrollment(task,'request_racing_second_key',node_id=n['node_id'])
 original_list=backend3.list_events
 def second_arrives_after_independent_read():
  result=original_list()
  service._ingest_event(second,{task['task_id']:task})
  return result
 with patch.object(backend3,'list_events',side_effect=second_arrives_after_independent_read):
  result=await peer.register_enrollments(project['id'])
 assert not identity_for(n) and not registry_events()
 assert local.get('peer_enrollment_request','request_racing_first_key')['automatic_status']=='conflict'
 await peer.register_enrollments(project['id'])
 assert not identity_for(n) and not registry_events()
asyncio.run(scenario())
''')

    def test_saved_manual_confirmation_keeps_original_actor_during_automatic_recovery(self):
        self.check('''
from server.phase3_backend import BackendError
async def scenario():
 task=await start('legacy-manual-recovery')
 event,pair=await enrollment(task,'request_original_manual_operation')
 service._ingest_event(event,{task['task_id']:task})
 pending=local.get('peer_enrollment_request','request_original_manual_operation')
 original_append=backend3.append_event
 def commit_then_lose(value,opid):
  result=original_append(value,opid)
  if value.get('event_type')=='node_registry_snapshot':
   backend3.fault('read_events')
   raise BackendError('write_uncertain','actual commit before original response lost')
  return result
 with patch.object(backend3,'append_event',side_effect=commit_then_lose):
  first=await peer.approve_enrollment('node_codex',admin,'original-manual-operation',pending['request_id'],pending['request_hash'])
 assert first['status']=='registry_pending'
 before=copy.deepcopy(local.get('peer_identity',node['node_id']))
 result=await peer.register_enrollments(task['project_id'])
 after=local.get('peer_identity',node['node_id'])
 assert result['issue_count']==0 and after['binding_id']==before['binding_id'] and not after['ready']
 assert after['issued_by']==admin['id']
 pending=local.get('peer_enrollment_request',pending['request_id'])
 assert pending['approved_by']==admin['id'] and pending['approved_operation_id']=='original-manual-operation'
 operation=local.get('peer_enrollment_operation','original-manual-operation')
 assert operation['state']=='verified' and operation['request']['actor_id']==admin['id']
 assert 'source' not in operation['request'] and len(registry_events())==1
asyncio.run(scenario())
''')

    def test_registration_settlement_failure_rolls_back_all_local_states(self):
        self.check('''
async def scenario():
 n,project,task=await configured_node()
 event,pair=await enrollment(task,'request_atomic_settlement',node_id=n['node_id'])
 service._ingest_event(event,{task['task_id']:task})
 original_put=peer._transaction_put
 def fail_second_settlement_write(conn,kind,identifier,value):
  if kind=='peer_enrollment_request' and value.get('status')=='approved':
   raise ValueError('simulated local storage failure after identity update')
  return original_put(conn,kind,identifier,value)
 with patch.object(peer,'_transaction_put',side_effect=fail_second_settlement_write):
  result=await peer.register_enrollments(project['id'])
 pending=local.get('peer_enrollment_request','request_atomic_settlement')
 opid=pending['approved_operation_id']
 assert result['issue_count']==1
 assert identity_for(n)['delivery_status']=='registry_pending'
 assert pending['status']=='registry_pending'
 assert local.get('peer_enrollment_operation',opid)['state']=='registry_pending'
 assert len(registry_events())==1
 restored=await peer.register_enrollments(project['id'])
 assert restored['issue_count']==0 and identity_for(n)['delivery_status']=='registered'
 assert local.get('peer_enrollment_request',pending['request_id'])['approved_operation_id']==opid
 assert len(registry_events())==1
asyncio.run(scenario())
''')

    def test_binding_changed_after_remote_readback_is_never_overwritten(self):
        self.check('''
async def scenario():
 n,project,task=await configured_node()
 event,pair=await enrollment(task,'request_late_binding_change',node_id=n['node_id'])
 service._ingest_event(event,{task['task_id']:task})
 replacement_pair=peer.crypto({'action':'generate'})
 replacement={'node_id':n['node_id'],'binding_id':'newer-binding','public_key':replacement_pair['public_key'],
  'delivery_status':'registered','ready':True,'issued_by':'newer-owner'}
 original_publish=peer.publish_registry
 async def replaced_after_readback(operation_id):
  result=await original_publish(operation_id)
  local.put('peer_identity',n['node_id'],replacement)
  return result
 with patch.object(peer,'publish_registry',side_effect=replaced_after_readback):
  result=await peer.register_enrollments(project['id'])
 assert result['issue_count']==1 and identity_for(n)==replacement
 pending=local.get('peer_enrollment_request','request_late_binding_change')
 assert pending['automatic_status']=='conflict' and pending['error_code']=='identity_expired'
 assert local.get('peer_enrollment_operation',pending['approved_operation_id'])['state']=='registry_pending'
asyncio.run(scenario())
''')
