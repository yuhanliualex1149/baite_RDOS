"""Member lifecycle and reader scope use guarded, persistent SQLite paths."""
from __future__ import annotations

import unittest

from tests.phase1_support import run_guarded
from tests.test_phase3_core import SETUP


MEMBERS_SETUP = SETUP + '''
from server import rdos_projects as projects, panel_members, central_accounts
def member_nodes(member_id):
    return [n for n in local.items('node') if n.get('member_id')==member_id]
def rejected(call, message=''):
    try:call()
    except ValueError as exc:
        assert not message or message in str(exc),(message,str(exc))
    else:raise AssertionError('expected rejection')
'''


class PanelMembersTest(unittest.TestCase):
    def check(self, body, restart=None):
        result = run_guarded(MEMBERS_SETUP + body, timeout=60,
            restart_body=MEMBERS_SETUP + restart if restart else None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_researcher_creation_registers_one_node_without_granting_a_project(self):
        self.check('''
before = projects.project_node_ids(service.PROJECT_ID)
result = store.create_user('new-researcher','新同事',['researcher'],'researcher-creation-password')
member = result['user']
assert member['identity_mode']=='actual'
assert len(member_nodes(member['id']))==1 and result['node']['member_id']==member['id']
assert result['node']['node_id']==member_nodes(member['id'])[0]['public_id']
assert projects.project_node_ids(service.PROJECT_ID)==before
assert projects.list_projects(member,True)==[]
assert panel_members.visible_nodes(member)==[]
assert not local.items('task_cache')
rejected(lambda:store.create_user('new-researcher','不同姓名',['researcher'],'another-password-only'),'已存在')
assert len(member_nodes(member['id']))==1
store.update_user(member['id'],{'display_name':'更新姓名'})
store.update_user(member['id'],{'roles':['admin']})
store.update_user(member['id'],{'roles':['researcher','admin']})
assert len(member_nodes(member['id']))==1
''')

    def test_first_researcher_role_adds_one_node_and_existing_nodes_stay_stable(self):
        self.check('''
created=store.create_user('only-admin','管理同事',['admin'],'admin-creation-password')
member=created['user']
assert 'node' not in created and not member_nodes(member['id'])
store.update_user(member['id'],{'roles':['admin','researcher']})
node_id=member_nodes(member['id'])[0]['node_id']
store.update_user(member['id'],{'roles':['researcher']})
assert [n['node_id'] for n in member_nodes(member['id'])]==[node_id]
old_nodes=[n['node_id'] for n in member_nodes('mem_lab_workbuddy_0001')]
store.update_user('mem_lab_workbuddy_0001',{'display_name':'现有研发成员'})
assert [n['node_id'] for n in member_nodes('mem_lab_workbuddy_0001')]==old_nodes
''')

    def test_default_dynamic_membership_is_frozen_once_before_new_node(self):
        self.check('''
original=central.get('project',service.PROJECT_ID)
original['nodes_explicit']=False
central.put('project',service.PROJECT_ID,original)
third={'node_id':'node_existing_third','public_id':'node_third','member_id':'mem_existing_third',
       'enabled':True,'display_name':'已有第三节点'}
local.put('node',third['node_id'],third)
before=projects.project_node_ids(service.PROJECT_ID)
assert third['node_id'] in before
member=store.create_user('after-freeze','新增研发',['researcher'],'new-after-freeze-password')['user']
saved=central.get('project',service.PROJECT_ID)
assert saved['nodes_explicit'] is True and saved['node_ids']==before
assert member_nodes(member['id'])[0]['node_id'] not in saved['node_ids']
projects.initialize(backend3.registry())
assert central.get('project',service.PROJECT_ID)['revision']==saved['revision']
assert projects.project_node_ids(service.PROJECT_ID)==before
''')

    def test_node_insert_failure_rolls_back_account_and_default_migration(self):
        self.check('''
import sqlite3
original=central.get('project',service.PROJECT_ID)
original['nodes_explicit']=False
central.put('project',service.PROJECT_ID,original)
with db.connection() as conn:
    conn.executescript("CREATE TRIGGER fail_member_node BEFORE INSERT ON phase3_local WHEN NEW.kind='node' BEGIN SELECT RAISE(ABORT,'node unavailable'); END;")
try:store.create_user('rolled-back','不能半成功',['researcher'],'atomic-node-password')
except sqlite3.IntegrityError:pass
else:raise AssertionError('expected actual SQLite insert failure')
assert not any(u['username']=='rolled-back' for u in store.users())
assert central.get('project',service.PROJECT_ID)['nodes_explicit'] is False
''')

    def test_disable_only_revokes_login_and_reenable_never_revives_old_session(self):
        self.check('''
password='disable-member-password'
member=store.create_user('to-disable','可停用成员',['researcher'],password)['user']
nodes=member_nodes(member['id'])
token=store.login(member['username'],password)['token']
store.update_user(member['id'],{'enabled':False})
assert store.login(member['username'],password) is None and store.session(token) is None
assert member_nodes(member['id'])==nodes
store.update_user(member['id'],{'enabled':True})
assert store.session(token) is None and store.login(member['username'],password)
assert member_nodes(member['id'])==nodes
store.put('state','member-restart-test',{'member_id':member['id'],'old_token':token,'nodes':nodes})
''', restart='''
saved=store.get('state','member-restart-test')
assert store.session(saved['old_token']) is None
assert store.login('to-disable','disable-member-password')
assert member_nodes(saved['member_id'])==saved['nodes']
''')

    def test_login_name_and_roles_revoke_sessions_and_update_legacy_mirror(self):
        self.check('''
token=store.login(admin_username,admin_password)['token']
legacy=db.create_admin_session(12)
store.update_user(admin['id'],{'username':'renamed-administrator'})
assert store.session(token) is None and not db.admin_session_valid(legacy)
assert store.login(admin_username,admin_password) is None
assert db.authenticate_admin('renamed-administrator',admin_password)
assert not db.authenticate_admin(admin_username,admin_password)
other_admin=store.create_user('another-admin','另一管理员',['admin'],'another-admin-password')['user']
token=store.login('renamed-administrator',admin_password)['token']
store.update_user(admin['id'],{'roles':['researcher']})
assert store.session(token) is None
assert not db.authenticate_admin('renamed-administrator',admin_password)
central_accounts.change_password(admin['id'],admin_password,'demoted-new-password','demoted-new-password')
assert not db.authenticate_admin('renamed-administrator','demoted-new-password')
store.update_user(admin['id'],{'roles':['admin']})
assert db.authenticate_admin('renamed-administrator','demoted-new-password')
store.update_user(admin['id'],{'enabled':False})
assert not db.authenticate_admin('renamed-administrator','demoted-new-password')
store.update_user(admin['id'],{'enabled':True})
assert db.authenticate_admin('renamed-administrator','demoted-new-password')
''')

    def test_concurrent_last_admin_changes_leave_an_enabled_administrator(self):
        self.check('''
from concurrent.futures import ThreadPoolExecutor
import threading
second=store.create_user('second-admin','另一管理员',['admin'],'second-admin-password')['user']
barrier=threading.Barrier(2)
def disable(user_id):
    barrier.wait()
    try:store.update_user(user_id,{'enabled':False})
    except ValueError:return 'denied'
    return 'disabled'
with ThreadPoolExecutor(max_workers=2) as pool:
    results=list(pool.map(disable,[admin['id'],second['id']]))
assert sorted(results)==['denied','disabled'],results
assert sum(u['enabled'] and 'admin' in u['roles'] for u in store.users())==1
last=next(u for u in store.users() if u['enabled'] and 'admin' in u['roles'])
rejected(lambda:store.update_user(last['id'],{'roles':['researcher']}),'启用的管理员')
''')

    def test_reader_scope_and_cards_are_checked_against_current_account_state(self):
        self.check('''
member=store.create_user('scoped-reader','项目同事',['researcher'],'scoped-reader-password')['user']
member_node=member_nodes(member['id'])[0]
assigned=projects.create_project({'name':'参与项目','node_ids':[member_node['node_id']]},admin,'member-project')
other_project=projects.create_project({'name':'其他项目','node_ids':[other['node_id']]},admin,'other-project')
assert [p['id'] for p in projects.list_projects(member,True)]==[assigned['id']]
assert projects.get_project(assigned['id'],member)['id']==assigned['id']
rejected(lambda:projects.get_project(other_project['id'],member),'未参与')
rejected(lambda:projects.get_project(service.PROJECT_ID,member),'未参与')
rejected(lambda:projects.create_project({'name':'越权项目','node_ids':[]},member,'no-create'),'管理员')
cards=panel_members.users_view(admin)['items']
card=next(u for u in cards if u['id']==member['id'])
assert card['nodes'][0]['node_id']==member_node['public_id']
assert card['nodes'][0]['onboard_url'].endswith('/nodes/'+member_node['public_id']+'/start')
rejected(lambda:panel_members.users_view(member),'管理员')
assert [n['node_id'] for n in panel_members.visible_nodes(member)]==[member_node['public_id']]
store.update_user(member['id'],{'enabled':False})
rejected(lambda:projects.list_projects(member),'不能读取')
''')

    def test_password_bounds_duplicate_names_and_member_records_do_not_store_secrets(self):
        self.check('''
marker='NOT-A-LOGGED-INITIAL-PASSWORD'
for password in ('short',marker*30):
    rejected(lambda:store.create_user('bad-password','新同事',['researcher'],password),'10 至 500')
result=store.create_user('with-password','新同事',['researcher'],marker)
member=result['user']
assert result['password']==marker and store.login('with-password',marker)
store.update_user(member['id'],{'display_name':'改名'})
rejected(lambda:store.update_user(member['id'],{'username':admin_username}),'已存在')
assert store.user(member['id'])['username']=='with-password'
assert marker not in json.dumps(panel_members.users_view(admin))
with db.connection() as conn:
    bodies=''.join(r[0] for r in conn.execute('SELECT payload FROM central_records'))
    assert marker not in bodies
generated=store.create_user('generated-password','自动密码',['admin'])
assert len(generated['password'])>=10
assert generated['password'] not in json.dumps(panel_members.users_view(admin))
''')


if __name__ == '__main__':
    unittest.main()
