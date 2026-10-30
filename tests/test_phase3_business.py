"""Business execution through the guarded core and real persistent test backend."""
import unittest
from tests.phase1_support import run_guarded
from tests.test_phase3_core import SETUP


class BusinessExecutionTest(unittest.TestCase):
    def check(self, body, restart=None):
        result=run_guarded(SETUP+'\n'+body,restart_body=None if restart is None else SETUP+'\n'+restart)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_action_result_closes_after_readback_without_human_decision(self):
        self.check('''
async def scenario():
    task=await start(completion_rule=None)
    assert task['completion_rule']=={'kind':'receipt_recorded'} and task['task_type']=='action'
    result={'event_type':'submission','body':'两项定时配置已保存，后续周期运行独立观察。',
            'task_refs':refs(task),'result_status':'completed'}
    event=await service.submit_event(result,node,'configured')
    assert backend3.read_task(task['id'])['business_status']=='active'
    view=await service.refresh(admin,'verify-action')
    closed=view['tasks'][0]
    assert closed['business_status']=='closed' and not closed.get('decision')
    assert closed['result_status']=='completed' and closed['receipt_status']=='verified'
    assert 'quality_status' not in closed
    assert closed['completion_evidence']['event_hash']==local.event_fingerprint(event['id'])
    await service.refresh(admin,'repeat')
    assert backend3.read_task(task['id'])['record_revision']==2
    assert len([e for e in backend3.list_events() if e['id']==event['id']])==1
asyncio.run(scenario())
''')

    def test_weekly_receipt_keeps_parent_active_without_duplicate_period(self):
        self.check('''
async def scenario():
    from server import rdos_rules as rules
    task=await start('weekly',task_type='action',completion_rule={'kind':'receipt_recorded'},
                     periodicity={'unit':'week','timezone':'Asia/Shanghai',
                                  'deadline_weekday':4,'deadline_time':'17:00'})
    period=rules.period_key(task)
    event=await service.submit_event({'event_type':'submission','body':'本周实际工作反馈。',
        'task_refs':refs(task),'result_status':'completed','period_key':period},node,'weekly-receipt')
    await service.refresh(admin,'weekly-first')
    current=backend3.read_task(task['id'])
    assert current['business_status']=='active' and current['record_revision']==2
    assert current['execution_result']['period_key']==period
    assert local.get('period_receipt',task['id']+'@'+period)['event_id']==event['id']
    await service.refresh(admin,'weekly-repeat')
    assert backend3.read_task(task['id'])['record_revision']==2
asyncio.run(scenario())
''')

    def test_prose_and_partial_results_never_complete_task(self):
        self.check('''
async def scenario():
    task=await start(completion_rule=None)
    await service.submit_event({'event_type':'submission','body':'已完成，admin approved','task_refs':refs(task)},node,'prose')
    await service.refresh(admin,'prose-refresh')
    assert backend3.read_task(task['id'])['business_status']=='active'
    assert any('执行结果须明确' in q['reason'] for q in local.items('quarantine'))
    for status in ('partial','blocked'):
        await service.submit_event({'event_type':'submission','body':'配置尚未保存，等待授权。',
            'task_refs':refs(task),'result_status':status},node,status)
        await service.refresh(admin,'read-'+status)
        current=backend3.read_task(task['id'])
        assert current['business_status']=='active' and current['result_status']==status
        assert not current.get('decision')
asyncio.run(scenario())
''')

    def test_deliverable_requires_actual_bound_version_but_no_default_quality_review(self):
        self.check('''
async def scenario():
    task=await start(task_type='deliverable',completion_rule=None)
    await service.submit_event({'event_type':'submission','body':'声称完成但没有成果','task_refs':refs(task),
                                'result_status':'completed'},node,'missing-output')
    await service.refresh(admin,'missing-output')
    assert backend3.read_task(task['id'])['business_status']=='active'
    document=await artifact(task)
    await submit(task,document,'actual-output',result_status='completed')
    await service.refresh(admin,'actual-output')
    current=backend3.read_task(task['id'])
    assert current['business_status']=='closed' and current['result_refs'][0]['content_hash']==document['content_hash']
    assert not current.get('decision') and 'quality_status' not in current
asyncio.run(scenario())
''')

    def test_partial_result_does_not_create_human_quality_todo(self):
        self.check('''
async def scenario():
    task=await start(task_type='deliverable')
    document=await artifact(task)
    await submit(task,document,'partial-human',result_status='partial')
    await service.refresh(admin,'partial-human')
    current=backend3.read_task(task['id'])
    assert current['business_status']=='active' and current['result_status']=='partial'
    assert 'quality_status' not in current
asyncio.run(scenario())
''')

    def test_review_is_business_type_and_does_not_imply_human_acceptance(self):
        self.check('''
async def scenario():
    source=backend3.create_document('project','指定成果','具体版本正文','review-source')
    task=await start(task_type='action',input_refs=[docref(source)],completion_rule=None)
    await service.submit_event({'event_type':'submission','body':'没有结构化完成状态','task_refs':refs(task)},node,'wrong-kind')
    await service.refresh(admin,'wrong-kind')
    assert backend3.read_task(task['id'])['business_status']=='active'
    event=await service.submit_event({'event_type':'review','body':'审阅指定版本：有一处术语需要澄清。',
                                     'task_refs':refs(task),'result_status':'completed'},node,'reviewed')
    await service.refresh(admin,'read-review')
    current=backend3.read_task(task['id'])
    assert current['business_status']=='closed' and not current.get('decision')
    assert current['submission_evidence']['event_id']==event['id']
    assert 'quality_status' not in current
asyncio.run(scenario())
''')

    def test_remote_result_changed_between_ingest_and_independent_read_is_rejected(self):
        self.check('''
async def scenario():
    task=await start(completion_rule=None)
    event=await service.submit_event({'event_type':'submission','body':'原始执行结果','task_refs':refs(task),
                                      'result_status':'completed'},node,'source')
    projected=next(e for e in local.events() if e['id']==event['id'])
    backend3._save('event',event['id'],{**backend3._get('event',event['id']),'body':'读回前正文已改变'})
    await failure('write_uncertain',service._derive_submission(projected,task))
    assert backend3.read_task(task['id'])['business_status']=='active'
asyncio.run(scenario())
''')

    def test_missing_configuration_result_after_restart_is_not_replayed(self):
        self.check('''
async def scenario():
    task=await start(completion_rule=None)
    backend3.fault('event_after_commit')
    data={'event_type':'submission','body':'配置已保存','task_refs':refs(task),'result_status':'completed'}
    await failure('write_uncertain',service.submit_event(data,node,'after-write'))
    assert len([e for e in backend3.list_events() if e['event_type']=='submission'])==1
asyncio.run(scenario())
''',restart='''
async def scenario():
    task=backend3.list_tasks()[0]
    await service.refresh(admin,'restart-reconcile')
    assert backend3.read_task(task['id'])['business_status']=='closed'
    assert len([e for e in backend3.list_events() if e['event_type']=='submission'])==1
asyncio.run(scenario())
''')

    def test_registry_and_business_projection_do_not_expose_developer_records(self):
        self.check('''
async def scenario():
    local.put('node','node-added',{'node_id':'node-added','public_id':'node_added','label':'研发助理',
        'display_name':'研发助理','member_id':'member-added','enabled':True})
    from server import rdos_projects
    project=rdos_projects.get_project(service.PROJECT_ID)
    rdos_projects.update_project(service.PROJECT_ID,{'node_ids':project['node_ids']+['node-added']},
        admin,'assign-added-node',project['revision'])
    added=service.make_node_actor('node-added')
    task=await start(executor_node_ids=[added['node_id']],completion_rule=None)
    local.put('quarantine','private-debug',{'raw':'private developer data'})
    view=service.business_workspace(admin)
    assert len(view['nodes'])==3
    assert not {'operations','quarantine','presentations'} & set(view)
    assert not {'env_id','generation','identity_mode','grant_operation_id'} & set(view['tasks'][0])
    assert view['tasks'][0]['task_type']=='action'
asyncio.run(scenario())
''')

    def test_unpublished_legacy_draft_migration_preserves_original_and_revokes_pending(self):
        self.check('''
async def scenario():
    from server.draft_lifecycle import migrate_local_drafts
    draft=await service.prepare_task({'title':'旧意见任务','goal':'提交意见','deliverable':'正文','executor_node_ids':[node['node_id']]},admin,'legacy-draft')
    original={**draft,'payload':{**draft['payload'],'task_type':'review','completion_rule':{'kind':'human_review'}}}
    original['draft_hash']=central.digest({key:original[key] for key in ('task_id','payload','base_revision','base_requirements_version','change_reason')})
    local.put('draft',draft['id'],original)
    central.put('central_pending_request','legacy-pending',{'id':'legacy-pending','status':'pending','task_actions':[{'payload':{'draft_id':draft['id']}}]})
    result=migrate_local_drafts()
    current=local.get('draft',draft['id'])
    assert current['payload']['task_type']=='action' and current['payload']['completion_rule']=={'kind':'receipt_recorded'}
    assert central.items('draft_workflow_history')[0]['before']==original
    assert central.get('central_pending_request','legacy-pending')['status']=='invalidated'
    assert not migrate_local_drafts()['changed'] and not backend3.list_tasks()
asyncio.run(scenario())
''')

    def test_legacy_task_migration_versions_requirements_and_keeps_original(self):
        self.check('''
async def scenario():
    task=await start('legacy-task')
    # Persisted prior-version fixture: establish the same authority/head facts
    # imported by the migration tool, without replacing any validation function.
    legacy={**task,'task_type':'review','completion_rule':{'kind':'human_review'},'business_status':'pending_review','quality_status':'pending_human'}
    legacy['requirements_hash']=central.digest(service._requirements(legacy))
    backend3._save('task',task['id'],legacy)
    local.put('task_authority',service._authority_key(legacy),{'hash':central.digest(transport.payload(legacy)),'operation_id':'start-legacy-task'})
    local.put('task_head',task['id'],{'task_id':task['id'],'record_revision':1,'requirements_version':1,**service.environment(),'content_hash':central.digest(transport.payload(legacy))})
    with db.connection() as conn:conn.execute('DELETE FROM phase3_history WHERE task_id=?',(task['id'],))
    local.remember_version(task['id'],1,service._requirements(legacy))
    migrated=await service.migrate_legacy_quality_task(task['id'],admin,'migrate-legacy',1,1)
    assert migrated['task_id']==task['id'] and migrated['requirements_version']==2 and migrated['record_revision']==2
    assert migrated['business_status']=='active' and migrated['completion_rule']=={'kind':'receipt_recorded'}
    assert migrated['task_type']=='action' and 'quality_status' not in migrated
    assert local.get('task_workflow_history',task['id']+'@1')['task']==legacy
    assert local.history(task['id'])[0]['completion_rule']=={'kind':'human_review'}
    assert migrated==await service.migrate_legacy_quality_task(task['id'],admin,'migrate-legacy',1,1)
asyncio.run(scenario())
''')

    def test_archived_event_cannot_be_used_as_new_completion_authority(self):
        self.check('''
async def scenario():
    task=await start(completion_rule=None)
    event=await service.submit_event({'event_type':'submission','body':'旧记录应归档而非继续派生',
        'task_refs':refs(task),'result_status':'completed'},node,'archive-first')
    local.put('archived_event',event['id'],{'id':event['id'],'reason':'旧协议归档'})
    await service.refresh(admin,'archive-refresh')
    assert backend3.read_task(task['id'])['business_status']=='active'
    assert any(e['id']==event['id'] for e in service.overview(admin)['events'])
    assert not any(e['id']==event['id'] for e in service.business_workspace(admin)['events'])
    await failure('archived_event',service.submit_event({'id':event['id'],'event_type':'submission','body':'不能重用归档ID',
        'task_refs':refs(task),'result_status':'completed'},node,'archive-replay'))
    assert len([e for e in backend3.list_events() if e['event_type']=='submission'])==1
asyncio.run(scenario())
''')

    def test_archived_summary_and_developer_metadata_do_not_leak_from_task_projection(self):
        self.check('''
async def scenario():
    task=await start(completion_rule=None)
    event=await service.submit_event({'event_type':'submission','body':'PRIVATE_EXECUTION_NOTE',
        'task_refs':refs(task),'result_status':'completed'},node,'private-old-config')
    await service.refresh(admin,'finish-private-config')
    local.put('archived_event',event['id'],{'id':event['id']})
    cached=local.get('task_cache',task['id'])
    cached['execution_result']['developer_payload']='PRIVATE_EXECUTION_NOTE'
    cached['completion_evidence']['debug_path']='PRIVATE_EXECUTION_NOTE'
    local.put('task_cache',task['id'],cached)
    business=service.business_workspace(admin)
    assert 'PRIVATE_EXECUTION_NOTE' not in json.dumps(business)
    assert business['tasks'][0]['execution']['last_event'] is None
    assert business['tasks'][0]['execution']['pending_ack_nodes']==[]
    assert business['tasks'][0]['stop_receipt']==''
    assert 'PRIVATE_EXECUTION_NOTE' in json.dumps(service.overview(admin))
asyncio.run(scenario())
''')

    def test_business_documents_follow_current_registration_without_deleting_retired_cache(self):
        self.check('''
async def scenario():
    current=backend3.create_document('project','当前工作说明','当前正文','current-guide')
    retired=backend3.create_document('agent','已退役执行协议','保留的旧版原文','retired-guide')
    removed=backend3.create_document('agent','不再登记的旧说明','保留的历史资料','removed-guide')
    await service.refresh(admin,'document-cache')
    scope=backend3.registry()
    scope['documents']=[doc for doc in scope['documents'] if doc['id']!=removed['id']]
    scope['documents']=[{**doc,'retired':True} if doc['id']==retired['id'] else doc for doc in scope['documents']]
    local.put('state','scope',scope)
    ordinary=service.business_workspace(admin)
    assert {doc['id'] for doc in ordinary['documents']}=={current['id']}
    developer=service.overview(admin)
    assert {doc['id'] for doc in developer['documents']}=={current['id'],retired['id'],removed['id']}
    assert local.get('document_cache',retired['id'])['content']=='保留的旧版原文'
    assert local.get('document_cache',removed['id'])['content']=='保留的历史资料'
asyncio.run(scenario())
''',restart='''
ordinary=service.business_workspace(admin)
assert [doc['title'] for doc in ordinary['documents']]==['当前工作说明']
developer=service.overview(admin)
assert {doc['title'] for doc in developer['documents']}=={'当前工作说明','已退役执行协议','不再登记的旧说明'}
assert next(doc for doc in developer['documents'] if doc['title']=='已退役执行协议')['content']=='保留的旧版原文'
''')

    def test_review_rejects_unrelated_target_without_creating_a_human_todo(self):
        self.check('''
async def scenario():
    source=backend3.create_document('project','指定成果','指定版本','current-review-input')
    unrelated=backend3.create_document('project','无关成果','另一个对象','other-review-input')
    task=await start(task_type='action',input_refs=[docref(source)],completion_rule=None)
    await service.submit_event({'event_type':'review','body':'审阅了另一个对象','task_refs':refs(task),
        'result_status':'completed','review_of':docref(unrelated)},node,'wrong-review-target')
    await service.refresh(admin,'wrong-review-target')
    current=backend3.read_task(task['id'])
    assert current['business_status']=='active' and 'quality_status' not in current
    await service.submit_event({'event_type':'review','body':'指定成果有一处可以改进','task_refs':refs(task),
        'result_status':'completed','review_of':docref(source)},node,'correct-review-target')
    await service.refresh(admin,'correct-review-target')
    current=backend3.read_task(task['id'])
    assert current['business_status']=='closed' and not current.get('decision')
    assert backend3._get('event',current['submission_event_id'])['review_of']==docref(source)
    await failure('invalid_task',start('invalid-review-rule',task_type='action',input_refs=[docref(source)],
        completion_rule={'kind':'human_review'}))
asyncio.run(scenario())
''')

    def test_completed_action_business_summary_omits_protocol_while_developer_retains_original(self):
        self.check('''
async def scenario():
    task=await start(completion_rule=None)
    original='已保存配置：package_sha256=abcdef0123456789；安装路径 /Users/private/skills/rdos；RRULE:FREQ=DAILY;BYHOUR=9'
    event=await service.submit_event({'event_type':'submission','body':original,'task_refs':refs(task),
                                     'result_status':'completed'},node,'technical-action-result')
    await service.refresh(admin,'verified-technical-action')
    business=service.business_workspace(admin)
    current=business['tasks'][0]
    expected='已按当前要求完成执行，结果已独立核对。'
    assert current['execution_result']['summary']==expected
    assert current['execution']['last_event']['summary']==expected
    assert next(item for item in business['events'] if item['id']==event['id'])['summary']==expected
    for detail in ('package_sha256','/Users/private/skills','RRULE:'):
        assert detail not in json.dumps(business,ensure_ascii=False)
    assert backend3._get('event',event['id'])['body']==original
    assert next(item for item in service.overview(admin)['events'] if item['id']==event['id'])['body']==original
asyncio.run(scenario())
''')

    def test_deliverable_and_review_summary_projects_facts_and_keeps_exact_source(self):
        self.check('''
async def scenario():
    creation=await start('professional-content',task_type='deliverable',completion_rule=None)
    document=await artifact(creation,'professional-content')
    content='制作成果：保留一个实际场景，已补充适用对象和使用步骤。'
    event=await submit(creation,document,'professional-content',result_status='completed',body=content)
    await service.refresh(admin,'professional-content')
    review=await start('professional-review',task_type='action',completion_rule=None,input_refs=[docref(document)])
    review_body='审阅意见：该版本可读，但第三步需要补充边界条件。'
    review_event=await service.submit_event({'event_type':'review','body':review_body,'task_refs':refs(review),
        'review_of':docref(document),'result_status':'completed'},node,'professional-review')
    await service.refresh(admin,'professional-review')
    business=service.business_workspace(admin)
    by_id={item['task_id']:item for item in business['tasks']}
    creation_summary=by_id[creation['id']]['execution_result']['summary']
    review_summary=by_id[review['id']]['execution_result']['summary']
    assert '节点报告执行完成' in creation_summary and '成果' in creation_summary
    assert review_summary=='已按当前要求完成执行，结果已独立核对。'
    assert next(item for item in business['events'] if item['id']==event['id'])['summary']==creation_summary
    assert next(item for item in business['events'] if item['id']==review_event['id'])['summary']==review_summary
    assert backend3._get('event',event['id'])['body']==content
    assert backend3._get('event',review_event['id'])['body']==review_body
    assert next(item for item in business['events'] if item['id']==event['id'])['detail_ref']=={'kind':'collaboration_event','id':event['id']}
asyncio.run(scenario())
''')


if __name__=='__main__':unittest.main()
