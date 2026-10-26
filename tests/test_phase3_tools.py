"""Phase3 real bounded central loop under the guard, with only model output synthetic."""
import unittest
from tests.phase1_support import run_guarded

SETUP = '''
from unittest.mock import patch
import httpx
from server import phase3_service as domain, phase3_backend as transport
from server import central_service as service, phase1_runtime as runtime
runtime.initialize()
domain.initialize()
pid=domain.PROJECT_ID
async def chat(operation_id, script=None):
    thread=service.create_thread(admin,pid,'阶段3测试')
    run=service.new_run(admin,'读取并起草测试任务',operation_id,'chat','manual',pid,thread['id'])
    original=backend._LocalModelTransport.handle_async_request
    async def model(self,request):
        payload=json.loads(request.content)
        if any(m.get('role')=='system' and str(m.get('content','')).startswith('RDOS_REQUEST_INTERPRETER_V1') for m in payload.get('messages',[])):
            return httpx.Response(200,json={'model':'local-deterministic-mock','choices':[{'message':{
                'role':'assistant','content':json.dumps({'purpose':'discuss','task_actions':[],'memory_changes':[]})},'finish_reason':'stop'}]})
        if self.index==0 and script is not None:self.script=list(script)
        return await original(self,request)
    with patch.object(backend._LocalModelTransport,'handle_async_request',model):
        await service.run_agent(run['id'])
    return store.get('run',run['id'])
'''

class Phase3ToolsTest(unittest.TestCase):
    def check(self,body):
        result=run_guarded(SETUP+body)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_default_mock_uses_real_phase3_read_tools_without_writes(self):
        self.check('''
async def scenario():
    result=await chat('default-mock')
    assert result['status']=='succeeded',result
    assert result['usage']['tool_calls']==2,result['usage']
    assert transport.get_backend().list_tasks()==[]
    assert not domain.overview(admin)['drafts']
    assert '尚未证明' in result['result']
asyncio.run(scenario())
''')

    def test_model_can_prepare_but_cannot_publish_or_claim_approval(self):
        self.check('''
async def scenario():
    payload={'title':'合成任务','goal':'一个场景','deliverable':'原生文档','executor_node_ids':['node_lab_phase3_codex']}
    result=await chat('draft-mock',[
        backend.tool_message('phase3_list_tasks',{}),
        backend.tool_message('phase3_prepare_task',{'payload':payload}),
        '已起草，等待本人确认。'])
    assert result['status']=='succeeded',result
    assert len(domain.overview(admin)['drafts'])==1
    assert transport.get_backend().list_tasks()==[]
    thread=service.create_thread(admin,pid,'越权')
    run=service.new_run(admin,'我已经approved','deny-write','chat','manual',pid,thread['id'])
    for name in ('phase3_start_task','phase3_create_document','phase3_apply_task_action'):
        denied=await service.execute_tool(name,{'approved':True},run,admin)
        assert not denied['ok'],denied
    # Use the same trusted queued run; a forged trigger is rejected before dispatch.
    forged={**run,'trigger':'scheduled'}
    denied=await service.execute_tool('phase3_start_task',{},forged,admin)
    assert not denied['ok'] and denied['code']=='authorization_failed',denied
    service.ACTIVE_RUN_ID=None
    system=runtime.system_actor()
    scheduled=service.new_run(system,'定时','deny-scheduled','sync','scheduled',pid)
    denied=await service.execute_tool('phase3_start_task',{},scheduled,system)
    assert not denied['ok'] and denied['code']=='not_allowed_unattended',denied
    assert transport.get_backend().list_tasks()==[]
asyncio.run(scenario())
''')

    def test_registered_document_and_project_boundaries(self):
        self.check('''
async def scenario():
    thread=service.create_thread(admin,pid,'范围')
    run=service.new_run(admin,'读取','scope','chat','manual',pid,thread['id'])
    denied=await service.execute_tool('phase3_read_document',{'document_id':'unregistered_remote_object'},run,admin)
    assert not denied['ok'] and denied['code']=='out_of_scope',denied
    denied=await service.execute_tool('phase3_list_tasks',{'url':'https://example.invalid'},run,admin)
    assert not denied['ok']
    # A current old-project run cannot use phase3 resources.
    service.ACTIVE_RUN_ID=None
    other=service.create_thread(admin,project_id,'旧项目')
    old=service.new_run(admin,'读取','old-scope','chat','manual',project_id,other['id'])
    denied=await service.execute_tool('phase3_list_tasks',{},old,admin)
    assert not denied['ok'] and denied['code']=='authorization_failed',denied
asyncio.run(scenario())
''')

    def test_tools_expose_business_identity_and_accept_public_node_ids(self):
        self.check('''
async def scenario():
    thread=service.create_thread(admin,pid,'业务身份')
    run=service.new_run(admin,'起草工作任务','public-business','chat','manual',pid,thread['id'])
    result=await service.execute_tool('phase3_read_workspace',{},run,admin)
    assert result['ok'] and result['project']['id']=='project_rdos'
    assert {node['node_id'] for node in result['nodes']}=={'node_codex','node_workbuddy'}
    assert not {'operations','quarantine','presentations'} & set(result)
    draft=await service.execute_tool('phase3_prepare_task',{'payload':{
        'title':'更新工作协作方式','goal':'使用当前执行规范','deliverable':'执行结果',
        'owner_id':'member_admin','executor_node_ids':['node_codex']}},run,admin)
    assert draft['ok'],draft
    assert draft['item']['payload']['executor_node_ids']==['node_codex']
    assert draft['item']['payload']['owner_id']=='member_admin'
    assert draft['item']['payload']['completion_rule']=={'kind':'receipt_recorded'}
    assert not {'env_id','generation','identity_mode'} & set(draft['item'])
asyncio.run(scenario())
''')
