"""One-shot formal Base migration. Run as the existing service user while stopped.

No URL, token, SQL or shell command can be supplied by a task row.  The
original Base/table IDs are constants.  Use --check to print a narrow source
summary; --apply requires a reviewed decision file, exact plan hash and a
fresh SQLite backup.  Remote mutations have durable send markers and readback.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from cutover import formal_migration as migration
from cutover import production_config as formal
from server import phase3_feishu as feishu


def require_file(path: Path):
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise migration.MigrationError('须使用受限的绝对普通文件')
    if path.stat().st_mode & 0o077:
        raise migration.MigrationError('正式资源或决策文件权限过宽')
    return path


def _name(row):
    return row.get('name', row.get('field_name', '')) if isinstance(row, dict) else ''


def unique_named(rows, name):
    found = [row for row in rows if _name(row) == name]
    if len(found) > 1:
        raise migration.MigrationError('飞书存在重复同名对象，停止')
    return found[0] if found else None


def table_id(row):
    return row.get('table_id', row.get('id', '')) if isinstance(row, dict) else ''


def make_executor(resources, state_root):
    runtime = state_root / 'runtime'
    runtime.mkdir(mode=0o700, exist_ok=True)
    (runtime / 'tmp').mkdir(mode=0o700, exist_ok=True)
    if runtime.is_symlink() or runtime.stat().st_mode & 0o077:
        raise migration.MigrationError('正式运行目录权限过宽')
    registry = {'env_id': 'rdos_formal', 'base_token': formal.BASE_TOKEN,
                'tasks_table': formal.TASK_TABLE,
                'events_table': resources.get('event_table') or 'tblPendingEvent000',
                'account_fingerprint': resources['account_fingerprint'],
                'documents': [], 'projects': [],
                'human_folder': formal.HUMAN_FOLDER, 'agent_folder': formal.AGENT_FOLDER}
    cfg = {'root': str(state_root), 'phase3': {
        'cli_path': resources['cli_path'], 'cli_sha256': resources['cli_sha256'],
        'registry_path': str(runtime / 'phase3_resources.json')}}
    executor = feishu._CLIExecutor(feishu._TOKEN, cfg, registry, setup=True)
    executor.identity()
    return executor


def read_rows(executor):
    tables = executor.tables()
    if not any(table_id(row) == formal.TASK_TABLE for row in tables):
        raise migration.MigrationError('原任务表不在原 Base 中')
    rows = executor.records(formal.TASK_TABLE)
    if len(rows) != migration.EXPECTED_TASKS:
        raise migration.MigrationError('原任务行数发生变化，须重取基线')
    for row in rows:
        migration.old_cells(row)
    return rows, tables


def ensure_table(executor, tables, journal):
    existing = unique_named(tables, '协作记录')
    if existing:
        token = table_id(existing)
        if not token or token == formal.TASK_TABLE:
            raise migration.MigrationError('协作记录表标识无效')
        return token
    if journal.get('schema:event_table') == 'sent':
        raise migration.MigrationError('建表结果待核对，不能再次创建')
    journal.mark('schema:event_table', 'sent')
    try:
        executor.create_table('协作记录')
    except Exception:
        pass  # The response may be lost; only a fresh table list can decide.
    existing = unique_named(executor.tables(), '协作记录')
    if not existing or not table_id(existing):
        raise migration.MigrationError('建表尚未独立读回，停止')
    journal.mark('schema:event_table', 'verified')
    return table_id(existing)


def ensure_fields(executor, table, names, journal):
    def current():
        result = executor.fields(table)
        return result.get('items', result.get('fields', []))
    existing = current()
    for name in names:
        if unique_named(existing, name):
            continue
        key = 'schema:' + table + ':' + name
        if journal.get(key) == 'sent':
            raise migration.MigrationError('字段建立结果待核对，不能再次创建')
        journal.mark(key, 'sent')
        try:
            executor.create_field(table, name)
        except Exception:
            pass
        existing = current()
        if not unique_named(existing, name):
            raise migration.MigrationError('字段尚未独立读回，停止')
        journal.mark(key, 'verified')


class RecordTransport:
    def __init__(self, executor):
        self.executor = executor

    def get_record(self, record_id):
        return self.executor.get_record(formal.TASK_TABLE, record_id)

    def update_record(self, record_id, fields):
        return self.executor.upsert(formal.TASK_TABLE, fields, record_id)


def backup_database(database, target):
    if target.exists() or target.is_symlink():
        raise migration.MigrationError('本轮备份目标已存在，不能覆盖')
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as source:
        with sqlite3.connect(target) as destination:
            source.backup(destination)
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise migration.MigrationError('正式数据库备份完整性检查失败')
    target.chmod(0o600)
    return hashlib.sha256(target.read_bytes()).hexdigest()


def save_manifest(path, resources):
    if path.exists() or path.is_symlink():
        previous = json.loads(require_file(path).read_text(encoding='utf-8'))
        if previous != resources:
            raise migration.MigrationError('正式资源登记与本次迁移不同，不能覆盖')
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(migration.canonical(resources) + '\n')
        stream.flush(); os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description='RDOS 2.0 original-Base migration')
    parser.add_argument('--resources', type=Path, required=True)
    parser.add_argument('--decisions', type=Path)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--prepare-schema', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--plan-sha256')
    parser.add_argument('--source-sha256')
    args = parser.parse_args()
    if sum((args.check, args.prepare_schema, args.apply)) != 1:
        parser.error('specify exactly one of --check, --prepare-schema or --apply')
    if os.environ.get('RDOS_RUNTIME_MODE') != 'production' or not feishu._FORMAL:
        raise migration.MigrationError('正式迁移须显式启用 production 模式')
    resources = json.loads(require_file(args.resources).read_text(encoding='utf-8'))
    # Validate every fixed resource before any CLI operation.  The real event
    # table token is discovered once during the one-shot migration.
    candidate = {**resources, 'event_table': resources.get('event_table') or 'tblPendingEvent000'}
    formal.validate_resources(candidate)
    if resources['cli_path'] != str(formal.LIVE_CLI):
        raise migration.MigrationError('仅允许正式服务器上的固定 CLI')
    if (formal.LIVE_CLI.is_symlink() or not formal.LIVE_CLI.is_file()
            or hashlib.sha256(formal.LIVE_CLI.read_bytes()).hexdigest() != resources['cli_sha256']):
        raise migration.MigrationError('正式 CLI 校验不匹配')
    state_root = formal.LIVE_DATABASE.parent
    executor = make_executor(candidate, state_root)
    rows, tables = read_rows(executor)
    source_hash = migration.digest([(row['record_id'], migration.old_cells(row)) for row in rows])
    existing_events = unique_named(tables, '协作记录')
    if existing_events:
        candidate['event_table'] = table_id(existing_events)
        formal.validate_resources(candidate)
        executor.registry['events_table'] = candidate['event_table']
    if args.check:
        summary = {'base': formal.BASE_TOKEN, 'task_table': formal.TASK_TABLE,
                          'rows': len(rows), 'source_sha256': source_hash,
                          'event_table': candidate['event_table'] if existing_events else None,
                          'event_table_present': bool(existing_events)}
        if args.decisions and existing_events:
            decisions = json.loads(require_file(args.decisions).read_text(encoding='utf-8'))
            summary['plan_sha256'] = migration.plan(rows, decisions, candidate)['content_sha256']
        elif args.decisions:
            summary['plan_pending_schema'] = True
        print(json.dumps(summary, ensure_ascii=False))
        return
    service = subprocess.run(['systemctl', 'is-active', 'baite-rdos'], check=False,
                             capture_output=True, text=True)
    if service.stdout.strip() not in ('inactive', 'failed'):
        raise migration.MigrationError('未证实旧服务已停止，不能迁移')
    if args.source_sha256 != source_hash:
        raise migration.MigrationError('原任务快照已变化，停止迁移')
    if args.prepare_schema:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup = state_root / 'cutover-backups' / ('baite-before-schema-' + stamp + '.db')
        backup_hash = backup_database(formal.LIVE_DATABASE, backup)
        journal = migration.FileJournal(state_root / 'runtime/formal_migration_journal.json')
        event_table = ensure_table(executor, tables, journal)
        executor.registry['events_table'] = event_table
        ensure_fields(executor, formal.TASK_TABLE, migration.EXTRA_TASK_FIELDS, journal)
        ensure_fields(executor, event_table, migration.EVENT_FIELDS, journal)
        print(json.dumps({'event_table': event_table, 'source_sha256': source_hash,
                          'sqlite_backup_sha256': backup_hash}, ensure_ascii=False))
        return
    if not args.decisions or not args.plan_sha256:
        parser.error('--apply requires --decisions and --plan-sha256')
    if not existing_events:
        raise migration.MigrationError('协作记录表尚未建置，先执行 --prepare-schema')
    decisions = json.loads(require_file(args.decisions).read_text(encoding='utf-8'))
    journal = migration.FileJournal(state_root / 'runtime/formal_migration_journal.json')
    prepared = migration.plan(rows, decisions, candidate)
    if args.plan_sha256 != prepared['content_sha256']:
        raise migration.MigrationError('迁移内容哈希与已复核方案不符，尚未写任务')
    for table, names in ((formal.TASK_TABLE, migration.EXTRA_TASK_FIELDS),
                         (candidate['event_table'], migration.EVENT_FIELDS)):
        fields = executor.fields(table)
        existing = fields.get('items', fields.get('fields', []))
        if any(not unique_named(existing, name) for name in names):
            raise migration.MigrationError('正式表结构不完整，停止迁移')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = state_root / 'cutover-backups' / ('baite-before-rdos2-' + stamp + '.db')
    backup_hash = backup_database(formal.LIVE_DATABASE, backup)
    resources['event_table'] = candidate['event_table']
    formal.validate_resources(resources)
    receipts = migration.apply_remote(prepared, RecordTransport(executor), journal)
    marker = migration.install_authority(formal.LIVE_DATABASE, prepared, receipts)
    # The CLI runs as the existing service user so it can reuse that user's
    # Feishu login.  It must not write under /etc.  A root-owned deployment
    # step installs this reviewed, read-back-bound manifest at LIVE_MANIFEST.
    save_manifest(state_root / 'runtime/rdos2-resources.ready.json', resources)
    print(json.dumps({'migrated': len(receipts), 'source_sha256': source_hash,
                      'content_sha256': marker['content_sha256'],
                      'event_table': candidate['event_table'], 'sqlite_backup_sha256': backup_hash}, ensure_ascii=False))


if __name__ == '__main__':
    main()
