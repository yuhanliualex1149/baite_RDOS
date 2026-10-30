"""Read-only central observation with real guard, service and disposable SQLite."""
import unittest
from tests.phase1_support import run_guarded


SETUP = '''
import hashlib, sqlite3, copy
from server import central_inspection as inspection, central_service as service
from server import phase1_runtime as runtime, phase3_service as domain, phase3_store as local
from server import rdos_projects as projects, phase3_backend as transport
runtime.initialize();domain.initialize()
pid=domain.PROJECT_ID
second=projects.create_project({'name':'独立项目','description':'第二个项目',
    'node_ids':['node_lab_phase3_workbuddy'],'folder_url':''},admin,'inspection-second-project')['id']
def run_for(project=''):
 service.ACTIVE_RUN_ID=None
 thread=service.create_thread(admin,project,'只读观察')
 return service.new_run(admin,'只检查，不处理任务或保存记忆','inspect-'+store.identifier('op'),'chat','manual',project,thread['id'])
def snapshot():
 with db.connection() as conn:
  output={table:[tuple(row) for row in conn.execute('SELECT * FROM '+table+' ORDER BY 1,2')]
          for table in ('phase3_local','phase3_mock','phase3_history','phase3_event_index')}
  output['central_records']=[tuple(row) for row in conn.execute("SELECT * FROM central_records WHERE kind!='inspection_receipt' ORDER BY kind,id")]
 return store.digest(output)
async def task():
 draft=await domain.prepare_task({'title':'检查已有成果','goal':'核对当前交付','deliverable':'执行结果',
     'executor_node_ids':['node_lab_phase3_workbuddy'],'task_type':'action'},admin,'inspection-draft')
 current=await domain.apply_task_action(draft['task_id'],'start',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'inspection-start',0,0)
 local.put('task_cache',current['task_id'],current)
 return current
'''


class CentralInspectionTest(unittest.TestCase):
    def check(self, body):
        result = run_guarded(SETUP + '\n' + body, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_organization_overview_covers_every_authorized_project_without_mutation(self):
        self.check('''
async def scenario():
 current=await task()
 run=run_for()
 before=snapshot()
 result=await inspection.execute('central_inspect_overview',{},run,admin)
 assert result['ok'],result
 ids={p['project']['id'] for p in result['evidence']['projects']}
 assert ids==set(run['allowed_projects']) and second in ids
 assert result['observed_at'] and result['read_only']
 assert snapshot()==before
 receipts=store.items('inspection_receipt');assert len(receipts)==1
 assert not any(k in json.dumps(receipts) for k in ('message','password','source_text'))
asyncio.run(scenario())
''')

    def test_fresh_read_sees_completion_event_without_ingest_or_task_transition(self):
        self.check('''
async def scenario():
 current=await task();authority=transport.get_backend()
 event={'id':'inspection-node-completed',**runtime.environment(),'project_id':pid,
  'event_type':'submission','claimed_node_id':'node_lab_phase3_workbuddy','body':'执行已完成',
  'task_refs':[{'task_id':current['task_id'],'requirements_version':1}],
  'artifact_refs':[],'result_status':'completed','created_at':db.utc_now()}
 authority.append_event(event,'inspection-append')
 run=run_for(pid);before=snapshot()
 result=await inspection.execute('central_inspect_overview',{'fresh':True},run,admin)
 assert result['ok'] and result['evidence']['complete'],result
 observed=result['evidence']['projects'][0]
 assert any(e['id']==event['id'] for e in observed['events'])
 assert observed['tasks'][0]['business_status']=='active'
 assert 'quality_evaluation' not in observed['tasks'][0]
 assert authority.read_task(current['task_id'])['business_status']=='active'
 assert not local.event_fingerprint(event['id'])
 assert snapshot()==before
asyncio.run(scenario())
''')

    def test_fresh_failure_is_explicit_and_preserves_existing_business_facts(self):
        self.check('''
async def scenario():
 await task();run=run_for(pid);transport.get_backend().fault('read_events')
 before=snapshot()
 result=await inspection.execute('central_inspect_overview',{'fresh':True},run,admin)
 assert result['ok'] and result['evidence']['complete'] is False,result
 assert result['evidence']['projects'][0]['fresh_status']=='failed'
 assert result['evidence']['projects'][0]['tasks']
 assert snapshot()==before
asyncio.run(scenario())
''')

    def test_code_reads_actual_lines_hash_and_rejects_paths_or_commands(self):
        self.check('''
async def scenario():
 run=run_for(pid);before=snapshot()
 response=await inspection.execute('central_inspect_code',{'path':'server/central_inspection.py','start_line':1,'end_line':12},run,admin)
 assert response['ok'],response
 value=response['evidence']
 actual=(root/'code/server/central_inspection.py').read_bytes()
 assert value['file_sha256']==hashlib.sha256(actual).hexdigest()
 assert value['lines'][0]['line']==1 and 'Bounded observation' in value['lines'][0]['text']
 found=await inspection.execute('central_inspect_code',{'query':'INSPECTION_TOOLS','limit':5},run,admin)
 assert found['ok'] and found['evidence']['matches']
 for args in ({'path':'../runtime/credentials.json'},{'path':str(root/'runtime/credentials.json')},
              {'path':'.git/config'},{'path':'.env.local'},{'path':'nodes/workbuddy/AGENTS.md'},
              {'query':'hello','command':'pwd'}):
  rejected=await inspection.execute('central_inspect_code',args,run,admin)
  assert not rejected['ok'],(args,rejected)
 assert snapshot()==before
asyncio.run(scenario())
''')

    def test_database_schema_and_query_are_fixed_readonly_and_scoped(self):
        self.check('''
async def scenario():
 current=await task();run=run_for(pid);before=snapshot()
 schema=await inspection.execute('central_inspect_database',{'schema':True},run,admin)
 assert schema['ok'] and schema['evidence']['schema_version']>0,schema
 assert 'password_hash' not in json.dumps(schema) and 'central_sessions' not in json.dumps(schema)
 rows=await inspection.execute('central_inspect_database',{'entity':'tasks','fields':['task_id','project_id','business_status'],
  'filters':{'task_id':current['task_id']}},run,admin)
 assert rows['ok'] and rows['evidence']['items']==[{'task_id':current['task_id'],'project_id':pid,'business_status':'active'}],rows
 for args in ({'entity':'central_users'},{'entity':'runs','fields':['message']},
              {'entity':'tasks','sql':'DELETE FROM phase3_local'},
              {'entity':'tasks','database':'/tmp/other.db'},
              {'entity':'tasks','filters':{'project_id':second}}):
  result=await inspection.execute('central_inspect_database',args,run,admin)
  assert not result['ok'],(args,result)
 with inspection._readonly_connection() as connection:
  assert connection.execute('PRAGMA query_only').fetchone()[0]==1
  for sql in ('SELECT password_hash FROM central_users','SELECT * FROM central_sessions',
              'DELETE FROM central_records','ATTACH DATABASE ":memory:" AS extra',
              'SELECT load_extension("unsafe")','PRAGMA writable_schema=ON'):
   try:connection.execute(sql)
   except sqlite3.DatabaseError:pass
   else:raise AssertionError(sql)
  assert connection.execute('SELECT count(*) FROM phase3_local').fetchone()[0]>0
 for uri in (db.db_path().as_uri()+'?mode=rw', db.db_path().as_uri()+'?mode=ro&immutable=1',
             (db.db_path().parent/'other.sqlite3').as_uri()+'?mode=ro'):
  try:sqlite3.connect(uri,uri=True)
  except PermissionError:pass
  else:raise AssertionError('guard allowed '+uri)
 assert snapshot()==before
asyncio.run(scenario())
''')

    def test_runs_tool_calls_and_operation_projection_cannot_leak_private_sources(self):
        self.check('''
async def scenario():
 run=run_for(pid)
 foreign={**run,'id':'foreign-inspect-run','user_id':'mem_other_admin','message':'PRIVATE_MESSAGE_SENTINEL',
  'steps':[{'type':'tool_result','name':'private-tool','result':{'ok':True,'content':'PRIVATE_TOOL_SENTINEL'}}]}
 store.put('run',foreign['id'],foreign)
 store.put('operation','shared-op',{'id':'shared-op','project_id':pid,'status':'uncertain',
  'action':'phase3_task:start','authorization':{'message':'PRIVATE_AUTH_SENTINEL'},
  'input':{'password':'SECRET_INPUT_SENTINEL'},'source_message':'PRIVATE_REQUEST_SENTINEL'})
 before=snapshot()
 for entity in ('runs','tool_calls','operations'):
  result=await inspection.execute('central_inspect_records',{'entity':entity},run,admin)
  assert result['ok'],result
  serialized=json.dumps(result)
  assert all(word not in serialized for word in ('PRIVATE_MESSAGE_SENTINEL','PRIVATE_TOOL_SENTINEL',
   'PRIVATE_AUTH_SENTINEL','SECRET_INPUT_SENTINEL','PRIVATE_REQUEST_SENTINEL','foreign-inspect-run'))
 assert snapshot()==before
 forged=await inspection.execute('central_inspect_overview',{}, {**run,'allowed_projects':[pid,second]},admin)
 assert not forged['ok']
 disabled={**admin,'enabled':False}
 with db.connection() as connection:connection.execute('UPDATE central_users SET enabled=0 WHERE id=?',(admin['id'],))
 assert not (await inspection.execute('central_inspect_code',{'query':'tools_specs'},run,admin))['ok']
asyncio.run(scenario())
''')

    def test_log_projection_hides_secrets_private_text_and_cannot_claim_correlation(self):
        self.check('''
async def scenario():
 logs=root/'runtime'
 (logs/'server.log').write_text('ERROR private request PRIVATE_CHAT_SENTINEL password=SECRET_LOG_SENTINEL\\n'+
  '  File "'+str(root/'code/server/central_inspection.py')+'", line 12, in execute\\n'+
  'ValueError: PRIVATE_EXCEPTION_SENTINEL\\nINFO application startup complete\\n')
 (logs/'blocked.jsonl').write_text(json.dumps({'at':db.utc_now(),'event':'blocked','channel':'network.connect',
  'body':'PRIVATE_RAW_SENTINEL','password':'SECRET_RAW_SENTINEL'})+'\\n')
 run=run_for();before=snapshot()
 for log in ('server.log','blocked.jsonl'):
  result=await inspection.execute('central_inspect_records',{'entity':'logs','log':log},run,admin)
  assert result['ok'] and result['evidence']['items'],result
  assert all(word not in json.dumps(result) for word in ('PRIVATE_CHAT_SENTINEL','SECRET_LOG_SENTINEL','PRIVATE_RAW_SENTINEL','SECRET_RAW_SENTINEL','PRIVATE_EXCEPTION_SENTINEL'))
  if log=='server.log':
   assert any(item.get('code_frame',{}).get('path')=='server/central_inspection.py' for item in result['evidence']['items'])
   assert any(item.get('exception_type')=='ValueError' for item in result['evidence']['items'])
 bad=await inspection.execute('central_inspect_records',{'entity':'logs','log':'credentials.json'},run,admin)
 assert not bad['ok']
 bad=await inspection.execute('central_inspect_records',{'entity':'logs','filters':{'task_id':'fake'}},run,admin)
 assert bad['code']=='unavailable_correlation'
 assert snapshot()==before
asyncio.run(scenario())
''')

    def test_artifact_and_uncertain_verification_remain_distinct_without_quality_workflow(self):
        self.check('''
async def scenario():
 current=await task()
 current.update(result_status='completed',receipt_status='uncertain',quality_status='not_evaluated',
  result_refs=[{'document_id':'already-created','revision':3,'content_hash':'observed-body-hash',
                'source_url':'https://scn33386s7ui.feishu.cn/docx/already-created'}])
 local.put('task_cache',current['task_id'],current)
 local.put('document_cache','already-created',{'id':'already-created','project_id':pid,
  'revision':3,'content_hash':'observed-body-hash','content':'业务成果原文，不应重做'})
 run=run_for(pid);before=snapshot()
 result=await inspection.execute('central_inspect_overview',{},run,admin)
 value=result['evidence']['projects'][0]['tasks'][0]
 assert value['execution_reported']=='completed'
 assert value['technical_verification']=='uncertain'
 assert 'quality_evaluation' not in value
 assert value['artifact_evidence'][0]['revision']==3
 assert value['business_status']=='active' and snapshot()==before
asyncio.run(scenario())
''')


if __name__ == '__main__':
    unittest.main()
