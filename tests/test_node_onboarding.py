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

    def test_lost_response_with_changed_remote_is_not_adopted_or_republished(self):
        self.check('''
async def scenario():
    n=new_node();assign(n)
    backend3.fault('task_after_commit')
    await failure('write_uncertain',node_onboarding.prepare(n['node_id'],admin))
    plan=local.get('node_onboarding',n['node_id'])
    remote=backend3.read_task(plan['task_id'])
    remote['title']='外部同版本改写'
    backend3._save('task',remote['task_id'],remote)
    count=len(backend3._items('operation'))
    await failure('operation_conflict',node_onboarding.prepare(n['node_id'],admin))
    assert local.get('node_onboarding',n['node_id'])==plan
    assert len(backend3._items('operation'))==count and len(backend3.list_tasks())==1
    assert backend3.read_task(plan['task_id'])==remote
    assert not local.get('task_authority',plan['task_id']+'@1')
    assert central.get('operation',plan['operation_id'])['status']=='conflict'
asyncio.run(scenario())
''')

    def test_unpublished_plan_can_be_replanned_after_project_is_deactivated(self):
        self.check('''
from unittest.mock import patch
async def scenario():
    n=new_node();old_project=assign(n)
    with patch.object(node_onboarding.service,'apply_task_action',side_effect=RuntimeError('before dispatch')):
        try:await node_onboarding.prepare(n['node_id'],admin)
        except RuntimeError:pass
        else:raise AssertionError('expected injected interruption')
    old_plan=local.get('node_onboarding',n['node_id'])
    assert not backend3.list_tasks() and central.get('operation',old_plan['operation_id']) is None
    await projects.deactivate_project(old_project['id'],admin,'deactivate-unpublished',old_project['revision'])
    new_project=projects.create_project({'name':'新接入项目','node_ids':[n['node_id']]},admin,'new-onboard-project')
    result=await node_onboarding.prepare(n['node_id'],admin)
    assert result['task']['project_id']==new_project['id']
    assert result['task']['task_id']==old_plan['task_id']
    assert len(backend3.list_tasks())==1
    assert local.get('draft',old_plan['draft_id'])['status']=='superseded'
    assert len(local.items('node_onboarding_history'))==1
    assert (await node_onboarding.prepare(n['node_id'],admin))['task']['task_id']==old_plan['task_id']
asyncio.run(scenario())
''')

    def test_new_admin_replans_only_a_confirmed_unpublished_plan(self):
        self.check('''
from unittest.mock import patch
async def scenario():
    n=new_node();assign(n)
    with patch.object(node_onboarding.service,'apply_task_action',side_effect=RuntimeError('before dispatch')):
        try:await node_onboarding.prepare(n['node_id'],admin)
        except RuntimeError:pass
    old_plan=local.get('node_onboarding',n['node_id'])
    successor=store.create_user('successor-admin','接任管理员',['admin'],'successor-admin-password')['user']
    store.update_user(admin['id'],{'enabled':False},actor_id=successor['id'])
    result=await node_onboarding.prepare(n['node_id'],successor)
    plan=local.get('node_onboarding',n['node_id'])
    assert result['task']['task_id']==old_plan['task_id'] and plan['actor_id']==successor['id']
    assert plan['operation_id']!=old_plan['operation_id'] and plan['attempt']==1
    assert len(backend3.list_tasks())==1
asyncio.run(scenario())
''')

    def test_unknown_remote_write_does_not_replan_or_use_successor_authority(self):
        self.check('''
from unittest.mock import patch
async def scenario():
    n=new_node();assign(n)
    with patch.object(backend3,'write_task',side_effect=RuntimeError('response unknown')):
        try:await node_onboarding.prepare(n['node_id'],admin)
        except RuntimeError:pass
        else:raise AssertionError('expected injected transport failure')
    old_plan=local.get('node_onboarding',n['node_id'])
    old_op=central.get('operation',old_plan['operation_id'])
    assert old_op['status']=='uncertain' and old_op.get('dispatched_at') and not backend3.list_tasks()
    successor=store.create_user('unknown-successor','接任管理员',['admin'],'unknown-successor-password')['user']
    store.update_user(admin['id'],{'enabled':False},actor_id=successor['id'])
    await failure('write_uncertain',node_onboarding.prepare(n['node_id'],successor))
    assert local.get('node_onboarding',n['node_id'])==old_plan
    assert not local.items('node_onboarding_history') and not backend3.list_tasks()
asyncio.run(scenario())
''')

    def test_pre_dispatched_operation_is_retired_before_successor_publishes(self):
        self.check('''
from unittest.mock import patch
async def scenario():
    n=new_node();assign(n)
    with patch.object(node_onboarding.service.ops,'run_write',side_effect=RuntimeError('before dispatch')):
        try:await node_onboarding.prepare(n['node_id'],admin)
        except RuntimeError:pass
        else:raise AssertionError('expected injected interruption')
    old=local.get('node_onboarding',n['node_id'])
    op=central.get('operation',old['operation_id'])
    assert op['status']=='pending' and not op.get('dispatched_at') and not backend3.list_tasks()
    successor=store.create_user('replacement-admin','接任管理员',['admin'],'replacement-admin-password')['user']
    store.update_user(admin['id'],{'enabled':False},actor_id=successor['id'])
    result=await node_onboarding.prepare(n['node_id'],successor)
    assert result['task']['task_id']==old['task_id'] and len(backend3.list_tasks())==1
    retired=central.get('operation',old['operation_id'])
    assert retired['status']=='conflict' and retired['error_code']=='onboarding_superseded'
    assert retired['superseded_by']==local.get('node_onboarding',n['node_id'])['operation_id']
asyncio.run(scenario())
''')

    def test_retirement_between_write_admission_and_begin_cannot_revive_old_operation(self):
        self.check('''
from unittest.mock import patch
from server import phase1_operations as operations
from server.central_service import WRITE_LOCK
async def scenario():
    n=new_node();assign(n)
    with patch.object(node_onboarding.service.ops,'run_write',side_effect=RuntimeError('before dispatch')):
        try:await node_onboarding.prepare(n['node_id'],admin)
        except RuntimeError:pass
    old=local.get('node_onboarding',n['node_id'])
    replacement=await node_onboarding._new_plan(n['node_id'],admin,
        old['operation_id'],old['task_id'],1)
    original_check=operations._check_saved_confirmation
    changed=[]; sent=[]
    def interleaved(operation_id):
        value=original_check(operation_id)
        if operation_id==old['operation_id'] and not changed:
            changed.append(True)
            node_onboarding._replace_unpublished_plan(n['node_id'],old,replacement)
        return value
    async def send():
        sent.append(True)
        return {'verified':True}
    with patch.object(operations,'_check_saved_confirmation',side_effect=interleaved):
        await failure('operation_conflict',operations.run_write(
            old['operation_id'],WRITE_LOCK,send))
    assert changed and not sent and not backend3.list_tasks()
    assert central.get('operation',old['operation_id'])['status']=='conflict'
    assert local.get('node_onboarding',n['node_id'])==replacement
asyncio.run(scenario())
''')

    def test_running_old_operation_cannot_be_retired_by_another_process(self):
        self.check('''
from unittest.mock import patch
async def scenario():
    n=new_node();assign(n)
    with patch.object(node_onboarding.service.ops,'run_write',side_effect=RuntimeError('before dispatch')):
        try:await node_onboarding.prepare(n['node_id'],admin)
        except RuntimeError:pass
    old=local.get('node_onboarding',n['node_id'])
    replacement=await node_onboarding._new_plan(n['node_id'],admin,
        old['operation_id'],old['task_id'],1)
    pending=central.get('operation',old['operation_id'])
    pending.update(status='running',active_attempt=1)
    central.put('operation',old['operation_id'],pending)
    await failure('write_uncertain',asyncio.to_thread(
        node_onboarding._replace_unpublished_plan,n['node_id'],old,replacement))
    assert local.get('node_onboarding',n['node_id'])==old
    assert central.get('operation',old['operation_id'])['status']=='running'
    assert not backend3.list_tasks()
asyncio.run(scenario())
''')
