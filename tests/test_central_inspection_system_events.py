"""Fresh inspection validates signed system events without ingesting them."""
import unittest

from tests.phase1_support import run_guarded
from tests.test_peer_enrollment import ENROLL_SETUP


SETUP = ENROLL_SETUP + '''
from server import central_inspection as inspection, central_service as hub
def run_for():
 hub.ACTIVE_RUN_ID=None
 thread=hub.create_thread(admin,local.PROJECT_ID,'只读核对系统事件')
 return hub.new_run(admin,'只读核对最新节点登记记录，不处理进展或保存记忆。',
     'inspect-'+store.identifier('op'),'chat','manual',local.PROJECT_ID,thread['id'])
def snapshot():
 with db.connection() as conn:
  output={table:[tuple(row) for row in conn.execute('SELECT * FROM '+table+' ORDER BY 1,2')]
      for table in ('phase3_local','phase3_mock','phase3_history','phase3_event_index')}
  output['central_records']=[tuple(row) for row in conn.execute(
      "SELECT * FROM central_records WHERE kind!='inspection_receipt' ORDER BY kind,id")]
 return central.digest(output)
async def system_events(*,unobserved_request=True):
 task=await start('inspection-signed-system',completion_rule={'kind':'receipt_recorded'})
 first,pair=await enrollment(task,'request_inspection_bound')
 service._ingest_event(first,{task['task_id']:task})
 pending=local.get('peer_enrollment_request','request_inspection_bound')
 await peer.approve_enrollment('node_codex',admin,'approve-inspection-bound',
     pending['request_id'],pending['request_hash'])
 identity=local.get('peer_identity','node_lab_phase3_codex')
 assert not identity['ready']
 new=None
 if unobserved_request:
  new,_=await enrollment(task,'request_inspection_unobserved',pair=pair)
 claim={'node_id':'node_codex','binding_id':identity['binding_id'],
    'capability':'peer_tasks_v1','skill_version':'1.2.0'}
 ack={'id':'evt_inspection_unobserved_ack','operation_id':'inspection-unobserved-ack',
    **service.environment(),'project_id':local.PROJECT_ID,'claimed_node_id':'node_lab_phase3_codex',
    'event_type':'peer_capability_ack','task_refs':[],'body':'节点已经提交能力回执。',
    'peer_ack':peer.signed(claim,pair['private_key'])}
 backend3.append_event(ack,ack['operation_id'])
 registry=next(e for e in backend3.list_events() if e.get('event_type')=='node_registry_snapshot')
 return task,first,new,ack,registry
'''


class CentralInspectionSystemEventsTest(unittest.TestCase):
    def check(self, body):
        result = run_guarded(SETUP + body, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_three_signed_system_event_types_are_complete_without_ingestion(self):
        self.check('''
async def scenario():
 task,first,new,ack,registry=await system_events()
 run=run_for();before=snapshot()
 result=await inspection.execute('central_inspect_overview',{'fresh':True},run,admin)
 assert result['ok'] and result['evidence']['complete'],result
 project=result['evidence']['projects'][0]
 assert project['fresh_status']=='succeeded' and not project.get('observation_issues'),project
 observed={e['id']:e for e in project['events']}
 for event in (first,new,ack,registry):assert event['id'] in observed,(event['id'],observed)
 assert local.get('peer_enrollment_request','request_inspection_unobserved') is None
 assert not local.get('peer_identity','node_lab_phase3_codex')['ready']
 assert not local.event_fingerprint(new['id']) and not local.event_fingerprint(ack['id'])
 assert snapshot()==before,'read-only overview mutated business, identity, operations or memory'
asyncio.run(scenario())
''')

    def test_tampered_system_signature_is_partial_and_keeps_original_states(self):
        self.check('''
async def scenario():
 task,first,new,ack,registry=await system_events()
 changed=copy.deepcopy(registry)
 proof=changed['registry_proof'];proof['payload_json']=proof['payload_json'].replace('ws_rdos','ws_forged')
 backend3._save('event',changed['id'],changed)
 run=run_for();before=snapshot()
 result=await inspection.execute('central_inspect_overview',{'fresh':True},run,admin)
 assert result['ok'] and result['evidence']['complete'] is False,result
 project=result['evidence']['projects'][0]
 assert project['fresh_status']=='partial'
 assert any(i.get('event_id')==changed['id'] for i in project['observation_issues']),project
 assert snapshot()==before
asyncio.run(scenario())
''')

    def test_wrong_node_and_task_reference_cannot_become_valid_system_observations(self):
        self.check('''
async def scenario():
 task,first,new,ack,registry=await system_events()
 wrong_node=copy.deepcopy(ack);wrong_node['claimed_node_id']='node_lab_phase3_workbuddy'
 backend3._save('event',wrong_node['id'],wrong_node)
 wrong_ref=copy.deepcopy(new);wrong_ref['task_refs'][0]['requirements_version']+=1
 backend3._save('event',wrong_ref['id'],wrong_ref)
 run=run_for();before=snapshot()
 result=await inspection.execute('central_inspect_overview',{'fresh':True},run,admin)
 assert result['ok'] and result['evidence']['complete'] is False,result
 issues=result['evidence']['projects'][0]['observation_issues']
 assert {wrong_node['id'],wrong_ref['id']} <= {i.get('event_id') for i in issues},issues
 assert snapshot()==before
asyncio.run(scenario())
''')

    def test_unknown_event_type_is_not_silently_ignored(self):
        self.check('''
async def scenario():
 task,first,new,ack,registry=await system_events()
 unknown={**copy.deepcopy(ack),'id':'evt_unknown_inspection_type',
    'event_type':'unregistered_future_system_type','body':'PRIVATE_RAW_EVENT_SENTINEL'}
 backend3._save('event',unknown['id'],unknown)
 run=run_for();before=snapshot()
 result=await inspection.execute('central_inspect_overview',{'fresh':True},run,admin)
 assert result['ok'] and result['evidence']['complete'] is False,result
 issues=result['evidence']['projects'][0]['observation_issues']
 assert any(i.get('event_id')==unknown['id'] and i.get('code')=='invalid_event' for i in issues),issues
 assert 'PRIVATE_RAW_EVENT_SENTINEL' not in json.dumps(result)
 assert snapshot()==before
asyncio.run(scenario())
''')

    def test_saved_registration_remains_observable_after_task_completion(self):
        self.check('''
async def scenario():
 task,first,new,ack,registry=await system_events(unobserved_request=False)
 await service.apply_task_action(task['task_id'],'manual_complete',{'note':'节点已完成配置'},
     admin,'close-inspection-setup',task['record_revision'],task['requirements_version'])
 run=run_for();before=snapshot()
 result=await inspection.execute('central_inspect_overview',{'fresh':True},run,admin)
 assert result['ok'] and result['evidence']['complete'],result
 assert not local.get('peer_identity','node_lab_phase3_codex')['ready']
 assert snapshot()==before
asyncio.run(scenario())
''')

    def test_saved_old_registration_remains_observable_after_requirements_upgrade(self):
        self.check('''
async def scenario():
 task,first,new,ack,registry=await system_events(unobserved_request=False)
 draft=await service.prepare_task({'task_id':task['task_id'],'goal':'保留身份，核对新版说明',
     'change_reason':'说明升级，原申请保留历史'},admin,'update-inspection-setup-draft')
 await service.apply_task_action(task['task_id'],'update',{'draft_id':draft['id'],
     'expected_hash':draft['draft_hash']},admin,'update-inspection-setup',
     task['record_revision'],task['requirements_version'])
 run=run_for();before=snapshot()
 result=await inspection.execute('central_inspect_overview',{'fresh':True},run,admin)
 assert result['ok'] and result['evidence']['complete'],result
 assert local.get('peer_enrollment_request','request_inspection_bound')['requirements_version']==1
 assert snapshot()==before
asyncio.run(scenario())
''')

    def test_large_failed_observation_keeps_safe_project_issue_metadata_in_run(self):
        self.check('''
from unittest.mock import patch
from server import lab_model_provider
def business_snapshot():
 with db.connection() as conn:
  output={table:[tuple(row) for row in conn.execute('SELECT * FROM '+table+' ORDER BY 1,2')]
      for table in ('phase3_local','phase3_mock','phase3_history','phase3_event_index')}
  output['central_records']=[tuple(row) for row in conn.execute(
      "SELECT * FROM central_records WHERE kind NOT IN ('inspection_receipt','run','message','central_request_plan') ORDER BY kind,id")]
 return central.digest(output)
async def scenario():
 task,first,new,ack,registry=await system_events()
 for index in range(55):
  event={'id':'evt_safe_large_'+str(index),'operation_id':'safe-large-'+str(index),
    **service.environment(),'project_id':local.PROJECT_ID,'claimed_node_id':'node_lab_phase3_codex',
    'event_type':'progress','task_refs':refs(task),'body':'RAW_BODY_MUST_NOT_APPEAR_'+str(index)}
  backend3.append_event(event,event['operation_id'])
 unknown={**copy.deepcopy(ack),'id':'evt_unknown_large_projection',
    'event_type':'unknown_system_type','body':'PRIVATE_UNRECOGNIZED_BODY_MUST_NOT_APPEAR'}
 backend3._save('event',unknown['id'],unknown)
 run=run_for();before=business_snapshot()
 agent=backend.make_agent([json.dumps({'purpose':'diagnose','task_actions':[],'memory_changes':[]}),
    '已检查完整记录。'])
 with patch.object(lab_model_provider,'make_agent',return_value=agent):
  await hub.run_agent(run['id'])
 saved=central.get('run',run['id'])
 assert saved['status']!='succeeded' and saved.get('analysis_complete') is not True,saved
 result=next(s['result'] for s in saved['steps'] if s.get('type')=='tool_result'
             and s.get('name')=='central_inspect_overview')
 assert result.get('details_available') is True,result
 assert result['ok'] is True and result['complete'] is False,result
 project=result['projects'][0]
 assert project['fresh_status']=='partial' and project['issue_count']==1,project
 assert project['issue_codes']=={'invalid_event':1},project
 assert project['issue_types']=={'Phase3Error':1},project
 assert project['event_count']>=55
 assert result['source_errors']==[]
 assert 'RAW_BODY_MUST_NOT_APPEAR' not in json.dumps(result)
 assert 'PRIVATE_UNRECOGNIZED_BODY_MUST_NOT_APPEAR' not in json.dumps(result)
 assert before==business_snapshot()
asyncio.run(scenario())
''')

    def test_indexed_system_event_cannot_change_outer_body_with_unchanged_signature(self):
        self.check('''
async def scenario():
 task,first,new,ack,registry=await system_events()
 original=local.event_fingerprint(first['id'])
 assert original
 changed=copy.deepcopy(first);changed['body']='PRIVATE_CHANGED_OUTER_BODY'
 assert changed['enrollment_proof']==first['enrollment_proof']
 backend3._save('event',changed['id'],changed)
 run=run_for();before=snapshot()
 result=await inspection.execute('central_inspect_overview',{'fresh':True},run,admin)
 assert result['ok'] and result['evidence']['complete'] is False,result
 project=result['evidence']['projects'][0]
 assert project['fresh_status']=='partial'
 assert any(i.get('event_id')==first['id'] and i.get('code')=='event_conflict'
            for i in project['observation_issues']),project
 assert local.event_fingerprint(first['id'])==original
 assert not local.event_fingerprint(new['id']) and not local.event_fingerprint(ack['id'])
 assert 'PRIVATE_CHANGED_OUTER_BODY' not in json.dumps(result)
 assert snapshot()==before
asyncio.run(scenario())
''')

    def test_failed_source_keeps_safe_error_code_and_type_without_business_changes(self):
        self.check('''
async def scenario():
 await system_events()
 run=run_for();before=snapshot();backend3.fault('read_events')
 result=await inspection.execute('central_inspect_overview',{'fresh':True},run,admin)
 assert result['ok'] is True and result['evidence']['complete'] is False,result
 summary=inspection.result_brief(result)
 assert summary['complete'] is False and summary['projects'][0]['fresh_status']=='failed'
 assert summary['source_errors']==[{'source':'events','code':'read_failed','error_type':'BackendError'}],summary
 assert snapshot()==before
asyncio.run(scenario())
''')


if __name__ == '__main__':
    unittest.main()
