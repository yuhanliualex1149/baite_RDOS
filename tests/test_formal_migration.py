from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cutover import formal_migration as migration
from cutover import legacy_task_conversion as legacy
from cutover import production_config as formal


class Journal(dict):
    def mark(self, key, value):
        self[key] = value


class Transport:
    def __init__(self, rows, *, lose=False):
        self.rows = {row['record_id']: copy.deepcopy(row) for row in rows}
        self.writes = 0
        self.lose = lose

    def get_record(self, record_id):
        return copy.deepcopy(self.rows[record_id])

    def update_record(self, record_id, fields):
        self.writes += 1
        self.rows[record_id]['fields'].update(fields)
        if self.lose:
            self.lose = False
            raise TimeoutError('response lost after commit')


class FormalMigrationTests(unittest.TestCase):
    def setUp(self):
        self.rows = []
        self.decisions = {'status': 'approved_for_migration', 'rows': {}}
        for number in range(18):
            task_id = 'RDOS-OLD-%02d' % number
            disabled = number < 9
            source = {'任务ID': task_id, '项目ID': None if number == 0 else 'rdos-self',
                      '发布状态': ['停用' if disabled else '已发布'],
                      '任务性质': ['一次性'], '阶段': None,
                      '写回标识': 'marker-%02d' % number,
                      '适用成员': None if number == 0 else 'mem_x_rLn9VBAe_z8xol',
                      '版本': '1', '任务类别': 'work', '关联任务': None,
                      '任务说明': '# 原始任务 %s\n原文不改。' % number}
            record_id = 'recMigration%02d' % number
            self.rows.append({'record_id': record_id, 'fields': source})
            self.decisions['rows'][task_id] = {
                'record_id': record_id, 'source_fields_sha256': legacy.digest(source),
                'decision': 'disabled' if disabled else 'pending_evidence'}
        self.resources = {'node_members': {'node_xiaofei': 'mem_x_rLn9VBAe_z8xol'},
                          'event_table': 'tblEventsFixture'}

    def test_exact_plan_preserves_original_rows_and_pauses_unproved_work(self):
        plan = migration.plan(self.rows, self.decisions, self.resources)
        self.assertEqual(plan['source_count'], 18)
        self.assertEqual(plan['records'][0]['task']['business_status'], 'closed')
        self.assertEqual(plan['records'][9]['task']['business_status'], 'paused')
        self.assertEqual(plan['records'][9]['task']['migration_source']['legacy_project_id'], 'rdos-self')
        self.assertEqual(plan['records'][9]['wire']['protocol_version'], 2)
        self.assertEqual(plan['records'][9]['wire']['task_id'], self.rows[9]['fields']['任务ID'])

    def test_missing_evidence_and_changed_row_stop_before_write(self):
        self.decisions['rows']['RDOS-OLD-09']['decision'] = 'completed'
        with self.assertRaises(migration.MigrationError):
            migration.plan(self.rows, self.decisions, self.resources)
        self.decisions['rows']['RDOS-OLD-09']['decision'] = 'pending_evidence'
        self.rows[9]['fields']['任务说明'] = 'edited'
        with self.assertRaises(migration.MigrationError):
            migration.plan(self.rows, self.decisions, self.resources)

    def test_response_lost_after_commit_reads_back_without_duplicate(self):
        plan = migration.plan(self.rows, self.decisions, self.resources)
        transport = Transport(self.rows, lose=True)
        journal = Journal()
        receipts = migration.apply_remote(plan, transport, journal)
        self.assertEqual(len(receipts), 18)
        self.assertEqual(transport.writes, 18)
        migration.apply_remote(plan, transport, journal)
        self.assertEqual(transport.writes, 18)
        self.assertEqual(transport.get_record('recMigration00')['fields']['项目ID'], None)

    def test_empty_text_read_as_null_is_verified_without_a_second_write(self):
        class FeishuTextTransport(Transport):
            def update_record(self, record_id, fields):
                super().update_record(record_id, {k: None if v == '' else v for k,v in fields.items()})
        plan = migration.plan(self.rows, self.decisions, self.resources)
        transport = FeishuTextTransport(self.rows)
        journal = Journal()
        self.assertEqual(len(migration.apply_remote(plan, transport, journal)),18)
        self.assertIsNone(transport.rows['recMigration00']['fields']['执行节点'])
        migration.apply_remote(plan, transport, journal)
        self.assertEqual(transport.writes,18)
        self.assertFalse(migration._same_columns({'状态':None},{'状态':'active'}))
        self.assertFalse(migration._same_columns({'记录数据':None},{'记录数据':'{}'}))

    def test_uncertain_write_without_visible_result_never_resends(self):
        plan = migration.plan(self.rows, self.decisions, self.resources)
        transport = Transport(self.rows)
        journal = Journal({'recMigration00': 'sent'})
        with self.assertRaises(migration.MigrationError):
            migration.apply_remote(plan, transport, journal)
        self.assertEqual(transport.writes, 0)

    def test_local_authority_installed_only_after_all_receipts(self):
        plan = migration.plan(self.rows, self.decisions, self.resources)
        transport = Transport(self.rows)
        receipts = migration.apply_remote(plan, transport, Journal())
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'original.db'
            with sqlite3.connect(database) as conn:
                conn.execute('CREATE TABLE central_users(id TEXT PRIMARY KEY)')
                conn.execute('CREATE TABLE central_records(kind TEXT,id TEXT,payload TEXT,updated_at TEXT,PRIMARY KEY(kind,id))')
                conn.executemany('INSERT INTO central_records VALUES(?,?,?,?)', [
                    ('central_memory','old-memory','{"content":"old agreement"}','before'),
                    ('message','retained-chat','{"content":"original dialogue"}','before')])
            with self.assertRaises(migration.MigrationError):
                migration.install_authority(database, plan, receipts[:-1])
            marker = migration.install_authority(database, plan, receipts)
            self.assertEqual(marker['source_count'], 18)
            with sqlite3.connect(database) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM central_records WHERE kind='central_memory'").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM central_records WHERE kind='cutover_memory_archive'").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM central_records WHERE kind='message'").fetchone()[0], 1)
                conn.execute('INSERT INTO central_records VALUES(?,?,?,?)', ('central_memory','new-memory','{"content":"new agreement"}','after'))
            migration.install_authority(database, plan, receipts)
            with sqlite3.connect(database) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM phase3_local WHERE kind='task_authority'").fetchone()[0], 18)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM phase3_local WHERE kind='state' AND id='formal_migration'").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM central_records WHERE kind='central_memory'").fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
