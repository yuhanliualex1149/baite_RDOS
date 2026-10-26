"""Phase 3 egress tests use a real audit hook and an offline CLI boundary."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

CODE = Path(__file__).resolve().parents[1]
CLI = Path('/opt/homebrew/lib/node_modules/@larksuite/cli/bin/lark-cli')


def isolated(body, *, guarded=True):
    with tempfile.TemporaryDirectory(prefix='rdos-phase3-egress-') as temporary:
        root = Path(temporary).resolve()
        for directory in ('code', 'config', 'runtime', 'runtime/tmp', 'nodes'):
            (root / directory).mkdir(exist_ok=True)
        for name in ('lab', 'server'):
            shutil.copytree(CODE / name, root / 'code' / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        cfg = {'schema_version': 1, 'environment': 'rdos_local_lab',
               'env_id': 'rdos2_lab_123456abcdef', 'root': str(root), 'port': 18761,
               'backends': {'documents': 'local_mock', 'model': 'local_mock'},
               'external_resources': {'enabled': False, 'folder': '', 'documents': [],
                                      'task_table': '', 'feedback': '', 'recipient': ''},
               'phase3': {'enabled': True, 'cli_path': str(CLI),
                          'cli_sha256': hashlib.sha256(CLI.read_bytes()).hexdigest(),
                          'registry_path': str(root / 'runtime/phase3_resources.json')}}
        (root / 'config/lab.json').write_text(json.dumps(cfg))
        (root / 'runtime/instance.json').write_text(json.dumps({'env_id': cfg['env_id'], 'generation': '1'*32}))
        registry = {'schema_version': 1, 'state': 'ready', 'env_id': cfg['env_id'], 'generation': '1'*32,
                    'project_id': 'lab_project_phase3_collab', 'backend': 'feishu_test',
                    'human_folder': 'CHZvfeJH5l5yGVdZhXIceSg5n6e',
                    'agent_folder': 'ZyWCfai9jlvVcMdo3iocwUSAnMe',
                    'base_token': 'basSyntheticPhase3', 'tasks_table': 'tblSyntheticTasks',
                    'events_table': 'tblSyntheticEvents', 'documents': [
                        {'id': 'docSyntheticPhase3', 'folder_id': 'CHZvfeJH5l5yGVdZhXIceSg5n6e',
                         'zone': 'human', 'title': 'Synthetic document', 'operation_id': 'seed'}],
                    'account_fingerprint': hashlib.sha256(json.dumps(
                        {'userOpenId': 'synthetic-user', 'appId': 'synthetic-app', 'brand': 'feishu'},
                        sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()}
        (root / 'runtime/phase3_resources.json').write_text(json.dumps(registry))
        env = {'PATH': '/usr/bin:/bin', 'HOME': os.environ['HOME'],
               'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(root / 'code'),
               'RDOS_LAB_MODE': '1', 'RDOS_LAB_ROOT': str(root),
               'RDOS_LAB_RUNTIME': str(root / 'runtime'), 'RDOS_LAB_ENV_ID': cfg['env_id'],
               'RDOS_LAB_GENERATION': '1'*32}
        bootstrap = '''
import json, os, sys, subprocess, socket
from pathlib import Path
from unittest.mock import patch
from lab import config, guard
from server import phase3_feishu as fs
root = Path(os.environ['RDOS_LAB_ROOT'])
cfg = config.load(root)
fs.configure(cfg)
guard.install(root, cfg['port'])
backend = fs.get_backend()
def denied(fn):
    try: fn()
    except (PermissionError, fs.FeishuTestError, ValueError): return
    raise AssertionError('expected refusal')
calls, rows = [], {}
document = {'title':'Synthetic document','content':'Original human text','revision_id':1}
documents={'docSyntheticPhase3':document}
folders={fs.HUMAN_FOLDER:[{'token':'docSyntheticPhase3','name':'Synthetic document','type':'docx','parent_token':fs.HUMAN_FOLDER}],
         fs.AGENT_FOLDER:[{'token':'oldUntouched','name':'Old draft','type':'docx','parent_token':fs.AGENT_FOLDER},
                          {'token':'basSyntheticPhase3','name':'Synthetic Base','type':'bitable','parent_token':fs.AGENT_FOLDER}]}
tables,field_rows={},{}
def remote(argv, **kw):
    sys.audit('subprocess.Popen', argv[0], argv, kw.get('cwd'), kw.get('env'))
    calls.append(tuple(argv[1:]))
    a = argv[1:]
    def flag(k): return a[a.index(k)+1]
    if a[:2] == ['auth','status']:
        response = {'userOpenId':'synthetic-user','appId':'synthetic-app','brand':'feishu','identity':'user','defaultAs':'auto'}
    elif a[:3] == ['drive','files','list']:
        folder=json.loads(flag('--params'))['folder_token']
        response={'data':{'files':folders[folder],'has_more':False}}
    elif a[:2] == ['base','+base-create']:
        folders[fs.AGENT_FOLDER].append({'token':'basCreated','name':flag('--name'),'type':'bitable','parent_token':fs.AGENT_FOLDER})
        response={'data':{'app_token':'basCreated'}}
    elif a[:2] == ['base','+table-list']:
        response={'data':{'items':list(tables.values()),'has_more':False}}
    elif a[:2] == ['base','+table-create']:
        identifier='tblCreated'+str(len(tables)+1)
        tables[identifier]={'table_id':identifier,'name':flag('--name')}
        field_rows[identifier]=[]
        response={'data':{'table_id':identifier}}
    elif a[:2] == ['base','+field-list']:
        response={'data':{'items':field_rows[flag('--table-id')],'has_more':False}}
    elif a[:2] == ['base','+field-create']:
        field=json.loads(flag('--json'));field_rows[flag('--table-id')].append(field)
        response={'data':{'field':field}}
    elif a[:2] == ['base','+record-list']:
        response = {'data':{'items':list(rows.values()),'has_more':False}}
    elif a[:2] == ['base','+record-upsert']:
        rid = flag('--record-id') if '--record-id' in a else 'recSynthetic'+str(len(rows)+1)
        rows[rid] = {'record_id':rid,'fields':json.loads(flag('--json'))}
        response = {'data':{'record_id':rid}}
    elif a[:2] == ['base','+record-get']:
        response = {'data':{'record':rows[flag('--record-id')]}}
    elif a[:2] == ['docs','+fetch']:
        response = {'data':{'document':dict(documents[flag('--doc')])}}
    elif a[:2] == ['docs','+create']:
        import re,html
        title=html.unescape(re.search(r'<title>(.*?)</title>',kw['input']).group(1))
        identifier='docCreated'+str(len(documents)+1)
        documents[identifier]={'title':title,'content':kw['input'],'revision_id':1}
        folder=flag('--parent-token')
        folders[folder].append({'token':identifier,'name':title,'type':'docx','parent_token':folder})
        response={'data':{'document_id':identifier}}
    elif a[:2] == ['docs','+update']:
        document=documents[flag('--doc')]
        if int(flag('--revision-id')) != document['revision_id']:
            return subprocess.CompletedProcess(argv,1,'','revision conflict: stale revision')
        import re
        document['content'] = re.sub(flag('--pattern'),lambda _:kw['input'],document['content'])
        document['revision_id'] += 1
        response = {'data':{'revision_id':document['revision_id']}}
    else:
        raise AssertionError('unexpected offline command '+str(a[:2]))
    return subprocess.CompletedProcess(argv,0,json.dumps(response),'')
'''
        if not guarded:
            bootstrap = bootstrap.replace("guard.install(root, cfg['port'])", "# setup is deliberately outside the Panel guard")
        return subprocess.run([sys.executable, '-B', '-c', textwrap.dedent(bootstrap)+'\n'+textwrap.dedent(body)],
                              cwd=root / 'code', env=env, capture_output=True, text=True, timeout=30)


class Phase3EgressTest(unittest.TestCase):
    def check(self, body):
        result = isolated(body)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_typed_task_write_independent_readback_and_stable_operation(self):
        self.check('''
            task = {'id':'lab_task_p3','task_id':'lab_task_p3','title':'Synthetic',
                    'env_id':cfg['env_id'],'generation':'1'*32,'project_id':'lab_project_phase3_collab',
                    'requirements_version':1,'record_revision':1,'business_status':'active',
                    'executor_node_ids':['node_lab_codex'],'goal':'Complete original text'}
            with patch.object(fs.subprocess,'run',side_effect=remote):
                result=backend.write_task(task,'op-test-1')
                assert result['goal'] == task['goal'] and result['record_id']
                before=len(calls)
                again=backend.write_task(task,'op-test-1')
                assert again['record_id']==result['record_id']
                assert not any(c[:2]==('base','+record-upsert') for c in calls[before:])
                denied(lambda:backend.write_task({**task,'goal':'changed'},'op-test-1'))
                assert sum(c[:2]==('base','+record-upsert') for c in calls)==1
                assert calls[-1][:2] != ('base','+record-upsert')
        ''')

    def test_old_objects_raw_process_and_network_remain_denied(self):
        self.check('''
            with patch.object(fs.subprocess,'run',side_effect=remote):
                denied(lambda:backend.read_document('JEI3daO6GouQcVxFJfrcPKDunff'))
                denied(lambda:backend.create_document('outside','x','y','bad-zone'))
                denied(lambda:backend.append_event({'id':'x','project_id':'other'},'bad-project'))
            assert calls==[]
            denied(lambda:sys.audit('subprocess.Popen',cfg['phase3']['cli_path'],
                    [cfg['phase3']['cli_path'],'auth','status'],str(root/'runtime'),{}))
            closed=socket.socket();closed.close()
            denied(lambda:closed.connect(('192.0.2.1',443)))
            from server.feishu_tools import FeishuTools
            denied(lambda:FeishuTools())
        ''')

    def test_exact_permit_is_single_use_and_never_allows_other_command(self):
        self.check('''
            def replay(argv, **kw):
                denied(lambda:sys.audit('subprocess.Popen',argv[0],
                    [argv[0],'config','show'],kw['cwd'],kw['env']))
                response=remote(argv,**kw)
                denied(lambda:sys.audit('subprocess.Popen',argv[0],argv,kw['cwd'],kw['env']))
                return response
            with patch.object(fs.subprocess,'run',side_effect=replay):
                assert backend.read_document('docSyntheticPhase3')['content']=='Original human text'
        ''')

    def test_lost_write_response_only_reconciles_and_preserves_full_body(self):
        self.check('''
            event={'id':'evt-p3','event_type':'result','body':'Exact original body',
                   'env_id':cfg['env_id'],'generation':'1'*32,'project_id':'lab_project_phase3_collab',
                   'claimed_node_id':'node_lab_codex','identity_mode':'role_test','task_refs':[]}
            def lost(argv,**kw):
                result=remote(argv,**kw)
                if argv[1:3]==['base','+record-upsert']:
                    raise subprocess.TimeoutExpired(argv,1)
                return result
            with patch.object(fs.subprocess,'run',side_effect=lost):
                result=backend.append_event(event,'op-event-1')
                assert result['body']=='Exact original body'
            with patch.object(fs.subprocess,'run',side_effect=remote):
                assert backend.append_event(event,'op-event-1')['body']==event['body']
            assert sum(c[:2]==('base','+record-upsert') for c in calls)==1
        ''')

    def test_document_patch_requires_verified_cas_and_matching_base(self):
        self.check('''
            with patch.object(fs.subprocess,'run',side_effect=remote):
                doc=backend.read_document('docSyntheticPhase3')
                denied(lambda:backend.patch_document(doc['id'],doc['revision'],doc['content_hash'],
                                                    'Original','Changed','op-patch'))
            assert not any(c[:2]==('docs','+update') for c in calls)
        ''')

    def test_unknown_event_write_cannot_resend_after_no_record_readback(self):
        self.check('''
            event={'id':'evt-unknown','event_type':'progress','body':'Complete body',
                   'env_id':cfg['env_id'],'generation':'1'*32,'project_id':fs.PROJECT_ID,
                   'claimed_node_id':'node_lab_phase3_codex','task_refs':[]}
            writes=[]
            def missing_response(argv,**kw):
                if argv[1:3]==['base','+record-upsert']:
                    sys.audit('subprocess.Popen',argv[0],argv,kw['cwd'],kw['env'])
                    writes.append(argv)
                    raise subprocess.TimeoutExpired(argv,1)
                return remote(argv,**kw)
            with patch.object(fs.subprocess,'run',side_effect=missing_response):
                denied(lambda:backend.append_event(event,'stable-unknown'))
                # A fresh backend reads the durable journal, then only rereads.
                restarted=fs.FeishuBackend(cfg,backend.resources,backend.executor)
                denied(lambda:restarted.append_event(event,'stable-unknown'))
            assert len(writes)==1
            assert json.loads((root/'runtime/phase3_operations.json').read_text())['stable-unknown']['state']=='sent'
        ''')

    def test_account_change_generic_cli_and_old_base_fail_before_write(self):
        self.check('''
            denied(lambda:backend.executor._run(['config','show']))
            denied(lambda:backend.executor._run(['api','GET','/open-apis/drive/v1/files']))
            denied(lambda:backend.executor._run(['base','+record-list','--as','user','--base-token','oldBase',
                  '--table-id',backend.resources['tasks_table'],'--format','json','--limit','200','--offset','0']))
            def wrong_account(argv,**kw):
                result=remote(argv,**kw)
                if argv[1:3]==['auth','status']:
                    result.stdout=json.dumps({'userOpenId':'different-user','appId':'synthetic-app','brand':'feishu'})
                return result
            with patch.object(fs.subprocess,'run',side_effect=wrong_account):
                denied(lambda:backend.list_events())
            assert not any(c[:2]==('base','+record-upsert') for c in calls)
        ''')

    def test_full_long_body_preserved_and_invalid_row_quarantined(self):
        self.check('''
            event={'id':'evt-long','event_type':'result','body':'完整原文'*6000,
                   'env_id':cfg['env_id'],'generation':'1'*32,'project_id':fs.PROJECT_ID,
                   'claimed_node_id':'node_lab_phase3_codex','task_refs':[],
                   'verified_actor':{'provider':'untrusted-self-report'}}
            with patch.object(fs.subprocess,'run',side_effect=remote):
                result=backend.append_event(event,'op-long')
                assert result['body']==event['body']
                assert result['verified_actor']==event['verified_actor']
                assert result['remote_actor']['provider']=='feishu_user'
                rows['recInvalid']={'record_id':'recInvalid','fields':{'记录数据':'not JSON'}}
                all_rows=backend.list_events()
                assert len(all_rows)==2 and sum(bool(r.get('_invalid_remote_record')) for r in all_rows)==1
                invalid=next(r for r in all_rows if r.get('_invalid_remote_record'))
                assert invalid['_raw_record']['fields']['记录数据']=='not JSON'
                assert 'not JSON' not in (root/'runtime/phase3_egress.jsonl').read_text()
        ''')

    def test_setup_mock_provisions_exact_objects_and_resume_does_not_duplicate(self):
        result=isolated('''
            from lab.phase3_setup import Provisioner
            (root/'runtime/phase3_resources.json').unlink()
            with patch.object(fs.subprocess,'run',side_effect=remote):
                provisioner=Provisioner(cfg)
                outcome=provisioner.run()
                assert outcome['cas_verified'] is True
                assert len(provisioner.resources['documents'])==5
                before=sum(c[:2] in (('docs','+create'),('base','+base-create'),('base','+table-create')) for c in calls)
                second=Provisioner(cfg).run()
                after=sum(c[:2] in (('docs','+create'),('base','+base-create'),('base','+table-create')) for c in calls)
                assert before==after==8,(before,after)
                assert 'oldUntouched' in provisioner.resources['excluded_object_ids']
                assert not any(c[:2]==('docs','+fetch') and 'oldUntouched' in c for c in calls)
        ''',guarded=False)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_external_output_discovery_is_metadata_only_until_valid_task_event(self):
        self.check('''
            taskid='task_p3_'+'a'*20
            node='node_lab_phase3_codex'
            title=backend.external_document_title(taskid,1,node,'Initial draft','external-create-op')
            raw='<title>'+title+'</title>\\n\\nComplete external body'
            documents['docExternal']={'title':title,'content':raw,'revision_id':1}
            folders[fs.HUMAN_FOLDER].append({'token':'docExternal','name':title,'type':'docx','parent_token':fs.HUMAN_FOLDER})
            task={'id':taskid,'task_id':taskid,'env_id':cfg['env_id'],'generation':'1'*32,'project_id':fs.PROJECT_ID,
                  'requirements_version':1,'business_status':'active','executor_node_ids':[node],'output_zone':'project'}
            event={'id':'evt-external','event_type':'submission','claimed_node_id':node,'body':'Original result',
                   'env_id':cfg['env_id'],'generation':'1'*32,'project_id':fs.PROJECT_ID,
                   'task_refs':[{'task_id':taskid,'requirements_version':1}]}
            ref={'document_id':'docExternal','revision':1,'content_hash':fs._hash(raw)}
            with patch.object(fs.subprocess,'run',side_effect=remote):
                denied(lambda:backend.read_document('docExternal'))
                result=backend.refresh_registry()
                assert result['discovered']==1 and result['bodies_read']==0
                assert not any(c[:2]==('docs','+fetch') for c in calls)
                denied(lambda:backend.register_event_document({**event,'claimed_node_id':'node_lab_phase3_workbuddy'},task,ref))
                denied(lambda:backend.register_event_document(event,{**task,'output_zone':'agent'},ref))
                assert not any(c[:2]==('docs','+fetch') for c in calls)
                document=backend.register_event_document(event,task,ref)
                assert document['content_hash']==ref['content_hash']
                assert document['body_content']=='Complete external body'
                assert any(d['id']=='docExternal' for d in backend.registry()['documents'])
                denied(lambda:backend.read_document('oldUntouched'))
        ''')

    def test_unknown_document_create_recovers_only_by_reading(self):
        self.check('''
            hidden=[True]
            def lost_create(argv,**kw):
                result=remote(argv,**kw)
                if argv[1:3]==['docs','+create']:
                    raise subprocess.TimeoutExpired(argv,1)
                if argv[1:4]==['drive','files','list'] and hidden[0]:
                    result.stdout=json.dumps({'data':{'files':[],'has_more':False}})
                return result
            with patch.object(fs.subprocess,'run',side_effect=lost_create):
                denied(lambda:backend.create_document('project','RDOS3 role_test lost create','Exact entire body','op-doc-lost'))
                assert backend.find_created_document('op-doc-lost') is None
                assert backend.find_created_document('never-dispatched') is None
                hidden[0]=False
                recovered=backend.find_created_document('op-doc-lost')
                assert fs.creation_matches(recovered,'Exact entire body')
            assert sum(c[:2]==('docs','+create') for c in calls)==1
        ''')

    def test_full_document_comparison_rejects_additional_unrequested_content(self):
        self.check('''
            def altered_create(argv,**kw):
                result=remote(argv,**kw)
                if argv[1:3]==['docs','+create']:
                    for key in documents:
                        if key!='docSyntheticPhase3':documents[key]['content']+='\\nUNREQUESTED EXTRA CONTENT'
                return result
            with patch.object(fs.subprocess,'run',side_effect=altered_create):
                denied(lambda:backend.create_document('project','RDOS3 role_test complete compare','Exact entire body','op-doc-extra'))
                denied(lambda:backend.find_created_document('op-doc-extra'))
            assert sum(c[:2]==('docs','+create') for c in calls)==1
        ''')

    def test_setup_unsafe_revision_behavior_keeps_cas_disabled_and_saves_new_version(self):
        result=isolated('''
            from lab.phase3_setup import Provisioner
            (root/'runtime/phase3_resources.json').unlink()
            def unsafe_revision(argv,**kw):
                argv=list(argv)
                if argv[1:3]==['docs','+update']:
                    identifier=argv[argv.index('--doc')+1]
                    argv[argv.index('--revision-id')+1]=str(documents[identifier]['revision_id'])
                return remote(argv,**kw)
            with patch.object(fs.subprocess,'run',side_effect=unsafe_revision):
                provisioner=Provisioner(cfg)
                result=provisioner.run()
                assert result['cas_verified'] is False
                assert len(provisioner.resources['documents'])==6
                assert any('并发验证保留版' in d['title'] for d in provisioner.resources['documents'])
        ''',guarded=False)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_setup_rejects_same_title_preexisting_base_before_any_mutation(self):
        result=isolated('''
            from lab.phase3_setup import Provisioner
            (root/'runtime/phase3_resources.json').unlink()
            name='RDOS阶段3协作登记 [P3-'+fs._hash(cfg['env_id'])[:12]+']'
            folders[fs.AGENT_FOLDER].append({'token':'basOldSameName','name':name,'type':'bitable','parent_token':fs.AGENT_FOLDER})
            with patch.object(fs.subprocess,'run',side_effect=remote):
                denied(lambda:Provisioner(cfg).run())
            assert not any(c[:2] in (('base','+base-create'),('base','+table-create'),('base','+field-create'),('docs','+create')) for c in calls)
        ''',guarded=False)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_enabled_configuration_missing_home_fails_closed_without_cli(self):
        result=isolated('''
            os.environ.pop('HOME')
            with patch.object(fs.subprocess,'run',side_effect=AssertionError('must not invoke')):
                try:fs.configure(cfg)
                except fs.FeishuTestError as exc:assert exc.code=='invalid_environment',exc.code
                else:raise AssertionError('HOME-less enabled config accepted')
                denied(lambda:fs.get_backend())
        ''',guarded=False)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_registered_resources_moved_outside_roots_are_not_read(self):
        self.check('''
            folders[fs.HUMAN_FOLDER]=[]
            folders[fs.AGENT_FOLDER]=[r for r in folders[fs.AGENT_FOLDER] if r['type']!='bitable']
            with patch.object(fs.subprocess,'run',side_effect=remote):
                denied(lambda:backend.read_document('docSyntheticPhase3'))
                denied(lambda:backend.list_tasks())
            assert not any(c[:2] in (('docs','+fetch'),('base','+record-list')) for c in calls)
        ''')


if __name__=='__main__':
    unittest.main()
