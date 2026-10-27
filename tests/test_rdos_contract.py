import json
import unittest
from server import rdos_contract as contract
from tests.test_phase3_egress import isolated


class ContractTest(unittest.TestCase):
    def test_public_identity_roundtrip_preserves_business_authority(self):
        registry={'env_id':'private-env','generation':'private-generation','project_id':'lab_project_phase3_collab','workspace_id':'ws_rdos'}
        event={'id':'event_1','event_type':'submission','project_id':registry['project_id'],
               'env_id':registry['env_id'],'generation':registry['generation'],'identity_mode':'role_test',
               'claimed_node_id':'node_lab_phase3_codex','body':'配置已保存','result_status':'completed',
               'verified_actor':{'id':'mem_lab_admin_0001','kind':'authenticated_panel','identity_mode':'role_test'}}
        public=contract.encode(event,registry)
        text=json.dumps(public)
        self.assertNotIn('private-env',text)
        self.assertNotIn('role_test',text)
        self.assertNotIn('node_lab',text)
        self.assertEqual(contract.decode(public,registry),event)
        with self.assertRaises(ValueError):contract.decode({**public,'workspace_id':'another'},registry)

    def test_guarded_real_adapter_writes_v2_and_rejects_legacy_after_cutover(self):
        result=isolated('''
backend.resources.update(public_protocol_version=2,workspace_id='ws_rdos',allow_legacy_reads=False)
assert '成果' in backend.external_document_title('task_p3_'+'a'*20,1,'node_registered_added','成果','registered-title')
task={'id':'task_normal','task_id':'task_normal','title':'核对资料','env_id':cfg['env_id'],
 'generation':'1'*32,'project_id':'lab_project_phase3_collab','requirements_version':1,
 'record_revision':1,'business_status':'active','executor_node_ids':['node_lab_phase3_codex']}
with patch.object(subprocess,'run',side_effect=remote):
 actual=backend.write_task(task,'normal-write')
 assert actual['task_id']==task['task_id']
 wire=json.loads(next(iter(rows.values()))['fields']['记录数据'])
 assert wire['protocol_version']==2 and wire['executor_node_ids']==['node_codex']
 assert not any(k in wire for k in ('env_id','generation','identity_mode'))
 assert backend.read_task(task['task_id'])['executor_node_ids']==task['executor_node_ids']
 row=next(iter(rows.values())); row['fields']['记录数据']=json.dumps(task)
 assert backend._decode(row,backend.resources['tasks_table']).get('_invalid_remote_record')
''')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
