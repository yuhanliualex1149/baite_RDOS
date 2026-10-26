import unittest

from tests.phase1_support import run_guarded


class BackendTests(unittest.TestCase):
    def run_case(self, body):
        result = run_guarded(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_backend_survives_a_fresh_guarded_process(self):
        result = run_guarded('''
tools = backend.make_tools(project_id)
task = tools.list_tasks()['items'][0]
fields = {'instructions': '跨真实进程保留的纯合成内容'}
assert tools.save_task(fields, 'restart-operation', task['record_id'])['status'] == 'verified'
store.put('test', 'restart', {'record_id': task['record_id']})
''', restart_body='''
tools = backend.make_tools(project_id)
record_id = store.get('test', 'restart')['record_id']
result = tools.save_task({'instructions': '跨真实进程保留的纯合成内容'}, 'restart-operation', record_id)
assert result['status'] == 'duplicate', result
assert backend.stats(project_id)['task_writes'] == 1
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_task_write_readback_idempotency_survives_seed(self):
        self.run_case('''
tools = backend.make_tools(project_id)
task = next(t for t in tools.list_tasks()['items'] if t['task_id'] == 'lab_task_codex')
before = task['instructions']
fields = {'task_id': task['task_id'], 'instructions': before + '\\n合成要求', 'project_id': project_id}
backend.set_fault(project_id, 'write_task_after_commit')
result = tools.save_task(fields, 'test-task-1', task['record_id'])
assert result['status'] == 'verified', result
assert backend.stats(project_id)['task_writes'] == 1
backend.seed()
again = backend.make_tools(project_id).save_task(fields, 'test-task-1', task['record_id'])
assert again['status'] == 'duplicate', again
assert backend.stats(project_id)['task_writes'] == 1
assert backend.make_tools(project_id).list_tasks()['items']
''')

    def test_readback_failure_leaves_one_durable_mutation(self):
        self.run_case('''
from server.feishu_tools import FeishuError
tools = backend.make_tools(project_id)
task = tools.list_tasks()['items'][0]
backend.set_fault(project_id, 'read_tasks_after_write', code='timeout')
try:
    tools.save_task({'instructions': '写入后读取失败的合成例子'}, 'test-uncertain', task['record_id'])
except FeishuError as exc:
    assert exc.code == 'write_uncertain', exc.code
else:
    raise AssertionError('write readback unexpectedly succeeded')
assert backend.stats(project_id)['task_writes'] == 1
assert next(t for t in backend.make_tools(project_id).list_tasks()['items'] if t['record_id'] == task['record_id'])['instructions'] == '写入后读取失败的合成例子'
''')

    def test_scoped_resources_and_project_mutations(self):
        self.run_case('''
from server.feishu_tools import FeishuError
tools = backend.make_tools(project_id)
for target in ('formal-token', 'lab_doc_phase1_b'):
    try:
        tools.read_source(target, 'docx')
    except FeishuError as exc:
        assert exc.code == 'out_of_scope'
    else:
        raise AssertionError('cross-project read permitted')
task = tools.list_tasks()['items'][0]
try:
    tools.save_task({'project_id': 'lab_project_phase1_b'}, 'bad-project', task['record_id'])
except FeishuError as exc:
    assert exc.code == 'out_of_scope', exc.code
else:
    raise AssertionError('cross-project mutation permitted')
assert backend.stats(project_id).get('task_writes', 0) == 0
try:
    backend.make_tools(project_id, [{'id': 'formal-token', 'kind': 'docx'}])
except FeishuError as exc:
    assert exc.code == 'out_of_scope'
else:
    raise AssertionError('unregistered source permitted')
''')

    def test_document_patch_uses_original_verification_algorithm(self):
        self.run_case('''
tools = backend.make_tools(project_id)
doc = tools.read_document('lab_doc_intro')
result = tools.publish_patch('docx', 'lab_doc_intro', doc['content_hash'], '', '纯合成新增段落', 'patch-1')
assert result['status'] == 'verified', result
assert tools.publish_patch('docx', 'lab_doc_intro', doc['content_hash'], '', '纯合成新增段落', 'patch-1')['status'] == 'duplicate'
assert backend.stats(project_id)['document_writes'] == 1
assert tools.append_feedback('合成反馈', 'feedback-1')['status'] == 'verified'
assert tools.append_feedback('合成反馈', 'feedback-1')['status'] == 'duplicate'
''')

    def test_real_agent_loop_and_unknown_tool_rejection(self):
        self.run_case('''
async def check():
    seen, steps = [], []
    async def execute(name, args):
        seen.append((name, args))
        return {'ok': True, 'items': []}
    async def emit(step):
        steps.append(step)
    agent = backend.make_agent(script=[backend.tool_message('read_feedback', {'project_id': project_id}),
                                        backend.tool_message('save_task', {'task_id': 'bad'}),
                                        '本地模拟完成；没有发布任务。'])
    result = await agent.run([{'role': 'user', 'content': 'read'}],
        [{'type': 'function', 'function': {'name': 'read_feedback', 'parameters': {'type': 'object'}}}], execute, emit)
    assert seen == [('read_feedback', {'project_id': project_id})], seen
    assert result['usage']['model_calls'] == 3
    assert result['usage']['tool_calls'] == 2
    assert any(s.get('result', {}).get('error', {}).get('code') == 'tool_unknown' for s in steps)
    assert result['model'] == 'local-deterministic-mock'
asyncio.run(check())
''')

    def test_default_panel_example_runs_tools_and_creates_only_local_draft(self):
        self.run_case('''
from server import central_service as service, phase1_runtime
phase1_runtime.initialize()
async def check():
    thread = service.create_thread(admin, project_id)
    run = service.new_run(admin, '请根据反馈起草更新既有任务', 'panel-draft-example', project_id=project_id, thread_id=thread['id'])
    await service.run_agent(run['id'])
    done = store.get('run', run['id'])
    assert done['status'] == 'succeeded', done
    drafts = store.items('task_draft')
    assert len(drafts) == 1 and drafts[0]['draft_action'] == 'update', drafts
    assert drafts[0]['base_task_record_id'].startswith('rec_lab_task_'), drafts
    assert backend.stats(project_id).get('task_writes', 0) == 0
    assert store.items('feedback'), 'seed feedback must be genuinely parsed'
    assert done['usage']['tool_calls'] == 3, done
    assert 'local_mock' in done['result'], done['result']
asyncio.run(check())
''')

    def test_fault_skip_and_bounded_real_worker_delay(self):
        self.run_case('''
from server.feishu_tools import FeishuError
tools = backend.make_tools(project_id)
backend.set_fault(project_id, 'read_document', code='timeout', skip=1)
assert tools.read_document('lab_feedback_phase1_a')['ok']
try:
    tools.read_document('lab_doc_intro')
except FeishuError as exc:
    assert exc.code == 'timeout'
else:
    raise AssertionError('second source should fail')
assert backend.stats(project_id)['document_reads'] == 1
assert tools.read_document('lab_doc_intro')['ok']
try:
    backend.set_delay(project_id, 'write_task', 2)
except ValueError:
    pass
else:
    raise AssertionError('unbounded delay accepted')
async def check():
    task = tools.list_tasks()['items'][0]
    backend.set_delay(project_id, 'write_task', 0.15)
    worker = asyncio.create_task(asyncio.to_thread(tools.save_task,
        {'instructions': '延迟后真实写入的合成内容'}, 'delayed-write', task['record_id']))
    for _ in range(100):
        if backend.stats(project_id).get('write_task_delay_started'):
            break
        await asyncio.sleep(0.005)
    else:
        raise AssertionError('real worker never entered delay')
    assert not worker.done()
    assert backend.stats(project_id).get('task_writes', 0) == 0
    result = await worker
    assert result['status'] == 'verified', result
    assert backend.stats(project_id)['task_writes'] == 1
    assert backend.stats(project_id)['task_reads'] == 3
asyncio.run(check())
''')

    def test_model_protocol_rejects_malformed_duplicate_and_incomplete_messages(self):
        self.run_case('''
from server.central_agent import AgentError
async def check():
    invoked = []
    async def execute(name, args):
        invoked.append(name)
        return {'ok': True}
    duplicate = backend.tool_message('read_feedback', {})
    duplicate['tool_calls'] *= 2
    cases = [({'http_status': 200, 'raw_body': '{broken-json'}, 'model_protocol'),
             (duplicate, 'model_protocol'),
             ({'choices': [{'message': {'role': 'assistant', 'content': 'truncated'}, 'finish_reason': 'length'}]}, 'model_incomplete')]
    for response, expected in cases:
        agent = backend.make_agent([response])
        try:
            await agent.run([{'role': 'user', 'content': 'test'}], [], execute)
        except AgentError as exc:
            assert exc.code == expected, (exc.code, expected)
        else:
            raise AssertionError('invalid model response accepted')
    assert not invoked, invoked
asyncio.run(check())
''')

    def test_model_http_retries_are_bounded_and_do_not_repeat_tools(self):
        self.run_case('''
from server.central_agent import AgentError
async def check():
    invoked, steps = [], []
    async def execute(name, args):
        invoked.append(name)
        return {'ok': True}
    async def emit(step):
        steps.append(step)
    specs = [{'type': 'function', 'function': {'name': 'read_feedback', 'parameters': {'type': 'object'}}}]
    agent = backend.make_agent([{'http_status': 503}, backend.tool_message('read_feedback', {}), 'local_mock complete'])
    result = await agent.run([{'role': 'user', 'content': 'test'}], specs, execute, emit)
    assert result['usage']['model_calls'] == 3, result
    assert result['usage']['tool_calls'] == 1 and invoked == ['read_feedback']
    assert sum(s['type'] == 'retry' for s in steps) == 1
    for script, code, requests in [([{'http_status': 503}] * 3, 'model_http', 2),
                                   ([{'http_status': 401}, 'unreachable'], 'model_auth', 1)]:
        steps.clear()
        failed = backend.make_agent(script)
        try:
            await failed.run([{'role': 'user', 'content': 'test'}], specs, execute, emit)
        except AgentError as exc:
            assert exc.code == code, exc.code
        else:
            raise AssertionError('persistent failure treated as success')
        assert sum(s['type'] == 'model_request' for s in steps) == requests, steps
    assert invoked == ['read_feedback'], invoked
asyncio.run(check())
''')

    def test_original_model_and_tool_budgets_stop_real_loop(self):
        self.run_case('''
from server.central_agent import AgentError
async def check():
    invoked = []
    async def execute(name, args):
        invoked.append(name)
        return {'ok': True}
    specs = [{'type': 'function', 'function': {'name': 'read_feedback', 'parameters': {'type': 'object'}}}]
    agent = backend.make_agent([backend.tool_message('read_feedback', {}), 'unreachable'])
    agent.max_model_calls = 1
    try:
        await agent.run([{'role': 'user', 'content': 'test'}], specs, execute)
    except AgentError as exc:
        assert exc.code == 'model_limit', exc.code
    else:
        raise AssertionError('model budget ignored')
    assert invoked == ['read_feedback']
    two = backend.tool_message('read_feedback', {}, 'first')
    two['tool_calls'] += backend.tool_message('read_feedback', {}, 'second')['tool_calls']
    agent = backend.make_agent([two, 'unreachable'])
    agent.max_tool_calls = 1
    invoked.clear()
    try:
        await agent.run([{'role': 'user', 'content': 'test'}], specs, execute)
    except AgentError as exc:
        assert exc.code == 'tool_limit', exc.code
    else:
        raise AssertionError('tool budget ignored')
    assert invoked == ['read_feedback'], invoked
asyncio.run(check())
''')


if __name__ == '__main__':
    unittest.main()
