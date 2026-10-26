"""Visible acceptance and private chats with shared Central references."""
import unittest
from tests.phase1_support import run_guarded


class ConversationReferenceTest(unittest.TestCase):
    def check(self, body):
        result=run_guarded(body)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_user_message_exists_before_interpretation_and_is_not_duplicated(self):
        self.check('''
from server import central_service as service,phase1_runtime as runtime,phase3_service
runtime.initialize();phase3_service.initialize()
pid=phase3_service.PROJECT_ID
thread=service.create_thread(admin,pid,'即时显示')
run=service.new_run(admin,'先查看我的问题。','accepted-before-model',project_id=pid,thread_id=thread['id'])
item=store.get('message',run['id']+':user')
assert item['content']=='先查看我的问题。' and item['operation_id']==run['operation_id']
assert item['thread_id']==thread['id'] and item['created_at']==run['created_at']
again=service.new_run(admin,'先查看我的问题。','accepted-before-model',project_id=pid,thread_id=thread['id'])
service.record_user_message(again)
assert len(store.items('message'))==1 and store.get('message',run['id']+':user')==item
assert not run.get('usage') and not store.items('operation')
''')

    def test_shared_reference_does_not_open_other_accounts_raw_chat_or_authorization(self):
        self.check('''
from server import central_service as service,phase1_runtime as runtime,phase3_service,central_api
runtime.initialize();phase3_service.initialize()
pid=phase3_service.PROJECT_ID
other=store.create_user('other-admin','另一管理员',['admin'],'temporary-passphrase')['user']
thread=service.create_thread(admin,pid,'原始私有会话')
run=service.new_run(admin,'PRIVATE_RAW_CONVERSATION','private-conversation',project_id=pid,thread_id=thread['id'])
assert service.list_threads(other,pid)==[]
try:service.get_thread(thread['id'],other,pid)
except PermissionError:pass
else:raise AssertionError('raw thread exposed')
assert central_api.visible_run(run,other) is False
try:central_api.require_run(run['id'],other)
except Exception as exc:assert exc.status_code==403
else:raise AssertionError('raw run exposed')
assert not store.items('operation')
''')

    def test_history_summary_uses_the_same_model_budget_and_can_not_hide_a_call(self):
        self.check('''
from server.central_agent import AgentError
spec=[{'type':'function','function':{'name':'lookup_history','parameters':{'type':'object','properties':{}}}}]
async def main():
 for budget in (3,4):
  agent=backend.make_agent([json.dumps({'purpose':'discuss'}),
   backend.tool_message('lookup_history',{}),json.dumps({'items':[{'decision':'历史建议'}]}),'有依据的业务答复'])
  agent.max_model_calls=budget
  await agent.interpret_request([{'role':'user','content':'当前请求'}])
  async def execute(name,args):
   value=await agent.interpret_request([{'role':'user','content':'内部历史归纳'}],stage='history_recall')
   return {'ok':True,**value['data']}
  try:
   result=await agent.run([{'role':'user','content':'以前如何判断？'}],spec,execute)
  except AgentError as exc:
   assert budget==3 and exc.code=='model_limit'
   assert agent.transport.index==3
  else:
   assert budget==4 and result['usage']['model_calls']==4 and result['usage']['tool_calls']==1,result
  assert agent._active_usage is None
asyncio.run(main())
''')
