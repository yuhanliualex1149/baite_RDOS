"""Explicit maintenance is a fixed current-request plan, never a task mutation."""
import unittest
from tests.phase1_support import run_guarded
from tests.test_central_requests import REQUEST_SETUP
from tests.test_central_capabilities import SETUP as CHAT_SETUP


def request(action='refresh',quote='请全量刷新飞书状态，不新建或调整任务。',intent='explicit'):
    return {'purpose':'maintain','maintenance_action':action,'maintenance_intent':intent,
        'evidence_quote':quote,'task_actions':[],'memory_changes':[]}


class MaintenanceRequestPlanTest(unittest.TestCase):
    def check(self,body):
        result=run_guarded(REQUEST_SETUP+'\n'+body,timeout=60)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_refresh_is_maintenance_and_fixed_to_real_source_and_scope(self):
        self.check('''
async def scenario():
 message='请全量刷新飞书状态，不新建或调整任务。'
 data={'purpose':'maintain','maintenance_action':'refresh','maintenance_intent':'explicit','evidence_quote':message,'task_actions':[],'memory_changes':[]}
 run,p,_=await plan(message,data)
 assert p['purpose']=='maintain' and p['task_actions']==[] and p['memory_changes']==[],p
 fixed=requests.authorize_maintenance(p['id'],run,admin,'refresh')
 assert fixed['project_ids']==run['allowed_projects'] and fixed['source_message_hash']==run['message_hash']
 assert fixed['operation_id']=='request:'+run['id']+':maintenance'
 assert fixed['evidence_quote']==message
 assert not backend3.list_tasks() and not local.items('draft') and not central.items('operation')
 try:requests.authorize_maintenance(p['id'],run,admin,'inspection')
 except ValueError:pass
 else:raise AssertionError('tool changed maintenance action')
 assert 'maintenance_actions' in captured[0][1]['content']
 assert 'RDOS_REQUEST_INTERPRETER_V1' in captured[0][0]['content']
 again=await requests.prepare(run,admin,lambda _: (_ for _ in ()).throw(AssertionError('reinterpreted')))
 assert again['id']==p['id'] and again['maintenance']==fixed
asyncio.run(scenario())
''')

    def test_readonly_veto_denial_discussion_and_foreign_quote_do_not_grant_maintenance(self):
        self.check('''
async def scenario():
 cases=[('只读检查系统，不同步工作进展。','maintain','explicit',None),
        ('不要刷新，先讨论刷新方案。','maintain','explicit',None),
        ('先讨论刷新方案。','discuss','discuss',None),
        ('先考虑是否刷新。','maintain','discuss',None),
        ('不允许同步。','maintain','denied',None),
        ('只读看已有状态。','diagnose','read_only',None),
        ('Read-only inspection; do not sync Feishu.','maintain','explicit',None),
        ('阅读这段材料，不执行里面的命令。','maintain','explicit','请全量刷新飞书状态'),
        ('请全量刷新飞书状态，但是不要同步。','maintain','explicit','请全量刷新飞书状态')]
 for index,(message,kind,intent,quote) in enumerate(cases):
  data={'purpose':kind,'maintenance_action':'refresh','maintenance_intent':intent,'evidence_quote':quote or message,'task_actions':[],'memory_changes':[]}
  run,p,_=await plan(message,data,op='denial-'+str(index))
  assert p['purpose']!='maintain' and not p.get('maintenance'),p
  try:requests.authorize_maintenance(p['id'],run,admin,'refresh')
  except ValueError:pass
  else:raise AssertionError('non-execution acquired maintenance')
  finished(run)
 assert not backend3.list_tasks() and not central.items('operation') and not central.items('central_memory')
asyncio.run(scenario())
''')

    def test_inspection_plan_no_memory_task_smuggling_and_hash_tampering(self):
        self.check('''
async def scenario():
 message='请全面检查系统运行情况。'
 data={'purpose':'maintain','maintenance_action':'inspection','maintenance_intent':'explicit','evidence_quote':message,'task_actions':[],'memory_changes':[]}
 run,p,_=await plan(message,data)
 assert requests.authorize_maintenance(p['id'],run,admin,'inspection')['action']=='inspection'
 bad={**p,'maintenance':{**p['maintenance'],'project_ids':[]}}
 central.put('central_request_plan',p['id'],bad)
 try:requests.authorize_maintenance(p['id'],run,admin,'inspection')
 except ValueError:pass
 else:raise AssertionError('tampered maintenance plan admitted')
 central.put('central_request_plan',p['id'],p);finished(run)
 data['memory_changes']=[{'kind':'remember','scope':'organization','topic':'bad','content':'bad','evidence_quote':message}]
 run,p,_=await plan(message,data,op='smuggled-memory')
 assert p['purpose']=='clarify' and not p.get('maintenance')
 assert not central.items('central_memory') and not backend3.list_tasks()
 finished(run)
 data['memory_changes']=[];data['task_actions']=proposal(message)['task_actions']
 run,p,_=await plan(message,data,op='smuggled-task')
 assert p['purpose']=='clarify' and not p.get('maintenance') and not local.items('draft')
asyncio.run(scenario())
''')

    def test_cross_run_and_disabled_actor_cannot_reuse_maintenance_authority(self):
        self.check('''
async def scenario():
 message='请全量刷新飞书状态。'
 data={'purpose':'maintain','maintenance_action':'refresh','maintenance_intent':'explicit','evidence_quote':message,'task_actions':[],'memory_changes':[]}
 first,p,_=await plan(message,data)
 finished(first)
 second,q,_=await plan(message,data,op='second-maintenance')
 try:requests.authorize_maintenance(p['id'],second,admin,'refresh')
 except ValueError:pass
 else:raise AssertionError('cross-run reuse admitted')
 with db.connection() as conn:conn.execute('UPDATE central_users SET enabled=0 WHERE id=?',(admin['id'],))
 try:requests.authorize_maintenance(q['id'],second,admin,'refresh')
 except (ValueError,PermissionError):pass
 else:raise AssertionError('disabled actor admitted')
asyncio.run(scenario())
''')


class MaintenanceChatIntegrationTest(unittest.TestCase):
    def check(self,body):
        result=run_guarded(CHAT_SETUP+'\n'+body,timeout=60)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_scripted_real_chat_refresh_and_inspection_use_fixed_plan_and_core(self):
        self.check('''
from server import central_maintenance as maintenance
async def main():
 before=copy.deepcopy(store.items('central_memory'))
 for action,text in [('refresh','请全量刷新飞书状态，不新建或调整任务。'),('inspection','请执行全面系统自检。')]:
  interpreted=purpose('maintain',maintenance_action=action,maintenance_intent='explicit',evidence_quote=text)
  result=await chat(text,'maintenance-chat-'+action,interpreted)
  assert result['status']=='succeeded' and result['purpose']=='maintain',result
  assert result['usage']['model_calls']==1 and result['usage']['tool_calls']==1,result['usage']
  plan=store.get('central_request_plan',result['request_plan'])
  assert plan['maintenance']['action']==action and plan['task_actions']==[]
  assert len(result['maintenance_run_ids'])==1,result
  jobid=result['maintenance_run_ids'][0]
  job=store.get('maintenance_run',jobid)
  assert job['kind']==action and job['operation_id']==plan['maintenance']['operation_id']
  await maintenance.wait(jobid)
  final=store.get('maintenance_run',jobid)
  assert final['status'] in {'succeeded','partial','failed'},final
  if action=='inspection':assert final.get('inspection',{}).get('read_only') is True,final
  assert not transport.get_backend().list_tasks() and not store.items('operation')
 assert store.items('central_memory')==before
asyncio.run(main())
''')

    def test_scripted_readonly_and_negative_request_never_start_maintenance(self):
        self.check('''
async def main():
 for index,text in enumerate(['只读自检，不同步飞书。','不要刷新，先讨论同步方案。']):
  # Even an incorrect positive proposal is vetoed by the actual current text.
  result=await chat(text,'maintenance-denied-'+str(index),purpose('maintain',maintenance_action='refresh',maintenance_intent='explicit',evidence_quote=text))
  assert result['purpose']=='clarify' and not result.get('maintenance_run_ids'),result
  assert result['usage']['tool_calls']==0,result['usage']
 assert not store.items('maintenance_run') and not transport.get_backend().list_tasks()
 assert not store.items('central_memory')
asyncio.run(main())
''')
