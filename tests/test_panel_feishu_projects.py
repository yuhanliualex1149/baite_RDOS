"""Project scoping through the real native adapter and guard, with offline CLI I/O.

Only subprocess responses are doubled. Command construction, single-use permits,
wire encoding, document registration and durable recovery run unchanged.
"""
from __future__ import annotations

import unittest

from tests.test_phase3_egress import isolated


PROJECTS = '''
backend.resources.update(public_protocol_version=2,workspace_id='ws_rdos',allow_legacy_reads=False)
second='project_second'; third='project_third'; second_root='folderSecondProject'
projects=[
 {'id':fs.PROJECT_ID,'name':'RDOS建设','status':'active','node_ids':['node_codex','node_workbuddy'],
  'folder_id':fs.HUMAN_FOLDER,'resolved_folder_id':fs.HUMAN_FOLDER},
 {'id':second,'name':'第二项目','status':'active','node_ids':['node_workbuddy'],
  'folder_id':second_root,'resolved_folder_id':second_root},
 {'id':third,'name':'第三项目','status':'active','node_ids':['node_workbuddy'],
  'folder_id':'','resolved_folder_id':fs.HUMAN_FOLDER},
]
backend.register_projects(projects)
folders[second_root]=[]
def task_for(project=second):
 return {'id':'task_p3_'+'a'*20,'task_id':'task_p3_'+'a'*20,'title':'整理资料',
  'env_id':cfg['env_id'],'generation':'1'*32,'project_id':project,
  'requirements_version':1,'record_revision':1,'business_status':'active',
  'executor_node_ids':['node_lab_phase3_workbuddy'],'output_zone':'project'}
def event_for(project=second):
 return {'id':'event_current','event_type':'submission','body':'成果已整理。',
  'env_id':cfg['env_id'],'generation':'1'*32,'project_id':project,'identity_mode':'role_test',
  'claimed_node_id':'node_lab_phase3_workbuddy',
  'task_refs':[{'task_id':'task_p3_'+'a'*20,'requirements_version':1}]}
def candidate(identifier='docSecondOutput',folder=None,content='完整成果正文'):
 folder=folder or second_root
 documents[identifier]={'title':'工作汇报','content':content,'revision_id':3}
 folders[folder].append({'token':identifier,'name':'工作汇报','type':'docx','parent_token':folder})
 return {'document_id':identifier,'revision':3,'content_hash':fs._hash(content)}
'''


class PanelFeishuProjectsTest(unittest.TestCase):
    def check(self, body):
        result = isolated(PROJECTS + '\n' + body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_registered_roots_and_fallback_persist_without_widening_cli(self):
        self.check('''
saved=config.read_json(backend.path)
assert {p['id']:p['folder_id'] for p in saved['projects']} == {
 fs.PROJECT_ID:fs.HUMAN_FOLDER,second:second_root,third:fs.HUMAN_FOLDER}
assert fs._folders(saved)=={fs.HUMAN_FOLDER,fs.AGENT_FOLDER,second_root}
assert fs._project_folder(saved,third)==fs.HUMAN_FOLDER
with patch.object(fs.subprocess,'run',side_effect=remote):
 assert backend.executor.list_root(second_root)==[]
 before=len(calls)
 denied(lambda:backend.executor.list_root('unregisteredFolder'))
 denied(lambda:backend.executor.create_document('unregisteredFolder','标题','正文'))
 denied(lambda:backend.executor._run(['drive','files','list','--as','user','--format','json',
  '--params',fs._json({'folder_token':'unregisteredFolder','page_size':200})]))
 denied(lambda:backend.executor._run(['docs','+create','--api-version','v2','--as','user',
  '--parent-token',second_root,'--doc-format','markdown','--content','-','--extra','arbitrary']))
 assert len(calls)==before
 denied(lambda:sys.audit('subprocess.Popen',cfg['phase3']['cli_path'],
  [cfg['phase3']['cli_path'],'auth','status'],str(root/'runtime'),{}))
''')

    def test_default_and_two_projects_roundtrip_same_tables_in_v2(self):
        self.check('''
with patch.object(fs.subprocess,'run',side_effect=remote):
 for index,project in enumerate((fs.PROJECT_ID,second,third)):
  task={**task_for(project),'id':'task_'+str(index),'task_id':'task_'+str(index)}
  result=backend.write_task(task,'write_project_'+str(index))
  assert result['project_id']==project and result['readback_verified']
  wire=json.loads(rows[result['record_id']]['fields']['记录数据'])
  assert wire['project_id']==('project_rdos' if project==fs.PROJECT_ID else project)
  assert wire['executor_node_ids']==['node_workbuddy']
  assert not {'env_id','generation','identity_mode'} & set(wire)
  assert fs.rdos_contract.decode(wire,backend.resources)['project_id']==project
 assert {t['project_id'] for t in backend.list_tasks()}=={fs.PROJECT_ID,second,third}
 assert sum(c[:2]==('base','+record-upsert') for c in calls)==3
 assert {c[c.index('--table-id')+1] for c in calls if c[:2]==('base','+record-upsert')}=={backend.resources['tasks_table']}
 rows.clear()
 for index,project in enumerate((fs.PROJECT_ID,second,third)):
  event={**event_for(project),'id':'event_'+str(index)}
  result=backend.append_event(event,'event_project_'+str(index))
  assert result['project_id']==project and result['readback_verified']
 assert {e['project_id'] for e in backend.list_events()}=={fs.PROJECT_ID,second,third}
 bad=next(iter(rows.values()));value=json.loads(bad['fields']['记录数据']);value['project_id']='unregistered_project'
 bad['fields']['记录数据']=fs._json(value)
 assert backend._decode(bad,backend.resources['events_table'])['_invalid_remote_record']
''')

    def test_registered_document_cannot_cross_projects_in_shared_human_folder(self):
        self.check('''
assert fs._project_folder(backend.resources,third)==fs.HUMAN_FOLDER
with patch.object(fs.subprocess,'run',side_effect=remote):
 before=len(calls)
 denied(lambda:backend.register_event_document(event_for(third),task_for(third),
  {'document_id':'docSyntheticPhase3','revision':1,'content_hash':fs._hash('Original human text')}))
 assert len(calls)==before
assert next(d for d in backend.resources['documents'] if d['id']=='docSyntheticPhase3').get('project_id',fs.PROJECT_ID)==fs.PROJECT_ID
''')

    def test_existing_task_cannot_be_retargeted_to_another_registered_project(self):
        self.check('''
with patch.object(fs.subprocess,'run',side_effect=remote):
 original=backend.write_task(task_for(second),'initial_task')
 before=sum(c[:2]==('base','+record-upsert') for c in calls)
 denied(lambda:backend.write_task({**task_for(third),'record_revision':2},'retarget_task'))
 denied(lambda:backend.write_task(task_for('unregistered_project'),'outside_task'))
 assert sum(c[:2]==('base','+record-upsert') for c in calls)==before
 assert backend.read_task(original['task_id'])['project_id']==second
''')

    def test_output_discovery_reads_metadata_then_registers_exact_project_reference(self):
        self.check('''
ref=candidate()
candidate('docExcludedOutput')
backend.resources['excluded_object_ids']=['docExcludedOutput']
with patch.object(fs.subprocess,'run',side_effect=remote):
 discovery=backend.refresh_registry()
 assert discovery['bodies_read']==0
 assert not any(c[:2]==('docs','+fetch') for c in calls)
 assert ref['document_id'] in backend._candidates and 'docExcludedOutput' not in backend._candidates
 denied(lambda:backend.read_document(ref['document_id']))
 result=backend.register_event_document(event_for(),task_for(),ref)
 assert result['project_id']==second and result['folder_id']==second_root
 assert result['readback_verified'] and result['revision']==3
 entry=next(d for d in config.read_json(backend.path)['documents'] if d['id']==ref['document_id'])
 assert entry['project_id']==second and entry['task_id']==task_for()['task_id']
 assert entry['folder_id']==second_root
 denied(lambda:backend.register_event_document(event_for(third),task_for(third),ref))
''')

    def test_new_output_rejects_wrong_root_node_version_and_excluded_object(self):
        self.check('''
wrong_root=candidate('docWrongRoot',fs.HUMAN_FOLDER)
ref=candidate()
excluded=candidate('docExcludedOutput')
backend.resources['excluded_object_ids']=['docExcludedOutput']
with patch.object(fs.subprocess,'run',side_effect=remote):
 backend.refresh_registry()
 denied(lambda:backend.register_event_document(event_for(),task_for(),wrong_root))
 denied(lambda:backend.register_event_document({**event_for(),'claimed_node_id':'node_lab_phase3_codex'},task_for(),ref))
 denied(lambda:backend.register_event_document({**event_for(),'task_refs':[{'task_id':task_for()['task_id'],'requirements_version':2}]},task_for(),ref))
 denied(lambda:backend.register_event_document(event_for(),task_for(),excluded))
 denied(lambda:backend.register_event_document(event_for(),task_for(),{**ref,'content_hash':'0'*64}))
 assert not any(d['id']==ref['document_id'] for d in backend.resources['documents'])
 assert not any(c[:2] in (('docs','+create'),('base','+record-upsert')) for c in calls)
''')

    def test_event_and_task_project_must_match_before_output_registration(self):
        self.check('''
ref=candidate()
with patch.object(fs.subprocess,'run',side_effect=remote):
 backend.refresh_registry();before=len(calls)
 denied(lambda:backend.register_event_document(event_for(third),task_for(second),ref))
 assert len(calls)==before
 assert not any(d['id']==ref['document_id'] for d in backend.resources['documents'])
''')

    def test_create_and_lost_response_recovery_stay_in_original_project_root(self):
        self.check('''
hidden={'active':False}
def lost_and_hidden(argv,**kw):
 result=remote(argv,**kw)
 if argv[1:3]==['docs','+create']:
  hidden['active']=True
  raise subprocess.TimeoutExpired(argv,1)
 if hidden['active'] and argv[1:4]==['drive','files','list']:
  data=json.loads(result.stdout)
  data['data']['files']=[r for r in data['data']['files'] if not r['token'].startswith('docCreated')]
  result.stdout=fs._json(data)
 return result
with patch.object(fs.subprocess,'run',side_effect=lost_and_hidden):
 denied(lambda:backend.create_document('project','项目汇报','只创建一次的完整正文','create_lost',project_id=second))
entry=config.read_json(backend.operations_path)['create_lost']
assert entry['state']=='sent' and entry['project_id']==second and entry['folder_id']==second_root
assert sum(c[:2]==('docs','+create') for c in calls)==1
# Reload durable data into the same registered executor scope; no guard is disabled.
saved=config.read_json(backend.path);backend.resources.clear();backend.resources.update(saved)
reopened=fs.FeishuBackend(cfg,backend.resources,backend.executor)
folders[fs.HUMAN_FOLDER].append({'token':'docOtherSameTitle','name':entry['remote_title'],'type':'docx','parent_token':fs.HUMAN_FOLDER})
documents['docOtherSameTitle']={'title':entry['remote_title'],'content':'其他项目同名正文','revision_id':1}
before=len(calls)
with patch.object(fs.subprocess,'run',side_effect=remote):
 recovered=reopened.find_created_document('create_lost')
 assert recovered['project_id']==second and recovered['folder_id']==second_root
 assert recovered['body_content']=='只创建一次的完整正文'
 again=reopened.create_document('project','项目汇报','只创建一次的完整正文','create_lost',project_id=second)
 assert again['id']==recovered['id']
 denied(lambda:reopened.create_document('project','项目汇报','只创建一次的完整正文','create_lost',project_id=third))
assert not any(c[:2]==('docs','+create') for c in calls[before:])
listed=[json.loads(c[c.index('--params')+1])['folder_token'] for c in calls[before:] if c[:3]==('drive','files','list')]
assert listed and set(listed)=={second_root}
assert config.read_json(reopened.operations_path)['create_lost']['state']=='verified'
''')


if __name__ == '__main__':
    unittest.main()
