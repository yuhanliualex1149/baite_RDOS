"""Next-version task administration through the guarded business core."""
import unittest
from tests.phase1_support import run_guarded
from tests.test_phase3_core import SETUP


class TaskManagementTest(unittest.TestCase):
    def check(self, body):
        result=run_guarded(SETUP+'\n'+body)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_manual_completion_retains_node_facts_and_replays_once(self):
        self.check('''
async def scenario():
    task=await start(completion_rule=None)
    closed=await service.apply_task_action(task['id'],'manual_complete',{'note':'管理员确认工作已完成'},admin,'manual',1,1)
    assert closed['business_status']=='closed' and closed['close_reason']=='manual_completed'
    assert closed['manual_completion']['actor_id']==admin['id']
    assert not closed.get('result_status') and not closed.get('receipt_status')
    assert not closed.get('completion_evidence') and not closed.get('decision')
    again=await service.apply_task_action(task['id'],'manual_complete',{'note':'管理员确认工作已完成'},admin,'manual',1,1)
    assert again==closed and backend3.read_task(task['id'])['record_revision']==2
    await failure('task_closed',service.apply_task_action(task['id'],'resume',{},admin,'reopen',2,1))
    await service.submit_event({'event_type':'submission','body':'晚到结果','task_refs':refs(task),'result_status':'completed'},node,'late')
    await service.refresh(admin,'late')
    assert backend3.read_task(task['id'])['close_reason']=='manual_completed'
    public=service.business_task(closed)
    assert 'quality_status' not in public and 'decision' not in public
asyncio.run(scenario())
''')

    def test_manual_completion_periodic_parent_and_node_denial(self):
        self.check('''
async def scenario():
    task=await start(completion_rule=None,periodicity={'unit':'week','timezone':'Asia/Shanghai','deadline_weekday':4,'deadline_time':'17:00'})
    await failure('authorization_failed',service.apply_task_action(task['id'],'manual_complete',{},node,'fake',1,1))
    closed=await service.apply_task_action(task['id'],'manual_complete',{},admin,'end-periodic',1,1)
    assert closed['business_status']=='closed' and closed['periodicity']==task['periodicity']
    assert closed['close_reason']=='manual_completed' and not local.items('period_receipt')
asyncio.run(scenario())
''')

    def test_new_quality_rules_and_acceptance_action_are_unavailable(self):
        self.check('''
async def scenario():
    for kind in ('human','human_review'):
        await failure('invalid_task',start('quality-'+kind,completion_rule={'kind':kind}))
    await failure('invalid_task',start('dedicated-review',task_type='review',completion_rule=None))
    task=await start(completion_rule=None)
    await failure('invalid_action',service.apply_task_action(task['id'],'accept_close',{},admin,'old-button',1,1))
    assert 'review' not in service.rules.TASK_TYPES
    assert not {'human','human_review'} & set(service.rules.COMPLETION_KINDS)
asyncio.run(scenario())
''')

    def test_delete_restore_invalidates_whole_pending_and_old_operation(self):
        self.check('''
async def scenario():
    from server.draft_lifecycle import transition,ensure_not_revoked
    fields={'title':'待发布','goal':'实际工作','deliverable':'回执','executor_node_ids':[node['node_id']]}
    draft=await service.prepare_task(fields,admin,'delete-me')
    request={'task_id':draft['task_id'],'action':'start','payload':{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},
        'actor_id':admin['id'],'expected_revision':0,'expected_requirements_version':0}
    op=central.operation('old-publish','phase3_task:start',request)
    from server import phase1_operations as ops
    ops.bind_confirmation(op,actor=admin,project_id=draft['project_id'],target_kind='phase3_task',target_id=draft['task_id'],
        draft_hash=central.digest(request),base={'record_revision':0,'requirements_version':0},input_fields=request)
    actions=[{'payload':{'draft_id':draft['id']}},{'payload':{'draft_id':'another-draft'}}]
    for kind in ('central_pending_request','central_request_plan'):
        central.put(kind,'pending-delete',{'id':'pending-delete','status':'pending','task_actions':actions})
    deleted=await transition('draft',draft['id'],'delete',admin,'delete',draft['draft_hash'])
    assert deleted['status']=='deleted'
    assert not service.business_workspace(admin)['drafts']
    assert len(service.business_workspace(admin)['deleted_drafts'])==1
    for kind in ('central_pending_request','central_request_plan'):
        assert central.get(kind,'pending-delete')['status']=='invalidated'
    assert deleted==await transition('draft',draft['id'],'delete',admin,'delete',draft['draft_hash'])
    restored=await transition('draft',draft['id'],'restore',admin,'restore',draft['draft_hash'])
    assert restored['status']=='draft' and central.get('draft_revoked_operation','old-publish')
    try:ensure_not_revoked('old-publish')
    except ValueError as exc:assert exc.code=='authorization_failed'
    else:raise AssertionError('restoration revived old grant')
    task=await service.apply_task_action(draft['task_id'],'start',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'fresh-publish',0,0)
    assert task['business_status']=='active'
    await failure('invalid_transition',transition('draft',draft['id'],'delete',admin,'delete-published',draft['draft_hash']))
asyncio.run(scenario())
''')

    def test_delete_between_prepare_and_write_prevents_publication(self):
        self.check('''
async def scenario():
    from server.draft_lifecycle import transition
    fields={'title':'并发草稿','goal':'实际工作','deliverable':'回执','executor_node_ids':[node['node_id']]}
    draft=await service.prepare_task(fields,admin,'race-draft')
    entered=asyncio.Event();proceed=asyncio.Event();original=service._verify_refs
    async def delayed(*args,**kwargs):
        entered.set();await proceed.wait();return await original(*args,**kwargs)
    service._verify_refs=delayed
    pending=asyncio.create_task(service.apply_task_action(draft['task_id'],'start',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'race-publish',0,0))
    await entered.wait()
    await transition('draft',draft['id'],'delete',admin,'race-delete',draft['draft_hash'])
    proceed.set()
    await failure('version_conflict',pending)
    assert not backend3.list_tasks()
asyncio.run(scenario())
''')

    def test_legacy_draft_and_proposal_support_reversible_deletion(self):
        self.check('''
async def scenario():
    from server.draft_lifecycle import transition
    from server import central_service
    for kind in ('task_draft','proposal'):
        item={'id':kind+'-old','project_id':local.PROJECT_ID,'draft_hash':'a'*64,'status':'draft','summary':'保留原文'}
        central.put(kind,item['id'],item)
        deleted=await transition(kind,item['id'],'delete',admin,'delete-'+kind,'a'*64)
        assert deleted['summary']=='保留原文' and deleted['status']=='deleted'
        restored=await transition(kind,item['id'],'restore',admin,'restore-'+kind,'a'*64)
        assert restored['summary']=='保留原文' and restored['status']=='draft'
asyncio.run(scenario())
''')

    def test_projectless_draft_rechecks_disabled_admin_after_lock_wait(self):
        self.check('''
async def scenario():
    from server.draft_lifecycle import transition
    from server.central_service import WRITE_LOCK
    async def denied_after_lock(identifier,action,operation):
        before=central.get('proposal',identifier)
        await WRITE_LOCK.acquire()
        pending=asyncio.create_task(transition('proposal',identifier,action,admin,operation,'a'*64))
        await asyncio.sleep(0)
        assert not pending.done()
        with db.connection() as conn:
            conn.execute('UPDATE central_users SET enabled=0 WHERE id=?',(admin['id'],))
        WRITE_LOCK.release()
        await failure('authorization_failed',pending)
        assert central.get('proposal',identifier)==before
        with db.connection() as conn:
            conn.execute('UPDATE central_users SET enabled=1 WHERE id=?',(admin['id'],))
    for action,status in [('delete','draft'),('restore','deleted')]:
        item={'id':'institution-'+action,'project_id':'','target_kind':'template',
              'target_id':'shared-template','status':status,'draft_hash':'a'*64}
        central.put('proposal',item['id'],item)
        await denied_after_lock(item['id'],action,'denied-'+action)
    # A successful operation's cached reply also requires current privileges.
    item={'id':'institution-replay','project_id':'','target_kind':'template',
          'target_id':'shared-template','status':'draft','draft_hash':'a'*64}
    central.put('proposal',item['id'],item)
    await transition('proposal',item['id'],'delete',admin,'replayed-delete','a'*64)
    await denied_after_lock(item['id'],'delete','replayed-delete')
asyncio.run(scenario())
''')
