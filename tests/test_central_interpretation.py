"""The request-only model stage shares the real guarded model loop budget."""
import unittest
from tests.phase1_support import run_guarded


class CentralInterpretationTest(unittest.TestCase):
    def check(self, body):
        result = run_guarded(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_outer_budget_keeps_model_timeout_cause_before_any_action(self):
        self.check('''
from server import central_service as service
run={'kind':'chat','steps':[{'type':'model_request','stage':'request_interpretation'},
    {'type':'retry','stage':'request_interpretation','code':'model_timeout'},
    {'type':'model_request','stage':'request_interpretation'}]}
assert service.model_interpretation_timed_out(run,asyncio.TimeoutError())
assert not service.model_interpretation_timed_out(run,ValueError('other failure'))
assert not service.model_interpretation_timed_out({**run,'kind':'sync'},asyncio.TimeoutError())
assert not service.model_interpretation_timed_out({**run,'steps':run['steps']+[{'type':'tool_call'}]},asyncio.TimeoutError())
''')

    def test_policy_is_loaded_and_parser_budget_is_carried_into_tool_loop(self):
        self.check('''
from server.central_agent import SYSTEM_PROMPT
assert SYSTEM_PROMPT == (root/'code/server/central_context/agent.md').read_text().strip()
assert '当前认证用户明确要求' in SYSTEM_PROMPT
seen=[]
agent=backend.make_agent([json.dumps({'purpose':'discuss','task_actions':[],'memory_changes':[]}), '正常业务回答'])
original=agent.transport.handle_async_request
async def capture(request):
 seen.append(json.loads(request.content))
 return await original(request)
agent.transport.handle_async_request=capture
async def main():
 parsed=await agent.interpret_request([{'role':'system','content':'独立请求解析'}, {'role':'user','content':'当前真实请求'}])
 assert parsed['data']['purpose']=='discuss' and parsed['usage']['model_calls']==1
 result=await agent.run([{'role':'user','content':'业务问答'}],[],lambda *_:None)
 assert result['usage']['model_calls']==2
 assert len(seen)==2 and 'tools' not in seen[0]
 assert seen[0]['messages'][0]['content'].startswith(SYSTEM_PROMPT)
 assert '<current_stage>\\n独立请求解析\\n</current_stage>' in seen[0]['messages'][0]['content']
 assert sum(m['role']=='system' for m in seen[0]['messages'])==1
 assert seen[0]['messages'][-1]=={'role':'user','content':'当前真实请求'}
 assert any('<central_memory>' in str(m.get('content','')) for m in seen[0]['messages'])
 assert seen[1]['messages'][0]['content']==SYSTEM_PROMPT
asyncio.run(main())
''')

    def test_parser_does_not_execute_a_tool_or_accept_invalid_response(self):
        self.check('''
from server.central_agent import AgentError
async def main():
 for response in ['not json','[]',backend.tool_message('phase3_execute_task_request',{'approved':True})]:
  agent=backend.make_agent([response])
  try:await agent.interpret_request([{'role':'user','content':'先讨论，不发布'}])
  except AgentError as exc:assert exc.code=='request_understanding_failed'
  else:raise AssertionError('invalid request interpretation accepted')
asyncio.run(main())
''')

    def test_interpretation_does_not_create_an_extra_model_budget(self):
        self.check('''
from server.central_agent import AgentError
async def main():
 agent=backend.make_agent([json.dumps({'purpose':'discuss'}),'must not be called'])
 agent.max_model_calls=1
 await agent.interpret_request([{'role':'user','content':'你好'}])
 try:await agent.run([{'role':'user','content':'继续'}],[],lambda *_:None)
 except AgentError as exc:assert exc.code=='model_limit'
 else:raise AssertionError('interpretation budget was reset')
 assert agent.transport.index==1
asyncio.run(main())
''')
