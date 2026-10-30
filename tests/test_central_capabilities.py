"""All three capabilities through the actual authenticated chat orchestration."""
import unittest
from tests.phase1_support import run_guarded

SETUP = '''
import copy
from unittest.mock import patch
from server import central_service as service, phase1_runtime as runtime
from server import phase3_service as tasks, phase3_backend as transport, phase3_store as local
from server import central_memory as memory, lab_model_provider
runtime.initialize();tasks.initialize()
pid=tasks.PROJECT_ID
captured=[]
async def chat(text,op,interpretation,script=None,thread=None,project=None,agent_options=None,after_interpretation=None):
 project=pid if project is None else project
 thread=thread or service.create_thread(admin,project,'业务对话')['id']
 run=service.new_run(admin,text,op,'chat','manual',project,thread)
 agent=backend.make_agent([json.dumps(interpretation,ensure_ascii=False),*(script or ['已核对当前记录。'])])
 for key,value in (agent_options or {}).items():setattr(agent,key,value)
 original=agent.transport.handle_async_request
 request_index=0
 async def capture(request):
  nonlocal request_index
  request_index+=1
  captured.append(json.loads(request.content))
  response=await original(request)
  if request_index==1 and after_interpretation:after_interpretation()
  return response
 agent.transport.handle_async_request=capture
 with patch.object(lab_model_provider,'make_agent',return_value=agent):await service.run_agent(run['id'])
 return store.get('run',run['id'])
def purpose(value='discuss',**fields):return {'purpose':value,'task_actions':[],'memory_changes':[],**fields}
def memory_change(kind,content='',**fields):
 return {'kind':kind,'scope':'account','topic':'汇报表达','content':content,**fields}
def material(payload):
 text=next(m['content'] for m in payload['messages'] if '<materials>' in str(m.get('content','')))
 return json.loads(text.split('<materials>\\n',1)[1].split('\\n</materials>',1)[0])
'''


class CentralCapabilitiesTest(unittest.TestCase):
    def check(self, body, restart=None):
        result = run_guarded(SETUP + '\n' + body, restart_body=(SETUP + '\n' + restart) if restart else None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_reading_memory_is_real_evidence_without_a_business_inspection_or_write(self):
        self.check('''
async def main():
 text='以后给我的汇报先讲结论。'
 await chat(text,'memory-evidence-save',purpose('memory',memory_changes=[memory_change('remember','先讲结论。',evidence_quote=text)]))
 before=copy.deepcopy(store.items('central_memory'))
 result=await chat('请查一下我约定的汇报习惯。','memory-evidence-read',purpose('diagnose'),[
   backend.tool_message('central_memory_search',{'query':'汇报表达'}),'约定是先讲结论。'])
 assert result['status']=='succeeded' and result['analysis_complete'],result
 assert store.items('central_memory')==before
 assert result['memory_refs'] and result['diagnostic_evidence'][0]['tool']=='central_memory_search'
 assert not store.items('inspection_receipt')
asyncio.run(main())
''')

    def test_memory_new_thread_correction_and_forgetting_strip_old_model_context(self):
        self.check('''
async def main():
 text='以后给我的汇报先说结论，再给必要依据。'
 one=await chat(text,'remember',purpose('memory',memory_changes=[memory_change('remember','先说结论，再给必要依据。',evidence_quote=text)]))
 assert one['status']=='succeeded' and '已记住' in one['result'],one
 assert len(store.items('central_memory'))==1 and one['memory_ref_versions']
 before=len(captured)
 two=await chat('汇报应怎样写？','recall',purpose())
 assert two['status']=='succeeded',two
 assert '先说结论，再给必要依据。' in next(m['content'] for m in captured[before+1]['messages'] if '<central_memory>' in str(m.get('content','')))
 forget='忘掉汇报表达这条约定。'
 three=await chat(forget,'forget',purpose('memory',memory_changes=[memory_change('forget',evidence_quote=forget)]),thread=one['thread_id'])
 assert '忘记' in three['result'],three
 before=len(captured)
 four=await chat('继续讨论汇报表达','after-forget',purpose(),thread=one['thread_id'])
 text_after=json.dumps(captured[before+1],ensure_ascii=False)
 assert '先说结论，再给必要依据。' not in text_after,text_after
 assert memory.recall(admin,[pid],'汇报')==[]
 assert text in [m['content'] for m in store.items('message')]
 store.put('check','memory-owner',{'id':one['id']})
asyncio.run(main())
''', restart='''
assert memory.recall(admin,[pid],'汇报')==[]
assert len(store.items('central_memory'))==1
assert (root/'runtime/central/memory.md').is_file()
''')

    def test_complete_request_executes_once_with_real_task_service_and_typed_result(self):
        self.check('''
async def main():
 text='请给WorkBuddy下发“交付文字检查”：检查本工区短汇报的可读性，交付一份修订后的短汇报，无其他输入，写回协作记录。'
 proposed={'action':'create','project_id':pid,'evidence_quote':text,'needs_draft':False,
  'payload':{'title':'交付文字检查','goal':'检查本工区短汇报的可读性','deliverable':'修订后的短汇报',
   'executor_node_ids':['node_workbuddy']}}
 one=await chat(text,'dispatch',purpose('execute',task_actions=[proposed]),['THIS_FREE_TEXT_CANNOT_CLAIM_NODE_COMPLETED'])
 assert one['status']=='succeeded',one
 assert '已下发' in one['result'] and '等待节点' in one['result']
 assert 'THIS_FREE_TEXT' not in one['result']
 rows=transport.get_backend().list_tasks();assert len(rows)==1
 operations=[o for o in store.items('operation') if o.get('action')=='phase3_task:start']
 assert len(operations)==1 and operations[0]['authorization']['source']=='authenticated_user_request'
 plan=store.get('central_request_plan',one['request_plan'])
 current={**one,'status':'running'};store.put('run',one['id'],current)
 result=await service.execute_tool('phase3_execute_task_request',{'plan_id':plan['id'],'action_index':0},current,admin)
 assert result['ok'] and len(transport.get_backend().list_tasks())==1,result
 assert one['usage']['model_calls']==1 and one['usage']['tool_calls']==1
asyncio.run(main())
''')

    def test_draft_is_the_displayed_version_and_confirmed_in_same_conversation(self):
        self.check('''
async def main():
 text='先给WorkBuddy起草一个检查表达的任务，不要发布。'
 proposed={'action':'create','project_id':pid,'evidence_quote':text,'needs_draft':True,
   'payload':{'title':'检查表达','goal':'检查报告是否清楚','deliverable':'一份可读的改稿','executor_node_ids':['node_workbuddy']}}
 first=await chat(text,'draft-request',purpose('draft',task_actions=[proposed]))
 assert first['status']=='succeeded' and '一份可读的改稿' in first['result']
 assert transport.get_backend().list_tasks()==[]
 plan=store.get('central_request_plan',first['request_plan'])
 second=await chat('按这份安排发布。','confirm-request',purpose('execute',confirm_pending_id=plan['pending_id'],evidence_quote='按这份安排发布。'),thread=first['thread_id'])
 assert second['status']=='succeeded' and '已下发' in second['result'],second
 assert len(transport.get_backend().list_tasks())==1
asyncio.run(main())
''')

    def test_discussion_and_injected_model_tool_cannot_publish_or_claim_success(self):
        self.check('''
async def main():
 result=await chat('考虑一下是否要派任务，先不要发布。','discussion',purpose(),[
   backend.tool_message('phase3_execute_task_request',{'plan_id':'forged','action_index':0}),
   '我已经发布并让节点完成了。'])
 assert result['status'] in {'failed','denied'},result
 assert '我已经发布' not in result['result']
 assert transport.get_backend().list_tasks()==[] and not store.items('operation')
asyncio.run(main())
''')

    def test_diagnostic_actual_loop_is_readonly_and_keeps_health_failures(self):
        self.check('''
async def main():
 runtime.record_failure(pid,'tool','synthetic-source','read_failed','待核对','known-before-diagnostic')
 protected={kind:copy.deepcopy(store.items(kind)) for kind in ['project_health','operation','central_memory','state']}
 before={kind:copy.deepcopy(local.items(kind)) for kind in ['task_cache','draft','trusted_event']}
 query='只检查当前系统和代码，不修改任务或记忆。'
 result=await chat(query,'diagnose',purpose('diagnose',memory_changes=[memory_change('remember','不得写',evidence_quote=query)]),[
   backend.tool_message('central_inspect_overview',{}),
   backend.tool_message('central_inspect_code',{'path':'server/central_context/agent.md','start_line':1,'end_line':8}),
   backend.tool_message('central_inspect_database',{'entity':'tasks','schema':True}),
   '已检查当前任务、源码和数据库结构；已知异常仍待核对。'])
 assert result['status']=='succeeded',result
 assert all(store.items(k)==v for k,v in protected.items())
 assert all(local.items(k)==v for k,v in before.items())
 assert len(store.items('inspection_receipt'))==3
 assert result['usage']['model_calls']==5
 totals=[s['usage'] for s in result['steps'] if s['type']=='model_response']
 for key in ('prompt_tokens','completion_tokens','total_tokens'):
  assert result['usage'].get(key,0)==sum(u.get(key,0) for u in totals),result['usage']
 assert result['memory_refs']==[]
asyncio.run(main())
''')

    def test_pause_during_interpretation_prevents_direct_task_publication(self):
        self.check('''
async def main():
 text='请给WorkBuddy下发材料检查任务：检查本工区材料并提交短报告。'
 proposed={'action':'create','project_id':pid,'evidence_quote':text,'needs_draft':False,
  'payload':{'title':'材料检查','goal':'检查本工区材料','deliverable':'短报告','executor_node_ids':['node_workbuddy']}}
 def pause():
  state=store.get('state','runtime') or {};state['paused']=True;store.put('state','runtime',state)
 result=await chat(text,'pause-after-interpretation',purpose('execute',task_actions=[proposed]),after_interpretation=pause)
 assert result['status']=='paused',result
 assert transport.get_backend().list_tasks()==[]
 assert not [o for o in store.items('operation') if o.get('action')=='phase3_task:start']
 assert '已下发' not in result.get('result','')
asyncio.run(main())
''')

    def test_tool_budget_does_not_silently_drop_a_second_authorized_action(self):
        self.check('''
async def main():
 text='请分别给WorkBuddy下发甲、乙两项材料检查，各提交一份短报告。'
 proposed=[{'action':'create','project_id':pid,'evidence_quote':text,'needs_draft':False,
  'payload':{'title':'材料检查'+name,'goal':'检查'+name+'材料','deliverable':'短报告','executor_node_ids':['node_workbuddy']}}
  for name in ['甲','乙']]
 result=await chat(text,'limited-action-budget',purpose('execute',task_actions=proposed),agent_options={'max_tool_calls':1})
 assert result['status']!='succeeded',result
 assert result.get('analysis_complete') is not True
 assert '上限' in result.get('error','')+result.get('result','') or '未完成' in result.get('error','')+result.get('result',''),result
 actual=transport.get_backend().list_tasks()
 assert len(actual)<=1
 assert all(task['business_status']=='active' for task in actual)
 assert result.get('usage',{}).get('tool_calls',0)<=1
asyncio.run(main())
''')

    def test_diagnostic_cannot_claim_an_inspection_without_tool_evidence(self):
        self.check('''
async def main():
 protected={kind:copy.deepcopy(store.items(kind)) for kind in ['project_health','operation','central_memory','state']}
 fabricated='已检查全部代码和系统，完全正常。'
 result=await chat('只读检查当前代码和系统是否正常。','diagnose-no-evidence',purpose('diagnose'),[fabricated])
 assert result['status']!='succeeded',result
 assert result.get('analysis_complete') is not True
 assert fabricated not in result.get('result','')
 assert not store.items('inspection_receipt')
 assert all(store.items(kind)==value for kind,value in protected.items())
asyncio.run(main())
''')

    def test_incomplete_fresh_diagnostic_keeps_the_gap_instead_of_model_success(self):
        self.check('''
async def main():
 protected={kind:copy.deepcopy(store.items(kind)) for kind in ['project_health','operation','central_memory','state']}
 before={kind:copy.deepcopy(local.items(kind)) for kind in ['task_cache','draft','trusted_event']}
 # Both the trusted initial observation and the model's explicit re-read fail.
 # A genuinely recovered read would correctly supersede the earlier gap.
 transport.get_backend().fault('read_events',count=2)
 fabricated='最新飞书记录已完整核对，全部正常。'
 result=await chat('请只读检查最新飞书状态。','diagnose-incomplete',purpose('diagnose'),[
  backend.tool_message('central_inspect_overview',{'fresh':True}),fabricated])
 reads=[step.get('result',{}) for step in result['steps'] if step.get('type')=='tool_result']
 assert any(item.get('evidence',{}).get('complete') is False for item in reads),result
 assert result['status']!='succeeded' and result.get('analysis_complete') is not True,result
 assert fabricated not in result.get('result','')
 assert any(word in result.get('result','')+result.get('error','') for word in ['未完成','不完整','缺口','未完整']),result
 assert all(store.items(kind)==value for kind,value in protected.items())
 assert all(local.items(kind)==value for kind,value in before.items())
asyncio.run(main())
''')

    def test_memory_database_write_failure_is_not_a_successful_run(self):
        self.check('''
async def main():
 # Fault the real SQLite write, keeping permission and memory logic intact.
 with db.connection() as connection:
  connection.execute("CREATE TRIGGER fail_memory_insert BEFORE INSERT ON central_records WHEN NEW.kind='central_memory' BEGIN SELECT RAISE(ABORT,'synthetic memory storage failure'); END")
 text='以后给我的汇报先说结论，请记住。'
 result=await chat(text,'memory-storage-failure',purpose('memory',memory_changes=[
  memory_change('remember','先说结论',evidence_quote=text)]))
 assert result['status']!='succeeded',result
 assert result.get('analysis_complete') is not True
 assert '未保存' in result.get('result','')+result.get('error',''),result
 assert '已记住' not in result.get('result','')
 assert not store.items('central_memory') and not transport.get_backend().list_tasks()
asyncio.run(main())
''')

    def test_memory_search_preserves_displayed_version_and_rejects_stale_reference(self):
        self.check('''
async def main():
 first_text='以后汇报先说结论，请记住这条表达约定。'
 first=await chat(first_text,'display-initial-memory',purpose('memory',memory_changes=[
  memory_change('remember','先说结论',evidence_quote=first_text)]))
 key=store.items('central_memory')[0]['id']
 searched=await chat('你记住了哪些汇报表达约定？','display-memory-search',purpose('memory'),[
  backend.tool_message('central_memory_search',{'query':'汇报表达'}),'当前记住的是先说结论。'],thread=first['thread_id'])
 assert searched['status']=='succeeded',searched
 assert searched.get('displayed_memory_ids')==[key],searched
 assert searched.get('displayed_memory_versions')=={key:1},searched
 changed_text='把汇报表达约定改为先列事实，再说结论。'
 changed=await chat(changed_text,'display-concurrent-memory-change',purpose('memory',memory_changes=[
  memory_change('correct','先列事实，再说结论',memory_id=key,evidence_quote=changed_text)]))
 assert changed['status']=='succeeded',changed
 current=copy.deepcopy(store.get('central_memory',key));assert current['version']==2,current
 reference='把刚才那条改成只报三点。'
 stale=await chat(reference,'display-stale-reference',purpose('memory',memory_changes=[
  memory_change('correct','只报三点',topic='刚才那条',evidence_quote=reference)]),thread=first['thread_id'])
 assert stale['status']!='succeeded',stale
 assert any(word in stale.get('result','')+stale.get('error','') for word in ['变化','核对','冲突']),stale
 assert store.get('central_memory',key)==current
asyncio.run(main())
''')
