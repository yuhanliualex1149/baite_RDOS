"""Stage 3 also closes old draft/immutable-history gaps under the real guard."""
import unittest
from unittest.mock import patch
from tests.phase1_support import run_guarded


class Phase3LegacyRegression(unittest.TestCase):
    def check(self, body):
        result=run_guarded(body,timeout=35)
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)

    def test_draft_operation_same_content_reuses_different_content_conflicts(self):
        self.check('''
from server import central_service as service
row=backend.make_tools(project_id).list_tasks()['items'][0]
fields={'task_id':row['task_id'],'instructions':row['instructions']+' 合成补充','member_ids':row['member_ids'],'draft_action':'update','reason':'测试'}
run={'id':'draft-fixture','operation_id':'stable-draft','project_id':project_id}
a=service.prepare_task(fields,run,admin,row)
b=service.prepare_task(fields,run,admin,row)
assert a==b
try:service.prepare_task({**fields,'instructions':'同ID不同正文'},run,admin,row)
except ValueError:pass
else:raise AssertionError('different draft request silently reused')
assert store.get('task_draft',a['id'])==a
''')

    def test_task_version_is_immutable_in_single_and_atomic_batch(self):
        self.check('''
row=backend.make_tools(project_id).list_tasks()['items'][0]
key=row['task_id']+'@'+row['version']
store.put('task_version',key,row)
for many in (False,True):
    changed={**row,'instructions':'不能覆盖的同版内容'}
    try:
        if many:store.put_many([('state','must-rollback',{'value':True}),('task_version',key,changed)])
        else:store.put('task_version',key,changed)
    except ValueError:pass
    else:raise AssertionError('immutable version overwritten')
    assert store.get('task_version',key)==row
    assert store.get('state','must-rollback') is None
''')

    def test_same_remote_version_mutation_keeps_processed_state_and_history(self):
        self.check('''
from server import central_service as service, phase1_runtime as runtime
runtime.initialize()
async def check():
    first=await service.collect_materials(project_id,'first')
    before=store.get('input_state',project_id)
    history=store.get('task_version','lab_task_codex@1')
    remote=store.get('lab_remote_task','lab_task_codex')
    remote['fields']['任务说明']='远端同版本非法改写'
    store.put('lab_remote_task',remote['id'],remote)
    try:await service.collect_materials(project_id,'second')
    except Exception as exc:assert getattr(exc,'code','')=='invalid_source',repr(exc)
    else:raise AssertionError('invalid version accepted')
    assert store.get('task_version','lab_task_codex@1')==history
    assert store.get('input_state',project_id)==before
    assert any(not r.get('complete') and r.get('error_code')=='invalid_source' for r in store.items('read_receipt'))
asyncio.run(check())
''')


class Phase3LauncherProtection(unittest.TestCase):
    def test_reset_refuses_phase3_before_any_process_or_data_mutation(self):
        import labctl
        with patch.object(labctl.config,'load',return_value={'phase3':{'enabled':True}}), \
             patch.object(labctl,'owned_process',side_effect=AssertionError('must not inspect/stop processes')), \
             patch.object(labctl.shutil,'rmtree',side_effect=AssertionError('must not delete')):
            with self.assertRaisesRegex(ValueError,'禁止 reset'):
                labctl.reset('irrelevant')
