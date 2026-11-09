"""Bounded node checks reuse snapshots without replacing independent evidence."""
import collections
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests import test_peer_tasks as peers
from tests import test_rdos_skill as business

node, row, task = business.node, business.row, business.task


class CheckSnapshotTests(unittest.TestCase):
    setUp = peers.PeerSkillTest.setUp
    tearDown = peers.PeerSkillTest.tearDown

    def count_reads(self):
        self.reads = collections.Counter()
        original = self.transport.records
        def records(table):
            self.reads[table] += 1
            return original(table)
        self.transport.records = records

    def test_table_reads_stay_constant_with_tasks_and_foreign_receipts(self):
        self.workflow.dispatch_peer(self.request, 'peer-fixture')
        self.transport.tasks.extend(row(task(task_id='task_'+str(i), id='task_'+str(i)), 'rec_'+str(i))
                                    for i in range(40))
        for i in range(30):
            event = {'protocol_version': 2, 'workspace_id': 'ws_rdos', 'project_id': 'other_project',
                'id': 'evt_'+str(i), 'operation_id': 'op_'+str(i), 'event_type': 'submission',
                'claimed_node_id': 'other_node', 'task_refs': [{'task_id': 'unrelated_task', 'requirements_version': 1}]}
            self.transport.events.append(row(event, 'event_'+str(i), 'event'))
        self.count_reads()
        with patch.object(self.workflow, '_peer_crypto', wraps=self.workflow._peer_crypto) as crypto:
            checked = self.workflow.check()
        self.assertEqual(self.reads, {'tbl_tasks': 1, 'tbl_events': 1})
        self.assertEqual(len(checked['tasks']), 40)
        self.assertEqual(checked['quarantined_records'], [])
        self.assertEqual(checked['diagnostics']['registry_builds'], 1)
        self.assertEqual(sum(c.args[0]['action'] == 'verify' for c in crypto.call_args_list), 2)
        self.assertIsNone(self.workflow._check_context)

    def test_receiver_retains_task_only_scope_and_quarantines_tampering(self):
        dispatched = self.workflow.dispatch_peer(self.request, 'peer-fixture')
        registration = business.manifest()
        registration.update(node_id='node_workbuddy', projects=[], peer=self.workflow.manifest['peer'])
        receiver = node.Workflow(registration, self.transport, node.State(Path(self.tmp.name)/'receiver.sqlite3'))
        try:
            self.count_reads()
            checked = receiver.check()
            self.assertEqual(checked['tasks'][0]['task']['task_id'], dispatched['task_id'])
            self.assertEqual(checked['tasks'][0]['plan']['reason'], 'ready')
            self.assertEqual(self.reads, {'tbl_tasks': 1, 'tbl_events': 1})
            with self.assertRaises(node.WorkflowError): receiver.project('project_rdos')
            value = json.loads(self.transport.tasks[0]['fields']['记录数据'])
            value['goal'] = 'tampered'
            self.transport.tasks[0] = row(value, 'peer_task_1')
            checked = receiver.check()
            self.assertEqual(checked['state'], 'attention_required')
            self.assertIn('task_tampered', [v['code'] for v in checked['quarantined_records']])
        finally:
            receiver.state.db.close()

    def test_next_check_and_standalone_prepare_read_current_data(self):
        self.transport.tasks = [row(task())]
        self.count_reads()
        self.workflow.check()
        self.transport.tasks = [row(task(requirements_version=2, record_revision=2))]
        second = self.workflow.check()
        self.assertEqual(second['tasks'][0]['task']['requirements_version'], 2)
        self.assertEqual(self.reads, {'tbl_tasks': 2, 'tbl_events': 2})
        self.transport.tasks = [row(task(requirements_version=3, record_revision=3))]
        self.assertEqual(self.workflow.prepare('task_example')['task']['requirements_version'], 3)
        self.assertEqual(self.reads['tbl_tasks'], 3)

    def test_partial_scan_does_not_advance_checkpoints_or_send_ack(self):
        self.transport.tasks = [row(task())]
        original = self.transport.records
        def records(table):
            if table == 'tbl_events': raise node.WorkflowError('incomplete_read', 'pagination interrupted')
            return original(table)
        self.transport.records = records
        with self.assertRaises(node.WorkflowError) as error: self.workflow.check()
        self.assertEqual(error.exception.code, 'incomplete_read')
        self.assertIsNone(self.workflow.state.get('checkpoints', 'task_example'))
        self.assertEqual(self.transport.append_count, 0)
        self.assertIsNone(self.workflow._check_context)

    def test_auto_ack_refreshes_once_then_next_check_only_reads(self):
        self.transport.tasks = [row(task())]
        with self.workflow.state.locked():
            self.workflow.state.put('operations', 'peer_enrollment_current', {'operation_id': 'enroll_fixture'})
            self.workflow.state.put('operations', 'enroll_fixture', {'kind': 'peer_enrollment',
                'state': 'approved', 'task_id': 'task_example', 'requirements_version': 1, 'request_id': 'fixture_request',
                'public_key': self.sender['public_key'], 'private_key': self.sender['private_key']})
        self.count_reads()
        with patch.object(self.transport, 'get', wraps=self.transport.get) as independent:
            checked = self.workflow.check()
        self.assertEqual(checked['peer_enrollment']['status'], 'ready')
        self.assertEqual(self.transport.append_count, 1)
        self.assertEqual(self.reads, {'tbl_tasks': 1, 'tbl_events': 2})
        self.assertGreater(independent.call_count, 0)
        self.reads.clear()
        self.workflow.check()
        self.assertEqual(self.transport.append_count, 1)
        self.assertEqual(self.reads, {'tbl_tasks': 1, 'tbl_events': 1})

    def test_listed_completion_still_requires_independent_readback(self):
        self.transport.tasks = [row(task())]
        self.workflow.submit('task_example', 'completed', 'done', 'fixture_completion')
        original = self.transport.get
        def changed(table, rid):
            result = original(table, rid)
            if result['fields'].get('类型') == 'submission':
                value = json.loads(result['fields']['记录数据'])
                value['body'] = 'changed after list'
                result['fields']['记录数据'] = node.canonical(value)
            return result
        self.transport.get = changed
        checked = self.workflow.check()
        self.assertEqual(checked['state'], 'attention_required')
        self.assertTrue(checked['tasks'][0]['plan']['uncertain_operations'])

    def test_expired_registry_does_not_authorize_peer_task(self):
        self.workflow.dispatch_peer(self.request, 'peer-fixture')
        registry = copy.deepcopy(self.registry)
        registry['expires_at'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        value = json.loads(self.transport.events[0]['fields']['记录数据'])
        value['registry_proof'] = self.workflow._peer_sign(registry, self.central)
        self.transport.events[0] = row(value, 'record_registry', 'event')
        checked = self.workflow.check()
        self.assertEqual(checked['state'], 'attention_required')
        self.assertIn('identity_expired', [v['code'] for v in checked['quarantined_records']])

    def test_irrelevant_peer_task_does_not_require_registry(self):
        self.workflow.dispatch_peer(self.request, 'peer-fixture')
        registration = business.manifest()
        registration['node_id'] = 'node_unrelated'
        outsider = node.Workflow(registration, self.transport, self.workflow.state)
        with patch.object(outsider, '_peer_registry', side_effect=AssertionError('irrelevant registry read')):
            self.assertEqual(outsider.check()['tasks'], [])

    def test_cached_registry_is_still_checked_for_expiry(self):
        self.transport.tasks = [row(task())]
        original = self.workflow.continue_peer_enrollment
        def enrollment():
            self.workflow._peer_registry()
            self.workflow._check_context['registry']['expires_at'] = '2000-01-01T00:00:00+00:00'
            with self.assertRaises(node.WorkflowError) as error: self.workflow._peer_registry()
            self.assertEqual(error.exception.code, 'identity_expired')
            return original()
        self.workflow.continue_peer_enrollment = enrollment
        self.workflow.check()


class CheckDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.transport = business.Transport(task())
        self.workflow = node.Workflow(business.manifest(), self.transport, node.State(Path(self.tmp.name)/'state.sqlite3'))
        self.now = 100.0
        self.clock = patch.object(node.time, 'monotonic', side_effect=lambda: self.now)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.workflow.state.db.close()
        self.tmp.cleanup()

    def assert_timeout(self, operation):
        with self.assertRaises(node.WorkflowError) as error: operation()
        self.assertEqual(error.exception.code, 'check_timeout')
        self.assertIn('phase', error.exception.details)
        self.assertIn('table_reads', error.exception.details)
        self.assertIsNone(self.workflow._check_context)

    def test_deadline_during_prefetch_does_not_update_task_state(self):
        original = self.transport.records
        def records(table):
            if table == 'tbl_events': self.now += 121
            return original(table)
        self.transport.records = records
        self.assert_timeout(self.workflow.check)
        self.assertIsNone(self.workflow.state.get('checkpoints', 'task_example'))

    def test_deadline_cannot_be_swallowed_by_enrollment_recovery(self):
        def enrollment():
            self.now += 121
            self.workflow._check_tick('peer_registry')
        self.workflow.continue_peer_enrollment = enrollment
        self.assert_timeout(self.workflow.check)

    def test_deadline_during_shared_skills_is_fatal(self):
        self.workflow.state.put('operations', 'shared_skill:fixture', {'folder_id': 'fixture', 'name': 'Fixture'})
        self.workflow.state.db.commit()
        def source(folder):
            self.now += 121
            self.workflow._check_tick('shared_skills')
        self.workflow._skill_source = source
        self.assert_timeout(self.workflow.check)

    def test_authentication_uses_same_deadline(self):
        def auth(args): self.now += 121
        self.transport.run = auth
        self.assert_timeout(lambda: self.workflow.check(authenticate=True))
        self.assertEqual(self.transport.task_reads, 0)

    def test_transport_subprocess_timeout_uses_remaining_budget(self):
        transport = object.__new__(node.Feishu)
        transport.binary = '/fixture/cli'
        transport.check_budget = node.CheckBudget(2)
        def expired(*args, **kwargs):
            self.assertEqual(kwargs['timeout'], 2)
            self.now += 2
            raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
        with patch.object(node.subprocess, 'run', side_effect=expired):
            with self.assertRaises(node.WorkflowError) as error: transport.run(['auth', 'status'])
        self.assertEqual(error.exception.code, 'check_timeout')

    def test_signature_timeout_is_not_quarantined_as_invalid_signature(self):
        self.workflow.manifest['peer'] = {'central_public_key': 'fixture_key'}
        value = {'event_type': 'node_registry_snapshot', 'workspace_id': 'ws_rdos',
                 'registry_proof': {'payload_json': '{}', 'signature': 'fixture_signature'}}
        original = self.transport.records
        self.transport.records = lambda table: ([{'fields': {'记录数据': json.dumps(value)}}]
                                                if table == 'tbl_events' else original(table))
        self.workflow.continue_peer_enrollment = self.workflow._peer_registry
        def expired(*args, **kwargs):
            self.now += 121
            raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
        with patch.object(node.subprocess, 'run', side_effect=expired):
            self.assert_timeout(self.workflow.check)

    def test_invalid_deadlines_rejected_before_reads(self):
        for seconds in (0, -1, float('nan'), float('inf')):
            with self.assertRaises(node.WorkflowError): self.workflow.check(seconds)
        self.assertEqual(self.transport.task_reads, 0)


if __name__ == '__main__':
    unittest.main()
