"""Business/developer boundaries through the real API and isolated persistent backend."""
from __future__ import annotations

import unittest

from tests.phase1_support import run_guarded


class BusinessAPITest(unittest.TestCase):
    def guarded(self, body):
        result = run_guarded(body, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_developer_entry_requires_admin_and_explicit_server_switch(self):
        self.guarded('''
from server.main import app
from server import central_api, phase3_service as service, phase3_store as local
import httpx
service.initialize()
local.put('quarantine','developer-evidence',{'raw':'PRIVATE-DEVELOPER-DETAIL'})
async def check():
    token=store.login(admin_username,admin_password)['token']
    member=store.create_user('business-member','研发人员',['researcher'],'business-only-password')['user']
    member_token=store.login(member['username'],'business-only-password')['token']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:18761') as client:
        for route in ('/dev','/api/central/phase3/developer'):
            assert (await client.get(route)).status_code==401
        client.cookies.set(central_api.COOKIE,token)
        for route in ('/dev','/api/central/phase3/developer'):
            response=await client.get(route)
            assert response.status_code==404,response.text
        config=root/'runtime/developer_ui.json'
        for bad in ('{}','[]','{"enabled":"true"}','{"enabled":false}','not JSON'):
            config.write_text(bad)
            assert (await client.get('/dev')).status_code==404
        config.write_text('{"enabled":true}')
        page=await client.get('/dev')
        assert page.status_code==200 and '<base href="/"' in page.text,page.text
        developer=await client.get('/api/central/phase3/developer')
        assert developer.status_code==200 and 'PRIVATE-DEVELOPER-DETAIL' in developer.text,developer.text
        client.cookies.set(central_api.COOKIE,member_token)
        for route in ('/dev','/api/central/phase3/developer'):
            assert (await client.get(route)).status_code==403
        client.cookies.set(central_api.COOKIE,token)
        config.write_text('{"enabled":false}')
        assert (await client.get('/api/central/phase3/developer')).status_code==404
asyncio.run(check())
''')

    def test_regular_project_list_hides_developer_projects_even_with_query_flag(self):
        self.guarded('''
from server.main import app
from server import central_api, phase3_service as service, phase3_store as local
import httpx
service.initialize()
local.put('state','business_workflow',{'version':1})
async def check():
    token=store.login(admin_username,admin_password)['token']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:18761',cookies={central_api.COOKIE:token}) as client:
        regular=await client.get('/api/central/projects')
        assert regular.status_code==200,regular.text
        assert [p['id'] for p in regular.json()['items']]==[local.PROJECT_ID],regular.text
        denied=await client.get('/api/central/projects?developer=true')
        assert [p['id'] for p in denied.json()['items']]==[local.PROJECT_ID],denied.text
        (root/'runtime/developer_ui.json').write_text('{"enabled":true}')
        still_regular=await client.get('/api/central/projects')
        assert [p['id'] for p in still_regular.json()['items']]==[local.PROJECT_ID]
        developer=await client.get('/api/central/projects?developer=true')
        ids={p['id'] for p in developer.json()['items']}
        assert local.PROJECT_ID in ids and 'lab_project_phase0' in ids,developer.text
asyncio.run(check())
''')

    def test_public_manifest_contains_registered_resources_but_no_task_body_or_server_state(self):
        self.guarded('''
from server.main import app
from server import phase3_service as service, phase3_backend as transport, phase3_store as local
import httpx
service.initialize()
registered=transport.get_backend().registry
transport.get_backend().registry=lambda: {**registered(),'base_token':'base_fixture','tasks_table':'tbl_tasks','events_table':'tbl_events','human_folder':'lab_phase3_project_root','agent_folder':'lab_phase3_agent_root'}
async def check():
    node=service.make_node_actor('node_lab_phase3_codex')
    draft=await service.prepare_task({'title':'PRIVATE-TASK-TITLE','goal':'PRIVATE-TASK-BODY',
        'deliverable':'PRIVATE-TASK-OUTPUT','executor_node_ids':[node['node_id']]},admin,'private-draft')
    await service.apply_task_action(draft['task_id'],'start',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'private-start',0,0)
    local.put('state','private-diagnostics',{'server_path':str(root),'env_id':os.environ['RDOS_LAB_ENV_ID'],'secret':'PRIVATE-DIAGNOSTIC'})
    def keys(value):
        if isinstance(value,dict):
            for key,item in value.items():
                yield key
                yield from keys(item)
        elif isinstance(value,list):
            for item in value:yield from keys(item)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:18761') as client:
        for route in ('/nodes/node_codex/start','/nodes/node_codex/manifest'):
            response=await client.get(route)
            assert response.status_code==200,response.text
            for private in ('PRIVATE-TASK-TITLE','PRIVATE-TASK-BODY','PRIVATE-TASK-OUTPUT','PRIVATE-DIAGNOSTIC',str(root),os.environ['RDOS_LAB_ENV_ID'],'role_test','node_lab_'):
                assert private not in response.text,(private,response.text)
        response=await client.get('/nodes/node_codex/manifest')
        manifest=response.json()
        assert manifest['node_id']=='node_codex' and manifest['protocol_version']==2
        assert not set(keys(manifest)) & {'env_id','generation','identity_mode','tasks','task_body','credentials','server_path','runtime_path','password','access_token'}
        assert manifest['skill']['url']=='http://127.0.0.1:18761/nodes/node_codex/skill.zip'
        assert (await client.get('/nodes/unknown/manifest')).status_code==404
        assert (await client.get('/nodes/unknown/skill.zip')).status_code==404
        # The package is built from the current registered scope, never a stale runtime ZIP.
        package=await client.get('/nodes/node_codex/skill.zip')
        import hashlib,io,zipfile
        assert package.status_code==200,package.text
        assert hashlib.sha256(package.content).hexdigest()==manifest['skill']['sha256']
        assert 'rdos-workflow/SKILL.md' in zipfile.ZipFile(io.BytesIO(package.content)).namelist()
        assert (await client.post('/nodes/node_codex/manifest',json={})).status_code in (405,503)
asyncio.run(check())
''')

    def test_normal_workspace_excludes_developer_evidence_and_preserves_business_completion(self):
        self.guarded('''
from server.main import app
from server import central_api, phase3_service as service, phase3_store as local
import httpx
service.initialize()
async def check():
    node=service.make_node_actor('node_lab_phase3_codex')
    draft=await service.prepare_task({'title':'更新工作方式','goal':'采用当前执行规范','deliverable':'配置结果',
        'executor_node_ids':[node['node_id']]},admin,'work-draft')
    task=await service.apply_task_action(draft['task_id'],'start',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'work-start',0,0)
    await service.submit_event({'event_type':'submission','body':'工作方式已更新','result_status':'completed',
        'task_refs':[{'task_id':task['task_id'],'requirements_version':1}]},node,'work-result')
    await service.refresh(admin,'work-readback')
    local.put('quarantine','private-debug',{'raw':'PRIVATE-RAW-PROTOCOL'})
    token=store.login(admin_username,admin_password)['token']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:18761',cookies={central_api.COOKIE:token}) as client:
        response=await client.get('/api/central/phase3/workspace')
        assert response.status_code==200,response.text
        value=response.json()
        assert not {'quarantine','operations','presentations','env_id','generation'} & set(value)
        assert 'PRIVATE-RAW-PROTOCOL' not in response.text and 'role_test' not in response.text and 'node_lab_' not in response.text
        current=next(t for t in value['tasks'] if t['task_id']==task['task_id'])
        assert current['completion_rule']=={'kind':'receipt_recorded'} and current['business_status']=='closed'
        assert current['result_status']=='completed' and current['receipt_status']=='verified' and 'quality_status' not in current
        assert not current.get('decision')
        assert current['executor_node_ids']==['node_codex']
        assert all('body' not in event and 'verified_actor' not in event for event in value['events'])
asyncio.run(check())
''')

    def test_third_registered_node_can_receive_task_using_public_identifier(self):
        self.guarded('''
from server.main import app
from server import central_api, phase3_service as service, phase3_store as local, phase3_backend as transport
import httpx
service.initialize()
registered=transport.get_backend().registry
transport.get_backend().registry=lambda: {**registered(),'base_token':'base_fixture','tasks_table':'tbl_tasks','events_table':'tbl_events','human_folder':'lab_phase3_project_root','agent_folder':'lab_phase3_agent_root'}
local.put('node','internal-third-node',{'node_id':'internal-third-node','public_id':'node_research','label':'研发助理',
    'display_name':'研发助理','member_id':'member-third','enabled':True})
async def check():
    token=store.login(admin_username,admin_password)['token']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:18761',
        headers={'Origin':'http://127.0.0.1:18761'},cookies={central_api.COOKIE:token}) as client:
        # Registration alone grants no Default membership. Assign this new
        # node explicitly before it can receive a project task.
        assigned=await client.patch('/api/central/projects/'+service.PROJECT_ID,json={
            'operation_id':'assign-third','expected_revision':1,
            'node_ids':['node_codex','node_workbuddy','node_research']})
        assert assigned.status_code==200,assigned.text
        workspace=await client.get('/api/central/phase3/workspace')
        assert workspace.status_code==200,workspace.text
        registered=workspace.json()['nodes']
        assert len(registered)==3
        assert next(n for n in registered if n['public_id']=='node_research')['node_id']=='node_research'
        manifest=await client.get('/nodes/node_research/manifest')
        assert manifest.status_code==200 and manifest.json()['node_id']=='node_research',manifest.text
        response=await client.post('/api/central/phase3/drafts',json={'operation_id':'third-draft','payload':{
            'title':'资料核对','goal':'核对当前依据','deliverable':'核对结果','executor_node_ids':['node_research']}})
        assert response.status_code==200,response.text
        draft=response.json()['item']
        response=await client.post('/api/central/phase3/tasks/'+draft['task_id']+'/actions/start',json={
            'operation_id':'third-start','expected_revision':0,'expected_requirements_version':0,
            'payload':{'draft_id':draft['id'],'expected_hash':draft['draft_hash']}})
        assert response.status_code==200,response.text
        assert response.json()['item']['executor_node_ids']==['node_research']
asyncio.run(check())
''')


if __name__=='__main__':unittest.main()
