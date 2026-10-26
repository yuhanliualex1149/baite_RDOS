"""Maintenance uses the real audit guard and existing business core."""
from __future__ import annotations
import unittest
from tests.phase1_support import run_guarded
from tests.test_shared_knowledge import NATIVE
from tests.test_phase3_egress import isolated

SETUP = '''
from datetime import datetime, timezone, timedelta
from server import central_maintenance as m, phase1_runtime
from server import phase3_service as service, phase3_store as local, phase3_backend as transport
from server import shared_knowledge as knowledge
phase1_runtime.initialize()
service.initialize()
backend3=transport.get_backend()
now=datetime(2026,10,23,14,59,tzinfo=timezone.utc)
m.initialize(now)
'''

class MaintenanceTests(unittest.TestCase):
    def check(self, body, restart=None):
        result = run_guarded(SETUP + '\n' + body,
            restart_body=SETUP + '\n' + restart if restart else None, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_schedule_time_and_enable_are_exact_and_do_not_backfill(self):
        self.check('''
state=m.state(admin)
assert len(state['schedules'])==2
for row in state['schedules']:
 assert row['timezone']=='Asia/Shanghai' and row['next_due_at']=='2026-10-23T15:00:00+00:00'
async def scenario():
 assert (await m.tick(now))['status']=='not_due'
 due=await m.tick(now+timedelta(minutes=1))
 assert due['status']=='started' and not due['missed_while_offline']
 run=await m.wait(due['run_id'])
 assert run['kind']=='inspection' and set(run['schedule_ids'])=={'hourly_refresh','weekly_inspection'}
 assert run['identity']=='system:maintenance' and run['requested_by']=='system:maintenance'
 assert (await m.tick(now+timedelta(minutes=1)))['status']=='not_due'
 assert run['inspection']['project_ids']==[local.PROJECT_ID]
asyncio.run(scenario())
row=store.get('maintenance_schedule','hourly_refresh')
disabled=m.set_schedule(row['id'],False,row['revision'],admin,now=now)
try:m.set_schedule(row['id'],True,row['revision'],admin,now=now)
except m.MaintenanceError as exc:assert exc.code=='version_conflict'
else:raise AssertionError('schedule conflict accepted')
again=m.set_schedule(row['id'],True,disabled['revision'],admin,now=now+timedelta(days=20))
assert m._time(again['next_due_at'])>now+timedelta(days=20)
''')

    def test_restart_only_latest_due_and_interrupted_never_replays(self):
        self.check('''
store.put('maintenance_run','orphan',{'id':'orphan','kind':'refresh','status':'running','project_ids':[local.PROJECT_ID],'created_at':m._iso(now)})
''', restart='''
assert store.get('maintenance_run','orphan')['status']=='interrupted'
async def scenario():
 due=await m.tick(now+timedelta(days=2,hours=2))
 assert due['status']=='started' and due['missed_while_offline']
 run=await m.wait(due['run_id'])
 assert run['kind']=='inspection'
 assert len(store.items('maintenance_run'))==2
 assert (await m.tick(now+timedelta(days=2,hours=2)))['status']=='not_due'
asyncio.run(scenario())
''')

    def test_principal_is_not_admin_and_shared_tables_read_once(self):
        self.check('''
second=__import__('server.rdos_projects',fromlist=['create_project']).create_project({'name':'第二项目','description':'保留项目','node_ids':[]},admin,'second-project')
counts={'tasks':0,'events':0}
old_tasks,old_events=backend3.list_tasks,backend3.list_events
def tasks():counts['tasks']+=1;return old_tasks()
def events():counts['events']+=1;return old_events()
backend3.list_tasks,backend3.list_events=tasks,events
actor=m._actor([local.PROJECT_ID])
assert m.permits(actor,'sync',local.PROJECT_ID) and not m.permits(actor,'sync',second['id'])
assert not m.permits({'id':'system:maintenance'},'sync',local.PROJECT_ID)
try:service._admin(actor)
except ValueError:pass
else:raise AssertionError('maintenance became admin')
async def scenario():
 run=await m.request_run('refresh',admin,'refresh-all',now=now)
 run=await m.wait(run['id'])
 assert counts=={'tasks':1,'events':1},counts
 assert set(run['project_ids'])=={local.PROJECT_ID,second['id']}
 assert len([r for r in run['results'] if r['source'].startswith('project_records:')])==2
 assert all(r['status']=='succeeded' for r in run['results'] if r['source'].startswith('project_records:'))
 same=await m.request_run('refresh',admin,'refresh-all',now=now)
 assert same['id']==run['id'] and counts=={'tasks':1,'events':1}
asyncio.run(scenario())
''')

    def test_receipt_processing_and_readonly_second_stage_preserve_quality(self):
        self.check('''
node=service.make_node_actor('node_lab_phase3_workbuddy')
async def scenario():
 draft=await service.prepare_task({'title':'配置类','goal':'已有配置的结果','deliverable':'协作记录表完整回执','executor_node_ids':[node['node_id']], 'task_type':'action','completion_rule':{'kind':'receipt_recorded'},'output_zone':'agent'},admin,'draft-maint')
 task=await service.apply_task_action(draft['task_id'],'start',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'start-maint',0,0)
 await service.submit_event({'event_type':'submission','body':'已保存配置并核对，首次到点执行另行观察。','task_refs':[{'task_id':task['id'],'requirements_version':1}], 'artifact_refs':[], 'result_status':'completed'},node,'done-maint')
 run=await m.request_run('inspection',admin,'inspect-result',now=now)
 run=await m.wait(run['id'])
 current=backend3.read_task(task['id'])
 assert current['business_status']=='closed' and current['receipt_status']=='verified'
 assert current['quality_status']=='not_evaluated' and not current.get('decision')
 before=store.digest([backend3.list_tasks(),backend3.list_events(),store.items('memory')])
 report=m.inspect_evidence([local.PROJECT_ID],now=now)
 after=store.digest([backend3.list_tasks(),backend3.list_events(),store.items('memory')])
 assert before==after and report['read_only'] and report['changes']==[]
 assert run['inspection']['read_only']
 assert report['tool_boundary']['process_guard_active']
 assert not report['tool_boundary']['business_scheduler_enabled']
 assert report['tool_boundary']['cli']['status']=='not_applicable'
 assert len(report['code_evidence']['files'])>=5
 assert all(len(value)==64 for value in report['code_evidence']['files'].values())
 assert report['not_checked']
 assert 'api_key' not in json.dumps(report).lower()
 assert report['model_evidence']['mode']=='mock'
asyncio.run(scenario())
''')

    def test_failed_shared_read_preserves_cache_and_sources_backoff_independently(self):
        self.check('''
async def scenario():
 first=await m.request_run('refresh',admin,'good',now=now);await m.wait(first['id'])
 prior=local.get('state','refresh:'+local.PROJECT_ID)
 backend3.fault('read_tasks')
 run=await m.request_run('refresh',admin,'fail',now=now);run=await m.wait(run['id'])
 assert next(r for r in run['results'] if r['source']=='business_tables')['status']=='failed'
 assert local.get('state','refresh:'+local.PROJECT_ID)==prior
 bad=store.get('maintenance_source','business_tables')
 assert bad['consecutive_failures']==1 and m._time(bad['next_retry_at'])==now+timedelta(minutes=5)
 assert store.get('maintenance_source','project_files:'+local.PROJECT_ID)['status']=='succeeded'
 # Manual retry is one controlled probe, and success clears this source alone.
 again=await m.request_run('refresh',admin,'recover',now=now);await m.wait(again['id'])
 assert store.get('maintenance_source','business_tables')['consecutive_failures']==0
 assert store.get('maintenance_source','selected_rag')['status']=='needs_attention'
asyncio.run(scenario())
''')

    def test_paused_duplicate_and_busy_do_not_queue(self):
        self.check('''
async def scenario():
 run=await m.request_run('refresh',admin,'first',now=now)
 same=await m.request_run('refresh',admin,'first',now=now)
 assert same['id']==run['id']
 coalesced=await m.request_run('refresh',admin,'second',now=now)
 assert coalesced['id']==run['id'] and coalesced['coalesced']
 assert len(store.items('maintenance_run'))==1
 await m.wait(run['id'])
 state=store.get('state','runtime');state['paused']=True;store.put('state','runtime',state)
 assert (await m.tick(now+timedelta(hours=1)))['status']=='paused'
 try:await m.request_run('refresh',admin,'paused',now=now)
 except m.MaintenanceError as exc:assert exc.code=='paused'
 else:raise AssertionError('paused accepted')
 try:await m.request_run('refresh',{'id':'system:maintenance'},'fake',now=now)
 except m.MaintenanceError as exc:assert exc.code=='authorization_failed'
 else:raise AssertionError('fake principal accepted')
asyncio.run(scenario())
''')

    def test_content_identity_removal_and_failure_do_not_erase_history(self):
        self.check('''
class Source:
 def __init__(self):self.content='同一份正文';self.revision='1';self.fail=False;self.present=True
 def read_knowledge(self,category):
  if self.fail:raise ValueError('PRIVATE_TOKEN_SHOULD_NOT_APPEAR')
  return [{'token':'docSource001','type':'docx','name':'知识','path':'知识','content_status':'full_text','content':self.content,'source_revision':self.revision}] if self.present else []
source=Source()
first=knowledge.sync('selected_rag',admin,'knowledge-a',backend=source)
item=first['items'][0];versions=len(store.items('knowledge_content'))
source.revision='2'
knowledge.sync('selected_rag',admin,'knowledge-b',backend=source)
assert len(store.items('knowledge_content'))==versions
assert knowledge.read_item(item['id'],admin)['item']['source_revision']=='2'
source.fail=True
knowledge.sync('selected_rag',admin,'knowledge-failed',backend=source)
assert knowledge.model_catalog(admin)['total_matches']==1
source.fail=False;source.present=False
knowledge.sync('selected_rag',admin,'knowledge-removed',backend=source)
assert knowledge.model_catalog(admin)['total_matches']==0
assert knowledge.read_item(item['id'],admin,item['version'])['content']=='同一份正文'
''')

    def test_skill_member_removed_only_after_complete_package(self):
        self.check('''
from server.phase3_feishu import KNOWLEDGE_ROOTS
class Source:
 def __init__(self):self.remove=False;self.fail=False
 def list_skill_folders(self):return [{'token':'skillFolder001','name':'协作规范','type':'folder','parent_token':KNOWLEDGE_ROOTS['shared_skills']}]
 def read_skill_folder(self,token):
  if self.fail:raise ValueError('partial-page')
  values=[{'token':token,'name':'协作规范','path':'协作规范','type':'folder','parent_token':KNOWLEDGE_ROOTS['shared_skills'],'content_status':'folder'},
          {'token':'skillEntry001','name':'SKILL.md','path':'协作规范/SKILL.md','type':'file','parent_token':token,'content_status':'full_text','content':'可读规范。'}]
  if not self.remove:values.append({'token':'skillHelper001','name':'helper.py','path':'协作规范/helper.py','type':'file','parent_token':token,'content_status':'file_listing'})
  return values
source=Source()
first=knowledge.sync('shared_skills',admin,'skill-first',backend=source)
package=next(item for item in first['items'] if item['type']=='skill_package')
old=knowledge.read_item(package['id'],admin)
member=next(item for item in old['files'] if item['name']=='helper.py')
source.remove=True;source.fail=True
knowledge.sync('shared_skills',admin,'skill-partial',backend=source)
assert store.get('knowledge_item',member['id'])['available']
assert knowledge.read_item(package['id'],admin)['version']==old['version']
source.fail=False
knowledge.sync('shared_skills',admin,'skill-removed-member',backend=source)
assert store.get('knowledge_item',member['id'])['available'] is False
assert knowledge.model_catalog(admin,query='helper.py')['items']==[]
assert any(item['id']==member['id'] for item in knowledge.read_item(package['id'],admin,old['version'])['files'])
''')

    def test_deadline_stops_new_sources_and_owner_remains_until_drain(self):
        self.check('''
import time
async def scenario():
 run={'id':'bounded-owner','kind':'refresh','status':'running','project_ids':[local.PROJECT_ID],'created_at':m._iso(now)}
 store.put('maintenance_run',run['id'],run)
 async def slow():
  await asyncio.sleep(.12)
  return {'status':'succeeded'}
 async def owner():
  result=await m._source(run,'slow',slow,manual=True,now=now,deadline=time.monotonic()+.02)
  assert result['error_code']=='maintenance_deadline'
  m._save(run,status='partial');m._ACTIVE.pop(run['id'])
 task=asyncio.create_task(owner());m._ACTIVE[run['id']]=task
 await asyncio.sleep(.06)
 assert store.get('maintenance_run',run['id'])['status']=='draining'
 duplicate=await m.request_run('refresh',admin,'deadline-second',now=now)
 assert duplicate['coalesced'] and duplicate['id']==run['id']
 await asyncio.shield(task)
 async def forbidden():raise AssertionError('new source after deadline')
 result=await m._source(run,'after',forbidden,manual=True,now=now,deadline=time.monotonic()-.01)
 assert result['status']=='skipped' and result['error_code']=='deadline'
 assert not m._ACTIVE
asyncio.run(scenario())
''')

    def test_http_requires_admin_and_strict_inputs(self):
        self.check('''
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.central_api import current_user
app=FastAPI();app.include_router(m.router)
app.dependency_overrides[current_user]=lambda:admin
with TestClient(app) as client:
 assert client.get('/api/central/maintenance/state').status_code==200
 assert client.post('/api/central/maintenance/refresh',json={'operation_id':'bad','url':'https://bad.invalid'}).status_code==422
 response=client.patch('/api/central/maintenance/schedule',json={'schedule_id':'hourly_refresh','enabled':False,'expected_revision':1})
 assert response.status_code==200,response.text
researcher=store.user('mem_lab_workbuddy_0001')
app.dependency_overrides[current_user]=lambda:researcher
with TestClient(app) as client:
 assert client.get('/api/central/maintenance/state').status_code==403
 assert client.get('/api/central/maintenance/runs').status_code==403
''')

class MaintenanceEgressTest(unittest.TestCase):
    def test_project_directory_uses_only_registered_root_and_does_not_expand_write_registry(self):
        result=isolated('''
with patch.object(fs.subprocess,'run',side_effect=remote):
 values=backend.read_project_files(fs.PROJECT_ID)
 assert len(values)==1 and values[0]['token']=='docSyntheticPhase3'
 assert values[0]['content']=='Original human text'
 folders[fs.HUMAN_FOLDER]=[]
 assert backend.read_project_files(fs.PROJECT_ID)==[]
 assert len(backend.resources['documents'])==1
 denied(lambda:backend.read_project_files('another-project'))
 assert not any(c[:2] in (('docs','+create'),('docs','+update'),('base','+record-upsert')) for c in calls)
''')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_reliable_marker_reuses_body_but_missing_marker_rereads(self):
        body = NATIVE + '''
import sqlite3,hashlib
os.environ['BAITE_DB_PATH']=str(root/'runtime/panel.sqlite3')
# Native and Markdown use reliable markers only after a complete prior snapshot.
folders['folderKnowledgeChild'][0]['modified_time']='1700000000'
folders[root_token][1]['modified_time']='1700000001'
with patch.object(fs.subprocess,'run',side_effect=knowledge_remote):
 first=backend.read_knowledge('selected_rag')
 first_native=sum(c[:2]==('docs','+fetch') for c in calls)
 first_download=sum(c[:2]==('drive','+download') for c in calls)
 digest=lambda v:hashlib.sha256((v if isinstance(v,str) else json.dumps(v,sort_keys=True,ensure_ascii=False)).encode()).hexdigest()
 with sqlite3.connect(os.environ['BAITE_DB_PATH']) as conn:
  conn.execute('CREATE TABLE central_records(kind,id,payload,updated_at,PRIMARY KEY(kind,id))')
  for item in first:
   if item.get('content_status')!='full_text':continue
   key='knowledge_'+digest(['selected_rag',item['token']])[:24]
   item.update(available=True,version='sha256:'+digest(item['content']))
   conn.execute('INSERT INTO central_records VALUES(?,?,?,?)',('knowledge_item',key,json.dumps(item),''))
   conn.execute('INSERT INTO central_records VALUES(?,?,?,?)',('knowledge_content',digest([key,item['version']]),json.dumps({'content':item['content']}),''))
 backend.read_knowledge('selected_rag')
 assert sum(c[:2]==('docs','+fetch') for c in calls)==first_native
 assert sum(c[:2]==('drive','+download') for c in calls)==first_download
 del folders['folderKnowledgeChild'][0]['modified_time']
 backend.read_knowledge('selected_rag')
 assert sum(c[:2]==('docs','+fetch') for c in calls)==first_native+1
 denied(lambda:backend.read_project_files('unregistered-project'))
'''
        result = isolated(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
