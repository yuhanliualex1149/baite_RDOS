"""Business behavior of the packaged node Skill; all external boundaries local."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
import subprocess
import sys
from unittest.mock import patch

from lab.build_node_skill import package_bytes
from server import rdos_rules

SOURCE = Path(__file__).resolve().parents[1] / 'node_skills' / 'rdos-workflow' / 'scripts' / 'rdos_node.py'
SPEC = importlib.util.spec_from_file_location('rdos_node_under_test', SOURCE)
node = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(node)


def task(**changes):
    value = {'protocol_version': 2, 'workspace_id': 'ws_rdos', 'project_id': 'project_rdos',
        'task_id': 'task_example', 'id': 'task_example', 'title': '更新工作协作方式', 'goal': '原位更新已有工作检查方式',
        'owner_id': 'owner_rdos', 'executor_node_ids': ['node_codex'], 'input_refs': [], 'deliverable': '保存配置并核对',
        'output_zone': 'project', 'dispatch_to': ['agent'], 'return_to': ['agent'], 'due_at': '', 'editable_document_ids': [],
        'task_type': 'action', 'deliverable_audience': ['researcher'], 'completion_rule': {'kind': 'receipt_recorded'},
        'business_status': 'active', 'requirements_version': 1, 'record_revision': 1, 'operation_id': 'publish-example'}
    value.update(changes)
    value['requirements_hash'] = rdos_rules.digest(rdos_rules.requirements(value))
    return value


def manifest():
    return {'protocol_version': 2, 'workspace_id': 'ws_rdos', 'project_id': 'project_rdos', 'node_id': 'node_codex',
        'base_token': 'base_example', 'tasks_table': 'tbl_tasks', 'events_table': 'tbl_events',
        'registered_resources': [], 'zones': {'project': {'folder_id': 'folder_project'}}}


def row(value, rid='record_1', kind='task'):
    fields = {'操作ID': value['operation_id'], '记录数据': node.canonical(value)}
    if kind == 'task':
        fields.update({'任务ID': value['task_id'], '标题': value['title'], '状态': value['business_status'],
            '执行节点': ', '.join(value['executor_node_ids']), '要求版本': str(value['requirements_version']), '记录修订': str(value['record_revision'])})
    else:
        fields.update({'事件ID': value['id'], '类型': value['event_type'], '自报节点': value['claimed_node_id'], '任务引用': node.canonical(value['task_refs'])})
    return {'record_id': rid, 'fields': fields}


class Transport:
    def __init__(self, value):
        self.task_value = value
        self.events = []
        self.append_count = self.create_count = 0
        self.lose_response = False
        self.hide_events = False
        self.documents = {}
        self.fetches = []
        self.task_reads = 0
        self.on_task_read = None
        self.on_fetch = None
        self.create_body_transform = lambda title, content: content

    def records(self, table):
        if table == 'tbl_tasks':
            self.task_reads += 1
            if self.on_task_read: self.on_task_read(self)
            return [row(copy.deepcopy(self.task_value))]
        return [] if self.hide_events else copy.deepcopy(self.events)

    def get(self, table, rid):
        return copy.deepcopy(next(r for r in self.events if r['record_id'] == rid))

    def append(self, fields):
        self.append_count += 1
        self.events.append({'record_id': 'event_' + str(self.append_count), 'fields': copy.deepcopy(fields)})
        if self.lose_response: raise node.WorkflowError('connection_failed', 'lost response after commit')

    def root(self, folder):
        return [{'token': key, 'type': 'docx', 'name': value['title']} for key, value in self.documents.items()
            if value.get('folder', 'folder_project') == folder]

    def fetch(self, docid):
        self.fetches.append(docid)
        if self.on_fetch: self.on_fetch(self, docid)
        return copy.deepcopy(self.documents[docid])

    def create(self, folder, title, content):
        self.create_count += 1
        docid = 'doc_created_' + str(self.create_count)
        content = self.create_body_transform(title, content)
        self.documents[docid] = {'document_id': docid, 'revision': 1, 'content_hash': node.sha(content), 'title': title, 'content': content,
            'folder': folder}
        if self.lose_response: raise node.WorkflowError('connection_failed', 'lost response after create')
        return {'document': {'document_id': docid}}


class MultiProjectTransport(Transport):
    def __init__(self, tasks):
        super().__init__(tasks[0])
        self.tasks = tasks
        self.extra_rows = []

    def records(self, table):
        if table != 'tbl_tasks': return super().records(table)
        self.task_reads += 1
        if self.on_task_read: self.on_task_read(self)
        return [row(copy.deepcopy(value), 'record_'+str(index)) for index,value in enumerate(self.tasks)] + copy.deepcopy(self.extra_rows)


def multi_manifest(second_status='active', same_folder=False):
    value=manifest()
    value['projects']=[{'id':'project_rdos','name':'RDOS 建设','status':'active','node_ids':['node_codex'],
        'zones':{'project':{'folder_id':'folder_project'},'agent':{'folder_id':'folder_agent'}},
        'registered_resources':[{'id':'doc_default'}]},
        {'id':'project_second','name':'另一项目','status':second_status,'node_ids':['node_codex'],
        'zones':{'project':{'folder_id':'folder_project' if same_folder else 'folder_second'},'agent':{'folder_id':'folder_agent'}},
        'registered_resources':[{'id':'doc_second'}]}]
    return value


class NodeBusinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.transport = Transport(task())
        self.workflow = node.Workflow(manifest(), self.transport, node.State(self.path))

    def tearDown(self):
        self.workflow.state.db.close()
        self.temp.cleanup()

    def test_action_completion_requires_result_and_reuses_verified_receipt(self):
        with self.assertRaises(rdos_rules.RuleViolation): self.workflow.submit('task_example', 'completed', '', 'complete')
        self.assertEqual(self.transport.append_count, 0)
        result = self.workflow.submit('task_example', 'completed', '已保存并读回原配置', 'complete')
        self.assertTrue(result['readback_verified'])
        repeated = self.workflow.submit('task_example', 'completed', '已保存并读回原配置', 'complete')
        self.assertEqual(repeated, result)
        self.assertEqual(self.transport.append_count, 1)
        self.assertEqual(self.workflow.prepare('task_example')['plan']['reason'], 'already_submitted')

    def test_partial_result_does_not_claim_task_completion(self):
        self.workflow.submit('task_example', 'partial', '已保存一项，另一项未保存', 'progress-1')
        self.assertEqual(self.workflow.prepare('task_example')['plan']['reason'], 'ready')
        event = json.loads(self.transport.events[0]['fields']['记录数据'])
        self.assertEqual(event['result_status'], 'partial')

    def test_wrong_node_and_paused_cannot_create_or_submit_completion(self):
        self.transport.task_value = task(executor_node_ids=['node_workbuddy'])
        with self.assertRaises(node.WorkflowError): self.workflow.submit('task_example', 'completed', '完成', 'complete')
        self.transport.task_value = task(business_status='paused')
        with self.assertRaises(node.WorkflowError): self.workflow.create_artifact('task_example', '正文', '内容', 'draft')
        with self.assertRaises(node.WorkflowError): self.workflow.submit('task_example', 'completed', '完成', 'complete')
        receipt = self.workflow.submit('task_example', 'partial', '已停止新增工作', 'pause-ack', event_type='paused_ack')
        self.assertTrue(receipt['readback_verified'])
        self.assertEqual(self.transport.create_count, 0)

    def test_version_change_retains_old_result_and_requires_current_work(self):
        self.workflow.prepare('task_example')
        self.workflow.submit('task_example', 'completed', '旧版完成', 'complete')
        self.transport.task_value = task(requirements_version=2, record_revision=2, goal='只保留一个场景')
        current = self.workflow.prepare('task_example')
        self.assertTrue(current['plan']['changed_requirements'])
        self.assertIn('perform_work', current['plan']['allowed_actions'])
        self.assertNotIn('submission_verified', current['plan']['reusable_steps'])
        self.assertEqual(len(self.transport.events), 1)

    def test_same_operation_different_content_conflicts_without_write(self):
        self.workflow.submit('task_example', 'completed', '原始结果', 'complete')
        with self.assertRaises(node.WorkflowError) as error:
            self.workflow.submit('task_example', 'completed', '不同结果', 'complete')
        self.assertEqual(error.exception.code, 'operation_conflict')
        self.assertEqual(self.transport.append_count, 1)

    def test_lost_response_after_real_append_is_read_back_once(self):
        self.transport.lose_response = True
        result = self.workflow.submit('task_example', 'completed', '真实写入后丢响应', 'complete')
        self.assertTrue(result['readback_verified'])
        self.assertEqual(self.transport.append_count, 1)

    def test_uncertain_write_survives_reopen_and_only_verifies(self):
        self.transport.hide_events = self.transport.lose_response = True
        with self.assertRaises(node.WorkflowError) as error:
            self.workflow.submit('task_example', 'completed', '结果', 'complete')
        self.assertEqual(error.exception.code, 'write_uncertain')
        self.workflow.state.db.close()
        self.workflow = node.Workflow(manifest(), self.transport, node.State(self.path))
        with self.assertRaises(node.WorkflowError): self.workflow.submit('task_example', 'completed', '结果', 'complete')
        self.assertEqual(self.transport.append_count, 1)
        self.transport.hide_events = False
        opid = self.workflow.operation_id(self.transport.task_value, 'complete', 'event')
        self.assertTrue(self.workflow.verify(opid)['readback_verified'])

    def test_before_submit_reread_refuses_changed_task(self):
        def mutate(transport):
            if transport.task_reads == 2:
                transport.task_value = task(requirements_version=2, record_revision=2, goal='新版要求')
        self.transport.on_task_read = mutate
        with self.assertRaises(node.WorkflowError) as error:
            self.workflow.submit('task_example', 'completed', '结果', 'complete')
        self.assertEqual(error.exception.code, 'version_conflict')
        self.assertEqual(self.transport.append_count, 0)

    def test_create_response_loss_requires_explicit_reference_and_never_recreates(self):
        self.transport.documents['unrelated'] = {'document_id': 'unrelated', 'title': '工作说明', 'content': '非本任务资料', 'revision': 1, 'content_hash': 'other'}
        self.transport.lose_response = True
        with self.assertRaises(node.WorkflowError) as error:
            self.workflow.create_artifact('task_example', '工作说明', '给成员看的内容', 'first-draft')
        self.assertEqual(error.exception.code, 'write_uncertain')
        self.assertEqual(self.transport.create_count, 1)
        self.assertNotIn('unrelated', self.transport.fetches)
        self.assertEqual(self.transport.fetches, [])
        opid = self.workflow.operation_id(self.transport.task_value, 'first-draft', 'artifact')
        self.workflow.state.db.close()
        self.workflow = node.Workflow(manifest(), self.transport, node.State(self.path))
        with self.assertRaises(node.WorkflowError): self.workflow.verify(opid)
        with self.assertRaises(node.WorkflowError): self.workflow.create_artifact('task_example', '工作说明', '给成员看的内容', 'first-draft')
        result = self.workflow.reconcile_artifact(opid, 'doc_created_1', 1, node.sha('给成员看的内容'))
        repeated = self.workflow.create_artifact('task_example', '工作说明', '给成员看的内容', 'first-draft')
        self.assertEqual(result, repeated)
        self.assertEqual(self.transport.create_count, 1)
        self.assertEqual(self.workflow.prepare('task_example')['plan']['reason'], 'submit_existing_result')

    def legacy_artifact(self, content='原业务正文', actual=None):
        opid = self.workflow.operation_id(self.transport.task_value, 'legacy-draft', 'artifact')
        expected = content + '\n\n<!-- rdos-operation:' + opid + ' -->'
        value = actual if actual is not None else content
        self.transport.documents['legacy_document'] = {'document_id': 'legacy_document', 'title': '原业务成果',
            'content': value, 'content_hash': node.sha(value), 'revision': 3}
        op = {'kind': 'artifact', 'task_id': 'task_example', 'requirements_version': 1, 'state': 'sent',
            'desired': {'folder': 'folder_project', 'title': '原业务成果', 'content': expected}, 'baseline_document_ids': []}
        self.workflow.state.put('operations', opid, op)
        self.workflow.state.put('checkpoints', 'task_example', {'task_id': 'task_example', 'requirements_version': 1,
            'record_revision': 1, 'completed_steps': [], 'uncertain_operations': [opid]})
        self.workflow.state.db.commit()
        return opid

    def test_response_identity_persisted_before_readback_survives_restart(self):
        self.transport.create_body_transform = lambda title, content: '<title>' + title + '</title>\n\n' + content
        def fail_first_fetch(transport, document_id):
            with node.sqlite3.connect(self.path) as other:
                op = json.loads(other.execute('SELECT payload FROM operations').fetchone()[0])
                self.assertEqual(op['response_document_id'], document_id)
                self.assertEqual(op['desired']['content'], '正文\n\n保留空行')
            raise node.WorkflowError('connection_failed', 'readback temporarily unavailable')
        self.transport.on_fetch = fail_first_fetch
        with self.assertRaises(node.WorkflowError):
            self.workflow.create_artifact('task_example', '业务成果', '正文\n\n保留空行', 'draft')
        opid = self.workflow.operation_id(self.transport.task_value, 'draft', 'artifact')
        self.workflow.state.db.close()
        self.workflow = node.Workflow(manifest(), self.transport, node.State(self.path))
        self.transport.on_fetch = None
        result = self.workflow.verify(opid)
        self.assertTrue(result['readback_verified'])
        self.assertNotIn('<!-- rdos-operation:', self.transport.documents['doc_created_1']['content'])
        self.assertEqual(result['artifact_ref']['content_hash'], node.sha('<title>业务成果</title>\n\n正文\n\n保留空行'))
        self.assertEqual(self.transport.create_count, 1)

    def test_real_export_shape_with_removed_comments_uses_create_response_identity(self):
        def render(title, content):
            body = node.re.sub(r'\n\n<!-- rdos-operation:[A-Za-z0-9_-]+ -->$', '', content)
            return '<title>' + title + '</title>\n\n' + body
        self.transport.create_body_transform = render
        result = self.workflow.create_artifact('task_example', '业务成果', '给成员看的正文', 'draft')
        self.assertTrue(result['readback_verified'])
        self.assertEqual(result['artifact_ref']['document_id'], 'doc_created_1')
        self.assertEqual(self.transport.documents['doc_created_1']['content'], '<title>业务成果</title>\n\n给成员看的正文')
        self.assertEqual(self.transport.create_count, 1)

    def test_response_identity_does_not_adopt_other_same_title_documents(self):
        original = self.transport.create
        def create_with_other(folder, title, content):
            response = original(folder, title, content)
            self.transport.documents['same_title_other'] = {'document_id': 'same_title_other', 'title': title,
                'content': content, 'content_hash': node.sha(content), 'revision': 1}
            return response
        self.transport.create = create_with_other
        result = self.workflow.create_artifact('task_example', '业务成果', '具体正文', 'draft')
        self.assertEqual(result['artifact_ref']['document_id'], 'doc_created_1')
        self.assertNotIn('same_title_other', self.transport.fetches)

    def test_direct_create_response_identity_is_saved_and_verified(self):
        original = self.transport.create
        def direct_response(folder, title, content):
            response = original(folder, title, content)
            return {'document_id': response['document']['document_id']}
        self.transport.create = direct_response
        result = self.workflow.create_artifact('task_example', '业务成果', '具体正文', 'draft')
        self.assertTrue(result['readback_verified'])
        self.assertEqual(result['artifact_ref']['document_id'], 'doc_created_1')
        saved = self.workflow.state.get('operations', result['operation_id'])
        self.assertEqual(saved['response_document_id'], 'doc_created_1')
        self.assertEqual(self.transport.create_count, 1)

    def test_conflicting_create_response_identities_remain_uncertain_without_read_or_retry(self):
        original = self.transport.create
        def conflicting_response(folder, title, content):
            response = original(folder, title, content)
            return {**response, 'document_id': 'different_document'}
        self.transport.create = conflicting_response
        with self.assertRaises(node.WorkflowError) as error:
            self.workflow.create_artifact('task_example', '业务成果', '具体正文', 'draft')
        self.assertEqual(error.exception.code, 'invalid_response')
        self.assertEqual(self.transport.fetches, [])
        opid = self.workflow.operation_id(self.transport.task_value, 'draft', 'artifact')
        self.assertNotIn('response_document_id', self.workflow.state.get('operations', opid))
        with self.assertRaises(node.WorkflowError) as error: self.workflow.verify(opid)
        self.assertEqual(error.exception.code, 'write_uncertain')
        with self.assertRaises(node.WorkflowError): self.workflow.create_artifact('task_example', '业务成果', '具体正文', 'draft')
        self.assertEqual(self.transport.create_count, 1)

    def test_response_identity_rejects_baseline_out_of_scope_and_changed_body(self):
        for mode in ('baseline', 'outside', 'changed'):
            with self.subTest(mode=mode):
                transport = Transport(task())
                workflow = node.Workflow(manifest(), transport, node.State(Path(self.temp.name) / (mode + '.sqlite3')))
                try:
                    original = transport.create
                    if mode == 'baseline':
                        transport.documents['doc_before'] = {'document_id': 'doc_before', 'title': '业务成果',
                            'content': '正文', 'content_hash': node.sha('正文'), 'revision': 1}
                        transport.create = lambda *args: {'document': {'document_id': 'doc_before'}}
                    else:
                        def create_changed(folder, title, content):
                            result = original(folder, title, content)
                            document = transport.documents['doc_created_1']
                            if mode == 'outside': document['folder'] = 'folder_other'
                            else:
                                document['content'] = content + ' changed'
                                document['content_hash'] = node.sha(document['content'])
                            return result
                        transport.create = create_changed
                    with self.assertRaises(node.WorkflowError) as error:
                        workflow.create_artifact('task_example', '业务成果', '正文', 'draft')
                    self.assertEqual(error.exception.code, 'out_of_scope' if mode == 'outside' else 'artifact_conflict')
                    if mode == 'baseline': self.assertEqual(transport.fetches, [])
                finally:
                    workflow.state.db.close()

    def test_legacy_stripped_marker_requires_exact_reconciliation_then_allows_submission(self):
        self.transport.task_value = task(task_type='deliverable')
        actual = '<title>原业务成果</title>\n\n原业务正文'
        opid = self.legacy_artifact(actual=actual)
        with self.assertRaises(node.WorkflowError) as error: self.workflow.verify(opid)
        self.assertEqual(error.exception.code, 'write_uncertain')
        with self.assertRaises(node.WorkflowError):
            self.workflow.create_artifact('task_example', '原业务成果', '原业务正文', 'legacy-draft')
        result = self.workflow.reconcile_artifact(opid, 'legacy_document', '3', node.sha(actual))
        self.assertEqual(result['artifact_ref']['revision'], 3)
        self.assertEqual(self.transport.create_count, 0)
        saved = self.workflow.state.get('operations', opid)
        self.assertEqual(saved['reconciliation']['method'], 'explicit_existing_result')
        self.assertEqual(saved['desired']['content'], '原业务正文\n\n<!-- rdos-operation:' + opid + ' -->')
        self.assertEqual(self.workflow.prepare('task_example')['plan']['reason'], 'submit_existing_result')
        self.workflow.submit('task_example', 'completed', '简报已交付', 'complete', [result['artifact_ref']])
        self.assertEqual(self.transport.append_count, 1)

    def test_legacy_marker_surviving_still_verifies_original_operation(self):
        opid = self.legacy_artifact()
        document = self.transport.documents['legacy_document']
        document['content'] = self.workflow.state.get('operations', opid)['desired']['content']
        document['content_hash'] = node.sha(document['content'])
        self.assertTrue(self.workflow.verify(opid)['readback_verified'])
        self.assertEqual(self.transport.create_count, 0)

    def test_verified_reference_remains_bound_to_its_original_version(self):
        created = self.workflow.create_artifact('task_example', '业务成果', '第一段\n\n第二段', 'draft')
        document = self.transport.documents[created['artifact_ref']['document_id']]
        document['revision'] = 2
        with self.assertRaises(node.WorkflowError) as error: self.workflow.verify(created['operation_id'])
        self.assertEqual(error.exception.code, 'version_conflict')
        self.assertEqual(self.workflow.state.get('operations', created['operation_id'])['result'], created)
        document['revision'] = 1
        document['content'] = '第一段\n第二段'
        document['content_hash'] = node.sha(document['content'])
        with self.assertRaises(node.WorkflowError) as error: self.workflow.verify(created['operation_id'])
        self.assertEqual(error.exception.code, 'artifact_conflict')
        self.assertEqual(self.transport.create_count, 1)

    def test_reconciliation_rejects_task_not_assigned_to_current_node(self):
        opid = self.legacy_artifact()
        self.transport.task_value = task(executor_node_ids=['node_workbuddy'])
        with self.assertRaises(node.WorkflowError) as error:
            self.workflow.reconcile_artifact(opid, 'legacy_document', 3, node.sha('原业务正文'))
        self.assertEqual(error.exception.code, 'out_of_scope')
        self.assertEqual(self.transport.fetches, [])

    def test_reconciliation_rejects_body_spacing_revision_hash_and_target_changes(self):
        opid = self.legacy_artifact(content='正文\n\n第二段')
        document = self.transport.documents['legacy_document']
        for field, bad in (('content', '正文\n第二段'), ('revision', 4), ('content_hash', 'a' * 64), ('title', '另一个标题'), ('folder', 'folder_other')):
            with self.subTest(field=field):
                original = copy.deepcopy(document)
                document[field] = bad
                if field == 'content': document['content_hash'] = node.sha(bad)
                with self.assertRaises(node.WorkflowError):
                    self.workflow.reconcile_artifact(opid, 'legacy_document', 3, node.sha('正文\n\n第二段'))
                document.clear(); document.update(original)
                self.assertNotIn('reconciliation', self.workflow.state.get('operations', opid))
        self.workflow.reconcile_artifact(opid, 'legacy_document', 3, document['content_hash'])
        self.transport.documents['other_document'] = {**document, 'document_id': 'other_document'}
        with self.assertRaises(node.WorkflowError) as error:
            self.workflow.reconcile_artifact(opid, 'other_document', 3, document['content_hash'])
        self.assertEqual(error.exception.code, 'operation_conflict')
        self.assertEqual(self.transport.create_count, 0)

    def test_reconciliation_refuses_baseline_and_between_read_changes(self):
        opid = self.legacy_artifact()
        op = self.workflow.state.get('operations', opid)
        op['baseline_document_ids'] = ['legacy_document']
        self.workflow.state.put('operations', opid, op); self.workflow.state.db.commit()
        with self.assertRaises(node.WorkflowError) as error:
            self.workflow.reconcile_artifact(opid, 'legacy_document', 3, node.sha('原业务正文'))
        self.assertEqual(error.exception.code, 'artifact_conflict')
        op['baseline_document_ids'] = []
        self.workflow.state.put('operations', opid, op); self.workflow.state.db.commit()
        def change_second_read(transport, document_id):
            if transport.fetches.count(document_id) == 2:
                transport.documents[document_id]['revision'] = 4
        self.transport.on_fetch = change_second_read
        with self.assertRaises(node.WorkflowError) as error:
            self.workflow.reconcile_artifact(opid, 'legacy_document', 3, node.sha('原业务正文'))
        self.assertEqual(error.exception.code, 'version_conflict')
        self.assertNotIn('reconciliation', self.workflow.state.get('operations', opid))

    def test_review_and_deliverable_use_distinct_business_conditions(self):
        self.transport.task_value = task(task_type='deliverable')
        with self.assertRaises(rdos_rules.RuleViolation): self.workflow.submit('task_example', 'completed', '没有成果', 'complete')
        ref = {'document_id': 'doc_input', 'revision': 3, 'content_hash': node.sha('指定稿')}
        self.transport.documents['doc_input'] = {**ref, 'title': '指定稿', 'content': '指定稿'}
        self.transport.task_value = task(task_type='review', input_refs=[ref])
        result = self.workflow.submit('task_example', 'completed', '已审阅指定版本，建议补充对象说明', 'review', review_of=ref)
        self.assertTrue(result['readback_verified'])
        event = json.loads(self.transport.events[0]['fields']['记录数据'])
        self.assertEqual(event['event_type'], 'review')
        self.assertEqual(event['artifact_refs'], [])
        self.assertEqual(event['review_of'], ref)

    def test_material_link_does_not_grant_additional_read_scope(self):
        with self.assertRaises(node.WorkflowError) as error:
            self.workflow.document({'document_id': 'doc_unregistered', 'revision': 1, 'content_hash': 'x'}, task())
        self.assertEqual(error.exception.code, 'out_of_scope')
        self.assertEqual(self.transport.fetches, [])

    def test_incomplete_page_never_reports_no_tasks(self):
        transport = node.Feishu(manifest(), binary=sys.executable)
        transport.run = lambda args: {'fields': [], 'record_id_list': [], 'data': [], 'has_more': True}
        with self.assertRaises(node.WorkflowError) as error: transport.records('tbl_tasks')
        self.assertEqual(error.exception.code, 'incomplete_read')

    def test_package_contains_identical_rules_and_only_public_manifest(self):
        content, metadata = package_bytes(manifest())
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            bundled = archive.read('rdos-workflow/scripts/rdos_rules.py')
            self.assertEqual(bundled, (SOURCE.parents[3] / 'server' / 'rdos_rules.py').read_bytes())
            self.assertEqual(json.loads(archive.read('rdos-workflow/manifest.json')), manifest())
        self.assertEqual(metadata['sha256'], __import__('hashlib').sha256(content).hexdigest())
        with self.assertRaises(ValueError): package_bytes({**manifest(), 'env_id': 'private'})
        with self.assertRaises(ValueError): package_bytes({**manifest(), 'nested': {'secret': 'private'}})
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            archive.extractall(self.temp.name)
        command = subprocess.run([sys.executable, '-B', str(Path(self.temp.name) / 'rdos-workflow/scripts/rdos_node.py'), '--help'], text=True, capture_output=True)
        self.assertEqual(command.returncode, 0, command.stderr)

    def test_remote_completed_record_with_changed_artifact_is_not_completion(self):
        self.transport.task_value = task(task_type='deliverable')
        created = self.workflow.create_artifact('task_example', '业务成果', '内容', 'draft')
        self.workflow.submit('task_example', 'completed', '提交成果', 'complete', [created['artifact_ref']])
        document = self.transport.documents[created['artifact_ref']['document_id']]
        document['revision'] = 2
        document['content_hash'] = node.sha('已变更')
        document['content'] = '已变更'
        self.workflow.state.db.execute('DELETE FROM checkpoints')
        self.workflow.state.db.commit()
        current = self.workflow.prepare('task_example')
        self.assertEqual(current['plan']['reason'], 'reconcile_before_retry')
        self.assertNotIn('submission_verified', current['plan']['reusable_steps'])
        self.assertEqual(self.transport.create_count, 1)

    def test_unconfirmed_old_operation_blocks_new_version_until_reconciled(self):
        self.transport.hide_events = self.transport.lose_response = True
        with self.assertRaises(node.WorkflowError): self.workflow.submit('task_example', 'completed', '旧要求结果', 'complete')
        opid = self.workflow.operation_id(self.transport.task_value, 'complete', 'event')
        self.transport.task_value = task(requirements_version=2, record_revision=2, goal='新要求')
        current = self.workflow.prepare('task_example')
        self.assertEqual(current['plan']['reason'], 'reconcile_before_retry')
        self.assertNotIn('perform_work', current['plan']['allowed_actions'])
        self.transport.hide_events = False
        self.workflow.verify(opid)
        current = self.workflow.prepare('task_example')
        self.assertIn('perform_work', current['plan']['allowed_actions'])
        self.assertNotIn('submission_verified', current['plan']['reusable_steps'])

    def test_closed_task_accepts_correction_and_archives_late_result(self):
        first = self.workflow.submit('task_example', 'completed', '初次结果', 'complete')
        self.transport.task_value = task(business_status='closed', record_revision=2)
        corrected = self.workflow.submit('task_example', 'partial', '更正原结果中的配置名称', 'correction-1',
            event_type='correction', correction_of=first['event_id'])
        archived = self.workflow.submit('task_example', 'partial', '补充已存在成果的归档线索', 'late-result', event_type='progress')
        self.assertTrue(corrected['readback_verified'])
        self.assertTrue(archived['readback_verified'])
        event = json.loads(self.transport.events[-1]['fields']['记录数据'])
        self.assertEqual(event['origin'], 'late_result')
        self.assertNotIn('result_status', event)

    def test_review_blocked_by_unreadable_material_can_report_blocker(self):
        ref = {'document_id': 'unavailable_input', 'revision': 3, 'content_hash': 'unavailable'}
        self.transport.task_value = task(task_type='review', input_refs=[ref])
        result = self.workflow.submit('task_example', 'blocked', '指定资料无访问权限', 'blocked')
        self.assertTrue(result['readback_verified'])
        self.assertEqual(self.transport.fetches, [])

    def test_single_review_input_is_explicitly_attached_when_omitted(self):
        ref = {'document_id': 'doc_input', 'revision': 3, 'content_hash': node.sha('指定稿')}
        self.transport.documents['doc_input'] = {**ref, 'title': '指定稿', 'content': '指定稿'}
        self.transport.task_value = task(task_type='review', input_refs=[ref])
        self.workflow.submit('task_example', 'completed', '审阅意见', 'review')
        event = json.loads(self.transport.events[-1]['fields']['记录数据'])
        self.assertEqual(event['review_of'], ref)

    def test_necessary_revision_requires_known_version_and_reason(self):
        first = self.workflow.create_artifact('task_example', '工作说明', '首稿', 'draft')
        with self.assertRaises(node.WorkflowError): self.workflow.create_artifact('task_example', '工作说明', '修订稿', 'revision')
        self.assertEqual(self.transport.create_count, 1)
        revised = self.workflow.create_artifact('task_example', '工作说明', '修订稿', 'revision', first['artifact_ref'], '按当前要求补齐缺少的对象说明')
        self.assertEqual(self.transport.create_count, 2)
        self.assertNotEqual(first['artifact_ref'], revised['artifact_ref'])
        self.assertIn(first['artifact_ref']['document_id'], self.transport.documents)

    def test_artifact_exists_does_not_block_meaningful_progress_reporting(self):
        self.workflow.create_artifact('task_example', '工作说明', '首稿', 'draft')
        result = self.workflow.submit('task_example', 'partial', '首稿已保存，正在核对要求', 'progress-after-draft', event_type='progress')
        self.assertTrue(result['readback_verified'])
        self.assertEqual(self.transport.create_count, 1)

    def test_multiple_review_inputs_accept_explicit_target_before_rules_validation(self):
        first = {'document_id': 'doc_first', 'revision': 3, 'content_hash': node.sha('指定稿')}
        second = {'document_id': 'doc_context', 'revision': 2, 'content_hash': node.sha('背景资料')}
        self.transport.documents['doc_first'] = {**first, 'title': '指定稿', 'content': '指定稿'}
        self.transport.documents['doc_context'] = {**second, 'title': '背景资料', 'content': '背景资料'}
        self.transport.task_value = task(task_type='review', input_refs=[first, second])
        with self.assertRaises(node.WorkflowError): self.workflow.submit('task_example', 'completed', '意见', 'review')
        result = self.workflow.submit('task_example', 'completed', '指定稿审阅意见', 'review', review_of=first)
        self.assertTrue(result['readback_verified'])
        event = json.loads(self.transport.events[-1]['fields']['记录数据'])
        self.assertEqual(event['review_of'], first)

    def test_cli_reuses_native_from_same_resolved_npm_package(self):
        package = Path(self.temp.name) / 'existing_package'
        (package / 'scripts').mkdir(parents=True)
        (package / 'bin').mkdir()
        wrapper = package / 'scripts/run.js'
        wrapper.write_text('#!/usr/bin/env node\n')
        native = package / 'bin/lark-cli'
        native.write_text('existing native sentinel')
        native.chmod(0o755)
        alias = Path(self.temp.name) / 'lark-cli'
        alias.symlink_to(wrapper)
        with patch.object(node.shutil, 'which', return_value=str(alias)):
            self.assertEqual(node.Feishu(manifest()).binary, str(native.resolve()))
        explicit = Path(self.temp.name) / 'other-installed-cli'
        explicit.write_text('existing explicit sentinel')
        self.assertEqual(node.Feishu(manifest(), binary=str(explicit)).binary, str(explicit.resolve()))
        native.unlink()
        self.assertEqual(node.Feishu(manifest(), binary=str(alias)).binary, str(wrapper.resolve()))

    def test_cli_reports_missing_without_installing_or_searching_other_roots(self):
        with patch.object(node.shutil, 'which', return_value=None):
            with self.assertRaises(node.WorkflowError) as missing: node.Feishu(manifest())
        self.assertEqual(missing.exception.code, 'connection_missing')
        with self.assertRaises(node.WorkflowError): node.Feishu(manifest(), binary=str(Path(self.temp.name) / 'not-installed'))

    def multi_workflow(self, registration=None, tasks=None):
        self.workflow.state.db.close()
        self.transport=MultiProjectTransport(tasks or [task(),task(task_id='task_second',id='task_second',project_id='project_second')])
        self.workflow=node.Workflow(registration or multi_manifest(),self.transport,node.State(self.path))
        return self.workflow

    def test_multi_project_routes_artifacts_and_receipts_to_task_project(self):
        workflow=self.multi_workflow()
        checked=workflow.check()
        self.assertEqual({item['task']['project_id'] for item in checked['tasks']},{'project_rdos','project_second'})
        created=workflow.create_artifact('task_second','项目汇报','第二项目的业务正文','draft')
        document=self.transport.documents[created['artifact_ref']['document_id']]
        self.assertEqual(document['folder'],'folder_second')
        submitted=workflow.submit('task_second','completed','汇报已提交','complete',[created['artifact_ref']])
        self.assertTrue(submitted['readback_verified'])
        written=json.loads(self.transport.events[0]['fields']['记录数据'])
        self.assertEqual(written['project_id'],'project_second')
        self.assertEqual(written['task_refs'],[{'task_id':'task_second','requirements_version':1}])
        self.assertEqual(workflow.state.get('checkpoints','task_second')['project_id'],'project_second')
        self.assertEqual(workflow.prepare('task_example')['plan']['reason'],'ready')
        self.assertEqual(workflow.submit('task_second','completed','汇报已提交','complete',[created['artifact_ref']]),submitted)
        self.assertEqual(self.transport.append_count,1)

    def test_other_project_rows_skip_but_malformed_other_project_rows_quarantine(self):
        unrelated=task(task_id='task_unrelated',id='task_unrelated',project_id='project_unrelated')
        workflow=self.multi_workflow(tasks=[task(),unrelated])
        unrelated_event={'protocol_version':2,'workspace_id':'ws_rdos','project_id':'project_unrelated',
            'id':'evt_unrelated','operation_id':'unrelated-op','claimed_node_id':'node_codex','event_type':'project_feedback',
            'task_refs':[],'body':'其他项目的反馈'}
        self.transport.events=[row(unrelated_event,'unrelated-event','event')]
        checked=workflow.check()
        self.assertEqual(len(checked['tasks']),1)
        self.assertEqual(checked['quarantined_records'],[])
        malformed=row(unrelated,'bad-readable-columns')
        malformed['fields']['标题']='与正文冲突'
        self.transport.extra_rows=[malformed,{'record_id':'not-object','fields':{'记录数据':'[]'}}]
        checked=workflow.check()
        self.assertEqual(checked['state'],'attention_required')
        self.assertEqual({item['record_id'] for item in checked['quarantined_records']},{'bad-readable-columns','not-object'})
        with self.assertRaises(node.WorkflowError) as error:workflow.prepare('task_unrelated')
        self.assertEqual(error.exception.code,'task_unavailable')

    def test_project_membership_and_task_assignment_are_both_required(self):
        registration=multi_manifest()
        registration['projects'][1]['node_ids']=['node_workbuddy']
        workflow=self.multi_workflow(registration)
        self.assertEqual([item['task']['task_id'] for item in workflow.check()['tasks']],['task_example'])
        with self.assertRaises(node.WorkflowError):workflow.create_artifact('task_second','成果','正文','draft')
        registration['projects'][1]['node_ids']=['node_codex']
        self.transport.tasks[1]=task(task_id='task_second',id='task_second',project_id='project_second',executor_node_ids=['node_workbuddy'])
        with self.assertRaises(node.WorkflowError) as error:workflow.submit('task_second','completed','完成','complete')
        self.assertEqual(error.exception.code,'not_assigned')
        self.assertEqual(self.transport.create_count+self.transport.append_count,0)

    def test_unassigned_project_receipts_do_not_report_missing_tasks(self):
        registration=manifest()
        registration['projects']=[]
        workflow=node.Workflow(registration,self.transport,self.workflow.state)
        event={'protocol_version':2,'workspace_id':'ws_rdos','project_id':'project_rdos',
            'id':'evt_other_task','operation_id':'other-task-op','claimed_node_id':'node_workbuddy',
            'event_type':'submission','task_refs':[{'task_id':'task_example','requirements_version':1}],
            'body':'其他节点的有效结果'}
        self.transport.events=[row(event,'other-receipt','event')]
        checked=workflow.check()
        self.assertEqual(checked['tasks'],[])
        self.assertEqual(checked['quarantined_records'],[])
        self.assertEqual(checked['state'],'no_work')
        self.assertEqual(self.transport.append_count+self.transport.create_count,0)
        with self.assertRaises(node.WorkflowError):workflow.prepare('task_example')
        malformed=row(event,'bad-receipt','event')
        malformed['fields']['自报节点']='冒名节点'
        self.transport.events.append(malformed)
        self.assertEqual(workflow.check()['quarantined_records'],[{'record_id':'bad-receipt','code':'record_conflict'}])

    def test_same_folder_does_not_merge_projects_or_reuse_other_task_completion(self):
        workflow=self.multi_workflow(multi_manifest(same_folder=True))
        workflow.submit('task_example','completed','第一项目完成','complete')
        second=workflow.prepare('task_second')
        self.assertEqual(second['plan']['reason'],'ready')
        self.assertIn('perform_work',second['plan']['allowed_actions'])
        artifact=workflow.create_artifact('task_second','第二项目成果','正文','draft')
        self.assertEqual(self.transport.documents[artifact['artifact_ref']['document_id']]['folder'],'folder_project')
        self.assertEqual(json.loads(self.transport.events[0]['fields']['记录数据'])['project_id'],'project_rdos')

    def test_registered_materials_are_scoped_to_the_task_project(self):
        workflow=self.multi_workflow()
        default_ref={'document_id':'doc_default','revision':1,'content_hash':node.sha('默认资料')}
        second_ref={'document_id':'doc_second','revision':1,'content_hash':node.sha('项目资料')}
        self.transport.documents['doc_default']={**default_ref,'title':'默认资料','content':'默认资料'}
        self.transport.documents['doc_second']={**second_ref,'title':'项目资料','content':'项目资料'}
        second=self.transport.tasks[1]
        with self.assertRaises(node.WorkflowError) as error:workflow.document(default_ref,second)
        self.assertEqual(error.exception.code,'out_of_scope')
        self.assertEqual(self.transport.fetches,[])
        self.assertEqual(workflow.document(second_ref,second)['content'],'项目资料')

    def test_inactive_project_blocks_new_execution_but_preserves_stop_receipts(self):
        workflow=self.multi_workflow(multi_manifest(second_status='deactivating'))
        plan=workflow.prepare('task_second')['plan']
        self.assertEqual(plan['reason'],'project_deactivating')
        self.assertNotIn('perform_work',plan['allowed_actions'])
        self.assertNotIn('report_paused_ack',plan['allowed_actions'])
        for action in (lambda:workflow.create_artifact('task_second','成果','正文','draft'),
                       lambda:workflow.submit('task_second','completed','完成','complete')):
            with self.assertRaises(node.WorkflowError):action()
        blocker=workflow.submit('task_second','blocked','项目正在停用，已停止新增工作','stop',event_type='blocker')
        self.assertTrue(blocker['readback_verified'])
        self.assertEqual(self.transport.create_count,0)
        self.transport.tasks[1]=task(task_id='task_second',id='task_second',project_id='project_second',business_status='paused',record_revision=2)
        workflow.manifest['projects'][1]['status']='inactive'
        ack=workflow.submit('task_second','partial','已暂停后续工作','pause-ack',event_type='paused_ack')
        self.assertTrue(ack['readback_verified'])
        self.transport.tasks[1]=task(task_id='task_second',id='task_second',project_id='project_second',business_status='closed',record_revision=3)
        self.assertTrue(workflow.submit('task_second','partial','已停止','closed-ack',event_type='closed_ack')['readback_verified'])

    def test_legacy_manifest_keeps_operation_identity_and_default_project_scope(self):
        expected='rdos_'+node.sha(['ws_rdos','node_codex','task_example',1,'event','complete'])[:32]
        self.assertEqual(self.workflow.operation_id(task(),'complete','event'),expected)
        other=task(task_id='task_other',id='task_other',project_id='project_other')
        workflow=self.multi_workflow(manifest(),[task(),other])
        self.assertEqual(len(workflow.check()['tasks']),1)
        self.assertEqual(workflow.check()['quarantined_records'],[])
        self.assertTrue(workflow.submit('task_example','completed','原任务完成','complete')['readback_verified'])

    def test_transport_only_allows_registered_member_project_folders(self):
        registration=multi_manifest()
        registration['projects'].append({'id':'project_else','status':'active','node_ids':['node_workbuddy'],
            'zones':{'project':{'folder_id':'folder_else'}},'registered_resources':[]})
        binary=Path(self.temp.name)/'existing-cli'
        binary.write_text('existing-cli')
        transport=node.Feishu(registration,binary=str(binary))
        calls=[]
        transport.run=lambda args,content=None:calls.append(args) or {'files':[],'has_more':False}
        self.assertEqual(transport.root('folder_second'),[])
        self.assertEqual(len(calls),1)
        for folder in ('folder_else','folder_unknown'):
            with self.assertRaises(node.WorkflowError):transport.root(folder)
            with self.assertRaises(node.WorkflowError):transport.create(folder,'成果','正文')
        self.assertEqual(len(calls),1)

    def test_uncertain_second_project_artifact_reconciles_after_project_stops(self):
        workflow=self.multi_workflow()
        self.transport.lose_response=True
        with self.assertRaises(node.WorkflowError):workflow.create_artifact('task_second','成果','业务正文','draft')
        opid=workflow.operation_id(self.transport.tasks[1],'draft','artifact')
        workflow.manifest['projects'][1]['status']='inactive'
        self.transport.lose_response=False
        result=workflow.reconcile_artifact(opid,'doc_created_1',1,node.sha('业务正文'))
        self.assertTrue(result['readback_verified'])
        self.assertEqual(workflow.verify(opid),result)
        self.assertEqual(self.transport.create_count,1)
        with self.assertRaises(node.WorkflowError):workflow.submit('task_second','completed','已交付','complete',[result['artifact_ref']])

    def test_manifest_rejects_ambiguous_registration_and_duplicate_task_ids(self):
        invalid=multi_manifest()
        invalid['projects'].append(copy.deepcopy(invalid['projects'][0]))
        with self.assertRaises(node.WorkflowError):node.manifest_value(invalid)
        invalid=multi_manifest();invalid['projects'][1]['registered_resources']=['bad-resource']
        with self.assertRaises(node.WorkflowError):node.manifest_value(invalid)
        workflow=self.multi_workflow(tasks=[task(),task(project_id='project_second')])
        with self.assertRaises(node.WorkflowError) as error:workflow.check()
        self.assertEqual(error.exception.code,'task_conflict')


if __name__ == '__main__':
    unittest.main()
