"""Shared decision summaries through the real guarded store and memory rules."""
import unittest

from tests.phase1_support import run_guarded
from tests.test_central_memory import SETUP as MEMORY_SETUP


SETUP = MEMORY_SETUP + '''
from server import central_recall as recall
def historical(message,reply,actor=admin,project=project_id,**fields):
 run=request(message,[],actor=actor,project=project,purpose='discuss')
 run.update(status='succeeded',completed_at=runtime.now(),**fields)
 store.put('run',run['id'],run)
 store.put('message',run['id']+':user',{'role':'user','run_id':run['id'],'thread_id':run['thread_id'],
  'project_id':project,'content':message,'created_at':run['completed_at']})
 store.put('message',run['id']+':assistant',{'role':'assistant','run_id':run['id'],'thread_id':run['thread_id'],
  'project_id':project,'content':reply,'created_at':run['completed_at'],
  'memory_refs':run.get('memory_refs',[]),'memory_ref_versions':run.get('memory_ref_versions',{})})
 return run
calls=[]
async def summarize(messages):
 calls.append(messages)
 payload=json.loads(messages[-1]['content']);key=payload['sources'][0]['source_key']
 return {'data':{'items':[{'decision':'先用小范围业务验证，再决定是否扩大。',
  'reason':'维护成本需要与工作收益相称。','conditions':'此结论是当时的判断参考。',
  'evidence_kind':'assistant_suggestion','source_keys':[key],
  'evidence_quote':'建议先从小范围验证开始'}]},'usage':{'model_calls':1},'model':'external-test-model'}
'''


class CentralRecallTest(unittest.TestCase):
    def check(self, body, restart=None):
        result = run_guarded(SETUP + '\n' + body,
                             restart_body=SETUP + '\n' + restart if restart else None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cross_account_business_summary_without_original_chat_or_private_links(self):
        self.check('''
other=store.create_user('recall-admin-b','管理员乙',['admin'],'temporary-admin-password')['user']
source=historical('scaleplan 的依据是什么？PRIVATE_RAW_SENTINEL https://private.example/thread/private /Users/private/raw.txt',
 '建议先从小范围验证开始，scaleplan 等真实收益清楚后再扩展。',actor=other)
current=request('回顾 scaleplan 的依据',[],purpose='discuss')
before={kind:store.items(kind) for kind in ('task','central_memory','operation')}
result=asyncio.run(recall.recall('scaleplan',current,admin,summarize))
assert result['ok'] and len(result['items'])==1 and len(calls)==1
body=json.dumps(result,ensure_ascii=False)
for forbidden in ('PRIVATE_RAW_SENTINEL','https://private.example',source['id'],source['thread_id'],source['message']):
 assert forbidden not in body,forbidden
assert result['items'][0]['evidence_kind']=='assistant_suggestion'
assert result['items'][0]['reference_only'] is True
assert '维护成本' in body and result['observed_at'] and result['coverage']['selected_conversations']==1
assert before=={kind:store.items(kind) for kind in before}
again=asyncio.run(recall.recall('scaleplan',current,admin,summarize))
assert again['coverage']['reused'] and len(calls)==1
# Sharing a derived judgment must not share the source thread API/history.
from server import central_service as service
assert service.interactive_history(current,admin)==[]
try:runtime.get_thread(source['thread_id'],admin)
except PermissionError:pass
else:raise AssertionError('derived recall exposed another account original thread')
assert source['thread_id'] not in {item['id'] for item in runtime.list_threads(admin,project_id)}
''')

    def test_project_environment_generation_and_run_kind_boundaries(self):
        self.check('''
historical('priorityplan 项目判断','建议先从小范围验证开始，priorityplan 应保持现范围。')
outside=historical('priorityplan 越界项目秘密','priorityplan 不应被返回。')
outside['project_id']='foreign-project';store.put('run',outside['id'],outside)
old=historical('priorityplan 旧代次秘密','priorityplan 不应被返回。',generation='0'*32)
scheduled=historical('priorityplan 定时秘密','priorityplan 不应被返回。',trigger='scheduler')
diagnostic=historical('priorityplan 诊断秘密','priorityplan 不应被返回。',purpose='diagnose')
current=request('priorityplan',[],purpose='discuss')
result=asyncio.run(recall.recall('priorityplan',current,admin,summarize))
assert result['coverage']['selected_conversations']==1
supplied=json.dumps(calls,ensure_ascii=False)
assert '越界项目秘密' not in supplied and '旧代次秘密' not in supplied
assert '定时秘密' not in supplied and '诊断秘密' not in supplied
for bad in ({**current,'allowed_projects':[project_id,'foreign-project']},
            {**current,'generation':'0'*32}):
 try:asyncio.run(recall.recall('priorityplan',bad,admin,summarize))
 except memory.MemoryError as exc:assert exc.status==403
 else:raise AssertionError('expanded recall scope accepted')
''')

    def test_project_recall_skips_wider_institution_conversation_and_full_scope_can_use_it(self):
        self.check('''
store.put('project','project-b',{'id':'project-b','name':'项目 B','member_ids':[]})
source=historical('outsideprojectplan 项目 B 的判断只适用于 B。',
 '建议先从小范围验证开始，outsideprojectplan 是项目 B 的判断。',project='')
source['allowed_projects']=[project_id,'project-b'];store.put('run',source['id'],source)
assert set(source['allowed_projects'])=={project_id,'project-b'}
limited=request('outsideprojectplan',[],purpose='discuss')
result=asyncio.run(recall.recall('outsideprojectplan',limited,admin,summarize))
assert result['ok'] and result['items']==[] and not calls
assert result['coverage'].get('excluded_broader_project_sources',0)>=1
whole=request('outsideprojectplan',[],project='',purpose='discuss')
whole={**whole,'allowed_projects':[project_id,'project-b']}
full=asyncio.run(recall.recall('outsideprojectplan',whole,admin,summarize))
assert full['ok'] and full['items'] and len(calls)==1
assert full['coverage']['selected_conversations']==1
assert '项目 B' in json.dumps(calls,ensure_ascii=False)
''')

    def test_no_match_does_not_invoke_model_and_current_memory_takes_priority(self):
        self.check('''
current=request('no-match-unique',[],purpose='discuss')
empty=asyncio.run(recall.recall('unmatchedtoken',current,admin,summarize))
assert empty['items']==[] and not calls and not store.items('central_conversation_index')
saved,_=apply('以后 briefstyle 先给结论。',[change('briefstyle','briefstyle 先给结论','以后 briefstyle 先给结论')])
historical('briefstyle 之前的讨论','建议先从小范围验证开始，briefstyle 可能再看。')
result=asyncio.run(recall.recall('briefstyle',current,admin,summarize))
assert result['coverage']['basis']=='current_memory' and not calls
assert result['items'][0]['decision']=='briefstyle 先给结论'
assert result['items'][0]['applicability']=={'kind':'account','account_id':admin['id']}
assert result['items'][0]['reference_only'] is True
''')

    def test_cache_reuse_after_process_restart(self):
        self.check('''
historical('restartplan 的依据','建议先从小范围验证开始，再决定 restartplan。')
current=request('restartplan',[],purpose='discuss')
first=asyncio.run(recall.recall('restartplan',current,admin,summarize))
assert not first['coverage']['reused'] and len(calls)==1
assert len(store.items('central_conversation_index'))==1
''',restart='''
current=request('restartplan',[],purpose='discuss')
second=asyncio.run(recall.recall('restartplan',current,admin,summarize))
assert second['ok'] and second['coverage']['reused'] and calls==[]
assert len(store.items('central_conversation_index'))==1
''')

    def test_forgetting_or_correcting_source_dependencies_invalidates_derived_summary(self):
        self.check('''
saved,origin=apply('以后表达先讲结果。',[change('表达','先讲结果','以后表达先讲结果')])
key=saved['changes'][0]['id']
source=historical('请回顾设计选择。','建议先从小范围验证开始，stalebasis 的依据是先讲结果。',
 memory_refs=[key],memory_ref_versions={key:1})
current=request('stalebasis',[],purpose='discuss')
first=asyncio.run(recall.recall('stalebasis',current,admin,summarize))
assert first['items'] and len(calls)==1
apply('表达改为先讲依据。',[change('表达','先讲依据','表达改为先讲依据',kind='correct')])
second=asyncio.run(recall.recall('stalebasis',current,admin,summarize))
assert second['items']==[] and len(calls)==1
apply('忘掉表达。',[change('表达','','忘掉表达',kind='forget')])
third=asyncio.run(recall.recall('stalebasis',current,admin,summarize))
assert third['items']==[] and len(calls)==1
''')

    def test_model_failure_or_revoked_actor_cannot_create_index(self):
        self.check('''
historical('failureplan 的依据','建议先从小范围验证开始，再决定 failureplan。')
current=request('failureplan',[],purpose='discuss')
async def broken(messages):raise RuntimeError('external summarizer unavailable')
try:asyncio.run(recall.recall('failureplan',current,admin,broken))
except RuntimeError:pass
else:raise AssertionError('failed summarizer falsely succeeded')
assert store.items('central_conversation_index')==[]
other=store.create_user('revoked-recall-admin','管理员乙',['admin'],'temporary-admin-password')['user']
current=request('failureplan',[],actor=other,purpose='discuss')
async def revoke(messages):
 result=await summarize(messages);store.update_user(other['id'],{'enabled':False});return result
try:asyncio.run(recall.recall('failureplan',current,other,revoke))
except memory.MemoryError as exc:assert exc.status==403
else:raise AssertionError('disabled actor created derived index')
assert store.items('central_conversation_index')==[]
''')

    def test_read_only_diagnostic_does_not_store_index_or_promote_suggestion_to_authority(self):
        self.check('''
source=historical('diagnosticplan 的依据','建议先从小范围验证开始，diagnosticplan 待定。')
current=request('diagnosticplan',[],purpose='diagnose')
before={kind:store.items(kind) for kind in ('task','central_memory','operation','health')}
async def unsupported_explicit(messages):
 result=await summarize(messages)
 result['data']['items'][0].update(evidence_kind='user_explicit',evidence_quote='这不是来源原话',
  decision='依据是 '+source['id']+' https://private.example/thread /Users/private/file.txt')
 return result
result=asyncio.run(recall.recall('diagnosticplan',current,admin,unsupported_explicit))
assert result['items'][0]['evidence_kind'] in {'inference','unknown'}
assert result['items'][0]['reference_only'] is True
body=json.dumps(result,ensure_ascii=False)
assert source['id'] not in body and 'https://private.example' not in body and '/Users/private' not in body
assert store.items('central_conversation_index')==[]
assert before=={kind:store.items(kind) for kind in before}
''')


if __name__ == '__main__':
    unittest.main()
