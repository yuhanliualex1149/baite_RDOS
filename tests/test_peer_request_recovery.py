"""Actual node recovery with temporary SQLite and isolated transport boundaries."""
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from tests import test_peer_tasks as peer_tests
from tests.test_rdos_skill import task
node, row = peer_tests.node, peer_tests.row


class PeerRequestRecoveryTest(unittest.TestCase):
    setUp = peer_tests.PeerSkillTest.setUp
    tearDown = peer_tests.PeerSkillTest.tearDown

    def prepare_enrollment(self):
        self.transport.tasks = [row(task(), 'setup-task')]
        self.workflow.state.db.execute("DELETE FROM operations WHERE id='peer_identity'")
        self.workflow.state.db.commit()

    def saved(self):
        pointer = self.workflow.state.get('operations', 'peer_enrollment_current')
        return pointer['operation_id'], self.workflow.state.get('operations', pointer['operation_id'])

    def unsent(self, *, legacy=False):
        self.prepare_enrollment()
        with patch.object(self.transport, 'append', side_effect=node.WorkflowError('connection_failed', 'lost response')):
            with self.assertRaises(node.WorkflowError) as error:
                self.workflow.enroll_peer('task_example')
        self.assertEqual(error.exception.code, 'write_uncertain')
        operation_id, saved = self.saved()
        if legacy:
            saved.pop('attempts', None)
            saved.pop('last_failure', None)
            with self.workflow.state.locked():
                self.workflow.state.put('operations', operation_id, saved)
        return operation_id, saved

    def test_not_sent_failure_keeps_sanitized_evidence_and_retries_original_request(self):
        self.prepare_enrollment()
        original_append = self.transport.append
        calls = []

        def stopped_before_request(fields):
            calls.append(copy.deepcopy(fields))
            if len(calls) == 1:
                raise node.WorkflowError('connection_failed', 'SECRET_ARGUMENT_MUST_NOT_BE_SAVED',
                    write_outcome='not_sent', details={'category': 'cli_not_started', 'errno': 2,
                                                      'stderr': 'SECRET_ARGUMENT_MUST_NOT_BE_SAVED'})
            return original_append(fields)

        crypto = self.workflow._peer_crypto
        with patch.object(self.workflow, '_peer_crypto', wraps=crypto) as crypto_calls, \
                patch.object(self.transport, 'append', side_effect=stopped_before_request):
            with self.assertRaises(node.WorkflowError):
                self.workflow.enroll_peer('task_example')
            opid, first = self.saved()
            self.assertEqual(first['state'], 'retryable')
            self.assertEqual(first['last_failure']['write_outcome'], 'not_sent')
            self.assertNotIn('SECRET_ARGUMENT', json.dumps(first))
            result = self.workflow.enroll_peer('task_example')
        _, saved = self.saved()
        self.assertTrue(result['readback_verified'])
        self.assertEqual(first['private_key'], saved['private_key'])
        self.assertEqual(first['desired'], saved['desired'])
        self.assertEqual(saved['state'], 'requested')
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(len([c for c in crypto_calls.call_args_list if c.args[0]['action'] == 'generate']), 1)
        self.assertEqual(len(saved['attempts']), 2)

    def test_explicitly_rejected_transport_outcome_does_not_become_permanent_sent(self):
        self.prepare_enrollment()
        with patch.object(self.transport, 'append', side_effect=node.WorkflowError('request_rejected', 'rejected', write_outcome='rejected')):
            with self.assertRaises(node.WorkflowError):
                self.workflow.enroll_peer('task_example')
        opid, first = self.saved()
        self.assertEqual(first['state'], 'retryable')
        result = self.workflow.enroll_peer('task_example')
        self.assertTrue(result['readback_verified'])
        self.assertEqual(self.saved()[0], opid)
        self.assertEqual(first['private_key'], self.saved()[1]['private_key'])

    def test_commit_then_lost_response_reconciles_without_new_write_or_key(self):
        self.prepare_enrollment()
        self.transport.lose_response = True
        first = self.workflow.enroll_peer('task_example')
        opid, original = self.saved()
        self.transport.lose_response = False
        self.assertTrue(first['readback_verified'])
        result = self.workflow.recover_peer(opid)
        self.assertTrue(result['readback_verified'])
        self.assertEqual(self.transport.append_count, 1)
        self.assertEqual(self.saved()[1]['private_key'], original['private_key'])

    def test_unknown_write_only_reconciles_until_original_record_becomes_visible(self):
        self.prepare_enrollment()
        append = self.transport.append
        records = self.transport.records

        def committed_then_hidden(fields):
            append(fields)
            self.transport.hide_events = True
            raise node.WorkflowError('connection_failed', 'response lost after commit')

        def visible_records(table):
            return [] if table == 'tbl_events' and self.transport.hide_events else records(table)

        with patch.object(self.transport, 'append', side_effect=committed_then_hidden), \
                patch.object(self.transport, 'records', side_effect=visible_records):
            with self.assertRaises(node.WorkflowError):
                self.workflow.enroll_peer('task_example')
        opid, saved = self.saved()
        self.transport.hide_events = False
        result = self.workflow.enroll_peer('task_example')
        self.assertTrue(result['readback_verified'])
        self.assertEqual(self.transport.append_count, 1)
        self.assertEqual(self.saved()[1]['private_key'], saved['private_key'])

    def test_legacy_sent_requires_preview_and_explicit_bound_recovery(self):
        opid, original = self.unsent(legacy=True)
        with patch.object(self.workflow, '_peer_crypto', wraps=self.workflow._peer_crypto) as crypto_calls:
            with self.assertRaises(node.WorkflowError):
                self.workflow.enroll_peer('task_example')
            waiting = self.workflow.continue_peer_enrollment()
            self.assertEqual(waiting['status'], 'request_unverified')
            self.assertFalse(waiting['task_completion_allowed'])
            with self.assertRaises(node.WorkflowError):
                self.workflow.recover_peer(opid, 'wrong-hash', True)
            preview = self.workflow.recover_peer(opid)
            self.assertFalse(preview['readback_verified'])
            self.assertEqual(self.transport.append_count, 0)
            result = self.workflow.recover_peer(opid, preview['plan_hash'], True)
            replay = self.workflow.recover_peer(opid, preview['plan_hash'], True)
        self.assertTrue(result['readback_verified'])
        self.assertTrue(replay['readback_verified'])
        self.assertEqual(self.transport.append_count, 1)
        self.assertEqual(self.saved()[1]['desired'], original['desired'])
        self.assertEqual(self.saved()[1]['private_key'], original['private_key'])
        self.assertTrue(self.saved()[1]['recovery_plan']['consumed'])
        self.assertNotIn('private_key', json.dumps(preview))
        self.assertFalse(any(c.args[0]['action'] == 'generate' for c in crypto_calls.call_args_list))

    def test_json_operation_identity_is_found_when_projection_is_missing(self):
        self.prepare_enrollment()
        self.workflow.enroll_peer('task_example')
        opid, saved = self.saved()
        remote = next(item for item in self.transport.events if json.loads(item['fields']['记录数据']).get('operation_id') == opid)
        for key in ['操作ID', '事件ID', '类型', '自报节点', '任务引用']:
            remote['fields'].pop(key, None)
        with patch.object(self.transport, 'get', wraps=self.transport.get) as reads:
            result = self.workflow.recover_peer(opid)
        self.assertTrue(result['readback_verified'])
        self.assertGreaterEqual(reads.call_count, 1)
        self.assertEqual(self.transport.append_count, 1)

    def test_same_content_physical_duplicates_are_one_verified_business_event(self):
        self.prepare_enrollment()
        self.workflow.enroll_peer('task_example')
        opid, saved = self.saved()
        remote = next(item for item in self.transport.events if json.loads(item['fields']['记录数据']).get('operation_id') == opid)
        duplicate = copy.deepcopy(remote)
        duplicate['record_id'] = 'same-logical-request-second-row'
        self.transport.events.append(duplicate)
        result = self.workflow.recover_peer(opid)
        self.assertEqual(result['physical_rows'], 2)
        self.assertEqual(result['logical_events'], 1)
        self.assertEqual(self.transport.append_count, 1)

    def test_same_identifier_different_body_or_nonempty_projection_conflict_never_overwrites(self):
        self.prepare_enrollment()
        self.workflow.enroll_peer('task_example')
        opid, saved = self.saved()
        remote = next(item for item in self.transport.events if json.loads(item['fields']['记录数据']).get('operation_id') == opid)
        original = copy.deepcopy(remote)
        for change in ['body', 'projection']:
            remote.clear(); remote.update(copy.deepcopy(original))
            if change == 'body':
                altered = json.loads(remote['fields']['记录数据']); altered['body'] = 'different'
                remote['fields']['记录数据'] = node.canonical(altered)
            else:
                remote['fields']['操作ID'] = 'another-operation'
            with self.assertRaises(node.WorkflowError) as error:
                self.workflow.recover_peer(opid)
            self.assertEqual(error.exception.code, 'operation_conflict')
        self.assertEqual(self.transport.append_count, 1)

    def test_independent_get_mismatch_is_rejected_even_when_list_matches(self):
        self.prepare_enrollment()
        self.workflow.enroll_peer('task_example')
        opid, saved = self.saved()
        actual_get = self.transport.get

        def changed_record(table, identifier):
            value = actual_get(table, identifier)
            event = json.loads(value['fields']['记录数据']); event['body'] = 'changed during readback'
            value['fields']['记录数据'] = node.canonical(event)
            return value

        with patch.object(self.transport, 'get', side_effect=changed_record):
            with self.assertRaises(node.WorkflowError) as error:
                self.workflow.recover_peer(opid)
        self.assertEqual(error.exception.code, 'operation_conflict')
        self.assertEqual(self.transport.append_count, 1)

    def test_incomplete_scan_never_issues_or_confirms_recovery(self):
        opid, saved = self.unsent(legacy=True)
        with patch.object(self.transport, 'records', side_effect=node.WorkflowError('incomplete_read', 'synthetic pagination break')):
            with self.assertRaises(node.WorkflowError):
                self.workflow.recover_peer(opid)
        self.assertEqual(self.transport.append_count, 0)
        self.assertNotIn('recovery_plan', self.saved()[1])

    def test_success_envelope_without_records_cannot_create_a_recovery_plan(self):
        opid, saved = self.unsent(legacy=True)
        real_transport = node.Feishu.__new__(node.Feishu)
        real_transport.manifest = self.workflow.manifest
        real_transport.binary = '/isolated/existing-cli'
        original_records = self.transport.records

        def records(table):
            return original_records(table) if table == 'tbl_tasks' else real_transport.records(table)

        with patch.object(self.transport, 'records', side_effect=records), \
                patch.object(real_transport, 'run', return_value={}):
            with self.assertRaises(node.WorkflowError) as error:
                self.workflow.recover_peer(opid)
        self.assertEqual(error.exception.code, 'incomplete_read')
        self.assertEqual(self.transport.append_count, 0)
        self.assertNotIn('recovery_plan', self.saved()[1])

    def test_independent_get_cannot_substitute_another_physical_record(self):
        self.prepare_enrollment()
        self.workflow.enroll_peer('task_example')
        opid, saved = self.saved()
        actual_get = self.transport.get

        def substituted(table, identifier):
            value = actual_get(table, identifier)
            value['record_id'] = 'different-physical-record'
            return value

        with patch.object(self.transport, 'get', side_effect=substituted):
            with self.assertRaises(node.WorkflowError) as error:
                self.workflow.recover_peer(opid)
        self.assertEqual(error.exception.code, 'readback_mismatch')
        self.assertEqual(self.transport.append_count, 1)

    def test_task_version_pause_cancel_or_scope_changes_forbid_retransmission(self):
        opid, original = self.unsent(legacy=True)
        preview = self.workflow.recover_peer(opid)
        changes = [task(requirements_version=2), task(business_status='paused'),
                   task(business_status='closed', close_reason='cancelled'),
                   task(executor_node_ids=['node_workbuddy'])]
        for current in changes:
            self.transport.tasks = [row(current, 'setup-task')]
            with self.assertRaises(node.WorkflowError):
                self.workflow.recover_peer(opid, preview['plan_hash'], True)
        self.assertEqual(self.transport.append_count, 0)
        self.assertEqual(self.saved()[1]['desired'], original['desired'])
        self.assertEqual(self.saved()[1]['private_key'], original['private_key'])

    def test_completed_task_may_reconcile_but_does_not_authorize_new_retry(self):
        opid, saved = self.unsent(legacy=True)
        self.transport.tasks = [row(task(business_status='closed', close_reason='completed'), 'setup-task')]
        preview = self.workflow.recover_peer(opid)
        with self.assertRaises(node.WorkflowError) as error:
            self.workflow.recover_peer(opid, preview['plan_hash'], True)
        self.assertEqual(error.exception.code, 'enrollment_not_authorized')
        self.assertEqual(self.transport.append_count, 0)

    def test_task_is_reread_after_full_event_scan_before_write(self):
        self.prepare_enrollment()
        records = self.transport.records

        def paused_after_scan(table):
            value = records(table)
            if table == 'tbl_events':
                self.transport.tasks = [row(task(business_status='paused'), 'setup-task')]
            return value

        with patch.object(self.transport, 'records', side_effect=paused_after_scan):
            with self.assertRaises(node.WorkflowError):
                self.workflow.enroll_peer('task_example')
        self.assertEqual(self.transport.append_count, 0)

    def test_ack_unknown_write_can_recover_exact_original_without_new_identity(self):
        with patch.object(self.transport, 'append', side_effect=node.WorkflowError('connection_failed', 'unknown after dispatch')):
            with self.assertRaises(node.WorkflowError):
                self.workflow.peer_ack()
        operation = next((opid, json.loads(payload)) for opid, payload in self.workflow.state.db.execute('select id,payload from operations')
                         if json.loads(payload).get('desired', {}).get('event_type') == 'peer_capability_ack')
        opid, saved = operation
        preview = self.workflow.recover_peer(opid)
        with patch.object(self.workflow, '_peer_crypto', wraps=self.workflow._peer_crypto) as calls:
            result = self.workflow.recover_peer(opid, preview['plan_hash'], True)
        self.assertTrue(result['readback_verified'])
        self.assertEqual(self.workflow.state.get('operations', opid)['desired'], saved['desired'])
        self.assertEqual(self.transport.append_count, 1)
        self.assertFalse(any(c.args[0]['action'] == 'generate' for c in calls.call_args_list))

    def test_acked_identity_rotated_before_recovery_cannot_replay_old_ack(self):
        with patch.object(self.transport, 'append', side_effect=node.WorkflowError('connection_failed', 'unknown after dispatch')):
            with self.assertRaises(node.WorkflowError):
                self.workflow.peer_ack()
        opid = next(opid for opid, payload in self.workflow.state.db.execute('select id,payload from operations')
                    if json.loads(payload).get('desired', {}).get('event_type') == 'peer_capability_ack')
        preview = self.workflow.recover_peer(opid)
        self.registry['nodes'][0]['binding_id'] = 'binding_replaced'
        registry_event = json.loads(self.transport.events[0]['fields']['记录数据'])
        registry_event['registry_proof'] = self.workflow._peer_sign(self.registry, self.central)
        self.transport.events[0] = row(registry_event, 'record_registry', 'event')
        with self.assertRaises(node.WorkflowError) as error:
            self.workflow.recover_peer(opid, preview['plan_hash'], True)
        self.assertEqual(error.exception.code, 'identity_expired')
        self.assertEqual(self.transport.append_count, 0)

    def test_old_ack_checkpoint_without_kind_or_attempt_metadata_recovers(self):
        with patch.object(self.transport, 'append', side_effect=node.WorkflowError('connection_failed', 'unknown')):
            with self.assertRaises(node.WorkflowError):
                self.workflow.peer_ack()
        opid, current = next((opid, json.loads(payload)) for opid, payload in self.workflow.state.db.execute('select id,payload from operations')
                             if json.loads(payload).get('desired', {}).get('event_type') == 'peer_capability_ack')
        legacy = {'desired': current['desired'], 'state': 'sent'}
        with self.workflow.state.locked():
            self.workflow.state.put('operations', opid, legacy)
        preview = self.workflow.verify(opid)
        self.assertEqual(preview['status'], 'recovery_confirmation_required')
        self.assertEqual(preview['plan']['attempt_count'], 0)
        result = self.workflow.recover_peer(opid, preview['plan_hash'], True)
        self.assertTrue(result['readback_verified'])
        self.assertEqual(self.transport.append_count, 1)

    def test_cli_preview_confirm_and_wrong_hash_use_real_main_workflow_and_sqlite(self):
        opid, saved = self.unsent(legacy=True)
        registration = Path(self.tmp.name) / 'manifest.json'
        registration.write_text(json.dumps(self.workflow.manifest))
        arguments = ['--manifest', str(registration), '--state', str(Path(self.tmp.name) / 'sender.sqlite3'),
                     'recover-peer', opid]

        def invoke(extra):
            output = io.StringIO()
            with patch.object(node, 'Feishu', return_value=self.transport), redirect_stdout(output):
                status = node.main(arguments + extra)
            return status, json.loads(output.getvalue())

        status, preview = invoke([])
        self.assertEqual(status, 0)
        self.assertEqual(preview['data']['status'], 'recovery_confirmation_required')
        self.assertEqual(self.transport.append_count, 0)
        status, rejected = invoke(['--confirm-retry', '--expected-hash', 'wrong'])
        self.assertEqual(status, 1)
        self.assertEqual(rejected['code'], 'version_conflict')
        self.assertEqual(self.transport.append_count, 0)
        status, confirmed = invoke(['--confirm-retry', '--expected-hash', preview['data']['plan_hash']])
        self.assertEqual(status, 0)
        self.assertTrue(confirmed['data']['readback_verified'])
        self.assertEqual(self.transport.append_count, 1)
        self.assertNotIn(saved['private_key'], json.dumps([preview, rejected, confirmed]))

    def test_previous_version_enrollment_cannot_complete_current_version_task(self):
        self.prepare_enrollment()
        self.workflow.enroll_peer('task_example')
        original = self.saved()[1]
        self.transport.tasks = [row(task(requirements_version=2, record_revision=2), 'setup-task')]
        with self.assertRaises(node.WorkflowError) as checked:
            self.workflow.ensure_peer_enrollment_completion('task_example')
        self.assertEqual(checked.exception.code, 'version_conflict')
        with self.assertRaises(node.WorkflowError) as submitted:
            self.workflow.submit('task_example', 'completed', 'cannot borrow old enrollment', 'v2-complete')
        self.assertEqual(submitted.exception.code, 'version_conflict')
        self.assertEqual(self.transport.append_count, 1)
        self.assertEqual(self.saved()[1]['desired'], original['desired'])

    def test_completion_guard_uses_locked_task_and_last_version_check_rejects_race(self):
        self.prepare_enrollment()
        self.workflow.enroll_peer('task_example')
        original = self.workflow.ensure_peer_enrollment_completion
        seen = []

        def changed_after_guard(task_id, current_task=None):
            seen.append((self.workflow.state.db.in_transaction, current_task))
            result = original(task_id, current_task)
            self.transport.tasks = [row(task(requirements_version=2, record_revision=2), 'setup-task')]
            return result

        with patch.object(self.workflow, 'ensure_peer_enrollment_completion', side_effect=changed_after_guard):
            with self.assertRaises(node.WorkflowError) as caught:
                self.workflow.submit('task_example', 'completed', 'done', 'race-complete')
        self.assertEqual(caught.exception.code, 'version_conflict')
        self.assertTrue(seen[0][0])
        self.assertEqual(seen[0][1]['requirements_version'], 1)
        self.assertEqual(self.transport.append_count, 1)

    def test_other_ordinary_task_does_not_require_unrelated_enrollment(self):
        self.prepare_enrollment()
        self.workflow.enroll_peer('task_example')
        self.transport.tasks = [row(task(task_id='task_ordinary', id='task_ordinary'), 'ordinary-task')]
        result = self.workflow.submit('task_ordinary', 'completed', 'ordinary result', 'ordinary-complete')
        self.assertTrue(result['readback_verified'])
        self.assertEqual(self.transport.append_count, 2)


class FeishuOutcomeTest(unittest.TestCase):
    def transport(self):
        value = node.Feishu.__new__(node.Feishu)
        value.binary = '/isolated/existing-cli'
        return value

    def test_only_narrow_launch_failures_are_known_not_sent(self):
        for error in [FileNotFoundError(2, 'not present'), PermissionError(13, 'not executable')]:
            with patch.object(node.subprocess, 'run', side_effect=error):
                with self.assertRaises(node.WorkflowError) as caught:
                    self.transport().run(['base', '+record-upsert'])
            self.assertEqual(caught.exception.write_outcome, 'not_sent')
        for error in [OSError('communication interrupted'), subprocess.TimeoutExpired('cli', 60)]:
            with patch.object(node.subprocess, 'run', side_effect=error):
                with self.assertRaises(node.WorkflowError) as caught:
                    self.transport().run(['base', '+record-upsert'])
            self.assertEqual(caught.exception.write_outcome, 'unknown')

    def test_cli_failure_remote_failure_and_bad_json_are_unknown_and_redacted(self):
        fixtures = [subprocess.CompletedProcess([], 1, 'SECRET_STDOUT', 'SECRET_STDERR'),
                    subprocess.CompletedProcess([], 1, json.dumps({'ok': False, 'error': {'code': 'permission_denied', 'message': 'SECRET'}}), ''),
                    subprocess.CompletedProcess([], 0, json.dumps({'code': 1254302, 'msg': 'SECRET'}), ''),
                    subprocess.CompletedProcess([], 0, json.dumps({'data': 'SECRET'}), '')]
        for completed in fixtures:
            with patch.object(node.subprocess, 'run', return_value=completed):
                with self.assertRaises(node.WorkflowError) as caught:
                    self.transport().run(['base', '+record-upsert'])
            self.assertEqual(caught.exception.write_outcome, 'unknown')
            self.assertNotIn('SECRET', str(caught.exception) + json.dumps(caught.exception.details))

    def test_incomplete_success_structure_is_not_a_verified_empty_table(self):
        transport=self.transport()
        transport.manifest={'base_token':'base_example','tasks_table':'tbl_tasks','events_table':'tbl_events'}
        incomplete=[{}, {'has_more':False}, {'items':None}, {'records':{}},
                    {'fields':[],'data':[]}, {'fields':['操作ID'],'record_id_list':['rec_one'],'data':[[]]},
                    {'fields':['操作ID'],'record_id_list':[],'data':[['op_one']]},
                    {'items':[{'record_id':'rec_one'}]}]
        for payload in incomplete:
            with patch.object(transport,'run',return_value=payload):
                with self.assertRaises(node.WorkflowError) as error:transport.records('tbl_events')
            self.assertEqual(error.exception.code,'incomplete_read')

    def test_explicit_empty_and_complete_table_shapes_still_work(self):
        transport=self.transport()
        transport.manifest={'base_token':'base_example','tasks_table':'tbl_tasks','events_table':'tbl_events'}
        for payload in [{'items':[],'has_more':False},{'records':[],'has_more':False},
                        {'fields':['操作ID'],'record_id_list':[],'data':[],'has_more':False}]:
            with patch.object(transport,'run',return_value=payload):self.assertEqual(transport.records('tbl_events'),[])
        response={'fields':['操作ID'],'record_id_list':['rec_one'],'data':[['op_one']],'has_more':False}
        with patch.object(transport,'run',return_value=response):
            self.assertEqual(transport.records('tbl_events'),[{'record_id':'rec_one','fields':{'操作ID':'op_one'}}])


if __name__ == '__main__':
    unittest.main()
