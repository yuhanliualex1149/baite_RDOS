"""Deterministic, fail-closed conversion of the original Base into RDOS 2.0.

The transport is intentionally injected.  A migration applies only to the
original physical records after checking all eleven original cells, then
independently reads each record.  SQLite authority is installed only after
every remote row has been verified.  A partial run can be inspected and
resumed without creating a replacement task or changing its original ID.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from cutover import legacy_task_conversion as legacy
from cutover import production_config as formal
from server import rdos_contract, rdos_rules


EXPECTED_TASKS = 18
EXTRA_TASK_FIELDS = ("标题", "状态", "执行节点", "要求版本", "记录修订",
                     "操作ID", "记录数据", "当前项目ID")
EVENT_FIELDS = ("事件ID", "类型", "自报节点", "任务引用", "操作ID", "记录数据")


class MigrationError(ValueError):
    pass


class FileJournal:
    """Durable send-before-call markers prevent a blind second mutation."""
    def __init__(self, path):
        self.path = Path(path)
        if self.path.exists():
            if self.path.is_symlink() or self.path.stat().st_mode & 0o077:
                raise MigrationError('迁移操作日志权限无效')
            self.values = json.loads(self.path.read_text(encoding='utf-8'))
        else:
            self.values = {}

    def get(self, key):
        return self.values.get(key)

    def mark(self, key, value):
        if value not in ('sent', 'verified', 'not_sent'):
            raise MigrationError('迁移操作状态无效')
        if value == 'not_sent' and (key != 'schema:event_table' or self.values.get(key) != 'sent'):
            raise MigrationError('只有已证明本地拒绝的建表调用可恢复为未发送')
        if self.values.get(key) == 'verified' and value != 'verified':
            raise MigrationError('已核对操作不能回退')
        self.values[key] = value
        temporary = self.path.with_name(self.path.name + '.next')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(canonical(self.values) + '\n')
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


def old_cells(row):
    fields = row.get('fields') if isinstance(row, dict) else None
    if not isinstance(fields, dict) or not legacy.LEGACY_FIELDS.issubset(fields):
        raise MigrationError('原任务行缺少十一项原始字段')
    return {key: fields[key] for key in legacy.LEGACY_FIELDS}


def plan(rows, decisions, resources):
    """Require a final, row-hash-bound decision for every original task."""
    if not isinstance(rows, list) or len(rows) != EXPECTED_TASKS:
        raise MigrationError('原任务记录数量变化，须重新取基线')
    if not isinstance(decisions, dict) or decisions.get('status') != 'approved_for_migration':
        raise MigrationError('逐行状态与依据尚未完成核对')
    table = decisions.get('rows')
    if not isinstance(table, dict) or len(table) != len(rows):
        raise MigrationError('逐行决策数量与原任务表不一致')
    members = resources['node_members']
    mapping = legacy.Mapping(
        {formal.DEFAULT_PROJECT: formal.HUMAN_FOLDER,
         formal.FINANCE_PROJECT: formal.FINANCE_FOLDER},
        {formal.DEFAULT_PROJECT: 'mem_p6BbcYJrwAcv_M5Y',
         formal.FINANCE_PROJECT: 'mem_p6BbcYJrwAcv_M5Y'},
        {member: node for node, member in members.items()}, resources['event_table'])
    registry = {'workspace_id': 'ws_rdos', 'env_id': 'rdos_formal',
                'generation': hashlib.sha256((formal.BASE_TOKEN + ':' + formal.TASK_TABLE).encode()).hexdigest()[:32],
                'default_project_id': formal.DEFAULT_PROJECT,
                'nodes': [{'node_id': n, 'public_id': n} for n in members],
                'projects': [{'id': formal.FINANCE_PROJECT,
                              'public_id': formal.FINANCE_PROJECT}]}
    prepared = []
    seen_ids = set()
    seen_records = set()
    for row in rows:
        record_id = row.get('record_id')
        source = old_cells(row)
        task_id = legacy.one(source['任务ID'])
        if not task_id or task_id in seen_ids or not record_id or record_id in seen_records:
            raise MigrationError('原任务或物理记录标识重复')
        seen_ids.add(task_id); seen_records.add(record_id)
        item = table.get(task_id)
        if not isinstance(item, dict) or item.get('record_id') != record_id:
            raise MigrationError('逐行决策未绑定原物理记录')
        if item.get('source_fields_sha256') != legacy.digest(source):
            raise MigrationError('原任务字段变化，停止迁移')
        decision = item.get('decision')
        if decision == 'completed' and not item.get('evidence'):
            raise MigrationError('完成任务须绑定可核对的原反馈证据')
        if decision == 'ongoing' and not item.get('evidence'):
            raise MigrationError('继续执行须说明最新证据与适用版本')
        converted = legacy.convert(record_id, source, decision, mapping)
        if converted['blockers'] and decision not in ('pending_evidence',):
            raise MigrationError('旧任务仍有未解迁移阻塞')
        task = converted['proposal']
        task.update(env_id=registry['env_id'], generation=registry['generation'])
        task['migration_source']['evidence'] = item.get('evidence', {})
        # The evidence is provenance, not a task requirement, but it changes
        # the immutable task content.  Both writer and authority use this task.
        wire = rdos_contract.encode(task, registry)
        columns = dict(converted['new_columns'])
        columns['记录数据'] = canonical(wire)
        prepared.append({'task_id': task_id, 'record_id': record_id,
                         'source': source, 'task': task, 'wire': wire,
                         'columns': columns})
    if set(table) != seen_ids:
        raise MigrationError('逐行决策含原表以外的任务')
    return {'source_count': len(rows), 'task_ids': sorted(seen_ids),
            'records': prepared, 'content_sha256': digest([
                (r['record_id'], r['source'], r['columns']) for r in prepared])}


def _same_columns(actual, desired):
    return all(actual.get(key) == value for key, value in desired.items())


def archive_initial_memory(conn):
    """One-time cutover reset; protected archives cannot enter normal recall."""
    rows = conn.execute("""SELECT kind,id,payload FROM central_records WHERE
        kind IN ('central_memory','central_memory_history','central_memory_suppression',
                 'central_memory_operation','central_agent_policy_history')
        OR (kind='state' AND id='central_agent_policy')""").fetchall()
    for kind, key, body in rows:
        archive = {'original_kind': kind, 'original_id': key, 'record': json.loads(body)}
        conn.execute('INSERT OR IGNORE INTO central_records VALUES(?,?,?,?)',
                     ('cutover_memory_archive', digest([kind, key]), canonical(archive),
                      datetime.now(timezone.utc).isoformat()))
        conn.execute('DELETE FROM central_records WHERE kind=? AND id=?', (kind, key))
    return len(rows)


def apply_remote(prepared, transport, journal):
    """Transport exposes get_record(record_id) and update_record(record_id, fields).

    A mutation response is never proof.  On timeout the next invocation only
    reads the original physical row and refuses another write if uncertain.
    """
    receipts = []
    for item in prepared['records']:
        original = transport.get_record(item['record_id'])
        if old_cells(original) != item['source']:
            raise MigrationError('原任务字段在迁移期间变化，停止')
        current = original['fields']
        if not _same_columns(current, item['columns']):
            if any(current.get(key) not in (None, '') for key in item['columns']):
                raise MigrationError('已有不同的 2.0 字段，不能覆盖')
            if journal.get(item['record_id']) == 'sent':
                raise MigrationError('原写入仍待核对，不能再次发送')
            journal.mark(item['record_id'], 'sent')
            try:
                transport.update_record(item['record_id'], item['columns'])
            except Exception as exc:
                # The request may have reached Feishu.  Read once; if still
                # absent, stop instead of blindly resending.
                check = transport.get_record(item['record_id'])
                if not _same_columns(check['fields'], item['columns']):
                    raise MigrationError('写入结果待核对，不重新发送') from exc
        checked = transport.get_record(item['record_id'])
        if old_cells(checked) != item['source'] or not _same_columns(checked['fields'], item['columns']):
            raise MigrationError('原记录独立读回不一致，停止')
        journal.mark(item['record_id'], 'verified')
        receipts.append({'task_id': item['task_id'], 'record_id': item['record_id'],
                         'body_sha256': digest(item['columns']['记录数据'])})
    return receipts


def install_authority(database, prepared, receipts):
    """Install readback-bound authority in the *same* existing SQLite file."""
    if {(r['task_id'], r['record_id']) for r in receipts} != {
            (r['task_id'], r['record_id']) for r in prepared['records']}:
        raise MigrationError('远端任务未全部独立读回')
    expected_hashes = {r['task_id']: digest(r['columns']['记录数据']) for r in prepared['records']}
    if any(r.get('body_sha256') != expected_hashes.get(r['task_id']) for r in receipts):
        raise MigrationError('远端独立读回正文指纹不一致')
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(database, timeout=20) as conn:
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='central_users'").fetchone():
            raise MigrationError('既有成员数据库不存在')
        conn.execute('''CREATE TABLE IF NOT EXISTS central_records (
            kind TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,updated_at TEXT NOT NULL,
            PRIMARY KEY(kind,id))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS phase3_local (
            kind TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,updated_at TEXT NOT NULL,
            PRIMARY KEY(kind,id))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS phase3_history (
            task_id TEXT NOT NULL,version INTEGER NOT NULL,payload_hash TEXT NOT NULL,payload TEXT NOT NULL,
            PRIMARY KEY(task_id,version))''')
        if not conn.execute("SELECT 1 FROM phase3_local WHERE kind='state' AND id='formal_migration'").fetchone():
            archive_initial_memory(conn)
        def immutable(kind, key, value):
            payload = canonical(value)
            row = conn.execute('SELECT payload FROM phase3_local WHERE kind=? AND id=?',
                               (kind, key)).fetchone()
            if row and json.loads(row[0]) != value:
                raise MigrationError('本地授权记录冲突')
            conn.execute('INSERT OR IGNORE INTO phase3_local VALUES(?,?,?,?)',
                         (kind, key, payload, now))
        for item in prepared['records']:
            task = item['task']
            key = task['task_id'] + '@' + str(task['record_revision'])
            immutable('task_authority', key, {
                'hash': digest(task), 'operation_id': task['operation_id']})
            immutable('task_head', task['task_id'], {
                'task_id': task['task_id'], 'record_revision': task['record_revision'],
                'requirements_version': task['requirements_version'],
                'env_id': task['env_id'], 'generation': task['generation'],
                'content_hash': digest(task)})
            requirements = rdos_rules.requirements(task)
            req_hash = digest(requirements)
            old = conn.execute('SELECT payload_hash FROM phase3_history WHERE task_id=? AND version=?',
                               (task['task_id'], task['requirements_version'])).fetchone()
            if old and old[0] != req_hash:
                raise MigrationError('要求历史版本发生冲突')
            conn.execute('INSERT OR IGNORE INTO phase3_history VALUES(?,?,?,?)',
                         (task['task_id'], task['requirements_version'], req_hash, canonical(requirements)))
            if task['business_status'] == 'active':
                immutable('grant', task['task_id'] + '@' + str(task['requirements_version']), {
                    'operation_id': task['grant_operation_id'],
                    'requirements_hash': task['requirements_hash'],
                    'owner_id': task['owner_id']})
            operation = {'id': task['operation_id'], 'action': 'legacy_task_migration',
                         'status': 'succeeded', 'project_id': task['project_id'],
                         'task_id': task['task_id'], 'record_id': item['record_id'],
                         'source_fields_sha256': legacy.digest(item['source']),
                         'result_sha256': digest(task), 'at': now}
            old = conn.execute("SELECT payload FROM central_records WHERE kind='operation' AND id=?",
                               (task['operation_id'],)).fetchone()
            if old and json.loads(old[0]) != operation:
                # A repeated invocation keeps the first timestamp and proof.
                stable = json.loads(old[0])
                if any(stable.get(k) != operation[k] for k in operation if k != 'at'):
                    raise MigrationError('迁移操作记录冲突')
            else:
                conn.execute('INSERT OR IGNORE INTO central_records VALUES(?,?,?,?)',
                             ('operation', task['operation_id'], canonical(operation), now))
        marker = {'source_count': prepared['source_count'],
                  'content_sha256': prepared['content_sha256'],
                  'task_ids': prepared['task_ids']}
        immutable('state', 'formal_migration', marker)
    return marker


def require_installed_marker(database):
    """A formal service cannot boot on an unmigrated original task table."""
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as conn:
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='phase3_local'").fetchone()
        if not table:
            raise MigrationError('正式任务迁移尚未安装，拒绝切换服务')
        row = conn.execute("SELECT payload FROM phase3_local WHERE kind='state' AND id='formal_migration'").fetchone()
        if not row:
            raise MigrationError('正式任务迁移尚未完成，拒绝切换服务')
        marker = json.loads(row[0])
        if marker.get('source_count') != EXPECTED_TASKS or len(marker.get('task_ids', [])) != EXPECTED_TASKS:
            raise MigrationError('正式任务迁移标记与原始任务数量不一致')
        if not isinstance(marker.get('content_sha256'), str) or len(marker['content_sha256']) != 64:
            raise MigrationError('正式任务迁移标记缺少内容校验')
        return marker
