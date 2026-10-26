"""Project registration and lifecycle through guarded persisted business paths."""
import unittest
from tests.phase1_support import run_guarded
from tests.test_phase3_core import SETUP

PROJECT_SETUP = SETUP + '''
from server import rdos_projects as projects

def create_project(op='second-project', **fields):
    return projects.create_project({'name':'第二个项目','description':'明确的工作目标',
        'node_ids':[other['node_id']],**fields},admin,op)

async def start_in_project(project, op='second-task', **fields):
    draft = await service.prepare_task({'title':'项目工作','goal':'完成当前工作',
        'deliverable':'可读的工作结果','executor_node_ids':[other['node_id']],
        'completion_rule':{'kind':'receipt_recorded'},**fields},admin,'draft-'+op,project['id'])
    return await service.apply_task_action(draft['task_id'],'start',{'draft_id':draft['id'],
        'expected_hash':draft['draft_hash']},admin,'start-'+op,0,0,project_id=project['id'])

def denied(code, call):
    try:call()
    except ValueError as exc:assert getattr(exc,'code','')==code,(getattr(exc,'code',''),str(exc))
    else:raise AssertionError('expected '+code)
'''


class PanelProjectsTest(unittest.TestCase):
    def check(self, body, restart=None):
        result = run_guarded(PROJECT_SETUP + '\n' + body,
            restart_body=None if restart is None else PROJECT_SETUP + '\n' + restart)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_registration_fallback_idempotency_and_default_protection(self):
        self.check('''
items=projects.list_projects(admin,True)
assert len(items)==1 and items[0]['is_default'] and items[0]['name']=='RDOS建设'
assert items[0]['id']==service.PROJECT_ID and items[0]['status']=='active'
assert not projects.is_registered('lab_project_phase0')
second=create_project()
assert second['folder_source']=='default' and second['resolved_folder_id']=='lab_phase3_project_root'
assert create_project()==second
assert projects.get_project(second['id'])['resolved_folder_id']==items[0]['resolved_folder_id']
denied('operation_conflict',lambda:create_project(name='相同操作不同名称'))
changed=projects.update_project(second['id'],{'name':'修改后的项目'},admin,'rename',1)
assert changed['revision']==2
assert projects.update_project(second['id'],{'name':'修改后的项目'},admin,'rename',1)==changed
denied('version_conflict',lambda:projects.update_project(second['id'],{'description':'过期修改'},admin,'stale',1))
denied('invalid_project',lambda:create_project('foreign',folder_url='https://example.com/drive/folder/abc'))
denied('authorization_failed',lambda:projects.create_project({'name':'没有授权','node_ids':[]},central.user('mem_lab_codex_0001'),'no-admin'))
async def scenario():
    await failure('default_project_protected',projects.preview_deactivate(service.PROJECT_ID,admin))
    await failure('default_project_protected',projects.deactivate_project(service.PROJECT_ID,admin,'no-default',1))
asyncio.run(scenario())
''')

    def test_task_routing_scope_and_cache_preserved_across_selected_sync(self):
        self.check('''
async def scenario():
    default_task=await start('default-task',completion_rule=None)
    second=create_project()
    second_task=await start_in_project(second)
    assert second_task['project_id']==second['id']
    await failure('invalid_task',service.prepare_task({'title':'错误节点','goal':'工作','deliverable':'结果',
        'executor_node_ids':[node['node_id']]},admin,'wrong-node',second['id']))
    await failure('out_of_scope',service.prepare_task({'task_id':second_task['id'],'change_reason':'错误项目'},admin,'wrong-project'))
    event=await service.submit_event({'event_type':'submission','body':'项目工作已完成',
        'task_refs':refs(second_task),'result_status':'completed'},other,'second-result')
    assert event['project_id']==second['id']
    default_view=await service.refresh(admin,'default-only')
    assert [t['id'] for t in default_view['tasks']]==[default_task['id']]
    assert backend3.read_task(second_task['id'])['business_status']=='active'
    view=await service.refresh(admin,'second-only',second['id'])
    assert [t['id'] for t in view['tasks']]==[second_task['id']]
    assert view['tasks'][0]['business_status']=='closed'
    assert len(local.items('task_cache'))==2
    assert len(service.business_workspace(admin)['tasks'])==1
    assert len(service.business_workspace(admin,second['id'])['tasks'])==1
    assert all(e['project_id']==second['id'] for e in view['events'])
    assert len(service.business_workspace(admin,second['id'])['nodes'])==1
    assert service.business_workspace(admin,second['id'])['scope']['zones']['project']['folder_id']=='lab_phase3_project_root'
    assert not local.items('quarantine')
asyncio.run(scenario())
''')

    def test_project_work_folder_editing_and_node_removal_guard(self):
        self.check('''
async def scenario():
    second=create_project(folder_url='https://scn33386s7ui.feishu.cn/drive/folder/AbCd123?from=copy')
    assert second['folder_url']=='https://scn33386s7ui.feishu.cn/drive/folder/AbCd123'
    assert second['folder_source']=='explicit'
    scope=projects.resource_scope(second['id'],backend3.registry())
    assert scope['zones']['project']['folder_id']=='AbCd123'
    assert scope['zones']['agent']['folder_id']=='lab_phase3_agent_root'
    before=projects.update_project(second['id'],{'folder_url':''},admin,'empty-folder-edit',1)
    assert before['folder_source']=='default' and before['revision']==2
    task=await start_in_project(before)
    denied('project_not_empty',lambda:projects.update_project(second['id'],{'folder_url':'https://scn33386s7ui.feishu.cn/drive/folder/NewFolder'},admin,'move-nonempty',2))
    denied('node_has_open_tasks',lambda:projects.update_project(second['id'],{'node_ids':[]},admin,'remove-working-node',2))
    await service.apply_task_action(task['id'],'cancel',{'reason':'停止工作'},admin,'end-before-removing',1,1)
    result=projects.update_project(second['id'],{'node_ids':[]},admin,'remove-closed-node',2)
    assert result['node_ids']==[]
asyncio.run(scenario())
''')

    def test_deactivation_pauses_before_inactive_and_reenable_does_not_resume(self):
        self.check('''
async def scenario():
    default_task=await start('unaffected-default')
    second=create_project()
    first=await start_in_project(second,'first')
    already_paused=await start_in_project(second,'already-paused')
    await service.apply_task_action(already_paused['id'],'pause',{},admin,'existing-pause',1,1)
    preview=await projects.preview_deactivate(second['id'],admin)
    assert [task['task_id'] for task in preview['tasks']]==[first['id']]
    stopped=await projects.deactivate_project(second['id'],admin,'deactivate',1)
    assert stopped['status']=='inactive' and not stopped['deactivation']['pending_task_ids']
    assert backend3.read_task(default_task['id'])['business_status']=='active'
    assert backend3.read_task(first['id'])['business_status']=='paused'
    assert backend3.read_task(already_paused['id'])['record_revision']==2
    assert projects.list_projects(admin)[0]['id']==service.PROJECT_ID
    assert len(projects.list_projects(admin))==1
    assert len(projects.list_projects(admin,True))==2
    await failure('project_inactive',service.prepare_task({'title':'停用项目新工作'},admin,'inactive-draft',second['id']))
    await failure('project_inactive',service.apply_task_action(first['id'],'resume',{},admin,'inactive-resume',2,1))
    again=await projects.deactivate_project(second['id'],admin,'deactivate',1)
    assert again['status']=='inactive' and backend3.read_task(first['id'])['record_revision']==2
    enabled=projects.reactivate_project(second['id'],admin,'reactivate',stopped['revision'])
    assert enabled['status']=='active' and backend3.read_task(first['id'])['business_status']=='paused'
asyncio.run(scenario())
''')

    def test_partial_pause_preserves_operation_and_recovers_after_restart(self):
        self.check('''
async def scenario():
    second=create_project()
    task=await start_in_project(second)
    backend3.fault('task_after_commit')
    result=await projects.deactivate_project(second['id'],admin,'recoverable-deactivation',1)
    assert result['status']=='deactivating' and result['deactivation']['status']=='partial'
    assert result['deactivation']['expected_revision']==1
    assert result['deactivation']['pending_task_ids']==[task['id']]
    assert backend3.read_task(task['id'])['business_status']=='paused'
    assert backend3.read_task(task['id'])['record_revision']==2
asyncio.run(scenario())
''', restart='''
from server.main import app
from server import central_api
import httpx
async def scenario():
    second=projects.list_projects(admin,True)[1]
    task=backend3.list_tasks()[0]
    # An older progress record may predate the explicit original-baseline field.
    raw=central.get('project',second['id'])
    raw['deactivation'].pop('expected_revision',None)
    central.put('project',second['id'],raw)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:18761',
        headers={'Origin':'http://127.0.0.1:18761'}) as client:
        client.cookies.set(central_api.COOKIE,central.login(admin_username,admin_password)['token'])
        response=await client.get('/api/central/projects/'+second['id']+'/deactivation-preview')
        assert response.status_code==200,response.text
        preview=response.json()
        resume=preview['resume_operation']
        assert resume=={'operation_id':'recoverable-deactivation','expected_revision':1},preview
        assert preview['revision']==2 and preview['revision']!=resume['expected_revision']
        assert projects.get_project(second['id'])['deactivation']['expected_revision']==1
        response=await client.post('/api/central/projects/'+second['id']+'/deactivate',json=resume)
        assert response.status_code==200,response.text
        stopped=response.json()['item']
    assert stopped['status']=='inactive' and not stopped['deactivation']['pending_task_ids']
    assert backend3.read_task(task['id'])['record_revision']==2
    assert len([e for e in backend3.list_events() if e['event_type']=='task_change'])==2
asyncio.run(scenario())
''')

    def test_late_feedback_retained_and_cross_project_event_rejected(self):
        self.check('''
async def scenario():
    second=create_project()
    task=await start_in_project(second)
    await projects.deactivate_project(second['id'],admin,'stop-project',1)
    event=await service.submit_event({'event_type':'submission','body':'晚到结果保留',
        'task_refs':refs(task),'result_status':'completed'},other,'late')
    view=await service.refresh(admin,'late-read',second['id'])
    assert backend3.read_task(task['id'])['business_status']=='paused'
    assert next(item for item in view['events'] if item['id']==event['id'])['late_or_inactive']
    foreign={**backend3._get('event',event['id']),'id':'cross-project','project_id':service.PROJECT_ID}
    backend3._save('event',foreign['id'],foreign)
    await service.refresh(admin,'cross-project')
    assert any('跨项目' in record['reason'] for record in local.items('quarantine'))
    assert not any(item['id']=='cross-project' for item in service.overview(admin)['events'])
asyncio.run(scenario())
''')

    def test_failed_sync_keeps_last_success_time_and_other_project_state(self):
        self.check('''
async def scenario():
    second=create_project()
    await start_in_project(second)
    good=await service.refresh(admin,'good',second['id'])
    at=good['last_refresh']['at']
    backend3.fault('read_tasks')
    await failure('read_failed',service.refresh(admin,'failed',second['id']))
    view=service.business_workspace(admin,second['id'])
    assert view['last_refresh']['at']==at
    assert not view['last_refresh']['complete'] and view['last_refresh']['error']
    assert view['tasks'] and not service.business_workspace(admin)['tasks']
asyncio.run(scenario())
''')

    def test_deliverables_use_registered_project_folder_and_complete_independently(self):
        self.check('''
async def scenario():
    default_task=await start('default-content',task_type='deliverable',completion_rule=None)
    default_document=await artifact(default_task,'default-content')
    second=create_project(folder_url='https://scn33386s7ui.feishu.cn/drive/folder/SecondFolder')
    task=await start_in_project(second,'new-project-content',task_type='deliverable')
    document=await service.create_document({'task_id':task['id'],'requirements_version':1,
        'title':'项目成果','content':'属于新项目的正文。'},other,'second-artifact')
    assert document['project_id']==second['id'] and document['folder_id']=='SecondFolder'
    assert default_document['folder_id']=='lab_phase3_project_root'
    await service.submit_event({'event_type':'submission','body':'项目成果已提交',
        'task_refs':refs(task),'artifact_refs':[docref(document)],'result_status':'completed'},other,'second-content-result')
    await service.refresh(admin,'second-complete',second['id'])
    assert backend3.read_task(task['id'])['business_status']=='closed'
    assert backend3.read_task(default_task['id'])['business_status']=='active'
    view=service.business_workspace(admin,second['id'])
    assert {doc['id'] for doc in view['documents']}=={document['id']}
    assert all(e['project_id']==second['id'] for e in service.overview(admin,second['id'])['events'])
    assert default_document['id'] not in {doc['id'] for doc in view['documents']}
asyncio.run(scenario())
''')

    def test_deactivation_does_not_hide_an_unreadable_known_task(self):
        self.check('''
async def scenario():
    second=create_project()
    task=await start_in_project(second)
    backend3._save('task',task['id'],{'_invalid_remote_record':True,'raw':'unreadable original'})
    await failure('write_uncertain',projects.deactivate_project(second['id'],admin,'unreadable-stop',1))
    assert projects.get_project(second['id'])['status']=='active'
    assert local.get('task_cache',task['id'])['business_status']=='active'
asyncio.run(scenario())
''')

    def test_project_created_by_second_admin_uses_registered_owner(self):
        self.check('''
async def scenario():
    second_admin=central.create_user('second-admin','项目管理员',['admin'],'second-admin-password')['user']
    project=projects.create_project({'name':'第二位管理员的项目','node_ids':[other['node_id']]},second_admin,'second-admin-project')
    assert project['owner_id']==second_admin['id']
    assert service.business_workspace(second_admin,project['id'])['project']['owner_id']==second_admin['id']
    draft=await service.prepare_task({'title':'本项目工作','goal':'完成目标','deliverable':'工作结果',
        'executor_node_ids':[other['node_id']]},second_admin,'second-admin-draft',project['id'])
    assert draft['payload']['owner_id']==second_admin['id']
    task=await service.apply_task_action(draft['task_id'],'start',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},
        second_admin,'second-admin-start',0,0,project_id=project['id'])
    assert task['owner_id']==second_admin['id'] and task['project_id']==project['id']
    assert backend3.read_task(task['id'])['owner_id']==second_admin['id']
    assert projects.get_project(service.PROJECT_ID)['owner_id']==service.OWNER_ID
asyncio.run(scenario())
''')

    def test_another_admin_keeps_project_owner_and_node_cannot_change_it(self):
        self.check('''
async def scenario():
    second_admin=central.create_user('project-owner','项目负责人',['admin'],'project-owner-password')['user']
    project=projects.create_project({'name':'固定项目责任','node_ids':[other['node_id']]},second_admin,'owned-project')
    fields={'title':'另一管理员起草','goal':'完成项目工作','deliverable':'结果','executor_node_ids':[other['node_id']]}
    draft=await service.prepare_task(fields,admin,'other-admin-draft',project['id'])
    assert draft['payload']['owner_id']==second_admin['id']
    await failure('out_of_scope',service.prepare_task({**fields,'owner_id':admin['id']},admin,'wrong-owner',project['id']))
    await failure('authorization_failed',service.prepare_task(fields,other,'node-cannot-author',project['id']))
    task=await service.apply_task_action(draft['task_id'],'start',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},
        admin,'other-admin-start',0,0,project_id=project['id'])
    assert task['owner_id']==second_admin['id']
    assert (central.get('operation','other-admin-start')['authorization']['actor_id'])==admin['id']
    await service.submit_event({'event_type':'progress','body':'owner_id=自报其他负责人',
        'owner_id':admin['id'],'task_refs':refs(task)},other,'claimed-owner')
    assert backend3.read_task(task['id'])['owner_id']==second_admin['id']
    assert projects.get_project(project['id'])['owner_id']==second_admin['id']
asyncio.run(scenario())
''')


if __name__ == '__main__':
    unittest.main()
