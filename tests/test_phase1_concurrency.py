"""T13: real service ownership and concurrency with bounded local transports.

No permission, confirmation, publishing, orchestration, or guard code is patched.
Only the explicit local backend delays its actual synchronous read/write work.
"""
from __future__ import annotations

import unittest

from tests.phase1_support import run_guarded


SETUP = '''
import time
from server import central_service as service, phase1_runtime as runtime, phase1_operations as ops
runtime.initialize()

def make_draft(operation='draft-one', position=0):
    tasks = sorted(backend.make_tools(project_id).list_tasks()['items'], key=lambda t: t['task_id'])
    task = tasks[position]
    return service.prepare_task({'task_id': task['task_id'], 'instructions': task['instructions']+'\\nT13 合成更新',
        'member_ids': task['member_ids'], 'draft_action': 'update', 'reason': '验证真实取消与并发'},
        {'id': 'fixture-run', 'operation_id': operation, 'project_id': project_id}, admin, task)

async def wait_for_delay(point):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if backend.stats(project_id).get(point + '_delay_started', 0):
            return
        await asyncio.sleep(0.005)
    raise AssertionError('实际工作线程未进入 ' + point)

async def must_be_busy(item, click, expected):
    try:
        await asyncio.wait_for(service.publish_task_draft(item['id'], item['draft_hash'], click, admin), 0.2)
    except ops.OperationError as exc:
        assert exc.code == expected, (exc.code, expected)
    else:
        raise AssertionError('并发确认应拒绝而非排队')
'''


class Phase1ConcurrencyTest(unittest.TestCase):
    def check(self, body: str):
        result = run_guarded(SETUP + '\n' + body, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cancelled_confirmation_keeps_real_write_owned_and_duplicate_reuses_result(self):
        self.check('''
async def scenario():
    item = make_draft()
    other = make_draft('draft-other', 1)
    before = backend.stats(project_id).copy()
    backend.set_delay(project_id, 'write_task', 1)
    confirmation = asyncio.create_task(service.publish_task_draft(item['id'], item['draft_hash'], 't13-confirm', admin))
    await wait_for_delay('write_task')
    assert service.WRITE_LOCK.locked()
    original_owner = ops.ACTIVE_WRITES['t13-confirm']
    assert not original_owner.done()
    assert store.get('operation', 't13-confirm')['dispatched_at']
    confirmation.cancel()
    try:
        await confirmation
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError('确认调用没有取消')
    assert confirmation.cancelled()
    assert service.WRITE_LOCK.locked(), '调用取消不能释放实际写入锁'
    assert ops.ACTIVE_WRITES['t13-confirm'] is original_owner and not original_owner.cancelled()
    assert backend.stats(project_id).get('task_writes', 0) == before.get('task_writes', 0)
    await must_be_busy(item, 'different-click-while-running', 'write_in_progress')
    await must_be_busy(other, 'other-operation-while-running', 'write_busy')
    assert not store.get('operation', 'different-click-while-running')
    assert not store.get('operation', 'other-operation-while-running')
    assert 'publication_operation_id' not in store.get('task_draft', other['id'])
    await asyncio.wait_for(asyncio.shield(original_owner), 3)
    await asyncio.sleep(0)
    assert not service.WRITE_LOCK.locked() and not ops.ACTIVE_WRITES
    op = store.get('operation', 't13-confirm')
    assert op['status'] == 'succeeded' and op['verified_at']
    assert len(op['attempts']) == 1 and op['attempts'][0]['status'] == 'succeeded'
    after_worker = backend.stats(project_id)
    assert after_worker['task_writes'] - before.get('task_writes', 0) == 1
    # Service baseline, FeishuTools pre-write check, independent verification.
    assert after_worker['task_reads'] - before['task_reads'] == 3
    resumed = await service.publish_task_draft(item['id'], item['draft_hash'], 'different-click-after-finish', admin)
    assert resumed['status'] == 'published' and resumed['publication_operation_id'] == 't13-confirm'
    assert backend.stats(project_id) == after_worker, '已核验结果不能再发出写入或重复读取'
    assert not store.get('operation', 'different-click-after-finish')
    remote = next(t for t in backend.make_tools(project_id).list_tasks()['items'] if t['task_id'] == item['task_id'])
    assert remote['instructions'] == item['instructions'] and remote['version'] == item['version']
    assert remote['record_id'] == item['base_task_record_id']
    assert store.get('task_draft', other['id'])['status'] == 'draft'
asyncio.run(scenario())
''')

    def test_concurrent_manual_run_and_tick_are_busy_without_queue(self):
        self.check('''
async def scenario():
    thread = service.create_thread(admin, project_id)
    backend.set_delay(project_id, 'read_tasks', 0.6)
    run = service.new_run(admin, '读取本项目测试资料', 't13-first-run', project_id=project_id, thread_id=thread['id'])
    service.launch(run)
    await wait_for_delay('read_tasks')
    active = service.ACTIVE_RUN_TASKS[run['id']]
    assert service.RUN_LOCK.locked() and service.ACTIVE_RUN_ID == run['id']
    started = time.monotonic()
    try:
        service.new_run(admin, '不能排队的新请求', 't13-overlap', project_id=project_id, thread_id=thread['id'])
    except ValueError as exc:
        assert '正在运行' in str(exc)
    else:
        raise AssertionError('并发运行没有被拒绝')
    assert time.monotonic() - started < 0.2
    result = await asyncio.wait_for(service.tick(project_id=project_id), 0.2)
    assert result == {'status': 'busy', 'executed': False}, result
    assert [r['id'] for r in store.items('run')] == [run['id']]
    assert len(service.ACTIVE_RUN_TASKS) == 1
    assert len([e for e in store.items('run_event') if e.get('kind') == 'not_executed']) == 2
    await asyncio.wait_for(asyncio.shield(active), 3)
    await asyncio.sleep(0)
    assert not service.ACTIVE_RUN_TASKS and not service.RUN_LOCK.locked() and service.ACTIVE_RUN_ID is None
    assert store.get('run', run['id'])['status'] == 'succeeded'
    next_run = service.new_run(admin, '读取本项目测试资料', 't13-after-finish', project_id=project_id, thread_id=thread['id'])
    service.launch(next_run)
    await asyncio.wait_for(asyncio.gather(*list(service.ACTIVE_RUN_TASKS.values())), 3)
    assert store.get('run', next_run['id'])['status'] == 'succeeded'
    assert len(store.items('run')) == 2
    assert backend.stats(project_id).get('task_writes', 0) == 0
asyncio.run(scenario())
''')

    def test_service_shutdown_waits_for_owned_write_to_verify_without_cancelling_it(self):
        self.check('''
async def scenario():
    item = make_draft()
    backend.set_delay(project_id, 'write_task', 0.5)
    confirmation = asyncio.create_task(service.publish_task_draft(item['id'], item['draft_hash'], 't13-shutdown-write', admin))
    await wait_for_delay('write_task')
    owner = ops.ACTIVE_WRITES['t13-shutdown-write']
    assert not owner.done() and service.WRITE_LOCK.locked()
    started = time.monotonic()
    await service.shutdown()
    elapsed = time.monotonic() - started
    assert elapsed < 2.5, elapsed
    assert not owner.cancelled() and owner.done()
    result = await confirmation
    assert result['status'] == 'published'
    assert not service.WRITE_LOCK.locked() and not ops.ACTIVE_WRITES
    op = store.get('operation', 't13-shutdown-write')
    assert op['status'] == 'succeeded' and op['verified_at'] and len(op['attempts']) == 1
    assert backend.stats(project_id)['task_writes'] == 1
    assert service.ACTIVE_RUN_ID is None
asyncio.run(scenario())
''')

    def test_service_shutdown_interrupts_running_read_without_marking_analysis_complete(self):
        self.check('''
async def scenario():
    backend.set_delay(project_id, 'read_tasks', 0.5)
    run = service.new_run(runtime.system_actor(), '受控单次检查', 't13-shutdown-run', 'sync', 'scheduled', project_id)
    service.launch(run)
    await wait_for_delay('read_tasks')
    assert service.RUN_LOCK.locked()
    started = time.monotonic()
    await service.shutdown()
    assert time.monotonic() - started < 2.5
    await asyncio.sleep(0)
    saved = store.get('run', run['id'])
    assert saved['status'] == 'interrupted' and saved['error_code'] == 'interrupted', saved
    assert not saved.get('analysis_complete')
    assert not service.ACTIVE_TASKS and not service.ACTIVE_RUN_TASKS and service.ACTIVE_RUN_ID is None
    assert not service.RUN_LOCK.locked()
    assert not (store.get('input_state', project_id) or {}).get('processed_fingerprint')
    assert not store.items('message') and not store.items('task_draft')
    assert backend.stats(project_id).get('task_writes', 0) == 0
asyncio.run(scenario())
''')


if __name__ == '__main__':
    unittest.main()
