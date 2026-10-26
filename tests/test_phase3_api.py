"""Transport and authentication checks run inside the real disposable lab guard."""
from __future__ import annotations

import unittest

from tests.phase1_support import run_guarded


class Phase3APITest(unittest.TestCase):
    def guarded(self, body: str):
        result = run_guarded(body, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_real_guarded_task_core_lifecycle_and_exact_artifact_acceptance(self):
        self.guarded('''
from server.main import app
from server import central_api, phase3_service
import httpx
phase3_service.initialize()

async def check():
    token = store.login(admin_username, admin_password)['token']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:18761',
            headers={'Origin':'http://127.0.0.1:18761'},cookies={central_api.COOKIE:token}) as client:
        payload={'title':'API真实核心闭环','goal':'只保留一个场景','owner_id':admin['id'],
            'executor_node_ids':['node_lab_phase3_codex'],'input_refs':[],
            'deliverable':'一份介绍与版本证据','output_zone':'project','dispatch_to':['agent'],
            'return_to':['agent','human'],'completion_rule':{'kind':'human'},'due_at':''}
        response=await client.post('/api/central/phase3/drafts',json={'operation_id':'core-draft','payload':payload})
        assert response.status_code==200,response.text
        draft=response.json()['item']
        async def act(task,action,operation_id,payload,revision=None,version=None):
            response=await client.post('/api/central/phase3/tasks/'+task['task_id']+'/actions/'+action,json={
                'operation_id':operation_id,'payload':payload,
                'expected_revision':task.get('record_revision',0) if revision is None else revision,
                'expected_requirements_version':task.get('requirements_version',0) if version is None else version})
            assert response.status_code==200,response.text
            return response.json()['item']
        initial={'task_id':draft['task_id'],'record_revision':0,'requirements_version':0}
        task=await act(initial,'start','core-start',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']})
        assert task['business_status']=='active' and task['requirements_version']==1,task
        replay=await act(initial,'start','core-start',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']})
        assert replay['record_revision']==task['record_revision'],replay
        paused=await act(task,'pause','core-pause',{'reason':'测试暂停'})
        assert paused['business_status']=='paused' and paused['requirements_version']==1,paused
        resumed=await act(paused,'resume','core-resume',{'reason':'恢复当前有效要求'})
        assert resumed['business_status']=='active' and resumed['requirements_version']==1,resumed
        update_payload={**payload,'task_id':task['task_id'],'goal':'仅保留一个具体场景与一个例子','change_reason':'收敛目标；保留旧稿，新稿重新执行'}
        prepared=await client.post('/api/central/phase3/drafts',json={'operation_id':'core-update-draft','payload':update_payload})
        assert prepared.status_code==200,prepared.text
        update_draft=prepared.json()['item']
        stale=await client.post('/api/central/phase3/tasks/'+task['task_id']+'/actions/update',json={
            'operation_id':'core-stale','expected_revision':task['record_revision'],'expected_requirements_version':1,
            'payload':{'draft_id':update_draft['id'],'expected_hash':update_draft['draft_hash']}})
        assert stale.status_code==409,stale.text
        updated=await act(resumed,'update','core-update',{'draft_id':update_draft['id'],'expected_hash':update_draft['draft_hash']})
        assert updated['requirements_version']==2 and updated['goal']==update_payload['goal'],updated
        # Real registered test-node tools and mock authority, with no patched
        # permission, publication, readback or state-transition implementation.
        node=phase3_service.make_node_actor('node_lab_phase3_codex')
        document=await phase3_service.create_document({'task_id':task['task_id'],'requirements_version':2,
            'title':'API闭环产物','content':'一个场景与一个例子'},node,'core-artifact-create')
        ref={'document_id':document['id'],'revision':document['revision'],'content_hash':document['content_hash']}
        await phase3_service.submit_event({'event_type':'submission','body':'已提交当前要求v2产物',
            'task_refs':[{'task_id':task['task_id'],'requirements_version':2}],
            'artifact_refs':[ref]},node,'core-node-submission')
        synced=await client.post('/api/central/phase3/sync',json={'operation_id':'core-sync'})
        assert synced.status_code==200,synced.text
        workspace=await client.get('/api/central/phase3/workspace')
        assert workspace.status_code==200,workspace.text
        submitted=next(t for t in workspace.json()['tasks'] if t['task_id']==task['task_id'])
        assert submitted['business_status']=='pending_review',submitted
        wrong=await client.post('/api/central/phase3/tasks/'+task['task_id']+'/actions/accept_close',json={
            'operation_id':'core-wrong-artifact','expected_revision':submitted['record_revision'],'expected_requirements_version':2,
            'payload':{'reason':'错误版本不得通过','artifact_id':ref['document_id'],'artifact_revision':99,'artifact_hash':ref['content_hash']}})
        assert wrong.status_code==409,wrong.text
        closed=await act(submitted,'accept_close','core-close',{'reason':'本人测试管理员明确验收此版本',
            'artifact_id':ref['document_id'],'artifact_revision':ref['revision'],'artifact_hash':ref['content_hash']})
        assert closed['business_status']=='closed',closed
        history=await client.get('/api/central/phase3/tasks/'+task['task_id']+'/history')
        assert history.status_code==200 and 'requirements_version' in history.text,history.text
        workspace=await client.get('/api/central/phase3/workspace')
        assert workspace.status_code==200,workspace.text
        current=next(t for t in workspace.json()['tasks'] if t['task_id']==task['task_id'])
        assert current['business_status']=='closed' and current['requirements_version']==2,current
        cancelled_draft_response=await client.post('/api/central/phase3/drafts',json={
            'operation_id':'core-cancel-draft','payload':{**payload,'title':'单独取消测试'}})
        assert cancelled_draft_response.status_code==200,cancelled_draft_response.text
        cancelled_draft=cancelled_draft_response.json()['item']
        cancel_task=await act({'task_id':cancelled_draft['task_id']},'start','core-cancel-start',{
            'draft_id':cancelled_draft['id'],'expected_hash':cancelled_draft['draft_hash']})
        cancelled=await act(cancel_task,'cancel','core-cancel',{'reason':'单独取消，不记为成功完成'})
        assert cancelled['business_status']=='closed' and cancelled['close_reason']=='cancelled',cancelled
        refused=await client.post('/api/central/phase3/tasks/'+cancelled['task_id']+'/actions/resume',json={
            'operation_id':'core-cancel-resume','expected_revision':cancelled['record_revision'],
            'expected_requirements_version':cancelled['requirements_version'],'payload':{}})
        assert refused.status_code==409,refused.text
asyncio.run(check())
''')

    def test_admin_identity_origin_and_strict_envelopes(self):
        self.guarded('''
from server.main import app
from server import central_api, phase3_api
import httpx

calls = []
phase3_api.service.initialize()
phase3_api.service.business_workspace = lambda actor: {'tasks': [], 'actor_id': actor['id']}
async def prepare(payload, actor, opid):
    calls.append((payload, actor['id'], opid))
    return {'id':'draft-api','draft_hash':'a'*64,'payload':payload}
phase3_api.service.prepare_task = prepare
redacted = phase3_api._display({'id':'sensitive-draft','draft_hash':'c'*64,'payload':{'password':'PRIVATE-DRAFT-SECRET'}})
assert redacted['display_redacted'] and redacted['draft_hash']=='c'*64,redacted
assert 'PRIVATE-DRAFT-SECRET' not in json.dumps(redacted),redacted

async def check():
    token = store.login(admin_username, admin_password)['token']
    member = store.create_user('api-researcher','测试研究员',['researcher'],'synthetic-researcher-password')['user']
    member_token = store.login(member['username'],'synthetic-researcher-password')['token']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:18761') as client:
        assert (await client.get('/api/central/phase3/workspace')).status_code == 401
        client.cookies.set(central_api.COOKIE, member_token)
        assert (await client.get('/api/central/phase3/workspace')).status_code == 403
        client.cookies.set(central_api.COOKIE, token)
        changed = await client.get('/api/central/phase3/workspace',headers={'X-RDOS-Actor':member['id']})
        assert changed.status_code == 409, changed.text
        good = await client.get('/api/central/phase3/workspace',headers={'X-RDOS-Actor':admin['id']})
        assert good.status_code == 200 and good.json()['actor_id'] == 'member_admin', good.text
        body = {'operation_id':'draft-api-op','payload':{'title':'接口草稿'}}
        no_origin = await client.post('/api/central/phase3/drafts',json=body)
        assert no_origin.status_code == 403, no_origin.text
        wrong_origin = await client.post('/api/central/phase3/drafts',json=body,headers={'Origin':'https://untrusted.example'})
        assert wrong_origin.status_code == 403, wrong_origin.text
        client.headers['Origin']='http://127.0.0.1:18761'
        extra = await client.post('/api/central/phase3/drafts',json={**body,'actor':{'roles':['admin'],'secret':'never-echo-input'}})
        assert extra.status_code == 422 and 'never-echo-input' not in extra.text, extra.text
        response = await client.post('/api/central/phase3/drafts',json=body)
        assert response.status_code == 200, response.text
        assert calls == [(body['payload'],admin['id'],'draft-api-op')], calls
        invalid = await client.post('/api/central/phase3/tasks/task-api/actions/update',json={
            'operation_id':'api-action','expected_revision':'1','expected_requirements_version':1,'payload':{}})
        assert invalid.status_code == 422, invalid.text
        assert (await client.post('/api/central/phase3/tasks/task-api/actions/approve',json={
            'operation_id':'api-action','expected_revision':1,'expected_requirements_version':1})).status_code in (422,503)
        assert (await client.get('/api/lab/tasks')).status_code == 503
        assert (await client.post('/api/lab/feedback',json={})).status_code == 503
asyncio.run(check())
''')

    def test_expected_versions_action_errors_and_draft_edit(self):
        self.guarded('''
from server.main import app
from server import central_api, phase3_api
import httpx

seen = []
phase3_api.service.initialize()
async def action(taskid, action, payload, actor, opid, revision, version):
    seen.append((taskid,action,payload,actor['id'],opid,revision,version))
    if revision == 1:
        error = ValueError('当前任务要求已经变化，请重新核对版本')
        error.code = 'version_conflict'
        raise error
    return {'task_id':taskid,'record_revision':revision+1,'requirements_version':version,'business_status':'paused'}
async def edit(draftid,payload,actor,expected_hash):
    seen.append((draftid,payload,actor['id'],expected_hash))
    return {'id':draftid,'payload':payload,'draft_hash':'b'*64}
phase3_api.service.apply_task_action = action
phase3_api.service.edit_draft = edit
phase3_api.service.task_history = lambda taskid,actor: [{'task_id':taskid,'requirements_version':2}]
phase3_api.service.get_task = lambda taskid,actor: {'task_id':taskid,'title':'当前任务'}

async def check():
    token = store.login(admin_username, admin_password)['token']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:18761',
            headers={'Origin':'http://127.0.0.1:18761'},cookies={central_api.COOKIE:token}) as client:
        body={'operation_id':'versioned-action','expected_revision':1,'expected_requirements_version':2,'payload':{'reason':'暂停核对'}}
        stale=await client.post('/api/central/phase3/tasks/task-api/actions/pause',json=body)
        assert stale.status_code == 409 and stale.json()['detail']['code']=='version_conflict', stale.text
        body['expected_revision']=3
        current=await client.post('/api/central/phase3/tasks/task-api/actions/pause',json=body)
        assert current.status_code==200 and current.json()['item']['record_revision']==4, current.text
        assert seen[-1] == ('task-api','pause',body['payload'],admin['id'],'versioned-action',3,2),seen
        updated=await client.post('/api/central/phase3/drafts/draft-api/edit',json={'payload':{'title':'修改草稿'},'expected_hash':'a'*64})
        assert updated.status_code==200 and updated.json()['item']['draft_hash']=='b'*64,updated.text
        assert seen[-1]==('draft-api',{'title':'修改草稿'},admin['id'],'a'*64),seen
        history=await client.get('/api/central/phase3/tasks/task-api/history')
        assert history.status_code==200 and history.json()['items'][0]['requirements_version']==2,history.text
asyncio.run(check())
''')

    def test_public_onboard_has_only_rules_and_registered_feishu_links(self):
        self.guarded('''
from server.main import app
from server import phase3_api, phase3_backend as transport
import httpx

phase3_api.service.initialize()
registered=transport.get_backend().registry
transport.get_backend().registry=lambda: {**registered(),'base_token':'base_fixture','tasks_table':'tbl_tasks','events_table':'tbl_events','human_folder':'lab_phase3_project_root','agent_folder':'lab_phase3_agent_root'}
async def check():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:18761') as client:
        response=await client.get('/lab/onboard/node_lab_phase3_codex')
        assert response.status_code==200,response.text
        assert '/nodes/node_codex/manifest' in response.text and '旧版说明' not in response.text
        response=await client.get('/nodes/node_codex/start')
        assert response.status_code==200,response.text
        assert 'https://github.com/larksuite/cli' in response.text
        assert 'lark-cli auth status --verify' in response.text
        for forbidden in ('role_test','node_lab','实验','PRIVATE-TASK-BODY','PRIVATE-CREDENTIAL','<script>'):
            assert forbidden not in response.text,(forbidden,response.text)
        manifest=await client.get('/nodes/node_codex/manifest')
        assert manifest.status_code==200,manifest.text
        data=manifest.json()
        assert data['node_id']=='node_codex' and data['protocol_version']==2
        assert not any(key in data for key in ('env_id','generation','identity_mode','tasks','credentials','server_path'))
        assert (await client.get('/nodes/not-a-node/start')).status_code==404
        assert (await client.post('/nodes/node_codex/start',json={})).status_code in (405,503)
asyncio.run(check())
''')


if __name__ == '__main__':
    unittest.main()
