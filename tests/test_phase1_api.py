"""API acceptance uses the real ASGI app in a guarded disposable experiment."""
from __future__ import annotations

import unittest
from tests.phase1_support import run_guarded


class Phase1APITest(unittest.TestCase):
    def guarded(self, body):
        result = run_guarded(body, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_route_boundary_and_required_thread(self):
        self.guarded('''
from server.main import app
from server import central_api
import httpx

async def check():
    token = store.login(admin['username'], os.environ['BAITE_ADMIN_PASSWORD'])['token']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:18760',
        headers={'Origin': 'http://127.0.0.1:18760'}, cookies={central_api.COOKIE: token}) as client:
        assert (await client.get('/api/lab/info')).status_code == 200
        for method, path in [('GET','/api/lab/context'), ('POST','/api/lab/feedback'),
                ('GET','/api/central/onboarding/status'), ('POST','/api/central/tasks'),
                ('POST','/api/central/tasks/fake/withdraw'), ('PATCH','/api/central/runtime'),
                ('POST','/api/central/sources'),
                ('GET','/api/admin/overview')]:
            response = await client.request(method, path, json={})
            assert response.status_code == 503, (method,path,response.status_code)
        # Project management is now explicitly enabled, with strict envelopes.
        assert (await client.post('/api/central/projects',json={})).status_code == 422
        assert (await client.post('/api/central/users',json={})).status_code == 422
        missing = await client.post('/api/central/chat', json={'message':'只讨论','operation_id':'missing-thread','project_id':project_id})
        assert missing.status_code == 422, missing.text
        created = await client.post('/api/central/threads', json={'project_id':project_id,'title':'API隔离测试'})
        assert created.status_code == 200, created.text
        thread = created.json()['item']
        listed = await client.get('/api/central/threads', params={'project_id':project_id})
        assert thread['id'] in [item['id'] for item in listed.json()['items']]
        mismatch = await client.post('/api/central/chat', json={'message':'只讨论','operation_id':'wrong-project',
             'project_id':'','thread_id':thread['id']})
        assert mismatch.status_code in (403,409), mismatch.text
asyncio.run(check())
''')

    def test_history_run_access_and_pagination(self):
        self.guarded('''
from server.main import app
from server import central_api, central_service as service
import httpx

async def check():
    other = store.create_user('other-admin','另一测试管理员',['admin'],'synthetic-other-password')['user']
    own_thread = service.create_thread(admin,project_id,'本人线程')
    foreign_thread = service.create_thread(other,project_id,'他人线程')
    for key, owner, thread in [('own',admin,own_thread),('foreign',other,foreign_thread)]:
        run = {'id':'run-'+key,'user_id':owner['id'],'project_id':project_id,'thread_id':thread['id'],
             'kind':'chat','trigger':'manual','status':'succeeded','created_at':'2026-10-19T00:00:00+00:00'}
        store.put('run',run['id'],run)
        store.put('message',run['id']+':user',{'id':run['id']+':user','run_id':run['id'],'role':'user',
             'project_id':project_id,'thread_id':thread['id'],'kind':'chat','trigger':'manual',
             'content':'private-'+key,'created_at':'2026-10-19T00:00:00+00:00'})
    store.put('run','run-system',{'id':'run-system','user_id':'system:scheduler','project_id':project_id,
        'kind':'sync','trigger':'scheduled','status':'failed','created_at':'2026-10-19T00:00:01+00:00'})
    token = store.login(admin['username'], os.environ['BAITE_ADMIN_PASSWORD'])['token']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:18760',
        headers={'Origin':'http://127.0.0.1:18760'}, cookies={central_api.COOKIE:token}) as client:
        unscoped = await client.get('/api/central/workspace',params={'project_id':project_id})
        assert unscoped.status_code == 200, unscoped.text
        assert unscoped.json()['messages'] == []
        scoped = await client.get('/api/central/workspace',params={'project_id':project_id,'thread_id':own_thread['id']})
        assert [item['content'] for item in scoped.json()['messages']] == ['private-own'], scoped.text
        foreign = await client.get('/api/central/workspace',params={'project_id':project_id,'thread_id':foreign_thread['id']})
        assert foreign.status_code == 403, foreign.text
        assert (await client.get('/api/central/runs/run-foreign')).status_code == 403
        assert (await client.post('/api/central/runs/run-foreign/retry')).status_code == 403
        assert (await client.get('/api/central/runs/run-system')).status_code == 200
        first = (await client.get('/api/central/runs',params={'limit':1})).json()
        assert first['total'] == 2 and first['next_offset'] == 1, first
        second = (await client.get('/api/central/runs',params={'limit':1,'offset':1})).json()
        assert {first['items'][0]['id'],second['items'][0]['id']} == {'run-own','run-system'}
        assert second['next_offset'] is None
        overview = await client.get('/api/central/overview')
        assert overview.json()['messages'] == [], overview.text
asyncio.run(check())
''')

    def test_mock_chat_draft_confirmation_readback_and_no_thread_sync(self):
        self.guarded('''
from server.main import app
from server import central_api, central_service as service, phase1_runtime
import httpx
phase1_runtime.initialize()

async def check():
    token = store.login(admin['username'], admin_password)['token']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:18761',
        headers={'Origin':'http://127.0.0.1:18761'}, cookies={central_api.COOKIE:token}) as client:
        thread = (await client.post('/api/central/threads', json={'project_id':project_id})).json()['item']
        response = await client.post('/api/central/chat', json={'message':'准备一份测试任务草稿',
            'operation_id':'api-draft','thread_id':thread['id'],'project_id':project_id})
        assert response.status_code == 200, response.text
        run_id = response.json()['run_id']
        await asyncio.gather(*list(service.ACTIVE_RUN_TASKS.values()))
        run_response = await client.get('/api/central/runs/'+run_id)
        run = run_response.json()['run']
        assert run['status'] == 'succeeded', run
        assert run['usage']['tool_calls'] == 3, run
        assert backend.stats(project_id).get('task_writes',0) == 0
        data = (await client.get('/api/central/workspace',params={'project_id':project_id,'thread_id':thread['id']})).json()
        assert len(data['messages']) == 2, data['messages']
        draft = next(item for item in data['task_drafts'] if item['run_id'] == run_id)
        assert draft['status'] == 'draft'
        path = '/api/central/task-drafts/'+draft['id']+'/publish'
        stale = await client.post(path,json={'expected_hash':'0'*64,'operation_id':'api-stale-confirm'})
        assert stale.status_code == 409, stale.text
        assert backend.stats(project_id).get('task_writes',0) == 0
        body = {'expected_hash':draft['draft_hash'],'operation_id':'api-confirm'}
        publish = await client.post(path,json=body)
        assert publish.status_code == 200, publish.text
        assert publish.json()['item']['status'] == 'published', publish.text
        repeat = await client.post(path,json=body)
        assert repeat.status_code == 200, repeat.text
        assert backend.stats(project_id)['task_writes'] == 1
        remote = next(t for t in backend.make_tools(project_id).list_tasks()['items'] if t['task_id']==draft['task_id'])
        assert remote['instructions'] == draft['instructions'], remote
        fresh = (await client.post('/api/central/threads',json={'project_id':project_id})).json()['item']
        clean = await client.get('/api/central/workspace',params={'project_id':project_id,'thread_id':fresh['id']})
        assert clean.json()['messages'] == [], clean.text
        sync = await client.post('/api/central/sync',json={'project_id':project_id,'operation_id':'api-check-once'})
        assert sync.status_code == 200, sync.text
        await asyncio.gather(*list(service.ACTIVE_RUN_TASKS.values()))
        synced = (await client.get('/api/central/runs/'+sync.json()['run_id'])).json()['run']
        assert synced['status'] == 'succeeded' and synced['thread_id'] is None, synced
        assert not any(m.get('run_id')==synced['id'] for m in store.items('message'))
        assert backend.stats(project_id)['task_writes'] == 1
        assert not phase1_runtime.overview()['scheduler_enabled']
    await service.shutdown()
asyncio.run(check())
''')

    def test_confirmed_proposal_cannot_edit_or_reject_before_reconciliation(self):
        self.guarded('''
from server.main import app
from server import central_api, central_service as service
from unittest.mock import patch
import httpx

async def check():
    proposal = {'id':'proposal-uncertain','status':'draft','project_id':project_id,
        'target_id':'lab_doc_intro','target_kind':'docx','base_hash':'1'*64,
        'draft_hash':'2'*64,'old_text':'原始合成正文','new_text':'已确认合成改动',
        'summary':'合成修改','publication_operation_id':'original-confirmation',
        'confirmed_by':admin['id'],'confirmed_at':'2026-10-19T00:00:00+00:00'}
    store.put('proposal',proposal['id'],proposal)
    store.put('operation','original-confirmation',{'id':'original-confirmation',
        'status':'uncertain','project_id':project_id,'dispatched_at':'2026-10-19T00:00:00+00:00'})
    token = store.login(admin['username'],admin_password)['token']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:18761',
        headers={'Origin':'http://127.0.0.1:18761'},cookies={central_api.COOKIE:token}) as client:
        with patch.object(service,'source_read',side_effect=AssertionError('source must not be read')) as source_read:
            edit = await client.post('/api/central/proposals/'+proposal['id']+'/edit',json={
                'summary':'试图更换已确认内容','old_text':'原始合成正文','new_text':'新的合成内容'})
            assert edit.status_code == 409, edit.text
            reject = await client.post('/api/central/proposals/'+proposal['id']+'/reject',json={'reason':'试图清除已确认操作'})
            assert reject.status_code == 409, reject.text
            assert source_read.call_count == 0
        assert store.get('proposal',proposal['id']) == proposal
        assert store.get('operation','original-confirmation')['status'] == 'uncertain'
        assert backend.stats(project_id).get('document_writes',0) == 0
asyncio.run(check())
''')

    def test_retained_diagnostics_are_redacted_and_draft_hash_is_preserved(self):
        self.guarded('''
from server.main import app
from server import central_api, central_service as service, phase1_runtime
import httpx
phase1_runtime.initialize()

async def check():
    private = '/Users/synthetic/private.txt Bearer synthetic-only-bearer'
    run = {'id':'run-retained','user_id':admin['id'],'project_id':project_id,'kind':'chat',
        'trigger':'manual','status':'failed','created_at':'2026-10-19T00:00:00+00:00',
        'technical_detail':private,'steps':[{'credential':'synthetic-only-credential'}]}
    store.put('run',run['id'],run)
    draft = {'id':'proposal-redacted','project_id':project_id,'status':'draft','draft_hash':'a'*64,
        'summary':'合成记录','old_text':'原文','new_text':'password=synthetic-only-password'}
    store.put('proposal',draft['id'],draft)
    phase1_runtime.record_failure(project_id,'read','test-source','timeout',private)
    token=store.login(admin['username'],admin_password)['token']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:18761',
        headers={'Origin':'http://127.0.0.1:18761'},cookies={central_api.COOKIE:token}) as client:
        for path in ('/api/central/overview','/api/central/diagnostics','/api/central/workspace?project_id='+project_id,
                     '/api/central/runs','/api/central/runs/run-retained'):
            response=await client.get(path)
            assert response.status_code==200,(path,response.text)
            for forbidden in ('/Users/synthetic','synthetic-only-bearer','synthetic-only-credential','synthetic-only-password'):
                assert forbidden not in response.text,(path,forbidden)
            if path.endswith(('overview','diagnostics')):
                assert response.json()['source_environment']=='local_mock'
                assert response.json()['client_acceptance']=='pending'
        overview=(await client.get('/api/central/overview')).json()
        shown=next(p for p in overview['proposals'] if p['id']==draft['id'])
        assert shown['draft_hash']==draft['draft_hash'] and shown['display_redacted'] is True
        assert store.get('proposal',draft['id'])==draft
        assert store.get('run',run['id'])==run
asyncio.run(check())
''')


if __name__ == '__main__':
    unittest.main()
