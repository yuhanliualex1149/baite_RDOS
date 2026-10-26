"""Confirmed human notes are versioned and never overwrite a person's comments."""
import unittest
from tests.phase1_support import run_guarded
from tests.test_phase3_core import SETUP

class Phase3PresentationsTest(unittest.TestCase):
    def check(self,body):
        result=run_guarded(SETUP+body)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_human_notes_new_version_preserves_human_comments(self):
        self.check('''
async def scenario():
    task=await start(dispatch_to=['agent','human'])
    key=task['id']+'@1'
    note=local.get('presentation',key)
    assert note and note['status']=='verified',note
    original=backend3.read_document(note['document_id'])
    edited=backend3.patch_document(original['id'],original['revision'],original['content_hash'],
        '此文档是任务登记的说明副本','人工意见：请保留。此文档是任务登记的说明副本','manual-note-edit')
    draft=await service.prepare_task({'task_id':task['id'],'goal':'只保留一个场景','change_reason':'本人调整'},admin,'note-update-draft')
    updated=await service.apply_task_action(task['id'],'update',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'note-update',1,1)
    second=local.get('presentation',task['id']+'@2')
    assert second['status']=='verified' and second['document_id']!=original['id'],second
    assert backend3.read_document(original['id'])['content']==edited['content']
    assert not service.overview(admin)['tasks'][0]['summary_stale']
    assert updated['requirements_version']==2
asyncio.run(scenario())
''')

    def test_uncertain_note_does_not_unpublish_or_recreate(self):
        self.check('''
async def scenario():
    backend3.fault('document_after_commit')
    task=await start(dispatch_to=['human'])
    note=local.get('presentation',task['id']+'@1')
    assert task['business_status']=='active' and note['status']=='uncertain',note
    assert len(backend3.registry()['documents'])==1
    from server.phase3_presentations import after_task_action
    recovered=await after_task_action(task,'start-one')
    assert recovered['status']=='verified' and len(backend3.registry()['documents'])==1,recovered
asyncio.run(scenario())
''')

    def test_agent_only_check_has_no_human_note_and_human_close_has_summary(self):
        self.check('''
async def scenario():
    task=await start()
    assert not local.items('presentation')
    document=await artifact(task)
    await submit(task,document)
    await service.refresh(admin,'summary-refresh')
    current=backend3.read_task(task['id'])
    closed=await service.apply_task_action(task['id'],'accept_close',{
        'artifact_id':document['id'],'artifact_revision':document['revision'],'artifact_hash':document['content_hash']},
        admin,'summary-close',current['record_revision'],1)
    summary=local.get('presentation',task['id']+'@1:closed')
    assert summary and summary['status']=='verified',summary
    actual=backend3.read_document(summary['document_id'])
    assert closed['quality_status']=='accepted'
    assert '管理员已确认当前提交' in actual['content']
    assert document['content_hash'] not in actual['content'] and closed['decision']['actor_id'] not in actual['content']
    assert actual['title'].startswith('工作总结｜') and 'role_test' not in actual['title']
asyncio.run(scenario())
''')

    def test_business_note_resolves_readers_nodes_and_source_versions(self):
        self.check('''
async def scenario():
    item=local.get('node',node['node_id'])
    item['display_name']='研发助手'
    local.put('node',item['node_id'],item)
    source=backend3.create_document('project','项目背景','明确的背景材料','note-source')
    task=await start(title='更新协作方式',dispatch_to=['human'],input_refs=[docref(source)],
        completion_rule={'kind':'receipt_recorded'},task_type='action',deliverable_audience=['researcher'])
    note=local.get('presentation',task['id']+'@1')
    actual=backend3.read_document(note['document_id'])
    assert actual['title']=='任务说明｜更新协作方式（第1版）'
    assert '研发助手' in actual['content'] and '研发人员' in actual['content']
    assert '项目背景（版本 1）' in actual['content']
    assert '核对执行结果与回执后完成' in actual['content']
    assert node['node_id'] not in actual['content'] and source['content_hash'] not in actual['content']
    assert 'role_test' not in actual['content'] and '测试归属' not in actual['content']
    assert '实验' not in actual['content'] and '独立账号隔离' not in actual['content']
asyncio.run(scenario())
''')

    def test_model_claim_does_not_authorize_human_document(self):
        self.check('''
async def scenario():
    task=await start()
    from server.phase3_presentations import after_task_action
    task['dispatch_to']=['human']
    central.put('operation','fabricated-note',{'action':'phase3_task:start',
        'planned_task':task,'authorization':{'source':'model_claim','target_id':task['task_id']}})
    before=len(backend3.registry()['documents'])
    try:await after_task_action(task,'fabricated-note')
    except ValueError:pass
    else:raise AssertionError('model claim must not publish a task note')
    assert len(backend3.registry()['documents'])==before
asyncio.run(scenario())
''')

    def test_existing_published_note_is_not_rewritten_by_renderer_upgrade(self):
        self.check('''
async def scenario():
    task=await start(dispatch_to=['human'])
    key=task['id']+'@1'
    note=local.get('presentation',key)
    actual=backend3.read_document(note['document_id'])
    edited=backend3.patch_document(actual['id'],actual['revision'],actual['content_hash'],
        '此文档是任务登记的说明副本','保留我的意见。此文档是任务登记的说明副本','human-note-comment')
    note.pop('prepared_body',None)
    note['input_hash']='historical-renderer-fingerprint'
    local.put('presentation',key,note)
    from server.phase3_presentations import after_task_action
    recovered=await after_task_action(task,'start-one')
    assert recovered==note
    assert backend3.read_document(actual['id'])['content']==edited['content']
    assert len(backend3.registry()['documents'])==1
asyncio.run(scenario())
''')

    def test_preupgrade_uncertain_note_recovers_original_body_without_recreate(self):
        self.check('''
async def scenario():
    backend3.fault('document_after_commit')
    task=await start(dispatch_to=['human'])
    key=task['id']+'@1'
    note=local.get('presentation',key)
    assert note['status']=='uncertain'
    original=backend3.find_created_document('start-one:human-note')
    changed=backend3.patch_document(original['id'],original['revision'],original['content_hash'],
        '# 任务说明','# 以前版本的说明','historic-renderer-body')
    note.pop('prepared_body',None)
    note['input_hash']=central.digest([task['requirements_hash'],changed['content']])
    local.put('presentation',key,note)
    from server.phase3_presentations import after_task_action
    recovered=await after_task_action(task,'start-one')
    assert recovered['status']=='verified' and recovered['document_id']==original['id']
    assert backend3.read_document(original['id'])['content']==changed['content']
    assert len(backend3.registry()['documents'])==1
asyncio.run(scenario())
''')
