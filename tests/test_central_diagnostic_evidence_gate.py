"""Observe through real services before a provider with only auto tool choice."""
import unittest
from tests.phase1_support import run_guarded
from tests.test_central_capabilities import SETUP

SCENARIO = '''
import httpx
from server import rdos_projects
local.put('state','business_workflow',{'version':1})
second=rdos_projects.create_project({'name':'第二项目','description':'独立诊断范围',
    'node_ids':['node_lab_phase3_workbuddy'],'folder_url':''},admin,'diagnostic-second-project')['id']
async def diagnostic(op,project='',tool=None,args=None,
                     text='检查目前各节点接入与任务回写情况。',agent_options=None):
 thread=service.create_thread(admin,project,'诊断先取证')['id']
 run=service.new_run(admin,text,op,'chat','manual',project,thread)
 agent=backend.make_agent()
 for key,value in (agent_options or {}).items():setattr(agent,key,value)
 requests=[];emitted=False
 async def respond(request):
  nonlocal emitted
  payload=json.loads(request.content);requests.append(payload)
  if not payload.get('tools'):
   message={'role':'assistant','content':json.dumps(purpose('diagnose'))}
  elif payload.get('tool_choice')!='auto':
   return httpx.Response(400,json={'error':{'message':'provider only supports auto with thinking'}})
  elif tool and not emitted:
   emitted=True
   message={'role':'assistant','content':None,'tool_calls':[{'id':'extra-read',
      'type':'function','function':{'name':tool,'arguments':json.dumps(args or {})}}]}
  else:message={'role':'assistant','content':'已核对本次取得的记录。'}
  return httpx.Response(200,json={'model':'local-diagnostic-provider','choices':[{
    'message':message,'finish_reason':'tool_calls' if message.get('tool_calls') else 'stop'}],
    'usage':{'prompt_tokens':3,'completion_tokens':2}})
 agent.transport.handle_async_request=respond
 with patch.object(lab_model_provider,'make_agent',return_value=agent):
  await service.run_agent(run['id'])
 return store.get('run',run['id']),requests
'''

class CentralDiagnosticEvidenceGateTest(unittest.TestCase):
    def check(self, body):
        result = run_guarded(SETUP + SCENARIO + body, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_auto_only_thinking_provider_gets_real_evidence_before_summary(self):
        self.check('''
async def main():
 protected={k:copy.deepcopy(store.items(k)) for k in ('operation','central_memory','state','project_health')}
 result,requests=await diagnostic('organization-evidence-before-auto',agent_options={
   'model':'deepseek-v4-pro','thinking':'enabled','reasoning_effort':'high','max_completion_tokens':393216})
 assert result['status']=='succeeded' and result['analysis_complete'],result
 assert len(requests)==2 and requests[1]['tool_choice']=='auto',requests
 assert all(p.get('thinking')=={'type':'enabled'} and p.get('reasoning_effort')=='high'
   and p['max_completion_tokens']==393216 for p in requests)
 receipt=store.items('inspection_receipt')
 assert len(receipt)==1 and set(receipt[0]['project_ids'])=={pid,second},receipt
 observed=material(requests[1])['current_request']['diagnostic_results'][0]
 assert observed['tool']=='central_inspect_overview'
 assert observed['result']['inspection_receipt_id']==receipt[0]['id']
 assert result['usage']['tool_calls']==1 and result['usage']['model_calls']==2
 before_summary=next(i for i,s in enumerate(result['steps']) if s.get('type')=='model_request' and s.get('model_call')==2)
 assert any(s.get('type')=='tool_result' and s.get('origin')=='trusted_diagnostic'
            for s in result['steps'][:before_summary])
 assert all(store.items(k)==v for k,v in protected.items())
asyncio.run(main())
''')

    def test_project_diagnostic_never_expands_to_other_project(self):
        self.check('''
async def main():
 result,requests=await diagnostic('project-evidence',project=pid)
 assert result['status']=='succeeded',result
 assert store.items('inspection_receipt')[0]['project_ids']==[pid]
asyncio.run(main())
''')

    def test_unclassified_question_without_actual_read_is_not_reported_complete(self):
        self.check('''
async def main():
 result,requests=await diagnostic('unobserved-code-question',text='只读检查当前代码和系统是否正常。')
 assert result['status']!='succeeded' and result.get('analysis_complete') is not True,result
 assert not store.items('inspection_receipt')
 assert any(f['code']=='evidence_missing' for f in result.get('tool_failures',[])),result
 assert '已核对本次取得的记录。' not in result.get('result','')
asyncio.run(main())
''')

    def test_failed_fresh_read_does_not_let_model_claim_complete(self):
        self.check('''
async def main():
 transport.get_backend().fault('read_events')
 result,requests=await diagnostic('failed-fresh-observation',text='请检查最新节点任务回写情况。')
 assert result['status']!='succeeded' and result.get('analysis_complete') is not True,result
 assert any(e['limits'] for e in result['diagnostic_evidence'])
 assert '已核对本次取得的记录。' not in result.get('result','')
 assert any(s.get('arguments',{}).get('fresh') is True for s in result['steps'] if s.get('type')=='tool_call')
asyncio.run(main())
''')

    def test_diagnostic_without_literal_readonly_cannot_start_sync(self):
        self.check('''
async def main():
 before=copy.deepcopy(store.items('maintenance_run'))
 result,requests=await diagnostic('diagnostic-implicit-readonly',project=pid,
     text='帮忙检查一下目前节点接入及任务回写情况。',tool='central_refresh_feishu')
 assert result['status']!='succeeded',result
 assert all(not {'central_refresh_feishu','central_start_inspection'} &
     {t['function']['name'] for t in p.get('tools',[])} for p in requests)
 assert store.items('maintenance_run')==before
 denied=await service.execute_tool('central_refresh_feishu',{},result,admin)
 assert not denied['ok'] and store.items('maintenance_run')==before,denied
asyncio.run(main())
''')

    def test_real_successful_reread_can_recover_an_initial_partial_observation(self):
        self.check('''
async def main():
 transport.get_backend().fault('read_events')
 result,requests=await diagnostic('recovered-fresh-observation',text='请检查最新节点任务回写情况。',
     tool='central_inspect_overview',args={'fresh':True})
 assert result['status']=='succeeded' and result['analysis_complete'],result
 reads=[s['result']['evidence'] for s in result['steps'] if s.get('type')=='tool_result'
        and s.get('name')=='central_inspect_overview']
 assert reads[0]['complete'] is False and reads[-1]['complete'] is True
 assert result['diagnostic_evidence'][0]['limits']==[]
asyncio.run(main())
''')

    def test_extra_memory_read_cannot_replace_current_work_observation(self):
        self.check('''
async def main():
 result,requests=await diagnostic('work-state-plus-memory',
     text='帮忙检查一下目前各节点接入状态、各任务下发及回写情况、各自定时任务回写情况。',
     tool='central_memory_search',args={'query':'协作规则'})
 assert result['status']=='succeeded' and result['analysis_complete'],result
 assert result['diagnostic_evidence_requirement']=='work_state'
 assert [e['tool'] for e in result['diagnostic_evidence']]==['central_inspect_overview','central_memory_search']
 assert result['diagnostic_evidence'][0]['work_state'] is True
 assert result['diagnostic_evidence'][1]['work_state'] is False
 assert len(store.items('inspection_receipt'))==1
asyncio.run(main())
''')

    def test_historical_judgment_observes_memory_without_live_project_reads(self):
        self.check('''
async def main():
 result,requests=await diagnostic('historical-task-judgment',
     text='历史上我们对任务回写规范是怎么判断的？')
 assert result['status']=='succeeded' and result['analysis_complete'],result
 assert result['diagnostic_evidence_requirement']=='relevant_source'
 assert not store.items('inspection_receipt')
 assert [e['tool'] for e in result['diagnostic_evidence']]==['central_memory_search']
 assert material(requests[1])['current_request']['diagnostic_results'][0]['arguments']['include_history'] is True
asyncio.run(main())
''')

    def test_current_remembered_rules_are_not_live_task_status(self):
        self.check('''
async def main():
 result,requests=await diagnostic('current-remembered-task-rules',
     text='现在记住了哪些关于任务回写的约定？')
 assert result['status']=='succeeded' and result['analysis_complete'],result
 assert result['diagnostic_evidence_requirement']=='relevant_source'
 assert not store.items('inspection_receipt')
asyncio.run(main())
''')

    def test_concrete_code_question_reads_requested_source_before_summary(self):
        self.check('''
async def main():
 result,requests=await diagnostic('concrete-code-evidence',project=pid,
     text='检查 server/central_chat.py 的实现。')
 assert result['status']=='succeeded' and result['analysis_complete'],result
 receipt=store.items('inspection_receipt')
 assert len(receipt)==1 and receipt[0]['tool']=='central_inspect_code'
 observed=material(requests[1])['current_request']['diagnostic_results'][0]
 assert observed['result']['evidence']['path']=='server/central_chat.py'
 assert observed['result']['evidence']['file_sha256']
asyncio.run(main())
''')

    def test_tool_budget_is_checked_before_automatic_diagnostic_read(self):
        self.check('''
async def main():
 result,requests=await diagnostic('no-read-budget',project=pid,agent_options={'max_tool_calls':0})
 assert result['status']!='succeeded' and result.get('error_code')=='tool_limit',result
 assert not store.items('inspection_receipt') and len(requests)==1
asyncio.run(main())
''')

if __name__ == '__main__':
    unittest.main()
