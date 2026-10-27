"""Inspect actual model requests without replacing the Agent or authorization."""
import unittest
from tests.phase1_support import run_guarded


SETUP='''
import copy
from unittest.mock import patch
from server import central_service as service, phase1_runtime as runtime
from server import phase3_service as domain, phase3_store as local, phase3_backend as transport
runtime.initialize()
domain.initialize()
pid=domain.PROJECT_ID
captured=[]
interpretation_requests=[]
MODEL_SCRIPT=['已查看当前项目记录。']
original=backend._LocalModelTransport.handle_async_request
async def capture(self,request):
    payload=json.loads(request.content)
    if any(m.get('role')=='system' and m.get('content','').startswith('RDOS_REQUEST_INTERPRETER_V1') for m in payload['messages']):
        import httpx
        interpretation_requests.append(payload)
        return httpx.Response(200,json={'model':'local-interpretation-fixture','choices':[{'message':{'role':'assistant','content':json.dumps({'purpose':'discuss','task_actions':[],'memory_changes':[]})},'finish_reason':'stop'}]},request=request)
    captured.append(payload)
    if self.index==0:self.script=copy.deepcopy(MODEL_SCRIPT)
    return await original(self,request)
patcher=patch.object(backend._LocalModelTransport,'handle_async_request',capture)
patcher.start()
def material_context(payload):
    text=next(m['content'] for m in payload['messages'] if m['role']=='user' and '<materials>' in m.get('content',''))
    return json.loads(text.split('<materials>\\n',1)[1].split('\\n</materials>',1)[0])
async def chat(project,operation):
    thread=service.create_thread(admin,project,'工作对话')
    run=service.new_run(admin,'说明当前工作情况',operation,'chat','manual',project,thread['id'])
    await service.run_agent(run['id'])
    return store.get('run',run['id'])
async def sync(operation):
    run=service.new_run(admin,'检查当前工作记录，必要时分析。',operation,'sync','manual',pid)
    await service.run_agent(run['id'])
    return store.get('run',run['id'])
async def task():
    draft=await domain.prepare_task({'title':'更新工作协作方式','goal':'采用当前执行规范','deliverable':'实际配置结果',
        'executor_node_ids':['node_lab_phase3_codex']},admin,'context-draft')
    return await domain.apply_task_action(draft['task_id'],'start',{'draft_id':draft['id'],'expected_hash':draft['draft_hash']},
        admin,'context-publish',0,0)
'''


class BusinessModelContextTest(unittest.TestCase):
    def check(self,body):
        result=run_guarded(SETUP+'\n'+body)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_project_model_receives_public_business_facts_without_environment_or_old_store(self):
        self.check('''
async def scenario():
    current=await task()
    metadata=store.get('project',pid)
    metadata.update(description='当前项目的协作目标',developer_context='DEVELOPMENT_PROJECT_DESCRIPTION',identity_mode='role_test')
    store.put('project',pid,metadata)
    store.put('task','old-store-current-project',{'id':'old-store-current-project','project_id':pid,'instructions':'LEGACY_TASK_STORE_SENTINEL'})
    store.put('task','old-other-project',{'id':'old-other-project','project_id':project_id,'instructions':'OLD_PROJECT_TASK_SENTINEL'})
    health=runtime.health(pid);health['developer_context']='DEVELOPER_HEALTH_SENTINEL';runtime.save_health(health)
    result=await chat(pid,'business-context')
    assert result['status']=='succeeded',result
    payload=captured[0]
    text=json.dumps(payload,ensure_ascii=False)
    for private in (*runtime.environment().values(),'DEVELOPER_HEALTH_SENTINEL','LEGACY_TASK_STORE_SENTINEL',
                    'OLD_PROJECT_TASK_SENTINEL','DEVELOPMENT_PROJECT_DESCRIPTION'):
        assert private not in text,private
    assert 'env_id' not in text and 'generation' not in text and 'role_test' not in text
    context=material_context(payload)
    assert context['project']['description']=='当前项目的协作目标'
    assert context['project']['id']=='project_rdos' and len(context['projects'])==1
    assert [item['task_id'] for item in context['tasks']]==[current['task_id']]
    assert context['tasks'][0]['executor_node_ids']==['node_codex']
    assert not {'health','materials','prior_operations'} & set(context)
    system='\\n'.join(m['content'] for m in payload['messages'] if m['role']=='system')
    assert 'panel_administrator' in system
    assert all(result[key]==value for key,value in runtime.environment().items())
    assert result['allowed_projects']==[pid]
asyncio.run(scenario())
''')

    def test_business_organization_chat_keeps_all_authorized_projects_without_rewriting_run_authority(self):
        self.check('''
async def scenario():
    await task()
    local.put('state','business_workflow',{'version':1})
    from server import rdos_projects
    second=rdos_projects.create_project({'name':'第二业务项目','description':'独立工作','node_ids':['node_lab_phase3_workbuddy'],'folder_url':''},admin,'org-second')
    thread=service.create_thread(admin,'','机构工作')
    run=service.new_run(admin,'查看当前工作','organization-business','chat','manual','',thread['id'])
    original_scope=list(run['allowed_projects'])
    assert pid in original_scope and len(original_scope)>1
    assert {tool['function']['name'] for tool in service.tool_specs(run)}>={
        'phase3_read_workspace','phase3_list_tasks','phase3_read_document','phase3_prepare_task',
        'phase3_list_knowledge','phase3_read_knowledge'}
    denied=await service.execute_tool('list_projects',{},run,admin)
    assert not denied['ok'] and denied['code']=='not_allowed'
    forged=await service.execute_tool('phase3_read_workspace',{}, {**run,'generation':'forged'},admin)
    assert not forged['ok'] and forged['code']=='authorization_failed'
    global MODEL_SCRIPT
    MODEL_SCRIPT=[backend.tool_message('phase3_read_workspace',{'project_id':'project_rdos'}),'已查看当前项目记录。']
    await service.run_agent(run['id'])
    result=store.get('run',run['id'])
    assert result['status']=='succeeded',result
    assert result['project_id']=='' and result['allowed_projects']==original_scope
    assert all(result[key]==value for key,value in runtime.environment().items())
    context=material_context(captured[0])
    assert {p['id'] for p in context['projects']}=={'project_rdos',second['id']}
    assert context['workspace'] is None
    assert {w['project']['id'] for w in context['workspaces']}=={'project_rdos',second['id']}
    final=json.dumps(captured[-1],ensure_ascii=False)
    assert 'env_id' not in final and 'generation' not in final and 'role_test' not in final
asyncio.run(scenario())
''')

    def test_phase3_check_uses_business_sources_and_unchanged_check_skips_model(self):
        self.check('''
async def scenario():
    await task()
    first=await sync('business-check')
    assert first['status']=='succeeded',first
    assert material_context(captured[0])['tasks'][0]['completion_rule']=={'kind':'receipt_recorded'}
    calls=len(captured)
    again=await sync('business-check-unchanged')
    assert again['status']=='succeeded' and again['no_analysis'] and again['usage']['model_calls']==0,again
    assert len(captured)==calls
asyncio.run(scenario())
''')

    def test_retry_uses_own_business_outcomes_without_prior_debug_records(self):
        self.check('''
async def scenario():
    global MODEL_SCRIPT
    MODEL_SCRIPT=[backend.tool_message('phase3_read_workspace',{}),{'http_status':401}]
    failed=await chat(pid,'business-retry')
    assert failed['status'] in ('failed','needs_attention'),failed
    for step in failed['steps']:
        if step.get('type')=='tool_result':step['result']['developer_context']='PRIVATE_PRIOR_TOOL_PAYLOAD'
    failed['previous_attempts']=[{'run_id':'foreign','project_id':project_id,'thread_id':'foreign-thread',
                                 'status':'failed','error':'FOREIGN_ATTEMPT_SENTINEL'}]
    failed['technical_detail']='PRIVATE_DEBUG_CONTEXT'
    store.put('run',failed['id'],failed)
    MODEL_SCRIPT=['当前工作记录已核对。']
    before=len(captured)
    queued=await service.retry_run(failed['id'],admin)
    await service.ACTIVE_RUN_TASKS[queued['id']]
    result=store.get('run',queued['id'])
    assert result['status']=='succeeded',result
    context=material_context(captured[before])
    assert len(context['previous_attempts'])==1
    assert context['previous_attempts'][0]['tool_outcomes']==[{'name':'phase3_read_workspace','ok':True,'code':''}]
    text=json.dumps(captured[before],ensure_ascii=False)
    assert all(value not in text for value in ('PRIVATE_PRIOR_TOOL_PAYLOAD','PRIVATE_DEBUG_CONTEXT','FOREIGN_ATTEMPT_SENTINEL'))
asyncio.run(scenario())
''')

    def test_default_context_keeps_one_task_fact_and_closed_detail_is_read_on_demand(self):
        self.check('''
async def scenario():
    global MODEL_SCRIPT
    from server import rdos_projects
    def trusted_scope(payload):
        for message in payload['messages']:
            if message['role']!='system':continue
            content=message['content'];start=content.find('{')
            if start<0:continue
            try:value=json.loads(content[start:])
            except ValueError:continue
            if isinstance(value,dict) and 'scope' in value:return value
        raise AssertionError([message['content'][:180] for message in payload['messages'] if message['role']=='system'])
    local.put('state','business_workflow',{'version':1})
    second=rdos_projects.create_project({'name':'另一个授权项目','description':'用于核对会话范围',
        'node_ids':['node_lab_phase3_workbuddy'],'folder_url':''},admin,'context-other-project')
    node=domain.make_node_actor('node_lab_phase3_codex')
    old_marker='CLOSED_REQUIREMENTS_ONLY_ON_EXPLICIT_READ_4f67'
    old_goal=old_marker+'；旧执行结构要求。'*180
    draft=await domain.prepare_task({'title':'已交付的文件简报','goal':old_goal,'deliverable':'可供项目成员阅读的简报',
        'task_type':'deliverable','completion_rule':{'kind':'receipt_recorded'},
        'executor_node_ids':[node['node_id']]},admin,'closed-context-draft')
    started=await domain.apply_task_action(draft['task_id'],'start',
        {'draft_id':draft['id'],'expected_hash':draft['draft_hash']},admin,'closed-context-start',0,0)
    artifact=await domain.create_document({'task_id':started['task_id'],
        'requirements_version':started['requirements_version'],'title':'工作文件简报','content':'工作文件已整理，成果保留在项目工作区。'},
        node,'closed-context-artifact')
    reference={'document_id':artifact['id'],'revision':artifact['revision'],'content_hash':artifact['content_hash']}
    await domain.submit_event({'event_type':'submission','body':'文件简报已提交。','result_status':'completed',
        'task_refs':[{'task_id':started['task_id'],'requirements_version':started['requirements_version']}],
        'artifact_refs':[reference]},node,'closed-context-result')
    refreshed=await domain.refresh(admin,'closed-context-readback')
    closed=next(item for item in refreshed['tasks'] if item['task_id']==started['task_id'])
    assert closed['business_status']=='closed' and closed['receipt_status']=='verified',closed
    active_marker='ACTIVE_REQUIREMENT_HAS_ONE_DEFAULT_COPY_2c90'
    active_draft=await domain.prepare_task({'title':'仍在执行的整理','goal':active_marker,
        'deliverable':'当前进展','executor_node_ids':[node['node_id']]},admin,'active-context-draft')
    active=await domain.apply_task_action(active_draft['task_id'],'start',
        {'draft_id':active_draft['id'],'expected_hash':active_draft['draft_hash']},admin,'active-context-start',0,0)

    MODEL_SCRIPT=[backend.tool_message('phase3_list_tasks',{}),'已按需读取原始任务要求。']
    result=await chat(pid,'compact-task-context')
    assert result['status']=='succeeded',result
    first=captured[0]
    context=material_context(first)
    serialized=json.dumps(context,ensure_ascii=False)
    assert old_marker not in json.dumps(first,ensure_ascii=False)
    assert serialized.count(active_marker)==1,serialized
    assert len(context['tasks'])==2 and len({item['task_id'] for item in context['tasks']})==2
    summary=next(item for item in context['tasks'] if item['task_id']==closed['task_id'])
    assert summary['business_status']=='closed' and summary['result_status']=='completed'
    assert summary['completion_rule']=={'kind':'receipt_recorded'}
    assert summary['result_refs'][0]['document_id']==artifact['id']
    assert summary['result_refs'][0]['revision']==artifact['revision']
    assert summary['result_refs'][0]['content_hash']==artifact['content_hash']
    assert 'goal' not in summary and 'deliverable' not in summary
    assert 'tasks' not in context['workspace']
    assert all('tasks' not in index and 'drafts' not in index for index in context['workspaces'])
    assert set(context['workspaces'][0]['task_ids'])=={closed['task_id'],active['task_id']}
    trusted=trusted_scope(first)
    assert trusted['scope']=={'kind':'project_conversation','project_ids':['project_rdos'],
        'represents_all_account_permissions':False}
    assert len(rdos_projects.readable_project_ids(admin))==2
    assert result['allowed_projects']==[pid]
    detail=json.loads(next(message['content'] for message in captured[1]['messages'] if message['role']=='tool'))
    assert detail['ok'] and detail['source']=='registered_task_authority',detail
    full=next(item for item in detail['items'] if item['task_id']==closed['task_id'])
    assert full['goal']==old_goal and full['business_status']=='closed'

    before=len(captured)
    MODEL_SCRIPT=['已查看全部授权项目。']
    organization=await chat('','compact-org-context')
    assert organization['status']=='succeeded',organization
    org_payload=captured[before]
    org=material_context(org_payload)
    org_trusted=trusted_scope(org_payload)
    assert org_trusted['scope']['kind']=='organization_conversation'
    assert org_trusted['scope']['represents_all_account_permissions'] is True
    assert set(org_trusted['scope']['project_ids'])=={'project_rdos',second['id']}
    assert {project['id'] for project in org['projects']}==set(org_trusted['scope']['project_ids'])
    assert old_marker not in json.dumps(org_payload,ensure_ascii=False)
    assert json.dumps(org,ensure_ascii=False).count(active_marker)==1
asyncio.run(scenario())
''')


if __name__=='__main__':unittest.main()
