"""Twelve migrated draft/diagnostic scenarios using real guarded service flows.

Unconfirmed Agent writes deny before transport. Uncertain confirmations only
reconcile; synthetic provider records/failures never replace the service policy.
"""
from __future__ import annotations
import unittest
from tests.phase1_support import run_guarded

SETUP = '''
from server import central_service as service, phase1_runtime as runtime, phase1_operations as ops
from server.feishu_tools import FeishuError, TASK_FIELDS
runtime.initialize()
researcher = store.user('mem_lab_codex_0001')
def context(op='prepare-one'):
    return {'id':'fixture-'+op,'operation_id':op,'project_id':project_id,'identity_mode':'role_test'}
def remote_task(task_id='WORK-1'):
    return next(t for t in backend.make_tools(project_id).list_tasks()['items'] if t['task_id']==task_id)
def original():
    values={'task_id':'WORK-1','project_id':project_id,'version':'2','status':'已发布','instructions':'原要求',
        'member_ids':[researcher['id']],'task_kind':'work','stage':'','nature':'一次性','related_task':'','operation_id':'old-op'}
    fields={name:','.join(values.get(key,[])) if key=='member_ids' else values.get(key,'') for key,name in TASK_FIELDS.items()}
    store.put('lab_remote_task','WORK-1',{'id':'WORK-1','project_id':project_id,'record_id':'rec-work-1','fields':fields})
    return remote_task()
def change_remote(**changes):
    row=store.get('lab_remote_task','WORK-1');row['fields'].update({TASK_FIELDS[k]:v for k,v in changes.items()})
    store.put('lab_remote_task','WORK-1',row)
def update_draft(source,op='prepare-one'):
    return service.prepare_task({'task_id':source['task_id'],'instructions':'修订任务','draft_action':'update',
        'reason':'反馈发现要求不清楚'},context(op),admin,existing_task=source)
def writes():return backend.stats(project_id).get('task_writes',0)
def real_run(op='agent-run',scheduled=False):
    if scheduled:return service.new_run(runtime.system_actor(),'受控检查',op,'sync','scheduled',project_id)
    thread=service.create_thread(admin,project_id)
    return service.new_run(admin,'讨论补充要求',op,project_id=project_id,thread_id=thread['id'])
def supplement_args():
    return {'project_id':project_id,'task_id':'SUP-1','related_task':'WORK-1','member_ids':[researcher['id']],
        'instructions':'补上成果位置','reason':'反馈缺少成果链接'}
async def expect_uncertain(draft,operation):
    try:await service.publish_task_draft(draft['id'],draft['draft_hash'],operation,admin)
    except (FeishuError,ops.OperationError) as exc:assert exc.code=='write_uncertain',exc.code
    else:raise AssertionError('unverified write reported success')
async def uncertain_supplement():
    original()
    draft=service.prepare_task({**supplement_args(),'draft_action':'create'},context('prepare-supplement'),admin,existing_task={})
    backend.set_fault(project_id,'read_tasks_after_write',code='timeout')
    await expect_uncertain(draft,'approved-supplement')
    assert writes()==1
    return draft
'''

class TaskDraftAndDiagnosticsTests(unittest.TestCase):
    def check(self,body):
        result=run_guarded(SETUP+'\n'+body,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_create_update_withdraw_require_confirmation_and_capture_remote_baseline(self):
        self.check('''
async def scenario():
    created=service.prepare_task({'task_id':'NEW-1','instructions':'完成合成盘点','member_ids':[researcher['id']],
        'draft_action':'create'},context(),admin,existing_task={})
    assert (created['draft_action'],created['base_task_hash'],created['version'])==('create','','1') and writes()==0
    published=await service.publish_task_draft(created['id'],created['draft_hash'],'publish-new',admin)
    assert published['status']=='published' and writes()==1
    source=original()
    changed=service.prepare_task({'task_id':'WORK-1','instructions':'补充产出版本','draft_action':'update',
        'reason':'原任务缺少可核对的成果版本'},context('prepare-update'),admin,existing_task=source)
    assert (changed['base_task_version'],changed['base_task_hash'])==('2',service.task_body_hash(source))
    updated=await service.publish_task_draft(changed['id'],changed['draft_hash'],'publish-update',admin)
    assert (updated['publication']['version'],updated['publication']['record_id'])==('3','rec-work-1')
    stopped=service.prepare_task({'task_id':'WORK-1','draft_action':'withdraw','reason':'合成工作已取消'},
        context('prepare-stop'),admin,existing_task=remote_task())
    assert (stopped['target_status'],stopped['instructions'])==('停用','补充产出版本')
    result=await service.publish_task_draft(stopped['id'],stopped['draft_hash'],'publish-stop',admin)
    assert (result['publication']['version'],result['publication']['status'])==('4','停用')
    await service.publish_task_draft(stopped['id'],stopped['draft_hash'],'another-confirmation',admin)
    assert writes()==3
    # Only provider field-list response is altered; real service must reject
    # without requesting field-create or changing any task.
    no_schema=service.prepare_task({'task_id':'NEW-2','instructions':'合成要求','member_ids':[researcher['id']]},
        context('missing-schema'),admin,existing_task={})
    cls=type(backend.make_tools(project_id)._local_backend);transport_run=cls.run;calls=[]
    def missing_fields(self,args,object_id=''):
        calls.append(tuple(args[:2]))
        return {'data':{'items':[]}} if args[:2]==['base','+field-list'] else transport_run(self,args,object_id)
    cls.run=missing_fields
    try:
        try:await service.publish_task_draft(no_schema['id'],no_schema['draft_hash'],'missing-schema-confirm',admin)
        except FeishuError as exc:assert exc.code=='not_configured',exc.code
        else:raise AssertionError('missing schema automatically expanded')
    finally:cls.run=transport_run
    assert calls==[('base','+field-list')] and writes()==3,calls
    assert not store.get('operation','missing-schema-confirm').get('dispatched_at')
asyncio.run(scenario())
''')

    def test_stale_remote_task_unlocks_draft_without_writing(self):
        self.check('''
async def scenario():
    draft=update_draft(original());change_remote(instructions='其他人刚修改的正文')
    try:await service.publish_task_draft(draft['id'],draft['draft_hash'],'publish-stale',admin)
    except service.TaskVersionChanged as exc:assert exc.code=='task_version_changed'
    else:raise AssertionError('stale source overwritten')
    saved=store.get('task_draft',draft['id'])
    assert saved['status']=='draft' and saved['needs_review']
    assert 'publication_operation_id' not in saved and saved['instructions']=='修订任务' and writes()==0
asyncio.run(scenario())
''')

    def test_uncertain_remote_write_keeps_original_operation_for_recheck(self):
        self.check('''
async def scenario():
    draft=update_draft(original());backend.set_fault(project_id,'read_tasks_after_write',code='timeout')
    await expect_uncertain(draft,'publish-uncertain')
    assert store.get('task_draft',draft['id'])['publication_operation_id']=='publish-uncertain'
    assert store.get('operation','publish-uncertain')['status']=='uncertain'
    resumed=await service.publish_task_draft(draft['id'],draft['draft_hash'],'new-click-id',admin)
    assert resumed['status']=='published' and resumed['publication']['operation_id']=='publish-uncertain' and writes()==1
    assert [a['mode'] for a in store.get('operation','publish-uncertain')['attempts']]==['write','reconcile']
asyncio.run(scenario())
''')

    def test_uncertain_task_not_written_reconciles_saved_payload_without_resending(self):
        self.check('''
async def scenario():
    source=original();draft=update_draft(source);backend.set_fault(project_id,'write_task',code='write_uncertain')
    await expect_uncertain(draft,'publish-original')
    assert remote_task()['version']=='2'
    assert store.get('operation','publish-original')['remote_baseline']['body_hash']==service.task_body_hash(source)
    before=backend.stats(project_id).copy()
    await expect_uncertain(draft,'ignored-new-click')
    assert writes()==0 and backend.stats(project_id)['task_reads']==before['task_reads']+1
    assert [a['mode'] for a in store.get('operation','publish-original')['attempts']]==['write','reconcile']
    assert not store.get('operation','ignored-new-click')
asyncio.run(scenario())
''')

    def test_uncertain_task_then_newer_remote_version_never_overwrites(self):
        self.check('''
async def scenario():
    draft=update_draft(original());backend.set_fault(project_id,'read_tasks_after_write',code='timeout')
    await expect_uncertain(draft,'publish-original')
    change_remote(version='4',instructions='其他人后续改动',operation_id='later-operation')
    await expect_uncertain(draft,'ignored-new-click')
    current=remote_task()
    assert (current['version'],current['instructions'])==('4','其他人后续改动') and writes()==1
    assert store.get('task_draft',draft['id'])['publication_operation_id']=='publish-original'
asyncio.run(scenario())
''')

    def test_agent_supplement_retry_cannot_create_or_reuse_confirmation(self):
        self.check('''
async def scenario():
    original();run=real_run('agent-supplement');before=backend.stats(project_id).copy()
    for _ in range(2):
        result=await service.execute_tool('save_task',supplement_args(),run,admin)
        assert not result['ok'] and result['code']=='not_allowed',result
    assert backend.stats(project_id)==before and not store.items('operation') and not store.items('task_draft')
asyncio.run(scenario())
''')

    def test_agent_supplement_cannot_write_after_related_task_changes(self):
        self.check('''
async def scenario():
    original();run=real_run('first-write-race',scheduled=True);change_remote(version='3',instructions='原任务被别人更新')
    before=backend.stats(project_id).copy()
    result=await service.execute_tool('save_task',supplement_args(),run,runtime.system_actor())
    assert not result['ok'] and result['code']=='not_allowed_unattended',result
    assert backend.stats(project_id)==before and writes()==0 and not store.items('operation')
asyncio.run(scenario())
''')

    def test_agent_uncertain_retry_cannot_resend_when_related_task_advanced(self):
        self.check('''
async def scenario():
    draft=await uncertain_supplement();change_remote(version='3',instructions='原任务被别人更新',operation_id='later-operation')
    run=real_run('related-advanced');before=backend.stats(project_id).copy()
    result=await service.execute_tool('save_task',supplement_args(),run,admin)
    assert not result['ok'] and result['code']=='not_allowed',result
    assert backend.stats(project_id)==before and writes()==1
    assert store.get('operation','approved-supplement')['status']=='uncertain'
    assert store.get('task_draft',draft['id'])['status']=='draft' and len(store.items('operation'))==1
asyncio.run(scenario())
''')

    def test_confirmed_uncertain_supplement_readback_survives_related_update(self):
        self.check('''
async def scenario():
    draft=await uncertain_supplement();change_remote(version='3',instructions='原任务被别人更新',operation_id='later-operation')
    # Exact existing result is reconciled through confirmation, never Agent save_task.
    result=await service.publish_task_draft(draft['id'],draft['draft_hash'],'another-click',admin)
    assert result['status']=='published' and result['publication']['task_id']=='SUP-1'
    assert result['publication_operation_id']=='approved-supplement' and writes()==1 and remote_task()['version']=='3'
    assert store.get('operation','approved-supplement')['attempts'][-1]['mode']=='reconcile'
asyncio.run(scenario())
''')

    def test_periodic_requirement_prepares_a_draft_without_remote_write(self):
        self.check('''
async def scenario():
    run=real_run('periodic-draft')
    result=await service.execute_tool('prepare_task',{'project_id':project_id,'requirement_kind':'periodic_feedback',
        'member_ids':[researcher['id']]},run,admin)
    assert result['ok'] and result['requires_confirmation'],result
    assert '不生成日报' in result['item']['instructions'] and result['item']['nature']=='周期'
    assert writes()==0 and not store.items('operation') and result['item']['status']=='draft'
asyncio.run(scenario())
''')

    def test_diagnostics_is_admin_only_and_does_not_return_raw_content(self):
        self.check('''
store.put('feedback','feedback-one',{'id':'feedback-one','project_id':project_id,'content':'private-example-secret',
    'source_readback':True,'reported_at':'2026-10-13T09:00:00+08:00','last_read_at':'2026-10-13T10:00:00+08:00'})
store.put('operation','write-one',{'id':'write-one','project_id':project_id,'action':'task','status':'uncertain','payload_hash':'private-example-secret'})
try:service.runtime_diagnostics(researcher)
except PermissionError:pass
else:raise AssertionError('researcher received admin diagnostics')
result=service.runtime_diagnostics(admin)
assert result['online']['feishu']['unresolved_operations']==1 and result['local_agents']['feedback_readback_records']==1
assert '未验证真实模型、飞书或客户端' in result['scope'] and 'private-example-secret' not in str(result)
''')

    def test_template_tool_proposal_is_confirmed_before_local_version_becomes_active(self):
        self.check('''
from server import central_templates
async def scenario():
    before=central_templates.snapshot('project.feedback');run=real_run('template-proposal')
    read=await service.execute_tool('read_material',{'object_id':'project.feedback','kind':'template'},run,admin)
    assert read['ok'] and read['version']==before['version'],read
    proposed=await service.execute_tool('propose_change',{'target_id':'project.feedback','target_kind':'template',
        'summary':'把反馈说明写得更明确','old_text':before['content'],'new_text':before['content']+'请注明共享成果的状态。',
        'evidence':'合成反馈显示成果状态未写清'},run,admin)
    assert proposed['ok'],proposed
    item=proposed['item'];assert item['status']=='draft' and central_templates.snapshot('project.feedback')['version']==before['version']
    try:await service.publish_proposal(item['id'],'wrong-hash','unapproved-template',admin)
    except ValueError:pass
    else:raise AssertionError('template changed without exact confirmation')
    assert not store.items('template_activation')
    published=await service.publish_proposal(item['id'],item['draft_hash'],'approve-template',admin)
    assert published['status']=='published' and central_templates.snapshot('project.feedback')['version']==before['version']+1
    again=await service.publish_proposal(item['id'],item['draft_hash'],'another-click',admin)
    assert again['publication']['version']==published['publication']['version'] and len(store.items('template_activation'))==1
    assert backend.stats(project_id).get('document_writes',0)==0
    assert store.get('operation','approve-template')['authorization']['draft_hash']==item['draft_hash']
asyncio.run(scenario())
''')

if __name__=='__main__':unittest.main()
