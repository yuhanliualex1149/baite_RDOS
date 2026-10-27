"""Current Panel accounts, real session/role checks, isolated guarded databases."""
from __future__ import annotations

import unittest

from tests.phase1_support import run_guarded


API = '''
from fastapi import FastAPI
from server import central_accounts as accounts
from server import central_api
import httpx
app = FastAPI()
app.include_router(accounts.router)
'''


class PanelAccountsTest(unittest.TestCase):
    def guarded(self, body, restart_body=None):
        result = run_guarded(API + body, restart_body=API + restart_body if restart_body else None, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_self_change_invalidates_all_target_sessions_and_legacy_mirror(self):
        self.guarded('''
new_password = 'my-new-password-for-accounts'
first = store.login(admin_username,admin_password)['token']
second = store.login(admin_username,admin_password)['token']
legacy = db.create_admin_session(12)
async def check():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:18761',cookies={central_api.COOKIE:first}) as client:
        response = await client.post('/api/central/auth/change-password',json={
            'current_password':admin_password,'new_password':new_password,'confirm_password':new_password})
        assert response.status_code == 200,response.text
        assert response.json() == {'ok':True,'relogin_required':True}
        assert 'Max-Age=0' in response.headers['set-cookie']
        assert admin_password not in response.text and new_password not in response.text
asyncio.run(check())
assert store.session(first) is None and store.session(second) is None
assert not db.admin_session_valid(legacy)
assert store.login(admin_username,admin_password) is None
assert store.login(admin_username,new_password)['user']['id'] == admin['id']
assert db.authenticate_admin(admin_username,new_password)
assert not db.authenticate_admin(admin_username,admin_password)
with db.connection() as conn:
    old = conn.execute('SELECT password_hash FROM admin_account WHERE id=1').fetchone()[0]
    current = conn.execute('SELECT password_hash FROM central_users WHERE id=?',(admin['id'],)).fetchone()[0]
    assert old == current and new_password not in current
audits = store.items('account_audit')
assert len(audits) == 1 and audits[0]['action'] == 'password_changed'
assert set(audits[0]) == {'id','actor_id','target_id','created_at','action'}
assert store.get('state','bootstrap_password_status')['invalidated'] is True
assert new_password not in json.dumps(store.items('account_audit'))
''')

    def test_admin_reset_verifies_actor_and_keeps_admin_session(self):
        self.guarded('''
member_password = 'member-old-password-only'
new_password = 'member-new-password-only'
member = store.create_user('member','研发人员',['researcher'],member_password)['user']
actor_token = store.login(admin_username,admin_password)['token']
member_token = store.login(member['username'],member_password)['token']
async def check():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:18761') as client:
        endpoint = '/api/central/users/'+member['id']+'/reset-password'
        body = {'current_password':admin_password,'new_password':new_password,'confirm_password':new_password}
        assert (await client.post(endpoint,json=body)).status_code == 401
        client.cookies.set(central_api.COOKIE,member_token)
        denied = await client.post('/api/central/users/'+admin['id']+'/reset-password',json={**body,'current_password':member_password})
        assert denied.status_code == 403,denied.text
        client.cookies.set(central_api.COOKIE,actor_token)
        wrong = await client.post(endpoint,json={**body,'current_password':'incorrect-admin-password'})
        assert wrong.status_code == 403,wrong.text
        assert store.session(member_token) and not store.items('account_audit')
        self_reset = await client.post('/api/central/users/'+admin['id']+'/reset-password',json=body)
        assert self_reset.status_code == 409,self_reset.text
        ok = await client.post(endpoint,json=body)
        assert ok.status_code == 200 and ok.json()['relogin_required'] is False,ok.text
        assert 'set-cookie' not in ok.headers
asyncio.run(check())
assert store.session(actor_token) and store.session(member_token) is None
assert store.login(member['username'],member_password) is None
assert store.login(member['username'],new_password)
assert store.get('state','bootstrap_password_status') is None
audit = store.items('account_audit')[0]
assert audit['action']=='password_reset' and audit['target_id']==member['id'] and audit['actor_id']==admin['id']
''')

    def test_failed_change_and_invalid_shape_do_not_echo_or_persist_secrets(self):
        self.guarded('''
token = store.login(admin_username,admin_password)['token']
marker = 'PRIVATE-new-password-not-for-logs'
async def check():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:18761',cookies={central_api.COOKIE:token}) as client:
        endpoint='/api/central/auth/change-password'
        base={'current_password':admin_password,'new_password':marker,'confirm_password':marker}
        for body in ({**base,'new_password':{'password':marker}}, {**base,'unexpected':marker}, {**base,'new_password':marker*30}, {**base,'new_password':'short'}):
            response=await client.post(endpoint,json=body)
            assert response.status_code==422,response.text
            assert marker not in response.text and admin_password not in response.text,response.text
        mismatch=await client.post(endpoint,json={**base,'confirm_password':'another-new-password'})
        assert mismatch.status_code==409,mismatch.text
        assert marker not in mismatch.text
        wrong=await client.post(endpoint,json={**base,'current_password':'wrong-password'})
        assert wrong.status_code==403,wrong.text
        assert marker not in wrong.text
asyncio.run(check())
assert store.session(token) and store.login(admin_username,admin_password)
assert not store.items('account_audit')
with db.connection() as conn:
    records=''.join(row[0] for row in conn.execute('SELECT payload FROM central_records'))
    assert marker not in records
''')

    def test_password_and_invalidated_bootstrap_survive_fresh_process(self):
        self.guarded('''
old=store.login(admin_username,admin_password)['token']
accounts.change_password(admin['id'],admin_password,'restart-new-password','restart-new-password')
store.put('state','password-test-old-session',{'token':old})
''', restart_body='''
assert store.login(admin_username,admin_password) is None
assert store.login(admin_username,'restart-new-password')
assert db.authenticate_admin(admin_username,'restart-new-password')
assert not store.session(store.get('state','password-test-old-session')['token'])
assert store.get('state','bootstrap_password_status')['invalidated'] is True
assert len(store.items('account_audit'))==1
''')

    def test_audit_failure_rolls_back_hash_mirror_and_session_revocations(self):
        self.guarded('''
import sqlite3
token=store.login(admin_username,admin_password)['token']
legacy=db.create_admin_session(12)
with db.connection() as conn:
    conn.executescript("CREATE TRIGGER fail_account_audit BEFORE INSERT ON central_records WHEN NEW.kind='account_audit' BEGIN SELECT RAISE(ABORT,'audit unavailable'); END;")
try:
    accounts.change_password(admin['id'],admin_password,'rollback-new-password','rollback-new-password')
except sqlite3.IntegrityError:pass
else:raise AssertionError('expected real database failure')
assert store.session(token) and db.admin_session_valid(legacy)
assert store.login(admin_username,admin_password) and db.authenticate_admin(admin_username,admin_password)
assert store.login(admin_username,'rollback-new-password') is None
assert store.get('state','bootstrap_password_status') is None
assert not store.items('account_audit')
''')

    def test_local_recovery_only_targets_enabled_admin_and_has_no_http_endpoint(self):
        self.guarded('''
member=store.create_user('local-member','研发人员',['researcher'],'member-recovery-password')['user']
disabled=store.create_user('disabled-admin','停用管理员',['admin'],'disabled-admin-password')['user']
store.update_user(disabled['id'],{'enabled':False})
for name in ('local-member','disabled-admin','missing-admin'):
    try:
        accounts.recover_password(name,'recovery-new-password','recovery-new-password')
    except accounts.AccountError as exc:
        assert exc.status==404
    else:raise AssertionError('recovered invalid account')
token=store.login(admin_username,admin_password)['token']
legacy=db.create_admin_session(12)
count=len(store.users())
assert accounts.recover_password(admin_username,'recovery-new-password','recovery-new-password')['ok']
assert len(store.users())==count
assert store.session(token) is None and not db.admin_session_valid(legacy)
assert store.login(admin_username,'recovery-new-password')
audit=store.items('account_audit')[0]
assert audit['action']=='password_recovered' and audit['actor_id']=='local:operator'
assert not any('recover' in route.path for route in accounts.router.routes)
''')


if __name__ == '__main__':
    unittest.main()
