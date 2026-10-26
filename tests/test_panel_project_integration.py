"""Registered projects across guarded administrator API and bounded model tools."""
import unittest
from tests.phase1_support import run_guarded
from tests.test_panel_projects import PROJECT_SETUP
from tests.test_phase3_egress import isolated


class PanelProjectIntegrationTest(unittest.TestCase):
    def check(self, body):
        result = run_guarded(PROJECT_SETUP + '\n' + body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_api_project_create_edit_and_scoped_task_authority(self):
        self.check('''
from server.main import app
from server import central_api
import httpx
async def scenario():
    token=central.login(admin_username,admin_password)['token']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:18761') as client:
        assert (await client.get('/api/central/projects')).status_code==401
        client.cookies.set(central_api.COOKIE,token)
        listing=await client.get('/api/central/projects')
        assert listing.status_code==200,listing.text
        assert len(listing.json()['items'])==1 and listing.json()['items'][0]['is_default']
        body={'operation_id':'http-project','name':'管理员项目','description':'项目说明','node_ids':['node_workbuddy'],'folder_url':''}
        assert (await client.post('/api/central/projects',json=body)).status_code==403
        client.headers['Origin']='http://127.0.0.1:18761'
        response=await client.post('/api/central/projects',json=body)
        assert response.status_code==200,response.text
        project=response.json()['item'];pid=project['id']
        assert project['node_ids']==['node_workbuddy'] and project['folder_source']=='default'
        assert not backend3.registry()['documents']
        assert (await client.post('/api/central/projects',json=body)).json()==response.json()
        bad=await client.post('/api/central/projects',json={**body,'name':'换内容'})
        assert bad.status_code==409,bad.text
        edited=await client.patch('/api/central/projects/'+pid,json={'operation_id':'http-edit','expected_revision':1,'description':'更新说明'})
        assert edited.status_code==200 and edited.json()['item']['revision']==2,edited.text
        stale=await client.patch('/api/central/projects/'+pid,json={'operation_id':'http-stale','expected_revision':1,'name':'过期设置'})
        assert stale.status_code==409,stale.text
        draft_body={'operation_id':'http-draft','payload':{'title':'本项目工作','goal':'完成本项目工作',
            'deliverable':'工作结果','executor_node_ids':['node_workbuddy']}}
        response=await client.post('/api/central/phase3/drafts?project_id='+pid,json=draft_body)
        assert response.status_code==200,response.text
        draft=response.json()['item']
        action={'operation_id':'http-start','expected_revision':0,'expected_requirements_version':0,
                'payload':{'draft_id':draft['id'],'expected_hash':draft['draft_hash']}}
        response=await client.post('/api/central/phase3/tasks/'+draft['task_id']+'/actions/start?project_id='+pid,json=action)
        assert response.status_code==200,response.text
        assert response.json()['item']['project_id']==pid
        current=await client.get('/api/central/phase3/workspace?project_id='+pid)
        assert current.status_code==200 and len(current.json()['tasks'])==1,current.text
        default=await client.get('/api/central/phase3/workspace?project_id=project_rdos')
        assert default.status_code==200 and not default.json()['tasks'],default.text
        wrong=await client.post('/api/central/phase3/tasks/'+draft['task_id']+'/actions/pause?project_id=project_rdos',json={
            'operation_id':'wrong-scope','expected_revision':1,'expected_requirements_version':1,'payload':{}})
        assert wrong.status_code==403,wrong.text
        invalid=await client.post('/api/central/phase3/drafts?project_id='+pid,json={**draft_body,
            'operation_id':'wrong-assignee','payload':{**draft_body['payload'],'executor_node_ids':['node_codex']}})
        assert invalid.status_code!=200,invalid.text
        preview=await client.get('/api/central/projects/'+pid+'/deactivation-preview')
        assert preview.status_code==200 and len(preview.json()['tasks'])==1,preview.text
        stopped=await client.post('/api/central/projects/'+pid+'/deactivate',json={'operation_id':'http-stop','expected_revision':2})
        assert stopped.status_code==200 and stopped.json()['item']['status']=='inactive',stopped.text
        assert backend3.read_task(draft['task_id'])['business_status']=='paused'
        response=await client.get('/api/central/projects')
        assert len(response.json()['items'])==1,response.text
        response=await client.get('/api/central/projects?include_inactive=true')
        assert len(response.json()['items'])==2,response.text
        opened=await client.post('/api/central/projects/'+pid+'/reactivate',json={'operation_id':'http-open',
            'expected_revision':stopped.json()['item']['revision']})
        assert opened.status_code==200 and opened.json()['item']['status']=='active',opened.text
        assert backend3.read_task(draft['task_id'])['business_status']=='paused'
        assert not backend3.registry()['documents']
        assert (await client.post('/api/central/tasks',json={'operation_id':'old-raw-entry'})).status_code==503
asyncio.run(scenario())
''')

    def test_model_tools_and_material_context_stay_in_new_project(self):
        self.check('''
from server import central_service as coordinator, rdos_contract
async def scenario():
    default=await start('default-context',title='DEFAULT_PRIVATE_CONTEXT')
    second=create_project()
    default_doc=backend3.create_document('project','默认项目专属资料','DEFAULT_DOCUMENT_BODY','default-doc')
    explicit_source=backend3.create_document('project','明确引用的共享资料','SHARED_SOURCE_BODY','explicit-source')
    task=await start_in_project(second,title='SECOND_PROJECT_CONTEXT',input_refs=[docref(explicit_source)])
    document=backend3.create_document('project','第二项目资料','SECOND_DOCUMENT_BODY','second-doc',project_id=second['id'])
    await service.refresh(admin,'default-context')
    with db.connection() as conn:
        conn.execute("DELETE FROM phase3_local WHERE kind='task_cache' AND id=?",(task['id'],))
        conn.execute("DELETE FROM phase3_local WHERE kind='document_cache' AND id=?",(explicit_source['id'],))
    await service.refresh(admin,'second-context',second['id'])
    assert local.get('document_cache',explicit_source['id'])['content']=='SHARED_SOURCE_BODY'
    thread=coordinator.create_thread(admin,second['id'],'项目对话')
    run=coordinator.new_run(admin,'读取本项目工作','new-project-run','chat','manual',second['id'],thread['id'])
    context,trusted=coordinator._phase3_model_context(run,admin)
    assert context['project']['id']==second['id'] and trusted['project_id']==second['id']
    assert 'DEFAULT_PRIVATE_CONTEXT' not in json.dumps(context)
    assert context['tasks'][0]['title']=='SECOND_PROJECT_CONTEXT'
    specs=coordinator.tool_specs(run)
    assert any(item['function']['name']=='phase3_prepare_task' for item in specs)
    view=await coordinator.execute_tool('phase3_read_workspace',{},run,admin)
    assert view['ok'] and view['project']['id']==second['id']
    assert [item['node_id'] for item in view['nodes']]==['node_workbuddy']
    denied=await coordinator.execute_tool('phase3_read_document',{'document_id':default_doc['id']},run,admin)
    assert not denied['ok']
    read=await coordinator.execute_tool('phase3_read_document',{'document_id':document['id']},run,admin)
    assert read['ok'] and read['content']=='SECOND_DOCUMENT_BODY',read
    scoped=projects.resource_scope(second['id'],backend3.registry())
    source=next(item for item in scoped['documents'] if item['id']==explicit_source['id'])
    assert source['project_id']==service.PROJECT_ID
    assert backend3.read_task(task['id'])['input_refs']==[docref(explicit_source)]
    shared=await coordinator.execute_tool('phase3_read_document',{'document_id':explicit_source['id']},run,admin)
    assert shared['ok'] and shared['content']=='SHARED_SOURCE_BODY',shared
    assert default_doc['id'] not in {item['id'] for item in scoped['documents']}
    draft=await coordinator.execute_tool('phase3_prepare_task',{'payload':{'title':'新项目草稿','goal':'目标',
        'deliverable':'结果','executor_node_ids':['node_workbuddy']}},run,admin)
    assert draft['ok'] and draft['item']['project_id']==second['id'],draft
    assert local.get('draft',draft['item']['id'])['project_id']==second['id']
    rejected=await coordinator.execute_tool('phase3_prepare_task',{'payload':{'title':'错误节点','goal':'目标',
        'deliverable':'结果','executor_node_ids':['node_codex']}},run,admin)
    assert not rejected['ok']
    wrong={**run,'project_id':service.PROJECT_ID}
    denied=await coordinator.execute_tool('phase3_read_workspace',{},wrong,admin)
    assert not denied['ok'] and denied['code']=='authorization_failed'
    assert not await coordinator.execute_tool('phase3_start_task',{'approved':True},run,admin)=={'ok':True}
    assert len(backend3.list_tasks())==2
asyncio.run(scenario())
''')

    def test_member_cannot_manage_projects_or_read_administrator_workspace(self):
        self.check('''
from server.main import app
from server import central_api
import httpx
async def scenario():
    member=central.create_user('project-member','项目参与者',['researcher'],'project-member-password')['user']
    token=central.login(member['username'],'project-member-password')['token']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:18761',
        headers={'Origin':'http://127.0.0.1:18761'}) as client:
        client.cookies.set(central_api.COOKIE,token)
        response=await client.get('/api/central/projects')
        assert response.status_code==200 and response.json()['items']==[],response.text
        for method,path,body in [('POST','/api/central/projects',{'operation_id':'no-member-create','name':'项目'}),
            ('PATCH','/api/central/projects/'+service.PROJECT_ID,{'operation_id':'no-member-edit','expected_revision':1,'name':'改名'}),
            ('POST','/api/central/projects/'+service.PROJECT_ID+'/deactivate',{'operation_id':'no-member-stop','expected_revision':1})]:
            response=await client.request(method,path,json=body)
            assert response.status_code==403,response.text
        assert (await client.get('/api/central/phase3/workspace')).status_code==403
asyncio.run(scenario())
''')

    def test_scoped_feishu_adapter_adds_only_registered_work_folder(self):
        result = isolated('''
pid='project_0123456789abcdefabcd'
folder='RegisteredProjectFolder'
folders[folder]=[]
backend.register_projects([{'id':pid,'name':'项目工作','status':'active',
    'node_ids':['node_workbuddy'],'folder_id':folder,'resolved_folder_id':folder}])
task={'id':'task_p3_0123456789abcdefabcd','task_id':'task_p3_0123456789abcdefabcd',
      'env_id':cfg['env_id'],'generation':'1'*32,'project_id':pid,
      'title':'项目任务','requirements_version':1,'record_revision':1,'business_status':'active',
      'executor_node_ids':['node_workbuddy'],'goal':'完成项目工作'}
with patch.object(fs.subprocess,'run',side_effect=remote):
    document=backend.create_document('project','项目成果','只属于本项目的正文。','new-project-output',project_id=pid)
    assert document['folder_id']==folder and document['project_id']==pid
    assert not any(item['token']==document['id'] for item in folders[fs.HUMAN_FOLDER])
    actual=backend.read_document(document['id'])
    assert actual['content_hash']==document['content_hash']
    before=len(calls)
    again=backend.create_document('project','项目成果','只属于本项目的正文。','new-project-output',project_id=pid)
    assert again['id']==document['id']
    assert not any(command[:2]==('docs','+create') for command in calls[before:])
    written=backend.write_task(task,'new-project-task')
    assert written['project_id']==pid
    assert backend.list_tasks()[0]['project_id']==pid
    denied(lambda:backend.create_document('project','无权项目','正文','outside-project',project_id='project_unregistered'))
    denied(lambda:backend.executor.list_root('UnregisteredFolder'))
denied(lambda:sys.audit('subprocess.Popen',cfg['phase3']['cli_path'],
    [cfg['phase3']['cli_path'],'docs','+create'],str(root/'runtime'),{}))
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_malformed_unscoped_records_preserve_original_and_show_sync_issue(self):
        self.check('''
async def scenario():
    default_task=await start('valid-foreign-task')
    second=create_project()
    task=await start_in_project(second,'valid-selected-task')
    invalid={'_invalid_remote_record':True,'record_id':'bad-physical-record',
             'raw':{'fields':{'记录数据':'ORIGINAL_INVALID_JSON'}},'error':'parse_error'}
    backend3._save('task','broken-remote-row',invalid)
    malformed=['ORIGINAL_INVALID_EVENT',{'arbitrary':'source preserved'}]
    backend3._save('event','broken-event-row',malformed)
    view=await service.refresh(admin,'malformed-table',second['id'])
    assert len(view['tasks'])==1 and view['tasks'][0]['task_id']==task['id']
    assert local.get('task_cache',default_task['id'])['project_id']==service.PROJECT_ID
    quarantined=local.items('quarantine')
    assert len(quarantined)==2 and all(item['scope']=='workspace' for item in quarantined),quarantined
    assert any(item['raw']==invalid for item in quarantined)
    assert any(item['raw']==malformed for item in quarantined)
    last=service.business_workspace(admin,second['id'])['last_refresh']
    assert last['complete'] and last['issue_count']==2 and last['issue_summary'],last
    assert not any(item['raw'].get('task_id')==default_task['id'] for item in quarantined if isinstance(item['raw'],dict))
asyncio.run(scenario())
''')


if __name__=='__main__':
    unittest.main()
