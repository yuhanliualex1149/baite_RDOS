"""Dispatch admission and recovery keep the guarded core and real write owner."""
import unittest
from tests.phase1_support import run_guarded
from tests.test_phase3_core import SETUP


class DispatchRecoveryTest(unittest.TestCase):
    def check(self,body):
        result=run_guarded(SETUP+'\n'+body,timeout=45)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_pure_backend_refusal_is_not_dispatched_or_an_authority(self):
        self.check('''
from unittest.mock import patch
from server.draft_lifecycle import transition
async def scenario():
    task=await start()
    draft=await service.prepare_task({'task_id':task['task_id'],'title':'拒绝的标题','change_reason':'调整显示名'},admin,'rejected-draft')
    with patch.object(backend3,'validate_task_write',side_effect=transport.BackendError('invalid_record','局部参数拒绝')):
        for repeat in range(2):
            await failure('invalid_record',service.apply_task_action(task['task_id'],'update',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'rejected-publish',1,1))
    op=central.get('operation','rejected-publish')
    assert op['status']=='failed' and not op.get('dispatched_at') and not op.get('task_dispatched')
    assert not local.get('task_authority',task['task_id']+'@2')
    assert not local.get('trusted_event',op['planned_event']['id'])
    assert backend3.read_task(task['task_id'])['record_revision']==1
    deleted=await transition('draft',draft['id'],'delete',admin,'delete-refused',draft['draft_hash'])
    assert deleted['status']=='deleted'
asyncio.run(scenario())
''')

    def test_read_failure_before_transport_is_failed_not_uncertain(self):
        self.check('''
from unittest.mock import patch
async def scenario():
    task=await start()
    draft=await service.prepare_task({'task_id':task['task_id'],'title':'新标题','change_reason':'标题'},admin,'read-fail-draft')
    original=backend3.read_task
    calls=[0]
    def fail_commit_read(task_id):
        calls[0]+=1
        # Initial request and locked version recheck succeed. Commit's read fails.
        if calls[0]==3:raise transport.BackendError('read_failed','读取失败')
        return original(task_id)
    with patch.object(backend3,'read_task',side_effect=fail_commit_read):
        await failure('read_failed',service.apply_task_action(task['task_id'],'update',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'read-fail',1,1))
    op=central.get('operation','read-fail')
    assert op['status']=='failed' and not op.get('dispatched_at')
    assert not local.get('task_authority',task['task_id']+'@2')
asyncio.run(scenario())
''')

    def test_actual_lost_response_is_uncertain_and_cannot_delete(self):
        self.check('''
from server.draft_lifecycle import transition
async def scenario():
    task=await start()
    draft=await service.prepare_task({'task_id':task['task_id'],'title':'新版','change_reason':'标题'},admin,'lost-draft')
    backend3.fault('task_after_commit')
    await failure('write_uncertain',service.apply_task_action(task['task_id'],'update',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'lost',1,1))
    op=central.get('operation','lost')
    assert op['status']=='uncertain' and op.get('dispatched_at')
    assert backend3.read_task(task['task_id'])['record_revision']==2
    assert not local.get('task_authority',task['task_id']+'@2')
    await failure('write_uncertain',transition('draft',draft['id'],'delete',admin,'delete-lost',draft['draft_hash']))
    result=await service.apply_task_action(task['task_id'],'update',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'lost',1,1)
    assert result['record_revision']==2 and local.get('task_authority',task['task_id']+'@2')
    assert len(backend3.list_tasks())==1
asyncio.run(scenario())
''')

    def test_real_transport_entry_has_durable_send_marker_and_restart_preserves_uncertainty(self):
        self.check('''
from unittest.mock import patch
from server import phase1_operations as ops
async def scenario():
    write_task,append_event=backend3.write_task,backend3.append_event
    entries=[]
    def checked_task(task,opid):
        saved=central.get('operation',opid[:-5])
        assert saved.get('dispatched_at') and saved.get('task_dispatched')
        assert saved.get('attempts',[])[-1].get('dispatched_at')
        entries.append('task');return write_task(task,opid)
    def checked_event(event,opid):
        saved=central.get('operation',opid[:-6])
        assert saved.get('dispatched_at') and saved.get('event_dispatched')
        entries.append('event');return append_event(event,opid)
    with patch.object(backend3,'write_task',side_effect=checked_task),patch.object(backend3,'append_event',side_effect=checked_event):
        task=await start('boundary')
    assert entries==['task','event']
    draft=await service.prepare_task({'task_id':task['task_id'],'title':'断响应','change_reason':'标题'},admin,'boundary-lost-draft')
    backend3.fault('task_after_commit')
    await failure('write_uncertain',service.apply_task_action(task['task_id'],'update',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'boundary-lost',1,1))
    result=ops.recover_interrupted()
    assert central.get('operation','boundary-lost')['status']=='uncertain'
asyncio.run(scenario())
''')

    def test_old_false_dispatch_retires_only_owned_projections_before_delete(self):
        self.check('''
from unittest.mock import patch
from server.draft_lifecycle import transition
async def scenario():
    task=await start()
    draft=await service.prepare_task({'task_id':task['task_id'],'title':'带姓名的标题','change_reason':'显示姓名'},admin,'old-draft')
    with patch.object(backend3,'write_task',side_effect=ValueError('旧兼容标题校验拒绝')):
        try:await service.apply_task_action(task['task_id'],'update',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'old',1,1)
        except ValueError:pass
        else:raise AssertionError('old refusal missing')
    op=central.get('operation','old');at=op['dispatched_at']
    # Exact historic pre-readback projections left by the previous implementation.
    local.immutable('task_authority',task['task_id']+'@2',{'operation_id':'old','hash':central.digest(transport.payload(op['prepared_task']))})
    event=op['prepared_event']
    local.immutable('trusted_event',event['id'],{'operation_id':'old','hash':central.digest(transport.payload(event))})
    proof={'kind':'legacy_upgrade_title_rejected_before_cli_v1','adapter_journal_hash':'a'*64,
           'task_operation_id':'old:task','event_operation_id':'old:event','transport_attempted':False}
    backend3.unwritten_legacy_upgrade_evidence=lambda *args:dict(proof)
    # A crash between retirement and soft deletion must remain recoverable,
    # including when someone first retries the already retired old publish.
    from server.phase3_write_recovery import recover_before_draft_delete
    from server.central_service import WRITE_LOCK
    async with WRITE_LOCK:
        await recover_before_draft_delete(draft['id'],draft['draft_hash'],admin,'draft_lifecycle:delete-old')
    await failure('operation_conflict',service.apply_task_action(task['task_id'],'update',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'old',1,1))
    deleted=await transition('draft',draft['id'],'delete',admin,'delete-old',draft['draft_hash'])
    retired=central.get('operation','old');receipt=central.get('unwritten_task_recovery','old')
    assert deleted['status']=='deleted' and retired['status']=='conflict'
    assert retired['dispatched_at']==at and receipt['before_operation']==op
    assert len(receipt['retired_projections'])==2
    assert not local.get('task_authority',task['task_id']+'@2') and not local.get('trusted_event',event['id'])
    assert local.get('task_authority',task['task_id']+'@1')
    correct=await service.prepare_task({'task_id':task['task_id'],'title':'正确说明','change_reason':'继续原任务'},admin,'correct-draft')
    result=await service.apply_task_action(task['task_id'],'update',{'draft_id':correct['id'],'expected_hash':correct['draft_hash']},admin,'correct',1,1)
    assert result['requirements_version']==2 and result['record_revision']==2
    assert len(backend3.list_tasks())==1 and local.get('task_authority',task['task_id']+'@2')['operation_id']=='correct'
    await failure('authorization_failed',service.apply_task_action(task['task_id'],'update',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'old',1,1))
asyncio.run(scenario())
''')

    def test_no_transport_proof_or_changed_remote_never_retires(self):
        self.check('''
from unittest.mock import patch
from server.draft_lifecycle import transition
async def scenario():
    task=await start()
    draft=await service.prepare_task({'task_id':task['task_id'],'title':'标题','change_reason':'调整'},admin,'unknown-draft')
    with patch.object(backend3,'write_task',side_effect=ValueError('未知失败')):
        try:await service.apply_task_action(task['task_id'],'update',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'unknown',1,1)
        except ValueError:pass
    original=central.get('operation','unknown')
    backend3.unwritten_legacy_upgrade_evidence=lambda *args:None
    await failure('write_uncertain',transition('draft',draft['id'],'delete',admin,'delete-unknown',draft['draft_hash']))
    assert central.get('operation','unknown')==original and not central.get('unwritten_task_recovery','unknown')
    await service.apply_task_action(task['task_id'],'pause',{},admin,'pause-after-unknown',1,1)
    backend3.unwritten_legacy_upgrade_evidence=lambda *args:{'kind':'legacy_upgrade_title_rejected_before_cli_v1','transport_attempted':False}
    await failure('version_conflict',transition('draft',draft['id'],'delete',admin,'delete-changed',draft['draft_hash']))
    assert central.get('operation','unknown')==original and not central.get('unwritten_task_recovery','unknown')
asyncio.run(scenario())
''')
