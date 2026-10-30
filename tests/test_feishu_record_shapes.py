"""Record response integrity uses the actual parser, with no external service."""
import copy
import unittest

from server.phase3_feishu import _CLIExecutor, FeishuTestError


class FeishuRecordShapeTest(unittest.TestCase):
    @staticmethod
    def executor(responses):
        executor = object.__new__(_CLIExecutor)
        executor.registry = {'tasks_table': 'tblSyntheticTasks', 'events_table': 'tblSyntheticEvents',
                             'base_token': 'basSyntheticRecords'}
        values = iter(responses)
        executor._run = lambda args: copy.deepcopy(next(values))
        return executor

    def test_actual_cli_matrix_shape_preserves_all_fields(self):
        data = {'fields': ['事件ID', '操作ID', '记录数据'], 'record_id_list': ['recSyntheticOne'],
                'data': [['evt_one', 'operation_one', '{"body":"完整正文"}']],
                'has_more': False, 'rev': 60, 'timezone': 'Asia/Shanghai'}
        self.assertEqual(_CLIExecutor._rows(data), [{'record_id': 'recSyntheticOne', 'fields': {
            '事件ID': 'evt_one', '操作ID': 'operation_one', '记录数据': '{"body":"完整正文"}'}}])
        self.assertEqual(_CLIExecutor._rows({'fields': [{'name': '名称'}, {'field_name': '正文'}, {'id': 'fld-id'}],
            'record_id_list': ['recSynthetic'], 'data': [['甲', ['富文本'], None]]}),
            [{'record_id': 'recSynthetic', 'fields': {'名称': '甲', '正文': ['富文本'], 'fld-id': None}}])

    def test_explicit_empty_tables_are_accepted(self):
        for data in ({'items': []}, {'records': []}, {'fields': [], 'record_id_list': [], 'data': []},
                     {'fields': ['事件ID'], 'record_id_list': [], 'data': [], 'has_more': False}):
            with self.subTest(data=data):
                self.assertEqual(_CLIExecutor._rows(data), [])

    def test_unknown_and_incomplete_success_objects_are_rejected(self):
        for data in (None, [], {}, {'has_more': False}, {'ok': True}, {'data': []}, {'fields': []},
                     {'fields': [], 'record_id_list': []}, {'record_id_list': [], 'data': []},
                     {'fields': [], 'data': []}):
            with self.subTest(data=data), self.assertRaises(FeishuTestError) as error:
                _CLIExecutor._rows(data)
            self.assertEqual(error.exception.code, 'invalid_response')

    def test_non_object_rows_and_non_list_collections_are_rejected(self):
        for data in ({'items': None}, {'records': {}}, {'items': [None]}, {'records': ['record']},
                     {'items': [[{'record_id': 'rec-one'}]]}):
            with self.subTest(data=data), self.assertRaises(FeishuTestError):
                _CLIExecutor._rows(data)
        for key in ('items', 'records'):
            row = {'record_id': 'recSynthetic', 'fields': {'事件ID': 'evt-one'}}
            self.assertEqual(_CLIExecutor._rows({key: [row]}), [row])

    def test_matrix_counts_and_types_are_checked_before_zipping(self):
        malformed = [
            {'fields': ['名称'], 'record_id_list': ['rec-one'], 'data': []},
            {'fields': ['名称'], 'record_id_list': [], 'data': [['甲']]},
            {'fields': ['名称', '正文'], 'record_id_list': ['rec-one'], 'data': [['甲']]},
            {'fields': ['名称'], 'record_id_list': ['rec-one'], 'data': [['甲', '多余正文']]},
            {'fields': ['名称'], 'record_id_list': ['rec-one'], 'data': ['甲']},
            {'fields': {}, 'record_id_list': [], 'data': []},
            {'fields': [], 'record_id_list': None, 'data': []},
            {'fields': [], 'record_id_list': [], 'data': None},
            {'fields': [None], 'record_id_list': ['rec-one'], 'data': [['甲']]},
            {'fields': [{}], 'record_id_list': ['rec-one'], 'data': [['甲']]},
            {'fields': ['名称', '名称'], 'record_id_list': ['rec-one'], 'data': [['甲', '乙']]},
            {'fields': ['名称'], 'record_id_list': [None], 'data': [['甲']]},
        ]
        for data in malformed:
            with self.subTest(data=data), self.assertRaises(FeishuTestError):
                _CLIExecutor._rows(data)

    def test_record_get_keeps_supported_wrapped_direct_and_matrix_forms(self):
        row = {'record_id': 'recSynthetic', 'fields': {'事件ID': 'evt-one'}}
        for data in ({'record': row}, row, {'items': [row]}, {'records': [row]},
                     {'fields': ['事件ID'], 'record_id_list': ['recSynthetic'], 'data': [['evt-one']]}):
            with self.subTest(data=data):
                executor = self.executor([{'data': data}])
                self.assertEqual(executor.get_record('tblSyntheticEvents', 'recSynthetic'), row)

    def test_incomplete_later_page_cannot_be_returned_as_a_complete_read(self):
        row = {'record_id': 'recSynthetic', 'fields': {'事件ID': 'evt-one'}}
        executor = self.executor([{'data': {'items': [row], 'has_more': True}}, {'data': {}}])
        with self.assertRaises(FeishuTestError) as error:
            executor.records('tblSyntheticEvents')
        self.assertEqual(error.exception.code, 'invalid_response')

    def test_record_get_rejects_wrong_identity_or_malformed_fields(self):
        for row, code in ((None, 'invalid_response'),
                          ({'record_id': 'recSynthetic', 'fields': []}, 'invalid_response'),
                          ({'fields': {}}, 'readback_mismatch'),
                          ({'record_id': 'recSomeoneElse', 'fields': {}}, 'readback_mismatch')):
            for data in ({'record': row}, {'items': [row]} if isinstance(row, dict) else {'record': row}):
                with self.subTest(data=data), self.assertRaises(FeishuTestError) as error:
                    self.executor([{'data': data}]).get_record('tblSyntheticEvents', 'recSynthetic')
                self.assertEqual(error.exception.code, code)
            if isinstance(row, dict) and 'record_id' in row:
                with self.subTest(direct=row), self.assertRaises(FeishuTestError) as error:
                    self.executor([{'data': row}]).get_record('tblSyntheticEvents', 'recSynthetic')
                self.assertEqual(error.exception.code, code)
