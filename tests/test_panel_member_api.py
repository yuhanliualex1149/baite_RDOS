"""Real HTTP account and project-scope acceptance in a guarded temporary app."""
from __future__ import annotations

import unittest

from tests.phase1_support import run_guarded
from tests.test_panel_projects import PROJECT_SETUP


API_SETUP = PROJECT_SETUP + '''
from server.main import app
from server import central_api, central_service as coordinator
import httpx
BASE='http://127.0.0.1:18761'
def authenticated(token):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=BASE,
        headers={'Origin':BASE},cookies={central_api.COOKIE:token})
'''


class PanelMemberAPITest(unittest.TestCase):
    def check(self, body):
        result = run_guarded(API_SETUP + body, timeout=80)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_http_account_lifecycle_revokes_sessions_and_preserves_node(self):
        self.check('''
async def scenario():
    admin_token=store.login(admin_username,admin_password)['token']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=BASE) as anonymous:
        assert (await anonymous.get('/api/central/users')).status_code==401
    async with authenticated(admin_token) as client:
        body={'username':'http-member','display_name':'研发同事','roles':['researcher'],
              'password':'http-member-initial-password'}
        response=await client.post('/api/central/users',json=body)
        assert response.status_code==200,response.text
        result=response.json();member=result['user'];node_id=result['node']['node_id']
        assert result['password']==body['password']
        duplicate=await client.post('/api/central/users',json=body)
        assert duplicate.status_code==409,duplicate.text
        listing=await client.get('/api/central/users')
        assert listing.status_code==200 and body['password'] not in listing.text,listing.text
        card=next(item for item in listing.json()['items'] if item['id']==member['id'])
        assert len(card['nodes'])==1 and card['nodes'][0]['node_id']==node_id
        assert card['nodes'][0]['onboard_url']==BASE+'/nodes/'+node_id+'/start'
        member_token=store.login(member['username'],body['password'])['token']
        renamed=await client.patch('/api/central/users/'+member['id'],json={'username':'http-renamed','display_name':'新显示名'})
        assert renamed.status_code==200 and renamed.json()['relogin_required'] is False,renamed.text
        async with authenticated(member_token) as member_client:
            assert (await member_client.get('/api/central/auth/me')).status_code==401
        assert not store.login('http-member',body['password'])
        member_token=store.login('http-renamed',body['password'])['token']
        disabled=await client.patch('/api/central/users/'+member['id'],json={'enabled':False})
        assert disabled.status_code==200 and disabled.json()['user']['enabled'] is False,disabled.text
        async with authenticated(member_token) as member_client:
            assert (await member_client.get('/api/central/auth/me')).status_code==401
        assert not store.login('http-renamed',body['password'])
        enabled=await client.patch('/api/central/users/'+member['id'],json={'enabled':True})
        assert enabled.status_code==200 and store.session(member_token) is None,enabled.text
        assert store.login('http-renamed',body['password'])
        roles=await client.patch('/api/central/users/'+member['id'],json={'roles':['admin','researcher']})
        assert roles.status_code==200,roles.text
        card=next(item for item in (await client.get('/api/central/users')).json()['items'] if item['id']==member['id'])
        assert [item['node_id'] for item in card['nodes']]==[node_id]
        assert node_id not in (await client.get('/api/central/projects')).json()['items'][0]['node_ids']
        assert not backend3.list_tasks() and not local.items('task_cache')
asyncio.run(scenario())
''')

    def test_self_edit_requires_relogin_and_last_admin_is_protected(self):
        self.check('''
async def scenario():
    token=store.login(admin_username,admin_password)['token']
    async with authenticated(token) as client:
        for edit in ({'enabled':False},{'roles':['researcher']}):
            denied=await client.patch('/api/central/users/'+admin['id'],json=edit)
            assert denied.status_code==409,denied.text
            assert store.session(token)
        legacy=db.create_admin_session(12)
        response=await client.patch('/api/central/users/'+admin['id'],json={'username':'http-admin-renamed'})
        assert response.status_code==200 and response.json()['relogin_required'] is True,response.text
        assert (await client.get('/api/central/auth/me')).status_code==401
        assert not db.admin_session_valid(legacy)
        assert db.authenticate_admin('http-admin-renamed',admin_password)
        assert not db.authenticate_admin(admin_username,admin_password)
asyncio.run(scenario())
''')

    def test_member_http_scope_only_reads_participating_project_business_data(self):
        self.check('''
async def scenario():
    member=store.create_user('http-reader','项目同事',['researcher'],'http-reader-password')['user']
    member_node=next(n for n in local.node_registry() if n.get('member_id')==member['id'])
    first=create_project('read-first',name='FIRST_ALLOWED_PROJECT',node_ids=[member_node['node_id']])
    second=create_project('read-second',name='SECOND_ALLOWED_PROJECT',node_ids=[member_node['node_id']])
    forbidden=create_project('read-forbidden',name='FORBIDDEN_PROJECT_NAME')
    one=await start_in_project(first,'first-reader-task',title='FIRST_ALLOWED_TASK',executor_node_ids=[member_node['node_id']])
    two=await start_in_project(second,'second-reader-task',title='SECOND_ALLOWED_TASK',executor_node_ids=[member_node['node_id']])
    secret=await start_in_project(forbidden,'forbidden-task',title='FORBIDDEN_TASK_TITLE')
    document=backend3.create_document('project','FIRST_ALLOWED_DOCUMENT','正文可回查','reader-document',project_id=first['id'])
    backend3.create_document('project','FORBIDDEN_DOCUMENT_TITLE','另一项目内容','forbidden-document',project_id=forbidden['id'])
    draft=await service.prepare_task({'title':'ADMIN_UNPUBLISHED_DRAFT','goal':'未确认目标',
        'deliverable':'未发布成果','executor_node_ids':[member_node['node_id']]},admin,'unpublished-reader-draft',first['id'])
    thread=coordinator.create_thread(admin,first['id'],'PRIVATE_ADMIN_CONVERSATION')
    for project in (first,second,forbidden):
        await service.refresh(admin,'read-cache-'+project['id'],project['id'])
    token=store.login(member['username'],'http-reader-password')['token']
    async with authenticated(token) as client:
        listing=await client.get('/api/central/projects?include_inactive=true')
        assert listing.status_code==200,listing.text
        assert {p['id'] for p in listing.json()['items']}=={first['id'],second['id']},listing.text
        assert listing.json()['default_folder_url']==''
        assert [n['node_id'] for n in listing.json()['nodes']]==[member_node['public_id']]
        for project,task in ((first,one),(second,two)):
            response=await client.get('/api/central/phase3/workspace',params={'project_id':project['id']})
            assert response.status_code==200,response.text
            body=response.json()
            assert body['read_only'] and [t['task_id'] for t in body['tasks']]==[task['task_id']],body
            assert body['drafts']==[]
            assert all(marker not in response.text for marker in ('FORBIDDEN_PROJECT_NAME','FORBIDDEN_TASK_TITLE','FORBIDDEN_DOCUMENT_TITLE','ADMIN_UNPUBLISHED_DRAFT','PRIVATE_ADMIN_CONVERSATION'))
            assert 'task_table_url' not in body['scope'] and 'operations' not in body
            history=await client.get('/api/central/phase3/tasks/'+task['task_id']+'/history')
            assert history.status_code==200 and history.json()['items'],history.text
            workspace=await client.get('/api/central/workspace',params={'project_id':project['id']})
            assert workspace.status_code==200 and workspace.json()['read_only'],workspace.text
            assert workspace.json()['messages']==[] and workspace.json()['runs']==[]
        for path in ('/api/central/phase3/workspace?project_id='+forbidden['id'],
                     '/api/central/workspace?project_id='+forbidden['id'],
                     '/api/central/phase3/tasks/'+secret['task_id']+'/history',
                     '/api/central/workspace?project_id='+first['id']+'&thread_id='+thread['id'],
                     '/api/central/threads?project_id='+first['id'],
                     '/api/central/users'):
            response=await client.get(path)
            assert response.status_code==403,(path,response.status_code,response.text)
        for path,body in (
            ('/api/central/users',{'username':'no-create','display_name':'无权','roles':['admin']}),
            ('/api/central/chat',{'operation_id':'no-chat','thread_id':thread['id'],'project_id':first['id'],'message':'无权发送'}),
            ('/api/central/phase3/sync?project_id='+first['id'],{'operation_id':'no-reader-sync'}),
            ('/api/central/phase3/drafts?project_id='+first['id'],{'operation_id':'no-reader-draft','payload':{}})):
            response=await client.post(path,json=body)
            assert response.status_code==403,(path,response.status_code,response.text)
        edited=await client.patch('/api/central/users/'+member['id'],json={'display_name':'无权修改'})
        assert edited.status_code==403,edited.text
asyncio.run(scenario())
''')

    def test_new_node_has_usable_entry_but_no_assigned_project(self):
        self.check('''
member=store.create_user('empty-reader','尚未分配项目',['researcher'],'empty-reader-password')['user']
new_node=next(n for n in local.node_registry() if n.get('member_id')==member['id'])
# Supply only transport registration facts in the isolated adapter. The route,
# package, registry membership and access checks below are the real functions.
class RegisteredTransport(transport.LocalBackend):
    def registry(self):
        return {**super().registry(),'base_token':'SyntheticBaseOnly',
            'tasks_table':'SyntheticTasksOnly','events_table':'SyntheticEventsOnly',
            'human_folder':'lab_phase3_project_root','agent_folder':'lab_phase3_agent_root'}
transport._LOCAL=RegisteredTransport()
async def scenario():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=BASE) as client:
        response=await client.get('/nodes/'+new_node['public_id']+'/manifest')
        assert response.status_code==200,response.text
        manifest=response.json()
        assert manifest['node_id']==new_node['public_id'] and manifest['projects']==[]
        assert not any(r.get('zone')=='project' for r in manifest['registered_resources'])
        assert not manifest['allowed_output_roots'].get('project')
        start=await client.get('/nodes/'+new_node['public_id']+'/start')
        assert start.status_code==200 and new_node['public_id'] in start.text
        assert '/nodes/'+new_node['public_id']+'/manifest' in start.text
    token=store.login(member['username'],'empty-reader-password')['token']
    async with authenticated(token) as client:
        response=await client.get('/api/central/projects')
        assert response.status_code==200 and response.json()['items']==[],response.text
        assert response.json()['nodes']==[]
        assert (await client.get('/api/central/phase3/workspace')).status_code==403
        knowledge=await client.get('/api/central/knowledge')
        assert knowledge.status_code==200,knowledge.text
asyncio.run(scenario())
''')

    def test_member_validation_never_echoes_password_and_transaction_rechecks_actor(self):
        self.check('''
async def scenario():
    marker='PRIVATE-HTTP-INITIAL-PASSWORD'
    token=store.login(admin_username,admin_password)['token']
    async with authenticated(token) as client:
        body={'username':'invalid-user','display_name':'新成员','roles':['researcher'],'password':marker}
        for invalid in ({**body,'password':{'secret':marker}}, {**body,'roles':['invalid-role']},
                        {**body,'password':marker*30}, {**body,'unknown':marker}):
            response=await client.post('/api/central/users',json=invalid)
            assert response.status_code==422,response.text
            assert marker not in response.text,response.text
        short=await client.post('/api/central/users',json={**body,'password':'short'})
        assert short.status_code==409 and 'short' not in short.text,short.text
    another=store.create_user('stale-actor','即将停用管理员',['admin'],'stale-actor-password')['user']
    store.update_user(another['id'],{'enabled':False})
    for action in (
        lambda:store.create_user('stale-created','不应创建',['admin'],'stale-created-password',actor_id=another['id']),
        lambda:store.update_user(admin['id'],{'display_name':'不应修改'},actor_id=another['id'])):
        try:action()
        except PermissionError:pass
        else:raise AssertionError('stale administrator authority accepted')
    assert not any(u['username']=='stale-created' for u in store.users())
    assert store.user(admin['id'])['display_name']==admin['display_name']
asyncio.run(scenario())
''')


if __name__ == '__main__':
    unittest.main()
