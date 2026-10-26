"""Schema preparation may be repeated only after independent readback."""
from __future__ import annotations

import unittest
import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cutover import migrate_cli
from cutover.formal_migration import MigrationError
from cutover.formal_migration import FileJournal


class Journal(dict):
    def mark(self, key, value):
        self[key] = value


class SchemaExecutor:
    def __init__(self, *, lose_table_response=False, lose_field_response=False):
        self.current_tables = [{'name': '任务说明', 'table_id': 'tblOriginal'}]
        self.current_fields = {'tblOriginal': [{'name': '任务ID', 'type': 'text'}]}
        self.tables_created = 0
        self.fields_created = 0
        self.lose_table_response = lose_table_response
        self.lose_field_response = lose_field_response

    def tables(self):
        return list(self.current_tables)

    def create_table(self, name):
        self.tables_created += 1
        self.current_tables.append({'name': name, 'table_id': 'tblNewEvents'})
        self.current_fields['tblNewEvents'] = []
        if self.lose_table_response:
            raise TimeoutError('response lost after commit')

    def fields(self, table):
        return {'items': list(self.current_fields[table])}

    def create_field(self, table, name):
        self.fields_created += 1
        self.current_fields[table].append({'name': name, 'type': 'text'})
        if self.lose_field_response:
            raise TimeoutError('response lost after commit')


class SchemaMigrationTests(unittest.TestCase):
    def test_proven_local_missing_schema_rejection_can_resume_without_erasing_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = FileJournal(Path(folder) / 'journal.json')
            journal.mark('schema:event_table', 'sent')
            executor = SimpleNamespace(binary='/fixed/cli', binary_hash='a'*64,
                                       runtime=Path(folder), child_env={'HOME':folder})
            response = {'ok':False, 'error':{'type':'validation','subtype':'invalid_argument',
                        'message':'required flag(s) "fields" not set'}}
            # The pinned CLI emits structured validation failures on stderr.
            with patch.object(migrate_cli.subprocess, 'run', return_value=subprocess.CompletedProcess([],1,'',json.dumps(response))) as call:
                migrate_cli.recover_local_table_rejection(executor, [], journal)
            self.assertEqual(call.call_args.args[0][-1], '--dry-run')
            self.assertEqual(journal.get('schema:event_table'), 'not_sent')
            proof = json.loads((Path(folder)/'formal_schema_local_rejection.json').read_text())
            self.assertEqual(proof['previous_journal']['schema:event_table'], 'sent')
            transport = SchemaExecutor()
            migrate_cli.ensure_table(transport, transport.tables(), journal)
            self.assertEqual(transport.tables_created,1)
            self.assertEqual(journal.get('schema:event_table'),'verified')

    def test_unknown_remote_error_cannot_clear_uncertain_schema_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = FileJournal(Path(folder)/'journal.json')
            journal.mark('schema:event_table','sent')
            executor = SimpleNamespace(binary='/fixed/cli',binary_hash='a'*64,
                                       runtime=Path(folder),child_env={'HOME':folder})
            with patch.object(migrate_cli.subprocess, 'run', return_value=subprocess.CompletedProcess([],1,'{"ok":false,"error":{"type":"network"}}','')):
                with self.assertRaises(MigrationError):
                    migrate_cli.recover_local_table_rejection(executor,[],journal)
            self.assertEqual(journal.get('schema:event_table'),'sent')
            self.assertFalse((Path(folder)/'formal_schema_local_rejection.json').exists())

    def test_lost_response_is_resolved_by_fresh_table_and_field_lists(self):
        executor = SchemaExecutor(lose_table_response=True, lose_field_response=True)
        journal = Journal()
        event_table = migrate_cli.ensure_table(executor, executor.tables(), journal)
        self.assertEqual(event_table, 'tblNewEvents')
        self.assertEqual(journal['schema:event_table'], 'verified')
        migrate_cli.ensure_fields(executor, event_table, ('事件ID', '记录数据'), journal)
        self.assertEqual(executor.fields_created, 2)
        self.assertEqual(migrate_cli.ensure_table(executor, executor.tables(), journal), event_table)
        migrate_cli.ensure_fields(executor, event_table, ('事件ID', '记录数据'), journal)
        self.assertEqual(executor.tables_created, 1)
        self.assertEqual(executor.fields_created, 2)

    def test_sent_marker_without_visible_object_refuses_second_create(self):
        executor = SchemaExecutor()
        journal = Journal({'schema:event_table': 'sent'})
        with self.assertRaises(MigrationError):
            migrate_cli.ensure_table(executor, executor.tables(), journal)
        self.assertEqual(executor.tables_created, 0)
        executor.current_tables.append({'name': '协作记录', 'table_id': 'tblNewEvents'})
        executor.current_fields['tblNewEvents'] = []
        journal['schema:tblNewEvents:事件ID'] = 'sent'
        with self.assertRaises(MigrationError):
            migrate_cli.ensure_fields(executor, 'tblNewEvents', ('事件ID',), journal)
        self.assertEqual(executor.fields_created, 0)


if __name__ == '__main__':
    unittest.main()
