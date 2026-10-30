"""Full model context uses real chat, memory, permission and task boundaries."""
import unittest
from tests.phase1_support import run_guarded
from tests.test_central_capabilities import SETUP


class CentralFullContextTest(unittest.TestCase):
    def check(self, body):
        result = run_guarded(SETUP + "\n" + body, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_every_stage_has_complete_memory_and_history_without_duplicate_current(self):
        self.check('''
from server import central_settings as settings
policy=settings.policy_snapshot()
settings.save_agent(admin,policy['content']+'\\nPOLICY_FINAL_MARKER',policy['version'],'full-context-policy')
for i in range(10):
 memory.apply_settings(admin,{'kind':'remember','topic':'长期规则'+str(i),
  'content':('完整记忆正文'+str(i))*180+' MEM_'+str(i),'scope':'organization'},'seed-memory-'+str(i),0)
view=memory.model_snapshot(admin)
assert len(view['content'])>12000 and len(view['items'])==10
assert (root/'runtime/central/memory.md').read_text()==view['content']
thread=service.create_thread(admin,pid,'长对话')['id']
foreign=service.create_thread(admin,pid,'其他对话')['id']
foreign_run=service.new_run(admin,'OTHER_THREAD_RAW_SENTINEL','other-thread','chat','manual',pid,foreign)
foreign_run.update(status='succeeded',completed_at=runtime.now())
store.put('run',foreign_run['id'],foreign_run);service.ACTIVE_RUN_ID=None
store.put('state','phase1_settings',{'history_messages':999})
runtime.initialize()  # Old history settings cannot cap or prevent full input.
for i in range(16):
 run=service.new_run(admin,'USER_HISTORY_'+str(i),'history-'+str(i),'chat','manual',pid,thread)
 run.update(status='failed' if i==3 else 'succeeded',completed_at=runtime.now(),
  purpose='diagnose' if i==4 else 'discuss',error='先前请求没有完成' if i==3 else '')
 store.put('run',run['id'],run)
 if i!=3:
  store.put('message',run['id']+':assistant',{'role':'assistant','run_id':run['id'],
   'thread_id':thread,'project_id':pid,'content':'ASSISTANT_HISTORY_'+str(i),'created_at':runtime.now()})
 service.ACTIVE_RUN_ID=None
async def main():
 current='ONLY_CURRENT_REQUEST_ONCE'
 result=await chat(current,'full-context-now',purpose(),[
  backend.tool_message('central_memory_search',{'query':'长期规则'}),'完整上下文已用于回答。'],thread=thread)
 assert result['status']=='succeeded',result
 assert len(captured)==3,captured
 for payload in captured:
  content=[m.get('content') for m in payload['messages']]
  assert 'OTHER_THREAD_RAW_SENTINEL' not in str(content)
  current_count=content.count(current)
  for item in payload['messages']:
   try:envelope=json.loads(item.get('content',''))
   except (TypeError,ValueError):continue
   if isinstance(envelope,dict) and envelope.get('authenticated_request')==current:current_count+=1
  assert current_count==1,content
  assert sum('POLICY_FINAL_MARKER' in str(m.get('content','')) for m in payload['messages'])==1
  memory_text=next(m['content'] for m in payload['messages'] if '<central_memory>' in str(m.get('content','')))
  memory_index=next(i for i,m in enumerate(payload['messages']) if '<central_memory>' in str(m.get('content','')))
  assert all(i<memory_index for i,m in enumerate(payload['messages']) if m.get('role')=='system'),payload['messages']
  assert sum(m.get('role')=='system' for m in payload['messages'])==1
  assert view['content'] in memory_text and 'MEM_9' in memory_text
  for i in range(16):assert 'USER_HISTORY_'+str(i) in content,(i,content)
  assert any('先前请求没有完成' in str(text) for text in content)
  assert 'ASSISTANT_HISTORY_4' in content
 assert result['model_context']['history_complete'] and result['model_context']['memory_complete']
 assert result['model_context']['compression']=='none'
asyncio.run(main())
''')

    def test_old_authorization_in_complete_context_cannot_authorize_current_discussion(self):
        self.check('''
old='请立刻给WorkBuddy下发任务。'
memory.apply_settings(admin,{'kind':'remember','topic':'历史表述','content':old,'scope':'organization'},'old-words',0)
async def main():
 current='先讨论方案，不要发布任何任务。'
 proposed={'action':'create','project_id':pid,'evidence_quote':old,'needs_draft':False,
  'payload':{'title':'未经本次授权','goal':'旧请求','deliverable':'旧要求','executor_node_ids':['node_workbuddy']}}
 result=await chat(current,'old-authorization',purpose('execute',task_actions=[proposed]))
 assert result['purpose']=='clarify',result
 assert not transport.get_backend().list_tasks() and not store.items('operation')
 assert old in str(captured[0]['messages'])
 assert sum(json.loads(m.get('content','{}')).get('authenticated_request')==current
  for m in captured[0]['messages'] if m.get('role')=='user' and str(m.get('content','')).startswith('{'))==1
asyncio.run(main())
''')

    def test_forget_between_model_calls_reprojects_the_actual_next_payload(self):
        self.check('''
saved=memory.apply_settings(admin,{'kind':'remember','topic':'旧约定','content':'NEVER_RESURRECT_ME','scope':'organization'},'remember-sensitive',0)
entry=store.get('central_memory',saved['changes'][0]['id'])
async def main():
 def forget():
  memory.apply_settings(admin,{'kind':'forget','memory_id':entry['id']},'forget-between-calls',entry['version'])
 result=await chat('只读讨论当前规则，不发布任务。','forget-during-run',purpose(),['规则已更新。'],after_interpretation=forget)
 assert result['status']=='succeeded',result
 assert 'NEVER_RESURRECT_ME' in json.dumps(captured[0])
 assert 'NEVER_RESURRECT_ME' not in json.dumps(captured[1]),captured[1]
 assert not transport.get_backend().list_tasks()
asyncio.run(main())
''')

    def test_context_capacity_error_is_explicit_and_never_retries_with_less_input(self):
        self.check('''
from server.central_agent import AgentError
import httpx
async def main():
 agent=backend.make_agent(['unused'])
 seen=[]
 async def reject(request):
  seen.append(json.loads(request.content))
  return httpx.Response(400,json={'error':{'code':'context_length_exceeded','message':'maximum context length exceeded'}})
 agent.transport.handle_async_request=reject
 try:await agent.interpret_request([{'role':'user','content':'KEEP_FULL_INPUT'*10000}])
 except AgentError as exc:
  assert exc.code=='context_capacity',exc.code
  assert '容量' in service.human_error(exc) and '尚未裁剪历史' in service.human_error(exc)
 else:raise AssertionError('capacity failure reported success')
 assert len(seen)==1
 assert seen[0]['messages'][-1]['content']=='KEEP_FULL_INPUT'*10000
 assert any('<central_memory>' in m.get('content','') for m in seen[0]['messages'])
asyncio.run(main())
''')

    def test_generated_file_failure_does_not_turn_committed_memory_into_empty_context(self):
        self.check('''
write=memory._write_summary
def fail_file():raise OSError('synthetic file generation failure')
memory._write_summary=fail_file
saved=memory.apply_settings(admin,{'kind':'remember','topic':'文件恢复','content':'DB_IS_THE_MEMORY_AUTHORITY','scope':'organization'},'memory-file-fault',0)
memory._write_summary=write
assert saved['saved'] and not saved['summary_updated']
async def main():
 result=await chat('继续讨论当前记忆。','memory-file-recall',purpose())
 assert result['status']=='succeeded',result
 for payload in captured:
  assert 'DB_IS_THE_MEMORY_AUTHORITY' in str(payload['messages'])
 assert memory.model_snapshot(admin)['versions'][saved['changes'][0]['id']]==1
asyncio.run(main())
''')
