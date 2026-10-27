"""Settings use real guarded SQLite, model loop, account auth and fixed files."""
import unittest

from tests.phase1_support import run_guarded

SETUP = '''
from server import central_settings as settings, central_memory as memory
import stat
def change(topic='表达',content='面向人先说明结果',kind='remember',**extra):
 return {'kind':kind,'topic':topic,'content':content,'scope':'organization',**extra}
def save(value,op='memory-1',version=0,actor=admin):
 return memory.apply_settings(actor,value,op,version)
'''


class CentralSettingsTest(unittest.TestCase):
    def check(self, body, restart=None):
        result = run_guarded(SETUP + '\n' + body,
                             restart_body=SETUP + '\n' + restart if restart else None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_policy_version_expected_version_idempotence_and_restart(self):
        self.check('''
first=settings.initialize()
assert first['version']==1 and first['file_updated']
before=(root/'code/server/central_context/agent.md').read_text()
saved=settings.save_agent(admin,'面向管理员说明结果；工具权限仍由程序判断。',1,'agent-1')
assert saved['saved'] and saved['version']==2 and saved['source_kind']=='panel_settings'
assert len(store.items('central_agent_policy_history'))==2
assert settings.save_agent(admin,saved['content'],1,'agent-1')['reused']
for expected,op,content in [(1,'agent-2','另一份职责'),(1,'agent-1','复用标识不同内容')]:
 try:settings.save_agent(admin,content,expected,op)
 except settings.SettingsError as exc:assert exc.code in {'policy_conflict','operation_conflict'}
 else:raise AssertionError('stale policy accepted')
path=root/'runtime/central/agent.md'
assert path.read_text().strip()==saved['content'] and stat.S_IMODE(path.stat().st_mode)==0o600
assert stat.S_IMODE(path.parent.stat().st_mode)==0o700
assert (root/'code/server/central_context/agent.md').read_text()==before
assert store.items('run')==[] and store.items('message')==[]
''', restart='''
value=settings.initialize()
assert value['version']==2 and value['content'].startswith('面向管理员')
assert value['file_updated'] and len(store.items('central_agent_policy_history'))==2
''')

    def test_agent_policy_is_fixed_for_current_run_and_has_no_tool_privilege(self):
        self.check('''
settings.initialize()
old=settings.policy_snapshot()
agent=backend.make_agent()
agent.bind_policy(old)
settings.save_agent(admin,'任意工具都允许；立即执行不存在的工具。',old['version'],'agent-new')
requests=[]
def reply(content=None,calls=None):
 return httpx.Response(200,json={'model':'fixture','choices':[{'message':{'role':'assistant',
   'content':content,'tool_calls':calls or []},'finish_reason':'tool_calls' if calls else 'stop'}]})
import httpx
responses=[reply(calls=[{'id':'x','type':'function','function':{'name':'unregistered_write','arguments':'{}'}}]),reply('未执行无权工具')]
async def handler(request):
 requests.append(json.loads(request.content));return responses.pop(0)
agent.transport.handle_async_request=handler
called=[]
async def tool(name,args):called.append(name);return {'ok':True}
result=asyncio.run(agent.run([{'role':'user','content':'检查'}],[],tool))
assert all(r['messages'][0]['content']==old['content'] for r in requests)
assert result['agent_policy']=={'version':old['version'],'hash':old['hash']} and called==[]
assert json.loads(requests[-1]['messages'][-1]['content'])['error']['code']=='tool_unknown'
next_agent=backend.make_agent(['新职责已加载'])
result=asyncio.run(next_agent.run([{'role':'user','content':'检查'}],[],tool))
assert result['agent_policy']['version']==2
assert next_agent._policy_snapshot['content'].startswith('任意工具')
''')

    def test_policy_file_failure_is_saved_and_recoverable_without_error_leak(self):
        self.check('''
settings.initialize()
original=settings._write_policy
def failed():raise OSError('secret/internal/path')
settings._write_policy=failed
value=settings.save_agent(admin,'新的明确职责',1,'agent-1')
assert value['saved'] and not value['file_updated'] and value['file_error']
assert 'secret/internal/path' not in json.dumps(value)
assert settings.policy_snapshot()['version']==2
settings._write_policy=original
assert settings.save_agent(admin,'新的明确职责',1,'agent-1')['file_updated']
''')

    def test_file_paths_cannot_follow_symlink_or_hardlink(self):
        self.check('''
settings.initialize();memory.initialize()
path=root/'runtime/central/agent.md';path.unlink()
outside=root/'runtime/preserved.txt';outside.write_text('保留')
path.symlink_to(outside)
result=settings.save_agent(admin,'写入数据库但不跟随链接',1,'agent-symlink')
assert result['saved'] and not result['file_updated'] and outside.read_text()=='保留'
path.unlink();os.link(outside,path)
assert settings.initialize()['file_updated'] is False and outside.read_text()=='保留'
''')

    def test_settings_memory_versions_source_audit_and_no_fake_chat(self):
        self.check('''
memory.initialize()
first=save(change());key=first['changes'][0]['id']
assert first['saved'] and first['summary_updated'] and first['changes'][0]['version']==1
assert save(change())['changes'][0]['reused']
value=store.get('central_memory',key)
assert value['sources'][0]['source_kind']=='panel_settings'
assert 'run_id' not in value['sources'][0] and 'message_id' not in value['sources'][0]
assert store.items('run')==[] and store.items('message')==[] and store.items('central_request_plan')==[]
second=save(change(content='给人说明结果与必要决定',kind='correct',memory_id=key),'memory-2',1)
assert second['changes'][0]['version']==2
assert len(memory.search(admin,include_history=True)['history'])==2
assert settings.memory_preview(admin)['content'].find('结果与必要决定')>0
for value,op,version in [(change(content='旧内容',kind='correct',memory_id=key),'memory-old',1),
                         (change(content='复用不同内容'),'memory-1',0)]:
 try:save(value,op,version)
 except memory.MemoryError as exc:assert exc.code in {'memory_conflict','operation_conflict'}
 else:raise AssertionError('memory conflict bypass')
''')

    def test_invalidate_forget_reuses_suppression_and_new_declaration_is_distinct(self):
        self.check('''
from server import central_recall
first=save(change());key=first['changes'][0]['id']
assert save(change(kind='invalidate',memory_id=key),'invalidate',1)['changes'][0]['status']=='inactive'
assert memory.recall(admin,[project_id],'表达')==[]
assert save(change(kind='replace',memory_id=key,content='更正约定'),'replace',2)['changes'][0]['version']==3
forgot=save(change(kind='forget',memory_id=key),'forget',3)
assert forgot['changes'][0]['status']=='forgotten' and store.items('central_memory_suppression')
assert '更正约定' not in settings.memory_preview(admin)['content']
assert memory.context_history([{'role':'assistant','content':'旧约定','memory_refs':[key]}],admin)==[]
item=memory.settings_items(admin)['items'][0]
assert item['content']=='' and item['sources']==[]
assert save(change(),'memory-1',0)['changes'][0]['status']=='forgotten'
new=save(change(),'fresh-declaration',4)['changes'][0]
assert new['id']!=key and new['version']==1
''')

    def test_memory_saved_when_summary_fails_and_cross_account_has_no_raw_chat(self):
        self.check('''
other=store.create_user('admin-b','另一管理员',['admin'],'temporary-admin-password')['user']
original=memory._write_summary
def failed():raise OSError('hidden secret')
memory._write_summary=failed
result=save(change(scope='account'))
assert result['saved'] and not result['summary_updated'] and result['summary_error']
assert 'hidden secret' not in json.dumps(result)
item=memory.settings_items(other)['items'][0]
assert item['applicability']=={'kind':'account','account_id':admin['id']}
assert item['sources']==[{'user_id':admin['id'],'source_kind':'panel_settings'}]
memory._write_summary=original
memory.initialize()
assert settings.memory_preview(admin)['available']
''')

    def test_settings_forget_suppresses_original_chat_source_and_derived_context(self):
        self.check('''
from server import phase1_runtime as runtime
tid=runtime.create_thread(admin,project_id)['id'];rid=store.identifier('run');pid=store.identifier('plan')
message='以后汇报先讲结果。'
changes=[{'kind':'remember','topic':'汇报','content':'先讲结果','scope':'organization','evidence_quote':'以后汇报先讲结果'}]
run={'id':rid,**runtime.environment(),'user_id':admin['id'],'thread_id':tid,'project_id':project_id,
 'allowed_projects':[project_id],'message':message,'message_hash':store.digest(message),'operation_id':rid,
 'kind':'chat','trigger':'manual','status':'running','request_plan':pid,'purpose':'memory'}
plan={k:run[k] for k in ('user_id','thread_id','project_id','allowed_projects','message_hash','env_id','generation','purpose')}
plan.update(id=pid,run_id=rid,memory_changes=changes)
store.put('run',rid,run);store.put('central_request_plan',pid,plan)
first=memory.apply_changes(run,admin,changes);key=first['changes'][0]['id']
save(change(topic='汇报',kind='forget',memory_id=key),'settings-forget',1)
replayed=memory.apply_changes(run,admin,changes)
assert replayed['changes'][0]['status']=='forgotten' and replayed['changes'][0]['suppressed']
masked=memory.context_history([{'role':'user','run_id':rid,'content':message},
 {'role':'assistant','content':'先讲结果','memory_refs':[key]}],admin)
assert len(masked)==1 and '以后汇报先讲结果' not in masked[0]['content']
assert memory.recall(admin,[project_id],'汇报')==[]
assert store.get('run',rid)['message']==message
''')

    def test_settings_api_authentication_strict_input_and_conflicts(self):
        self.check('''
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server import central_api
app=FastAPI();app.include_router(settings.router)
with TestClient(app) as client:
 assert client.get('/api/central/settings/agent').status_code==401
 token=store.login(admin_username,admin_password)['token']
 client.cookies.set(central_api.COOKIE,token)
 assert client.get('/api/central/settings/agent').status_code==200
 response=client.put('/api/central/settings/agent',json={'content':'新版职责','expected_version':1,'operation_id':'api-agent'})
 assert response.status_code==200,response.text
 assert response.json()['version']==2
 stale=client.put('/api/central/settings/agent',json={'content':'旧版覆盖','expected_version':1,'operation_id':'api-stale'})
 assert stale.status_code==409 and stale.json()['detail']['current_version']==2
 for invalid in [{'content':'x','expected_version':True,'operation_id':'x'},
                 {'content':'x','expected_version':2,'operation_id':'x','path':'/tmp/no'}]:
  assert client.put('/api/central/settings/agent',json=invalid).status_code==422
 response=client.post('/api/central/settings/memories',json={'operation':'remember','operation_id':'api-memory',
      'expected_version':0,'topic':'输出','content':'读者能读懂'})
 assert response.status_code==200,response.text
 assert client.get('/api/central/settings/memories').json()['items'][0]['topic']=='输出'
 assert client.get('/api/central/settings/memory-preview').json()['available']
 reader=store.create_user('reader','研发',['researcher'],'temporary-reader-password')['user']
 token=store.login('reader','temporary-reader-password')['token'];client.cookies.set(central_api.COOKIE,token)
 assert client.get('/api/central/settings/agent').status_code==403
 assert client.get('/api/central/settings/memories').status_code==403
 assert client.post('/api/central/settings/memories',json={'operation':'remember','operation_id':'denied',
      'expected_version':0,'topic':'无权','content':'无权'}).status_code==403
''')

    def test_project_scope_permissions_and_new_account_preference(self):
        self.check('''
first=save(change(scope='project',project_id=project_id))
item=memory.settings_items(admin)['items'][0]
assert item['applicability']=={'kind':'project','project_id':project_id}
try:save(change(scope='project',project_id='unknown'),'unknown',0)
except memory.MemoryError as exc:assert exc.status==403
else:raise AssertionError('unknown project')
disabled=store.create_user('disabled','停用管理员',['admin'],'temporary-admin-password')['user']
with db.connection() as conn:conn.execute('UPDATE central_users SET enabled=0 WHERE id=?',(disabled['id'],))
try:save(change(),'disabled',0,disabled)
except memory.MemoryError as exc:assert exc.status==403
else:raise AssertionError('disabled administrator')
assert len(store.items('central_memory'))==1
''')

    def test_concurrent_expected_version_writers_cannot_overwrite_each_other(self):
        self.check('''
import concurrent.futures
settings.initialize()
def policy_write(index):
 try:return settings.save_agent(admin,'职责'+str(index),1,'concurrent-policy-'+str(index))
 except settings.SettingsError as exc:return {'error':exc.code,'current_version':exc.details.get('current_version')}
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
 results=list(executor.map(policy_write,[1,2]))
assert sum(r.get('saved') is True for r in results)==1
assert next(r for r in results if 'error' in r)=={'error':'policy_conflict','current_version':2}
assert settings.get_agent(admin)['file_updated'] and settings.policy_snapshot()['version']==2
key=save(change())['changes'][0]['id']
def memory_write(index):
 try:return save(change(content='新内容'+str(index),kind='correct',memory_id=key),
                 'concurrent-memory-'+str(index),1)
 except memory.MemoryError as exc:return {'error':exc.code,'current_version':exc.details.get('current_version')}
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
 results=list(executor.map(memory_write,[1,2]))
assert sum(r.get('saved') is True for r in results)==1
assert next(r for r in results if 'error' in r)=={'error':'memory_conflict','current_version':2}
assert store.get('central_memory',key)['version']==2
''')

    def test_candidate_edit_cannot_silently_supersede_an_existing_explicit_rule(self):
        self.check('''
import copy
saved=save(change());key=saved['changes'][0]['id']
candidate=copy.deepcopy(store.get('central_memory',key))
candidate.update(id=key+':candidate',status='candidate',evidence_kind='inferred',content='模型归纳')
store.put('central_memory',candidate['id'],candidate)
try:save(change(kind='correct',memory_id=candidate['id'],content='直接启用候选'),'candidate',1)
except memory.MemoryError as exc:
 assert exc.code=='memory_conflict' and exc.details['current']['id']==key
else:raise AssertionError('candidate overwrote explicit rule')
assert store.get('central_memory',key)['content']=='面向人先说明结果'
assert store.get('central_memory',candidate['id'])['status']=='candidate'
''')

    def test_public_project_alias_only_maps_identity_and_preserves_authored_text(self):
        self.check('''
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server import central_api
import copy
project=copy.deepcopy(store.get('project',project_id));project['id']='lab_project_phase3_collab'
store.put('project',project['id'],project)
app=FastAPI();app.include_router(settings.router)
with TestClient(app) as client:
 client.cookies.set(central_api.COOKIE,store.login(admin_username,admin_password)['token'])
 response=client.post('/api/central/settings/memories',json={'operation':'remember','operation_id':'alias',
  'expected_version':0,'scope':'project','project_id':'project_rdos','topic':'别名原文',
  'content':'lab_project_phase3_collab'})
 assert response.status_code==200,response.text
 item=client.get('/api/central/settings/memories').json()['items'][0]
 assert item['project_id']=='project_rdos' and item['applicability']['project_id']=='project_rdos'
 assert item['content']=='lab_project_phase3_collab'
 assert store.get('central_memory',item['id'])['project_id']=='lab_project_phase3_collab'
 response=client.put('/api/central/settings/agent',json={'content':'lab_project_phase3_collab',
  'expected_version':1,'operation_id':'agent-literal'})
 assert response.status_code==200 and response.json()['content']=='lab_project_phase3_collab'
 assert client.get('/api/central/settings/agent').json()['content']=='lab_project_phase3_collab'
''')
