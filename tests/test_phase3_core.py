"""Real guarded phase-3 domain tests; only the authoritative local adapter is synthetic."""
import unittest
from tests.phase1_support import run_guarded

SETUP='''
from server import phase3_service as service, phase3_store as local, phase3_backend as transport
from server import central_store as central, phase1_runtime
phase1_runtime.initialize()
service.initialize()
backend3=transport.get_backend()
node=service.make_node_actor('node_lab_phase3_codex')
other=service.make_node_actor('node_lab_phase3_workbuddy')
async def start(op='one',**changes):
    fields={'title':'合成介绍','goal':'一个场景','deliverable':'可读介绍','executor_node_ids':[node['node_id']],'completion_rule':{'kind':'receipt_recorded'}}
    fields.update(changes)
    draft=await service.prepare_task(fields,admin,'draft-'+op)
    return await service.apply_task_action(draft['task_id'],'start',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'start-'+op,0,0)
async def artifact(task,op='one'):
    return await service.create_document({'task_id':task['task_id'],'requirements_version':task['requirements_version'],'title':'介绍','content':'合成原文，保留人的意见。'},node,'artifact-'+op)
def refs(task):return [{'task_id':task['task_id'],'requirements_version':task['requirements_version']}]
def docref(document):return {'document_id':document['id'],'revision':document['revision'],'content_hash':document['content_hash']}
async def submit(task,document,op='one',**extra):
    return await service.submit_event({'event_type':'submission','body':'已提交初稿','task_refs':refs(task),'artifact_refs':[docref(document)],'result_status':'completed',**extra},node,'submit-'+op)
async def failure(code,call):
    try:await call
    except ValueError as exc:assert getattr(exc,'code','')==code,(type(exc).__name__,getattr(exc,'code',''),str(exc))
    else:raise AssertionError('expected '+code)
'''

class Phase3CoreTest(unittest.TestCase):
    def check(self,body,restart=None):
        result=run_guarded(SETUP+'\n'+body,restart_body=None if restart is None else SETUP+'\n'+restart)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_repeat_initialization_keeps_existing_project_record_timestamp(self):
        self.check('''
with db.connection() as conn:
    before=conn.execute("SELECT updated_at FROM central_records WHERE kind='project' AND id=?",
        (service.PROJECT_ID,)).fetchone()[0]
service.initialize()
with db.connection() as conn:
    after=conn.execute("SELECT updated_at FROM central_records WHERE kind='project' AND id=?",
        (service.PROJECT_ID,)).fetchone()[0]
assert before==after
''')

    def test_real_lifecycle_submission_closes_after_exact_readback(self):
        self.check('''
async def scenario():
    task=await start()
    assert not service.get_task(task['id'],admin).get('result_refs')
    document=await artifact(task)
    event=await submit(task,document)
    view=await service.refresh(admin,'refresh')
    task=view['tasks'][0]
    assert task['business_status']=='closed' and task['requirements_version']==1 and task['record_revision']==2
    derived=next(o for o in central.items('operation') if o.get('task_id')==task['id'] and o.get('action')=='phase3_task_execution' and o.get('prepared_task'))
    assert derived['authority_kind']=='task_execution_grant' and not derived.get('authorization')
    assert task['completion_evidence']['event_hash']==local.event_fingerprint(event['id'])
    assert task['close_reason']=='completed' and not task.get('decision')
asyncio.run(scenario())
''')

    def test_opinions_are_ordinary_work_and_stay_in_shared_table(self):
        self.check('''
async def scenario():
    source=backend3.create_document('project','待审阅成果','明确的成果版本','review-input')
    task=await start('opinions',executor_node_ids=[other['node_id']],task_type='action',input_refs=[docref(source)])
    message={'event_type':'submission','body':'独立审阅：初稿有一个需核实的问题；此处保留完整意见。',
             'task_refs':refs(task),'artifact_refs':[],'result_status':'completed'}
    event=await service.submit_event(message,other,'opinion-event')
    assert next(e for e in backend3.list_events() if e['id']==event['id'])['body']==message['body']
    assert event==await service.submit_event(message,other,'opinion-event')
    view=await service.refresh(admin,'read-opinions')
    current=next(t for t in view['tasks'] if t['task_id']==task['task_id'])
    assert current['business_status']=='closed' and current['result_refs']==[]
    assert current['submission_evidence']['event_id']==event['id']
    assert not current.get('decision') and 'quality_status' not in current
asyncio.run(scenario())
''')

    def test_table_only_execution_requires_structured_completed_result(self):
        self.check('''
async def scenario():
    task=await start('receipt',deliverable='配置回执只写入协作记录表',output_zone='agent')
    message={'event_type':'submission','body':'两个定时配置已保存；实际到点触发待核对。',
             'task_refs':refs(task),'artifact_refs':[]}
    await service.submit_event(message,node,'prose-only')
    await service.refresh(admin,'prose-only')
    assert backend3.read_task(task['id'])['business_status']=='active'
    event=await service.submit_event({**message,'result_status':'completed'},node,'receipt-event')
    view=await service.refresh(admin,'receipt-refresh')
    current=next(t for t in view['tasks'] if t['task_id']==task['task_id'])
    assert current['business_status']=='closed' and current['result_refs']==[]
    proof=current['submission_evidence']
    assert proof['event_id']==event['id'] and proof['content_hash']==central.digest(message['body'])
    assert not current.get('decision')
asyncio.run(scenario())
''')

    def test_draft_operation_collision_and_stale_action_reject(self):
        self.check('''
async def scenario():
    fields={'title':'测试','goal':'核对','deliverable':'正文','executor_node_ids':[node['node_id']]}
    draft=await service.prepare_task(fields,admin,'draft-same')
    assert draft==await service.prepare_task(fields,admin,'draft-same')
    await failure('operation_conflict',service.prepare_task({**fields,'goal':'另一目标'},admin,'draft-same'))
    task=await service.apply_task_action(draft['task_id'],'start',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'start-same',0,0)
    again=await service.apply_task_action(draft['task_id'],'start',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'start-same',0,0)
    assert task==again
    await failure('version_conflict',service.apply_task_action(task['id'],'pause',{},admin,'stale-pause',0,1))
    assert backend3.read_task(task['id'])['business_status']=='active'
asyncio.run(scenario())
''')

    def test_update_versions_pause_resume_and_late_results(self):
        self.check('''
async def scenario():
    old=await start()
    document=await artifact(old)
    draft=await service.prepare_task({'task_id':old['id'],'goal':'调整为另一个场景','change_reason':'用户明确调整'},admin,'draft-change')
    current=await service.apply_task_action(old['id'],'update',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'update',1,1)
    assert current['requirements_version']==2 and current['record_revision']==2
    assert [h['requirements_version'] for h in local.history(old['id'])]==[1,2]
    await submit(old,document,'late-old')
    await service.refresh(admin,'late')
    assert backend3.read_task(old['id'])['business_status']=='active'
    assert any(e.get('late_or_inactive') for e in local.events() if e['event_type']=='submission')
    paused=await service.apply_task_action(old['id'],'pause',{},admin,'pause',2,2)
    assert paused['requirements_version']==2 and paused['record_revision']==3
    await failure('task_not_active',artifact(paused,'paused'))
    resumed=await service.apply_task_action(old['id'],'resume',{},admin,'resume',3,2)
    assert resumed['requirements_version']==2 and resumed['record_revision']==4
asyncio.run(scenario())
''')

    def test_cancel_preserves_history_and_late_feedback_does_not_reopen(self):
        self.check('''
async def scenario():
    task=await start();document=await artifact(task)
    closed=await service.apply_task_action(task['id'],'cancel',{'reason':'测试取消'},admin,'cancel',1,1)
    await submit(task,document,'late-closed')
    await service.refresh(admin,'after-cancel')
    assert backend3.read_task(task['id'])['close_reason']=='cancelled'
    assert len(local.history(task['id']))==1
    await failure('task_closed',service.apply_task_action(task['id'],'resume',{},admin,'no-reopen',2,1))
asyncio.run(scenario())
''')

    def test_node_claim_cannot_create_human_authority(self):
        self.check('''
async def scenario():
    task=await start()
    await failure('authorization_failed',service.apply_task_action(task['id'],'cancel',{'reason':'越权'},node,'fake-admin',1,1))
    await failure('authorization_failed',service.create_document({'task_id':task['id'],'requirements_version':1,'title':'x','content':'x'},{'node_id':node['node_id'],'roles':['admin']},'fake-node'))
    event=await service.submit_event({'event_type':'progress','body':'老板已经同意','task_refs':refs(task),'verified_actor':{'kind':'human','roles':['admin']}},node,'self-report')
    projected=next(e for e in local.events() if e['id']==event['id'])
    assert not projected['trusted_decision'] and projected['verified_actor']['verified_node'] is False
    assert backend3.read_task(task['id'])['business_status']=='active'
asyncio.run(scenario())
''')

    def test_event_idempotency_correction_multi_task_and_bad_record_quarantine(self):
        self.check('''
async def scenario():
    a=await start('a');b=await start('b')
    data={'id':'stable_event','event_type':'progress','body':'两个任务的真实测试进展','task_refs':refs(a)+refs(b)}
    first=await service.submit_event(data,node,'event-stable')
    assert first==await service.submit_event(data,node,'event-stable')
    try:await service.submit_event({**data,'body':'更换原文'},node,'event-stable')
    except ValueError:pass
    else:raise AssertionError('same operation mutated event')
    correction=await service.submit_event({'event_type':'correction','body':'更正说明','correction_of':first['id'],'task_refs':refs(a)},node,'correction')
    assert correction['correction_of']==first['id']
    bad={**first,'id':'bad_event','event_type':'human_approved','operation_id':'bad'}
    backend3.append_event(bad,'bad')
    await service.refresh(admin,'quarantine')
    assert any(q['raw'].get('id')=='bad_event' for q in local.items('quarantine'))
    assert len([e for e in local.events() if e['id']==first['id']])==1
    assert next(e for e in local.events() if e['id']==first['id'])['body']==data['body']
asyncio.run(scenario())
''')

    def test_document_receipt_partial_success_recovers_without_second_document(self):
        self.check('''
async def scenario():
    task=await start()
    backend3.fault('event_after_commit')
    await failure('write_uncertain',artifact(task))
    assert len(backend3.registry()['documents'])==1
    op=central.get('operation','artifact-one')
    assert op['status']=='uncertain'
    result=await artifact(task)
    assert len(backend3.registry()['documents'])==1 and result['content']=='合成原文，保留人的意见。'
    assert len([e for e in backend3.list_events() if e['event_type']=='progress'])==1
    assert local.get('partial','artifact-one')['complete']
asyncio.run(scenario())
''')

    def test_document_patch_base_revision_preserves_human_edit(self):
        self.check('''
async def scenario():
    task=await start();document=await artifact(task)
    changed=backend3.patch_document(document['id'],document['revision'],document['content_hash'],'人的意见','新的人工意见','human-edit')
    await failure('version_conflict',service.patch_document(document['id'],{'task_id':task['id'],'requirements_version':1,'old_text':'合成原文','new_text':'修订稿'},node,'stale-patch',document['revision'],document['content_hash']))
    assert backend3.read_document(document['id'])['content']==changed['content']
    result=await service.patch_document(document['id'],{'task_id':task['id'],'requirements_version':1,'old_text':'合成原文','new_text':'修订稿'},node,'fresh-patch',changed['revision'],changed['content_hash'])
    assert '新的人工意见' in result['content'] and result['revision']==3
asyncio.run(scenario())
''')

    def test_backend_is_authority_and_partial_read_keeps_cache(self):
        self.check('''
async def scenario():
    task=await start();await service.refresh(admin,'first')
    cached=local.get('task_cache',task['id']);cached['title']='只改缓存'
    local.put('task_cache',task['id'],cached)
    assert backend3.read_task(task['id'])['title']=='合成介绍'
    backend3.fault('read_events')
    await failure('read_failed',service.refresh(admin,'failed-read'))
    assert local.get('task_cache',task['id'])['title']=='只改缓存'
    await service.refresh(admin,'restored')
    assert local.get('task_cache',task['id'])['title']=='合成介绍'
    tampered=backend3.read_task(task['id']);tampered['goal']='未经确认修改'
    backend3._save('task',task['id'],tampered)
    await service.refresh(admin,'tampered')
    assert service.get_task(task['id'],admin)['integrity_error']
    assert backend3.read_task(task['id'])['goal']=='未经确认修改'
    assert len(local.history(task['id']))==1
asyncio.run(scenario())
''')

    def test_machine_completion_requires_original_explicit_rule_and_actual_readback(self):
        self.check('''
async def scenario():
    source=backend3.create_document('project','合成依据','版本一','source')
    task=await start(completion_rule={'kind':'source_versions_match'},input_refs=[docref(source)],output_zone='agent',return_to=['agent'])
    await service.submit_event({'event_type':'check_result','body':'核对版本一致','task_refs':refs(task)},node,'check')
    await service.refresh(admin,'machine-check')
    closed=backend3.read_task(task['id'])
    assert closed['business_status']=='closed' and closed['close_reason']=='completed'
    assert closed['completion_evidence']['grant_operation_id']=='start-one'
    assert len(backend3.registry()['documents'])==1
asyncio.run(scenario())
''')

    def test_unknown_or_other_node_artifact_is_denied(self):
        self.check('''
async def scenario():
    task=await start()
    await failure('authorization_failed',service.create_document({'task_id':task['id'],'requirements_version':1,'title':'x','content':'x'},other,'other-node'))
    await failure('out_of_scope',service.create_document({'task_id':task['id'],'requirements_version':1,'title':'x','content':'x','zone':'agent'},node,'wrong-zone'))
    assert not backend3.registry()['documents']
asyncio.run(scenario())
''')

    def test_uncertain_task_publication_recovers_in_fresh_process(self):
        self.check('''
async def scenario():
    backend3.fault('event_after_commit')
    try:await start()
    except ValueError:pass
    else:raise AssertionError('fault missing')
    assert central.get('operation','start-one')['status']=='uncertain'
asyncio.run(scenario())
''',restart='''
async def scenario():
    draft=local.items('draft')[0]
    result=await service.apply_task_action(draft['task_id'],'start',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'start-one',0,0)
    assert result['record_revision']==1
    assert len(backend3.list_tasks())==1 and len(backend3.list_events())==1
    assert local.get('grant',result['id']+'@1')
    assert local.get('draft',draft['id'])['status']=='published'
    # Recover an already-succeeded operation whose display projection was
    # lost by the former reconciliation path. Do not reissue any I/O.
    draft['status']='draft';local.put('draft',draft['id'],draft)
    def never_write(*args,**kwargs):raise AssertionError('verified task was resent')
    backend3.write_task=never_write;backend3.append_event=never_write
    again=await service.apply_task_action(draft['task_id'],'start',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'start-one',0,0)
    assert again['record_revision']==1 and local.get('draft',draft['id'])['status']=='published'
    assert len(backend3.list_tasks())==1 and len(backend3.list_events())==1
    changed={**draft,'status':'draft','draft_hash':'a-newer-edit'}
    local.put('draft',draft['id'],changed)
    await service.apply_task_action(draft['task_id'],'start',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'start-one',0,0)
    assert local.get('draft',draft['id'])==changed
asyncio.run(scenario())
''')

    def test_confirmed_head_rejects_remote_rollback_across_restart(self):
        self.check('''
async def scenario():
    task=await start()
    paused=await service.apply_task_action(task['id'],'pause',{},admin,'pause',1,1)
    assert local.get('task_head',task['id'])['record_revision']==2
    backend3._save('task',task['id'],task)
    await failure('task_rollback',artifact(task,'rolled-back'))
    view=await service.refresh(admin,'rollback')
    assert view['tasks'][0]['business_status']=='paused' and view['tasks'][0]['integrity_error']
    assert not backend3.registry()['documents']
asyncio.run(scenario())
''',restart='''
async def scenario():
    task=backend3.list_tasks()[0]
    assert local.get('task_head',task['id'])['record_revision']==2
    await failure('task_rollback',artifact(task,'restart-rollback'))
    assert not backend3.registry()['documents']
asyncio.run(scenario())
''')

    def test_refresh_retries_if_task_advances_while_documents_are_read(self):
        self.check('''
import threading
from unittest.mock import patch
async def scenario():
    source=backend3.create_document('project','输入资料','可读正文','refresh-race-source')
    task=await start('refresh-race',input_refs=[docref(source)])
    entered=threading.Event(); release=threading.Event()
    original=backend3.read_document
    def delayed(identifier):
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        return original(identifier)
    with patch.object(backend3,'read_document',side_effect=delayed):
        reading=asyncio.create_task(service.refresh(admin,'race-read'))
        assert await asyncio.to_thread(entered.wait,10)
        paused=await service.apply_task_action(task['id'],'pause',{},admin,'race-pause',1,1)
        release.set()
        view=await reading
    current=next(item for item in view['tasks'] if item['task_id']==task['id'])
    assert current['record_revision']==2 and current['business_status']=='paused'
    assert not current.get('integrity_error')
    assert local.get('task_head',task['id'])['record_revision']==2
    assert local.get('task_cache',task['id'])['record_revision']==2
    assert not any(q['source']=='task' and q['raw'].get('task_id')==task['id'] for q in local.items('quarantine'))
asyncio.run(scenario())
''')

    def test_refresh_retries_instead_of_quarantining_a_concurrent_advance(self):
        self.check('''
import threading
from unittest.mock import patch
async def scenario():
    task=await start('early-race')
    entered=threading.Event(); release=threading.Event()
    original=backend3.list_events
    def delayed():
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        return original()
    with patch.object(backend3,'list_events',side_effect=delayed):
        reading=asyncio.create_task(service.refresh(admin,'race-before-validate'))
        assert await asyncio.to_thread(entered.wait,10)
        await service.apply_task_action(task['id'],'pause',{},admin,'early-race-pause',1,1)
        release.set()
        view=await reading
    current=next(item for item in view['tasks'] if item['task_id']==task['id'])
    assert current['record_revision']==2 and current['business_status']=='paused'
    assert not current.get('integrity_error')
    assert not any(q['source']=='task' for q in local.items('quarantine'))
asyncio.run(scenario())
''')

    def test_failed_refresh_does_not_index_events_from_an_uncommitted_snapshot(self):
        self.check('''
from unittest.mock import patch
async def scenario():
    task=await start('stale-event-snapshot')
    event=service._event(task,'stale-event-progress','progress','新的进展回执',service._principal(node))
    backend3.append_event(event,'stale-event-progress')
    system_event={**event,'id':'stale-system-event','operation_id':'stale-system-event',
                  'event_type':'peer_capability_ack','task_refs':[]}
    backend3.append_event(system_event,'stale-system-event')
    before=len(local.events())
    with patch.object(local,'replace_cache',side_effect=local.RefreshRace('interleaved write')), \
            patch.object(service,'_ingest_event',wraps=service._ingest_event) as ingest:
        await failure('refresh_race',service.refresh(admin,'stale-event-refresh'))
        assert ingest.call_count==0
    assert len(local.events())==before
    assert local.get('state','refresh:'+service.PROJECT_ID)['complete'] is False
asyncio.run(scenario())
''')

    def test_malformed_nested_records_are_isolated_from_valid_submission(self):
        self.check('''
async def scenario():
    task=await start();document=await artifact(task)
    valid=await submit(task,document)
    bads=[{'task_refs':['bad']},{'task_refs':[{'task_id':task['id'],'requirements_version':True}]},
          {'artifact_refs':['bad']},{'artifact_refs':[{'document_id':document['id'],'revision':False,'content_hash':'x'}]}]
    for index,fields in enumerate(bads):
        backend3.append_event({**valid,**fields,'id':'bad-'+str(index),'operation_id':'bad-'+str(index)},'bad-'+str(index))
    result=await service.refresh(admin,'mixed')
    assert result['tasks'][0]['business_status']=='closed'
    assert len([q for q in result['quarantine'] if q['source']=='event'])==4
    assert len([e for e in result['events'] if e['id']==valid['id']])==1
    fields={'title':'bad','goal':'bad','deliverable':'bad','executor_node_ids':[node['node_id']],
            'input_refs':[{'document_id':document['id'],'revision':True,'content_hash':document['content_hash']}]}
    await failure('invalid_task',service.prepare_task(fields,admin,'bad-input'))
    await failure('authorization_failed',service.create_document({'task_id':task['id'],'requirements_version':True,'title':'bad','content':'bad'},node,'bool-version'))
asyncio.run(scenario())
''')

    def test_deleted_remote_task_remains_visible_as_missing_without_recreation(self):
        self.check('''
async def scenario():
    task=await start()
    with db.connection() as conn:conn.execute("DELETE FROM phase3_mock WHERE kind='task'")
    result=await service.refresh(admin,'missing')
    assert result['tasks'][0]['id']==task['id'] and '消失' in result['tasks'][0]['integrity_error']
    assert any(q['source']=='missing_task' for q in result['quarantine'])
    await failure('task_missing',artifact(task,'missing'))
    assert not backend3.list_tasks()
asyncio.run(scenario())
''')

    def test_cross_task_and_seed_outputs_do_not_complete_task(self):
        self.check('''
async def scenario():
    first=await start('first');document=await artifact(first,'first')
    second=await start('second',executor_node_ids=[other['node_id']],completion_rule={'kind':'artifact_present'})
    seed=backend3.create_document('project','合成背景','不是任务产物','seed')
    for i,doc in enumerate([document,seed]):
        await service.submit_event({'event_type':'submission','body':'引用未授权产物','task_refs':refs(second),'artifact_refs':[docref(doc)]},other,'cross-task-'+str(i))
    await service.refresh(admin,'cross-task')
    assert backend3.read_task(second['id'])['business_status']=='active'
    assert len([q for q in local.items('quarantine') if q['source']=='submission_validation'])==2
asyncio.run(scenario())
''')

    def test_paused_partial_result_updated_version_resumes_active_and_preserves_remote_history(self):
        self.check('''
async def scenario():
    old=await start();document=await artifact(old);await submit(old,document,result_status='partial')
    await service.refresh(admin,'submitted')
    await service.apply_task_action(old['id'],'pause',{},admin,'pause-review',2,1)
    draft=await service.prepare_task({'task_id':old['id'],'goal':'新版要求','change_reason':'用户调整目标'},admin,'new-version')
    updated=await service.apply_task_action(old['id'],'update',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'updated',3,1)
    assert updated['business_status']=='paused' and updated['resume_status']=='active'
    resumed=await service.apply_task_action(old['id'],'resume',{},admin,'resumed',4,2)
    assert resumed['business_status']=='active' and not resumed['result_refs']
    remote=backend3.list_events()
    initial=next(e for e in remote if e['operation_id']=='start-one:event')
    changed=next(e for e in remote if e['operation_id']=='updated:event')
    assert initial['requirements_snapshot']['goal']=='一个场景'
    assert changed['requirements_snapshot']['goal']=='新版要求' and changed['change_reason']=='用户调整目标'
    assert changed['requirements_hash']==updated['requirements_hash'] and changed['record_revision']==4
    assert changed['old_results_policy']=='retained_historical_not_accepted_for_new_version'
asyncio.run(scenario())
''')

    def test_superseded_and_current_acknowledgement_are_visible(self):
        self.check('''
async def scenario():
    task=await start()
    closed=await service.apply_task_action(task['id'],'cancel',{'reason':'由后续任务替代','close_reason':'superseded'},admin,'superseded',1,1)
    await service.submit_event({'event_type':'closed_ack','body':'已停止当前任务','task_refs':refs(closed)},node,'closed-ack')
    view=await service.refresh(admin,'acks')
    current=view['tasks'][0]
    assert current['close_reason']=='superseded' and current['business_status']=='closed'
    assert current['execution']['acknowledged_nodes']==[node['node_id']] and not current['execution']['pending_ack_nodes']
    assert '已收到' in current['stop_receipt'] and '同账号自报' in current['stop_receipt']
asyncio.run(scenario())
''')

    def test_uncertain_document_create_and_patch_retry_only_read(self):
        self.check('''
async def scenario():
    task=await start();backend3.fault('document_after_commit')
    await failure('write_uncertain',artifact(task))
    create=backend3.create_document
    def never_create(*args,**kwargs):raise AssertionError('uncertain create was resent')
    backend3.create_document=never_create
    document=await artifact(task)
    backend3.create_document=create
    request={'task_id':task['id'],'requirements_version':1,'old_text':'合成原文','new_text':'改好正文'}
    backend3.fault('document_after_commit')
    await failure('write_uncertain',service.patch_document(document['id'],request,node,'patch-unknown',document['revision'],document['content_hash']))
    patch=backend3.patch_document
    def never_patch(*args,**kwargs):raise AssertionError('uncertain patch was resent')
    backend3.patch_document=never_patch
    actual=await service.patch_document(document['id'],request,node,'patch-unknown',document['revision'],document['content_hash'])
    backend3.patch_document=patch
    assert actual['revision']==2 and actual['content'].startswith('改好正文')
    assert len(backend3.registry()['documents'])==1
asyncio.run(scenario())
''')

    def test_dispatched_missing_task_receipt_is_not_blindly_reappended(self):
        self.check('''
async def scenario():
    backend3.fault('event_after_commit')
    try:await start()
    except ValueError:pass
    else:raise AssertionError('fault missing')
    op=central.get('operation','start-one')
    assert op['event_dispatched'] and local.get('task_head',op['prepared_task']['id'])['record_revision']==1
    with db.connection() as conn:conn.execute("DELETE FROM phase3_mock WHERE kind='event'")
    def never_append(*args,**kwargs):raise AssertionError('uncertain event was resent')
    backend3.append_event=never_append
    draft=local.items('draft')[0]
    await failure('write_uncertain',service.apply_task_action(draft['task_id'],'start',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'start-one',0,0))
    assert len(backend3.list_tasks())==1 and not backend3.list_events()
asyncio.run(scenario())
''')

    def test_schema_initialization_is_versioned_and_preserves_existing_records(self):
        self.check('''
async def scenario():
    task=await start();document=await artifact(task)
    marker=local.get('state','phase3_schema')
    assert marker['version']==1 and marker['created_at']
    before=(backend3.list_tasks(),backend3.list_events(),local.history(task['id']))
    service.initialize();service.initialize()
    assert local.get('state','phase3_schema')==marker
    assert before==(backend3.list_tasks(),backend3.list_events(),local.history(task['id']))
    assert backend3.read_document(document['id'])==document
asyncio.run(scenario())
''')

    def test_unknown_or_invalid_schema_fails_before_any_ddl(self):
        self.check('''
with db.connection() as conn:conn.execute('DROP TABLE phase3_mock')
for version in [2,0,True,'1',None]:
    invalid={'version':version,'created_at':'preserved'}
    local.put('state','phase3_schema',invalid)
    try:service.initialize()
    except RuntimeError:pass
    else:raise AssertionError('unknown schema accepted')
    assert local.get('state','phase3_schema')==invalid
    with db.connection() as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='phase3_mock'").fetchone()
''')

    def test_authorized_new_version_patch_proves_only_exact_result_version(self):
        self.check('''
async def scenario():
    task=await start();original=await artifact(task)
    draft=await service.prepare_task({'task_id':task['id'],'goal':'局部修改升级','change_reason':'保留文档并改正文'},admin,'patch-v2-draft')
    current=await service.apply_task_action(task['id'],'update',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'patch-v2-task',1,1)
    await submit(current,original,'unmodified-v1')
    await service.refresh(admin,'old-document')
    assert backend3.read_task(task['id'])['business_status']=='active'
    request={'task_id':task['id'],'requirements_version':2,'old_text':'合成原文','new_text':'第二版正文'}
    backend3.fault('document_after_commit')
    await failure('write_uncertain',service.patch_document(original['id'],request,node,'patch-v2',original['revision'],original['content_hash']))
    changed=await service.patch_document(original['id'],request,node,'patch-v2',original['revision'],original['content_hash'])
    assert local.get('document_grant',original['id'])['requirements_version']==1
    proofs=local.items('document_version_grant')
    assert {p['requirements_version'] for p in proofs}=={1,2}
    proof=next(p for p in proofs if p['requirements_version']==2)
    assert proof['revision']==changed['revision'] and proof['content_hash']==changed['content_hash']
    await submit(current,changed,'patched-v2')
    await service.refresh(admin,'new-document')
    assert backend3.read_task(task['id'])['business_status']=='closed'
asyncio.run(scenario())
''')

    def test_explicit_editable_document_patch_produces_task_output_proof(self):
        self.check('''
async def scenario():
    document=backend3.create_document('project','允许修改的合成材料','保留人工意见，替换目标段','editable-seed')
    task=await start(editable_document_ids=[document['id']])
    changed=await service.patch_document(document['id'],{'task_id':task['id'],'requirements_version':1,
        'old_text':'替换目标段','new_text':'已授权局部修订'},node,'patch-seed',document['revision'],document['content_hash'])
    assert not local.get('document_grant',document['id'])
    await submit(task,changed)
    await service.refresh(admin,'patched-seed')
    assert backend3.read_task(task['id'])['business_status']=='closed'
    assert '保留人工意见' in backend3.read_document(document['id'])['content']
asyncio.run(scenario())
''')

if __name__=='__main__':unittest.main()
