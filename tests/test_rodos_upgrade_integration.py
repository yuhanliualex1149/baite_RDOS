"""Rodos upgrade boundaries through the actual Central tools and task services.

Only the remote source responses and model responses are synthetic. The guard,
request context, tool dispatch, maintenance owner, source snapshots and output
projection all execute as application code in an isolated database.
"""
from __future__ import annotations
import unittest
from tests.phase1_support import run_guarded
from tests.test_central_capabilities import SETUP


class RodosUpgradeIntegrationTest(unittest.TestCase):
    def check(self, body):
        result = run_guarded(SETUP + '\n' + body, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_execute_tool_starts_queries_and_deduplicates_scoped_refresh(self):
        self.check('''
from server import central_maintenance as maintenance,rdos_projects
async def scenario():
 other=rdos_projects.create_project({'name':'其他项目','description':'只用于范围核对','node_ids':[]},admin,'other-project-maintenance')
 thread=service.create_thread(admin,pid,'刷新工作进展')
 run=service.new_run(admin,'请刷新当前项目的飞书状态。','tool-refresh','chat','manual',pid,thread['id'])
 first=await service.execute_tool('central_refresh_feishu',{},run,admin)
 assert first['ok'],first
 identifier=first['maintenance_run']['id']
 saved=store.get('maintenance_run',identifier)
 assert saved['project_ids']==[pid] and saved['identity']=='system:maintenance'
 assert saved['status'] in {'queued','running'}
 again=await service.execute_tool('central_refresh_feishu',{},run,admin)
 assert again['ok'] and again['maintenance_run']['id']==identifier
 assert len(store.items('maintenance_run'))==1
 queried=await service.execute_tool('central_maintenance_status',{'run_id':identifier},run,admin)
 assert queried['ok'] and queried['maintenance_run']['id']==identifier
 forged={**run,'allowed_projects':[pid,other['id']]}
 denied=await service.execute_tool('central_refresh_feishu',{},forged,admin)
 assert not denied['ok'] and denied['code']=='authorization_failed',denied
 result=await maintenance.wait(identifier)
 assert result['status'] in {'succeeded','partial'},result
 # Status queries cannot be used to expose another project's maintenance data.
 outside=await maintenance.request_run('refresh',admin,'outside-refresh',[other['id']])
 await maintenance.wait(outside['id'])
 denied=await service.execute_tool('central_maintenance_status',{'run_id':outside['id']},run,admin)
 assert not denied['ok'] and '范围' in denied['error'],denied
 # Callers cannot use the tool as a generic arbitrary-resource reader.
 count=len(store.items('maintenance_run'))
 denied=await service.execute_tool('central_refresh_feishu',{'project_id':other['id'],'url':'https://invalid.example'},run,admin)
 assert not denied['ok'] and len(store.items('maintenance_run'))==count
 assert transport.get_backend().list_tasks()==[] and memory.recall(admin,[pid],'')==[]
asyncio.run(scenario())
''')

    def test_explicit_readonly_omits_and_rejects_mutating_refresh_even_if_model_calls_it(self):
        self.check('''
from server import central_maintenance as maintenance
async def scenario():
 thread=service.create_thread(admin,pid,'只读观察')
 run=service.new_run(admin,'只读检查，不要刷新，不处理回执。','read-only-refresh','chat','manual',pid,thread['id'])
 names={item['function']['name'] for item in service.tool_specs(run)}
 assert 'central_maintenance_status' in names
 assert not {'central_refresh_feishu','central_start_inspection'} & names
 before=copy.deepcopy(store.items('maintenance_run'))
 tasks_before=transport.get_backend().list_tasks()
 events_before=transport.get_backend().list_events()
 for name in ('central_refresh_feishu','central_start_inspection'):
  denied=await service.execute_tool(name,{},run,admin)
  assert not denied['ok'],denied
 assert store.items('maintenance_run')==before
 assert transport.get_backend().list_tasks()==tasks_before and transport.get_backend().list_events()==events_before
 service.ACTIVE_RUN_ID=None
 # The real model loop must not override an explicit read-only instruction.
 result=await chat('只读检查，不要刷新，不处理回执。','readonly-hard-call',purpose('diagnose'),[
   backend.tool_message('central_refresh_feishu',{}),'全部同步并处理完毕。'])
 assert result['status'] in {'failed','denied'},result
 assert '全部同步并处理完毕' not in result['result']
 assert store.items('maintenance_run')==before
asyncio.run(scenario())
''')

    def test_actual_central_loop_projects_started_job_status_over_model_completion_claim(self):
        self.check('''
import threading
from server import central_maintenance as maintenance
async def scenario():
 transport_backend=transport.get_backend()
 actual_list=transport_backend.list_tasks
 release=threading.Event()
 def held_remote_read():
  if not release.wait(3):raise ValueError('synthetic-read-not-released')
  return actual_list()
 transport_backend.list_tasks=held_remote_read
 try:
  result=await chat('请刷新当前项目的飞书工作状态。','refresh-through-real-loop',purpose(),[
    backend.tool_message('central_refresh_feishu',{}),
    'BAD_MODEL_CLAIM：全量刷新已经完成，所有任务均已验收。'])
  assert result['status']=='succeeded',result
  assert result.get('analysis_complete') is False
  assert result.get('maintenance_complete') is False
  assert result.get('maintenance_run_ids')
  assert 'BAD_MODEL_CLAIM' not in result['result'] and '均已验收' not in result['result']
  assert '已启动' in result['result'] and '尚未完成' in result['result'],result
  assert result['usage']['tool_calls']==1 and result['usage']['model_calls']==3,result['usage']
  assert any(step.get('name')=='central_refresh_feishu' for step in result['steps'])
  job=store.get('maintenance_run',result['maintenance_run_ids'][0])
  assert job['status'] in {'queued','running'}
 finally:
  release.set()
 for identifier in result.get('maintenance_run_ids',[]):
  await maintenance.wait(identifier)
 assert actual_list()==[] and transport_backend.list_events()==[]
 assert not store.items('central_memory')
asyncio.run(scenario())
''')

    def test_project_directory_new_read_partial_preservation_and_removed_default_scope(self):
        self.check('''
from server import central_maintenance as maintenance,rdos_projects
async def scenario():
 transport_backend=transport.get_backend()
 other=rdos_projects.create_project({'name':'另一工作项目','description':'隔离范围','node_ids':[]},admin,'project-source-other')
 raw=[{'token':'docNewHumanSource001','type':'docx','name':'工作说明','path':'资料/工作说明',
       'content_status':'full_text','content':'这份工作说明来自当前登记项目目录，不授予其他动作权限。','source_revision':'7'}]
 fail=False
 def project_files(project_id):
  if fail:raise ValueError('RAW_SOURCE_FAILURE_NOT_FOR_CHAT')
  return copy.deepcopy(raw)
 transport_backend.read_project_files=project_files
 first=await maintenance.request_run('refresh',admin,'source-first',[pid])
 await maintenance.wait(first['id'])
 thread=service.create_thread(admin,pid,'当前目录')
 run=service.new_run(admin,'读取项目工作说明。','source-read','chat','manual',pid,thread['id'])
 workspace=await service.execute_tool('phase3_read_workspace',{},run,admin)
 assert workspace['ok'],workspace
 item=next(doc for doc in workspace['documents'] if doc.get('title')=='工作说明')
 identifier=item['id']
 read=await service.execute_tool('phase3_read_document',{'document_id':identifier},run,admin)
 assert read['ok'] and read['content']==raw[0]['content'] and str(read['revision'])=='7',read
 saved=next(row for row in store.items('central_project_source') if row['source_token']==raw[0]['token'])
 history_key=store.digest([saved['id'],saved['version']])
 historical=copy.deepcopy(store.get('central_project_content',history_key))
 fail=True
 failed=await maintenance.request_run('refresh',admin,'source-failed',[pid])
 failed=await maintenance.wait(failed['id'])
 assert failed['status']=='partial',failed
 assert 'RAW_SOURCE_FAILURE_NOT_FOR_CHAT' not in json.dumps(failed)
 still_read=await service.execute_tool('phase3_read_document',{'document_id':identifier},run,admin)
 assert still_read['ok'] and still_read['content']==raw[0]['content']
 assert store.get('central_project_content',history_key)==historical
 fail=False;raw.clear()
 removed=await maintenance.request_run('refresh',admin,'source-removed',[pid])
 await maintenance.wait(removed['id'])
 current=await service.execute_tool('phase3_read_workspace',{},run,admin)
 assert identifier not in {doc['id'] for doc in current['documents']}
 for object_id in (identifier,'docNewHumanSource001'):
  denied=await service.execute_tool('phase3_read_document',{'document_id':object_id},run,admin)
  assert not denied['ok'] and denied['code']=='source_removed',denied
 assert store.get('central_project_content',history_key)==historical
 # A registered source never expands another thread's project scope.
 denied=await service.execute_tool('phase3_read_document',{'document_id':identifier,'project_id':other['id']},run,admin)
 assert not denied['ok'] and denied['code']=='authorization_failed',denied
 assert transport_backend.list_tasks()==[] and not store.items('central_memory')
asyncio.run(scenario())
''')

    def test_real_main_lifespan_boundary_exposes_only_reviewed_upgrade_routes(self):
        self.check('''
from fastapi.testclient import TestClient
from server.main import app
from server import central_api,central_maintenance as maintenance
from lab import guard
import time
base='http://127.0.0.1:18761'
with TestClient(app,base_url=base) as client:
 assert guard._active
 # These GETs must reach real authentication, rather than the outer503 gate.
 for path in ('/api/central/settings/agent','/api/central/settings/memories',
              '/api/central/settings/memory-preview','/api/central/maintenance/state',
              '/api/central/maintenance/runs','/api/central/peer/nodes'):
  response=client.get(path)
  assert response.status_code==401,(path,response.status_code,response.text)
 login=client.post('/api/central/auth/login',headers={'Origin':base},
      json={'username':admin_username,'password':admin_password})
 assert login.status_code==200,login.text
 for path in ('/api/central/settings/agent','/api/central/settings/memories',
              '/api/central/settings/memory-preview','/api/central/maintenance/state',
              '/api/central/maintenance/runs','/api/central/peer/nodes'):
  response=client.get(path)
  assert response.status_code==200,(path,response.status_code,response.text)
  assert response.headers['Cache-Control']=='no-store'
  assert "default-src 'self'" in response.headers['Content-Security-Policy']
 current=client.get('/api/central/settings/agent').json()
 agent_body={'operation_id':'middleware-agent-save','expected_version':current['version'],
             'content':current['content']+'\\n\\n请面向读者解释实际结果。'}
 for origin in (None,'https://untrusted.example'):
  headers={} if origin is None else {'Origin':origin}
  denied=client.put('/api/central/settings/agent',headers=headers,json=agent_body)
  assert denied.status_code==403,denied.text
 assert client.get('/api/central/settings/agent').json()['version']==current['version']
 saved=client.put('/api/central/settings/agent',headers={'Origin':base},json=agent_body)
 assert saved.status_code==200,saved.text
 assert saved.json()['version']==current['version']+1
 memory_response=client.post('/api/central/settings/memories',headers={'Origin':base},json={
   'operation_id':'middleware-memory-save','expected_version':0,'operation':'remember',
   'topic':'中间件验收约定','content':'用临时数据核对真实页面入口。','scope':'organization'})
 assert memory_response.status_code==200,memory_response.text
 preview=client.get('/api/central/settings/memory-preview')
 assert preview.status_code==200 and '中间件验收约定' in preview.json()['content'],preview.text
 state=client.get('/api/central/maintenance/state').json()
 hourly=next(row for row in state['schedules'] if row['id']=='hourly_refresh')
 changed=client.patch('/api/central/maintenance/schedule',headers={'Origin':base},json={
   'schedule_id':'hourly_refresh','enabled':False,'expected_revision':hourly['revision']})
 assert changed.status_code==200 and changed.json()['enabled'] is False,changed.text
 # Exercise both POST paths and persisted detail paths through middleware.
 for path,opid in (('/api/central/maintenance/refresh','middleware-refresh'),
                   ('/api/central/maintenance/inspect','middleware-inspect')):
  started=client.post(path,headers={'Origin':base},json={'operation_id':opid})
  assert started.status_code==200,started.text
  identifier=started.json()['id']
  detail=client.get('/api/central/maintenance/runs/'+identifier)
  assert detail.status_code==200 and detail.json()['id']==identifier,detail.text
  for _ in range(300):
   detail=client.get('/api/central/maintenance/runs/'+identifier)
   if detail.json()['status'] not in {'queued','running','draining'}:break
   time.sleep(.01)
  assert detail.json()['status'] in {'succeeded','partial','failed'},detail.text
 # Binding validation must be reached without issuing a private identity.
 assert not local.items('peer_identity')
 identity=client.post('/api/central/peer/nodes/node_workbuddy/identity',headers={'Origin':base},json={})
 assert identity.status_code==422,identity.text
 enrollment=client.post('/api/central/peer/nodes/node_workbuddy/approve-enrollment',headers={'Origin':base},json={})
 assert enrollment.status_code==422,enrollment.text
 assert not local.items('peer_identity')
 for method,path in (('GET','/api/central/settings/arbitrary-file'),
                     ('PUT','/api/central/settings/agent/extra'),
                     ('POST','/api/central/settings/agent'),
                     ('GET','/api/central/maintenance/unknown'),
                     ('GET','/api/central/maintenance/runs/id/extra'),
                     ('POST','/api/central/maintenance/schedule'),
                     ('POST','/api/central/peer/nodes/node_workbuddy/identity/extra')):
  denied=client.request(method,path,headers={'Origin':base},json={})
  assert denied.status_code==503,(method,path,denied.status_code,denied.text)
 # The safety boundary remains installed throughout the real lifecycle.
 assert guard._active
 try:__import__('socket').socket().connect(('192.0.2.1',443))
 except PermissionError:pass
 else:raise AssertionError('egress guard was disabled')
 assert transport.get_backend().list_tasks()==[]
 assert not local.items('peer_identity')
''')
