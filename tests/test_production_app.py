"""Boot the formal ASGI entry against an isolated existing database.

The fake CLI stops at identity.  This verifies startup and HTTP routing, not
formal Feishu access, which must be proved separately at cutover.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest


class FormalAppStartupTests(unittest.TestCase):
    def _run_formal_fixture(self, *, migration_only=False):
        source = textwrap.dedent(r'''
            import hashlib, os, sqlite3, tempfile, subprocess, json, shutil
            from pathlib import Path
            from unittest.mock import patch
            from fastapi.testclient import TestClient
            from cutover.production_config import FormalConfig, BASE_TOKEN, TASK_TABLE, TASK_VIEW, HUMAN_FOLDER, AGENT_FOLDER, SELECTED_RAG_FOLDER, SHARED_SKILLS_FOLDER, FINANCE_PROJECT, FINANCE_FOLDER
            from server import production_runtime, db

            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / 'central').mkdir()
                policy = root / 'central/agent.md'
                policy.write_text('# Formal central duties\n', encoding='utf-8')
                policy.chmod(0o600)
                if os.environ.get('RDOS_TEST_FORMAL_MIGRATION') == '1':
                    (root/'central/memory.md').write_text('保留本人直接编辑的记忆文件。\n',encoding='utf-8')
                    (root/'central/memory.md').chmod(0o600)
                database = root / 'baite.db'
                with sqlite3.connect(database) as conn:
                    conn.execute('CREATE TABLE central_users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL, roles_json TEXT NOT NULL, password_hash TEXT NOT NULL, identity_mode TEXT NOT NULL, enabled INTEGER NOT NULL, created_at TEXT NOT NULL)')
                    conn.executemany('INSERT INTO central_users VALUES(?,?,?,?,?,?,?,?)', [
                        ('mem_p6BbcYJrwAcv_M5Y', 'admin', 'Formal Admin', '["admin"]', db.hash_password('fixture-password-123'), 'role_test', 1, '2026-01-01'),
                        ('mem_x_rLn9VBAe_z8xol', 'xiaofei', 'Xiaofei', '["researcher"]', 'placeholder', 'actual', 1, '2026-01-01'),
                        ('mem_g3nKLUxRovywCSTk', 'duoduo', 'Duoduo', '["researcher"]', 'placeholder', 'actual', 1, '2026-01-01'),
                    ])
                    conn.execute('CREATE TABLE central_records(kind TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(kind,id))')
                    old_rule = '执行完成、回执已读回和质量评价分别记录；只有发布时明确要求人工质量验收的任务才交管理员判断。'
                    old_policy = {'id':'central_agent_policy', 'version':7,
                                  'content':'保留本人在线编辑的职责。\n'+old_rule,
                                  'hash':'fixture-old-policy','source_kind':'panel_settings',
                                  'updated_at':'2026-01-01','updated_by':'actual-admin'}
                    custom_memory = {'id':'custom_memory','version':3,'topic':'持续偏好',
                        'topic_key':'持续偏好','scope':'organization','status':'active',
                        'content':'保留本人自己的表达与项目经验。','sources':[],
                        'account_id':'','project_id':'','created_at':'2026-01-01','updated_at':'2026-01-01'}
                    old_memory = {**custom_memory,'id':'old_rule_memory','topic':'任务完成规则',
                                  'topic_key':'任务完成规则','content':old_rule,'version':2}
                    for kind, identifier, value in (
                        ('state','central_agent_policy',old_policy),
                        ('state','central_memory_schema',{'version':2,'history_imported':False}),
                        ('central_memory',custom_memory['id'],custom_memory),
                        ('central_memory',old_memory['id'],old_memory)):
                        conn.execute('INSERT INTO central_records VALUES(?,?,?,?)',
                                     (kind,identifier,json.dumps(value,ensure_ascii=False),'2026-01-01'))
                    finance = {'id': FINANCE_PROJECT, 'name': '原财赋项目', 'status': 'active',
                               'folder_id': FINANCE_FOLDER, 'member_ids': ['mem_x_rLn9VBAe_z8xol']}
                    conn.execute('INSERT INTO central_records VALUES(?,?,?,?)',
                                 ('project', FINANCE_PROJECT, json.dumps(finance), '2026-01-01'))
                    conn.execute('INSERT INTO central_records VALUES(?,?,?,?)',
                                 ('project', 'prj_0d9b2a0ecec486cdf8', json.dumps({
                                     'id':'prj_0d9b2a0ecec486cdf8','name':'百特 AI 项目','status':'active',
                                     'rdos_project_schema':1,'member_ids':['mem_p6BbcYJrwAcv_M5Y'],
                                     'node_ids':['node_yuhan'],'nodes_explicit':True,'revision':1}), '2026-01-01'))
                    conn.execute('CREATE TABLE phase3_local(kind TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(kind,id))')
                    marker = {'source_count': 18, 'task_ids': ['task-%02d' % i for i in range(18)], 'content_sha256': 'a'*64}
                    conn.execute('INSERT INTO phase3_local VALUES(?,?,?,?)',
                                 ('state', 'formal_migration', json.dumps(marker), '2026-01-01'))
                cli = root / 'cli'
                cli.write_text('fixture', encoding='utf-8')
                cli.chmod(0o755)
                fingerprint = 'a' * 64
                resources = {'schema_version': 1, 'mode': 'production',
                    'base_token': BASE_TOKEN, 'task_table': TASK_TABLE,
                    'task_view': TASK_VIEW, 'event_table': 'tblFormalEvents123',
                    'default_project_id': 'project_rdos',
                    'finance_project_id': FINANCE_PROJECT, 'finance_folder': FINANCE_FOLDER,
                    'human_folder': HUMAN_FOLDER, 'agent_folder': AGENT_FOLDER,
                    'selected_rag_folder': SELECTED_RAG_FOLDER,
                    'shared_skills_folder': SHARED_SKILLS_FOLDER,
                    'cli_path': str(cli), 'cli_sha256': hashlib.sha256(cli.read_bytes()).hexdigest(),
                    'account_fingerprint': fingerprint,
                    'node_members': {'node_yuhan': 'mem_p6BbcYJrwAcv_M5Y',
                                     'node_xiaofei': 'mem_x_rLn9VBAe_z8xol',
                                     'node_duoduo': 'mem_g3nKLUxRovywCSTk'},
                    'agent_md': str(policy), 'memory_md': str(root / 'central/memory.md')}
                formal = FormalConfig(resources, database, cli, policy, root / 'central/memory.md')
                os.environ.update(RDOS_RUNTIME_MODE='production', BAITE_PUBLIC_URL='https://yjmt.cn/baite-rdos',
                    BAITE_DB_PATH=str(database), BAITE_SESSION_SECRET='x'*32,
                    BAITE_ADMIN_USERNAME='admin', BAITE_ADMIN_PASSWORD='not-used-in-existing-db',
                    BAITE_LLM_API_KEY='fixture-only', BAITE_LLM_BASE_URL='https://ark.cn-beijing.volces.com/api/plan/v3',
                    BAITE_LLM_MODEL='deepseek-v4-pro', BAITE_LLM_THINKING='enabled', BAITE_LLM_REASONING_EFFORT='high')
                with patch.object(production_runtime, 'load', return_value=formal):
                    from server.production_app import app
                from server import peer_tasks
                node_binary = peer_tasks.BUNDLED_NODE if Path(peer_tasks.BUNDLED_NODE).is_file() else shutil.which('node')
                if node_binary:
                    # Use the already installed test runtime; never install one.
                    production_runtime._PEER_NODE = Path(node_binary)
                from server import phase3_feishu
                records = {TASK_TABLE: {}, 'tblFormalEvents123': {}}
                task_writes = []
                native_run = subprocess.run
                def transport(argv, **kwargs):
                    if argv[0] != str(cli):
                        if str(argv[-1]).endswith('peer_crypto.cjs'):
                            assert set(kwargs['env']) == {'HOME','PATH','TMPDIR'}
                        return native_run(argv, **kwargs)
                    a = argv[1:]
                    def flag(key): return a[a.index(key)+1]
                    table = flag('--table-id') if '--table-id' in a else ''
                    if a[:3] == ['drive', 'files', 'list']:
                        folder = json.loads(flag('--params'))['folder_token']
                        value = {'data': {'files': ([{'token':BASE_TOKEN,'type':'bitable','name':'原Base','parent_token':AGENT_FOLDER}]
                                                   if folder == AGENT_FOLDER else []), 'has_more':False}}
                    elif a[:2] == ['base', '+record-list']:
                        value = {'data': {'items': list(records[table].values()), 'has_more': False}}
                    elif a[:2] == ['base', '+record-upsert']:
                        rid = flag('--record-id') if '--record-id' in a else 'recFormal'+str(len(records[table])+1)
                        records[table][rid] = {'record_id': rid, 'fields': json.loads(flag('--json'))}
                        if table == TASK_TABLE: task_writes.append(rid)
                        value = {'data': {'record_id': rid}}
                    elif a[:2] == ['base', '+record-get']:
                        value = {'data': {'record': records[table][flag('--record-id')]}}
                    else:
                        return subprocess.CompletedProcess(argv, 1, '', 'offline presentation unavailable')
                    return subprocess.CompletedProcess(argv, 0, json.dumps(value), '')
                with patch.object(phase3_feishu._CLIExecutor, 'identity', return_value={'account_fingerprint': fingerprint}), patch.object(phase3_feishu.subprocess, 'run', side_effect=transport):
                    with TestClient(app, root_path='/baite-rdos', base_url='https://yjmt.cn') as client:
                        if os.environ.get('RDOS_TEST_FORMAL_MIGRATION') == '1':
                            from server import central_store, central_settings, next_release_migration as migration
                            current = central_settings.policy_snapshot()
                            assert current['version']==8 and current['source_kind']=='release_migration'
                            assert '保留本人在线编辑的职责。' in current['content']
                            assert old_rule not in current['content']
                            assert migration.RULE in current['content'] and migration.CONTEXT_RULE in current['content']
                            assert central_store.get('central_agent_policy_history','7')==old_policy
                            assert central_store.get('central_memory','custom_memory')==custom_memory
                            changed = central_store.get('central_memory','old_rule_memory')
                            assert changed['version']==3 and old_rule not in changed['content']
                            assert any(x['content']==old_rule for x in central_store.items('central_memory_history'))
                            assert policy.read_text()=='# Formal central duties\n'
                            assert (root/'central/memory.md').read_text()=='保留本人直接编辑的记忆文件。\n'
                            from server import central_memory
                            effective = central_memory.model_snapshot()['content']
                            assert custom_memory['content'] in effective and old_rule not in effective
                            assert central_store.get('state',migration.MIGRATION)
                            assert migration.initialize()['reused']
                            assert central_settings.policy_snapshot()==current
                            assert not records[TASK_TABLE] and not records['tblFormalEvents123'] and not task_writes
                            raise SystemExit(0)
                        page = client.get('/baite-rdos/')
                        assert page.status_code == 200, page.text[:300]
                        assert '<base href="/baite-rdos/" />' in page.text
                        assert 'RDOS 实验环境' not in page.text
                        info = client.get('/baite-rdos/api/runtime/info')
                        assert info.status_code == 200 and info.json()['mode'] == 'production', info.text[:300]
                        js = client.get('/baite-rdos/static/central.js')
                        assert js.status_code == 200 and "api/runtime/info" in js.text
                        assert client.get('/baite-rdos/api/lab/info').status_code == 410
                        health = client.get('/baite-rdos/api/health')
                        assert health.status_code == 200 and health.json()['mode'] == 'production'
                        login = client.post('/baite-rdos/api/central/auth/login',
                            headers={'Origin': 'https://yjmt.cn'},
                            json={'username': 'admin', 'password': 'fixture-password-123'})
                        assert login.status_code == 200, login.text[:300]
                        assert 'Path=/baite-rdos/' in login.headers['set-cookie']
                        assert client.get('/baite-rdos/api/central/auth/me').status_code == 200
                        from server import central_store
                        denied_pause = client.patch('/baite-rdos/api/central/runtime', json={'paused': True})
                        assert denied_pause.status_code == 403
                        assert not (central_store.get('state', 'runtime') or {}).get('paused')
                        paused = client.patch('/baite-rdos/api/central/runtime',
                            headers={'Origin': 'https://yjmt.cn'}, json={'paused': True})
                        assert paused.status_code == 200 and paused.json()['runtime']['paused'] is True
                        assert client.get('/baite-rdos/api/runtime/info').json()['runtime']['paused'] is True
                        assert client.get('/baite-rdos/api/central/overview').json()['runtime']['paused'] is True
                        assert client.get('/baite-rdos/api/central/maintenance/state').json()['paused'] is True
                        resumed = client.patch('/baite-rdos/api/central/runtime',
                            headers={'Origin': 'https://yjmt.cn'}, json={'paused': False})
                        assert resumed.status_code == 200
                        assert client.get('/baite-rdos/api/runtime/info').json()['runtime']['paused'] is False
                        assert client.get('/baite-rdos/api/central/overview').json()['runtime']['paused'] is False
                        projects = client.get('/baite-rdos/api/central/projects')
                        assert projects.status_code == 200, projects.text[:300]
                        assert 'rdos-self' in projects.text
                        assert FINANCE_FOLDER in projects.text
                        assert all(p['id'] != 'prj_0d9b2a0ecec486cdf8' for p in projects.json()['items'])
                        from server import central_store
                        archived_ai = central_store.get('project','prj_0d9b2a0ecec486cdf8')
                        assert archived_ai['name'] == '百特 AI 项目' and archived_ai['merged_into'] == 'project_rdos'
                        assert archived_ai['status'] == 'inactive'
                        from server import rdos_projects
                        assert not rdos_projects.is_registered(archived_ai['id'])
                        old_project = next(p for p in projects.json()['items'] if p['id'] == FINANCE_PROJECT)
                        assert old_project['name'] == '原财赋项目'
                        assert old_project['node_ids'] == ['node_xiaofei'], old_project
                        # The original tenant folder is a registered location;
                        # adding a node must not relocate or re-register it.
                        legacy_url='https://shbebetter.feishu.cn/drive/folder/'+FINANCE_FOLDER
                        saved=central_store.get('project',FINANCE_PROJECT)
                        saved.update(folder_url=legacy_url)
                        central_store.put('project',FINANCE_PROJECT,saved)
                        edited=client.patch('/baite-rdos/api/central/projects/'+FINANCE_PROJECT,
                            headers={'Origin':'https://yjmt.cn'}, json={'operation_id':'formal-finance-members',
                                'expected_revision':old_project['revision'], 'name':old_project['name'],
                                'description':old_project.get('description',''), 'folder_url':legacy_url,
                                'node_ids':['node_xiaofei','node_yuhan']})
                        assert edited.status_code==200,edited.text
                        assert edited.json()['item']['folder_url']==legacy_url
                        assert edited.json()['item']['node_ids']==['node_xiaofei','node_yuhan']
                        manifest = client.get('/baite-rdos/nodes/node_xiaofei/manifest')
                        assert manifest.status_code == 200, manifest.text[:300]
                        assert manifest.json()['node_id'] == 'node_xiaofei'
                        assert manifest.json()['tasks_table_id'] == TASK_TABLE
                        assert all(p['id'] != archived_ai['id'] for p in manifest.json()['projects'])
                        assert manifest.json()['skill']['url'] == 'https://yjmt.cn/baite-rdos/nodes/node_xiaofei/skill.zip'
                        package = client.get('/baite-rdos/nodes/node_xiaofei/skill.zip')
                        assert package.status_code == 200
                        assert hashlib.sha256(package.content).hexdigest() == manifest.json()['skill']['sha256']
                        assert manifest.json()['peer']['available'] is True, manifest.json()['peer']
                        legacy_entry=client.post('/baite-rdos/api/central/phase3/nodes/node_yuhan/onboarding',
                            headers={'Origin':'https://yjmt.cn'},json={})
                        assert legacy_entry.status_code==200,legacy_entry.text
                        assert legacy_entry.json()['task'] is None and not task_writes
                        proof = peer_tasks.signed({'body':'正式模式签名核对'})
                        assert peer_tasks.verified(proof, peer_tasks.public_config()['central_public_key']) == {'body':'正式模式签名核对'}
                        try: production_runtime.run_peer_crypto({'action':'execute','command':'outside'})
                        except ValueError: pass
                        else: raise AssertionError('generic command escaped crypto boundary')
                        assert client.get('/baite-rdos/api/central/peer/nodes').status_code == 200
                        from server import central_service, phase3_store, phase1_operations, lab_panel
                        assert phase3_store.get('state', 'business_workflow')
                        assert phase1_operations._environment() == tuple(lab_panel.environment()[k] for k in ('env_id','generation'))
                        specs = central_service.tool_specs({'project_id':'', 'allowed_projects':['project_rdos',FINANCE_PROJECT]})
                        assert any(t['function']['name'] == 'phase3_list_tasks' for t in specs)
                        assert not any(t['function']['name'] == 'list_tasks' for t in specs)
                        headers = {'Origin': 'https://yjmt.cn'}
                        draft = client.post('/baite-rdos/api/central/phase3/drafts', headers=headers,
                            json={'operation_id':'formal-draft-proof', 'project_id':'project_rdos',
                                  'payload':{'title':'隔离动作验证','goal':'核对真实核心路径','deliverable':'结构化结果',
                                             'executor_node_ids':['node_yuhan']}})
                        assert draft.status_code == 200, draft.text[:600]
                        item = draft.json()['item']
                        action = {'operation_id':'formal-start-proof','expected_revision':0,'expected_requirements_version':0,
                                  'payload':{'draft_id':item['id'],'expected_hash':item['draft_hash']}}
                        published = client.post('/baite-rdos/api/central/phase3/tasks/'+item['task_id']+'/actions/start',
                                                headers=headers,json=action)
                        assert published.status_code == 200, published.text[:800]
                        assert published.json()['item']['business_status'] == 'active'
                        repeated = client.post('/baite-rdos/api/central/phase3/tasks/'+item['task_id']+'/actions/start',
                                               headers=headers,json=action)
                        assert repeated.status_code == 200, repeated.text[:500]
                        assert len(task_writes) == 1, task_writes
                        assert len(records[TASK_TABLE]) == 1 and len(records['tblFormalEvents123']) == 1
                        from server import phase3_backend
                        event = phase3_backend.get_backend().list_events()[0]
                        assert event['verified_actor']['identity_mode'] == 'role_test'
                        assert central_store.user('mem_p6BbcYJrwAcv_M5Y')['identity_mode'] == 'role_test'
                        # Exercise the formal node receipt and automatic close,
                        # whose private provenance is a shared-account claim.
                        from server import phase3_service, rdos_contract
                        # Only the external CLI response is synthetic. Nodes
                        # write Feishu directly; the node HTTP path is closed.
                        receipt = {'id':'formal-node-receipt','operation_id':'formal-node-receipt',
                            **phase3_service.environment(),'project_id':'project_rdos',
                            'event_type':'submission','body':'配置完成；到点运行另验。','result_status':'completed',
                            'claimed_node_id':'node_yuhan','origin':'node_report','artifact_refs':[], 'created_at':db.utc_now(),
                            'recipients':['agent'],'task_refs':[{'task_id':item['task_id'],'requirements_version':1}]}
                        wire_receipt = rdos_contract.encode(receipt,phase3_backend.get_backend().resources)
                        records['tblFormalEvents123']['recNodeReceipt']={'record_id':'recNodeReceipt','fields':{
                            '事件ID':receipt['id'],'类型':'submission','自报节点':'node_yuhan',
                            '任务引用':phase3_feishu._json(receipt['task_refs']),'操作ID':receipt['operation_id'],
                            '记录数据':json.dumps(wire_receipt,ensure_ascii=False)}}
                        synchronized = client.post('/baite-rdos/api/central/phase3/sync',headers=headers,
                            json={'operation_id':'formal-node-result-sync'})
                        assert synchronized.status_code == 200, synchronized.text[:500]
                        task = phase3_backend.get_backend().read_task(item['task_id'])
                        assert task['business_status']=='closed' and task['receipt_status']=='verified', phase3_store.items('quarantine')
                        assert all(name not in task for name in ('quality_status','decision','review_evidence'))
                        assert all(o['status']=='succeeded' for o in central_store.items('operation')
                                   if o.get('prepared_event'))
                        settled = next(e for e in phase3_backend.get_backend().list_events()
                                       if e.get('origin')=='task_execution_grant')
                        assert settled['verified_actor']['identity_mode']=='shared_account_claim'
                        grant_key=item['task_id']+'@1'
                        grant=phase3_store.get('grant',grant_key)
                        phase3_store.put('grant',grant_key,{**grant,'requirements_hash':'mismatched'})
                        untrusted = next(e for e in phase3_backend.get_backend().list_events() if e['id']==settled['id'])
                        assert untrusted['verified_actor']['identity_mode']=='actual'
                        phase3_store.put('grant',grant_key,grant)
                        # A changed public body cannot inherit the original
                        # operation's private provenance or trusted event hash.
                        raw = next(iter(records['tblFormalEvents123'].values()))
                        wire = json.loads(raw['fields']['记录数据'])
                        wire['body'] += ' changed externally'
                        raw['fields']['记录数据'] = json.dumps(wire,ensure_ascii=False)
                        altered = phase3_backend.get_backend().list_events()[0]
                        assert altered['verified_actor']['identity_mode'] == 'actual'
                        assert altered['body'] != event['body']
                    # A direct edit after any release preflight must survive a
                    # real formal lifespan, even when the DB projection differs.
                    registry_file = root / 'runtime/phase3_resources.json'
                    registry = json.loads(registry_file.read_text(encoding='utf-8'))
                    finance_view = next(p for p in registry['projects'] if p['id'] == FINANCE_PROJECT)
                    finance_view['name'] = 'direct registry edit after preflight'
                    registry_file.write_text(json.dumps(registry, ensure_ascii=False), encoding='utf-8')
                    registry_file.chmod(0o600)
                    registry_bytes = registry_file.read_bytes()
                    policy.write_text('direct user policy edit after preflight\n', encoding='utf-8')
                    memory_view = root / 'central/memory.md'
                    memory_view.write_text('direct user memory edit after preflight\n', encoding='utf-8')
                    policy.chmod(0o600)
                    memory_view.chmod(0o600)
                    with TestClient(app, root_path='/baite-rdos', base_url='https://yjmt.cn') as client:
                        assert client.get('/baite-rdos/api/health').status_code == 200
                        assert policy.read_text() == 'direct user policy edit after preflight\n'
                        assert memory_view.read_text() == 'direct user memory edit after preflight\n'
                        assert registry_file.read_bytes() == registry_bytes
                        login = client.post('/baite-rdos/api/central/auth/login',
                            headers={'Origin': 'https://yjmt.cn'},
                            json={'username': 'admin', 'password': 'fixture-password-123'})
                        assert login.status_code == 200, login.text
                        current = client.get('/baite-rdos/api/central/settings/agent').json()
                        assert current['file_updated'] is False
                        assert '重启会保留文件' in current['file_error']
                        stale = client.get('/baite-rdos/api/central/settings/memory-preview').json()
                        assert stale['available'] is False and '不会自动生效' in stale['error']
                        saved = client.put('/baite-rdos/api/central/settings/agent',
                            headers={'Origin': 'https://yjmt.cn'},
                            json={'content':'explicit Panel policy', 'expected_version':current['version'],
                                  'operation_id':'formal-explicit-policy'})
                        assert saved.status_code == 200 and saved.json()['file_updated'], saved.text
                        remembered = client.post('/baite-rdos/api/central/settings/memories',
                            headers={'Origin': 'https://yjmt.cn'},
                            json={'operation':'remember', 'operation_id':'formal-explicit-memory',
                                  'expected_version':0, 'topic':'正式记忆测试', 'content':'显式更新有效'})
                        assert remembered.status_code == 200 and remembered.json()['summary_updated'], remembered.text
                        assert client.get('/baite-rdos/api/central/settings/memory-preview').json()['available'] is True
                    assert policy.read_text() == 'explicit Panel policy\n'
                    assert '显式更新有效' in memory_view.read_text()
                    policy.write_text('direct policy edit before failed startup\n', encoding='utf-8')
                    memory_view.write_text('direct memory edit before failed startup\n', encoding='utf-8')
                    from server import central_maintenance
                    with patch.object(central_maintenance, 'start', side_effect=RuntimeError('fixture startup failure')):
                        try:
                            with TestClient(app, root_path='/baite-rdos', base_url='https://yjmt.cn'):
                                raise AssertionError('startup unexpectedly succeeded')
                        except RuntimeError as exc:
                            assert str(exc) == 'fixture startup failure'
                    assert policy.read_text() == 'direct policy edit before failed startup\n'
                    assert memory_view.read_text() == 'direct memory edit before failed startup\n'
                    assert registry_file.read_bytes() == registry_bytes
                    # A missing summary can be recreated from the current DB;
                    # an existing file is never replaced by that retry path.
                    memory_view.unlink()
                    with TestClient(app, root_path='/baite-rdos', base_url='https://yjmt.cn') as client:
                        assert client.get('/baite-rdos/api/health').status_code == 200
                    assert '显式更新有效' in memory_view.read_text()
                    assert policy.read_text() == 'direct policy edit before failed startup\n'
                    assert registry_file.read_bytes() == registry_bytes
                    memory_view.unlink()
                    from server import central_memory
                    original_link = os.link
                    def concurrent_memory_edit(source, destination, *args, **kwargs):
                        if Path(destination) == memory_view:
                            memory_view.write_text('concurrent direct memory edit\n', encoding='utf-8')
                        return original_link(source, destination, *args, **kwargs)
                    with patch.object(central_memory.os, 'link', side_effect=concurrent_memory_edit):
                        with TestClient(app, root_path='/baite-rdos', base_url='https://yjmt.cn') as client:
                            assert client.get('/baite-rdos/api/health').status_code == 200
                    assert memory_view.read_text() == 'concurrent direct memory edit\n'
                with sqlite3.connect(database) as conn:
                    assert conn.execute('SELECT COUNT(*) FROM central_users').fetchone()[0] == 3
                    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='phase3_mock'").fetchone()[0] == 0
        ''')
        env = dict(os.environ)
        env.pop('RDOS_LAB_RUNTIME', None)
        env['RDOS_TEST_FORMAL_MIGRATION'] = '1' if migration_only else '0'
        result = subprocess.run([sys.executable, '-B', '-c', source], cwd=os.getcwd(),
                                env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_existing_accounts_and_subpath_boot_without_lab_guard(self):
        self._run_formal_fixture()

    def test_formal_startup_migrates_existing_duties_and_memory_without_external_writes(self):
        self._run_formal_fixture(migration_only=True)


if __name__ == '__main__':
    unittest.main()
