"""Initial setup is part of onboarding, through the real task business path."""
import unittest
from tests.phase1_support import run_guarded
from tests.test_panel_member_api import API_SETUP

SETUP=API_SETUP+'''
from server import node_onboarding
def new_node():
    value=store.create_user('setup-member','新节点',['researcher'],'setup-member-password')
    return local.get('node',value['node']['node_id'])
def assign(n):
    return projects.create_project({'name':'节点参与项目','description':'实际工作范围',
        'node_ids':[n['node_id']]},admin,'setup-project')
'''

class NodeOnboardingTests(unittest.TestCase):
    def check(self,body,restart=None):
        result=run_guarded(SETUP+body,timeout=60,
            restart_body=None if restart is None else SETUP+restart)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_copy_prepares_one_scoped_setup_task_and_reuses_completion(self):
        self.check('''
async def scenario():
    n=new_node();project=assign(n)
    token=store.login(admin_username,admin_password)['token']
    async with authenticated(token) as client:
        route='/api/central/phase3/nodes/'+n['public_id']+'/onboarding'
        response=await client.post(route,json={})
        assert response.status_code==200,response.text
        result=response.json();task=result['task']
        assert task['project_id']==project['id'] and task['executor_node_ids']==[n['node_id']]
        assert task['business_status']=='active' and task['completion_rule']=={'kind':'receipt_recorded'}
        assert all(word in task['goal'] for word in ('持久安装','09:00','16:30','Asia/Shanghai','ID','独立读回'))
        assert n['node_id'] not in projects.project_node_ids(service.PROJECT_ID)
        assert task['task_id'] in result['instruction'] and n['public_id'] in result['instruction']
        again=await client.post(route,json={})
        assert again.status_code==200 and again.json()['task']['task_id']==task['task_id'],again.text
        assert len(backend3.list_tasks())==1
        actor=service.make_node_actor(n['node_id'])
        await service.submit_event({'event_type':'submission','task_refs':refs(task),
            'result_status':'completed','body':'Skill已安装，两条定时安排已保存并核对'},actor,'setup-result')
        await service.refresh(admin,'setup-result-sync',project['id'])
        completed=await client.post(route,json={})
        assert completed.status_code==200,completed.text
        assert completed.json()['task']['business_status']=='closed'
        assert len(backend3.list_tasks())==1
asyncio.run(scenario())
''')

    def test_requires_admin_active_membership_and_never_upgrades_legacy_nodes(self):
        self.check('''
async def scenario():
    n=new_node()
    token=store.login(admin_username,admin_password)['token']
    route='/api/central/phase3/nodes/'+n['public_id']+'/onboarding'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=BASE,headers={'Origin':BASE}) as client:
        assert (await client.post(route,json={})).status_code==401
    async with authenticated(token) as client:
        response=await client.post(route,json={})
        assert response.status_code==409,response.text
        assert not backend3.list_tasks()
        old=await client.post('/api/central/phase3/nodes/node_workbuddy/onboarding',json={})
        assert old.status_code==200 and old.json()['task'] is None,old.text
        assert not backend3.list_tasks()
        assign(n)
        member_token=store.login('setup-member','setup-member-password')['token']
        async with authenticated(member_token) as reader:
            assert (await reader.post(route,json={})).status_code==403
        assert not backend3.list_tasks()
        client.headers.pop('Origin')
        assert (await client.post(route,json={})).status_code==403
        store.update_user(n['member_id'],{'enabled':False})
        client.headers['Origin']=BASE
        assert (await client.post(route,json={})).status_code!=200
        assert not backend3.list_tasks()
asyncio.run(scenario())
''')

    def test_committed_write_with_lost_response_recovers_after_restart(self):
        self.check('''
async def scenario():
    n=new_node();assign(n)
    backend3.fault('task_after_commit')
    await failure('write_uncertain',node_onboarding.prepare(n['node_id'],admin))
    assert len(backend3.list_tasks())==1
    plan=local.get('node_onboarding',n['node_id'])
    assert central.get('operation',plan['operation_id'])['status']=='uncertain'
asyncio.run(scenario())
''',restart='''
async def scenario():
    n=next(n for n in local.items('node') if n.get('registration_source')=='member_account')
    before=local.get('node_onboarding',n['node_id'])
    result=await node_onboarding.prepare(n['node_id'],admin)
    assert result['task']['task_id']==before['task_id']
    assert len(backend3.list_tasks())==1
    assert central.get('operation',before['operation_id'])['status']=='succeeded'
asyncio.run(scenario())
''')

    def test_concurrent_copy_creates_one_task_without_parallel_schedules(self):
        self.check('''
async def scenario():
    n=new_node();assign(n)
    results=await asyncio.gather(node_onboarding.prepare(n['node_id'],admin),
        node_onboarding.prepare(n['node_id'],admin),return_exceptions=True)
    assert any(isinstance(r,dict) for r in results)
    for r in results:
        if isinstance(r,Exception):assert r.code=='write_busy',str(r)
    assert len(backend3.list_tasks())==1
    assert len(local.items('node_onboarding'))==1
asyncio.run(scenario())
''')
