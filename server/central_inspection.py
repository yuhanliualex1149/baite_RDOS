"""Bounded observation tools for the central Agent, never business processing.

Only inspection receipts may be written here. In particular, a fresh Feishu
read does not ingest events, move task state, reconcile writes or save memory.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from server import db, central_store as store, phase1_runtime as runtime

INSPECTION_TOOLS = frozenset({
    'central_inspect_overview', 'central_inspect_code',
    'central_inspect_records', 'central_inspect_database',
})
_CODE = Path(__file__).resolve().parents[1]
_MAX_SCAN = 2000
_SOURCE_TYPES = {'.py', '.md', '.js', '.cjs', '.css', '.html', '.json', '.toml', '.yaml', '.yml', '.txt', '.ts'}
_EXCLUDED = {'runtime', 'nodes', 'backups', 'evidence', 'node_modules', '__pycache__', '.venv', '.git'}
_LOGS = {'server.log', 'blocked.jsonl', 'phase3_egress.jsonl'}
_TABLES = {'central_records', 'phase3_local', 'phase3_history', 'phase3_event_index'}
_FIELDS = {
    'projects': ('id', 'name', 'description', 'status', 'revision', 'is_default', 'owner_id', 'node_ids'),
    'tasks': ('id', 'task_id', 'project_id', 'title', 'business_status', 'requirements_version', 'record_revision',
              'result_status', 'receipt_status', 'result_refs', 'updated_at', 'integrity_error'),
    'events': ('id', 'project_id', 'event_type', 'task_refs', 'artifact_refs', 'claimed_node_id', 'result_status',
               'created_at', 'sequence', 'late_or_inactive', 'correction_of', 'body'),
    'task_history': ('task_id', 'requirements_version', 'version', 'payload_hash', 'title', 'goal', 'deliverable',
                     'task_type', 'completion_rule', 'executor_node_ids'),
    'runs': ('id', 'project_id', 'user_id', 'trigger', 'kind', 'status', 'operation_id', 'created_at', 'started_at',
             'completed_at', 'error_code', 'error', 'retry_count', 'usage'),
    'operations': ('id', 'project_id', 'action', 'status', 'run_id', 'task_id', 'target_id', 'phase', 'error_code',
                   'created_at', 'updated_at', 'verification_status', 'requirements_version', 'record_revision'),
    'health': ('project_id', 'status', 'last_checked_at', 'last_full_read_at', 'last_analysis_at', 'next_due',
               'uncertain_operations', 'consecutive_failures', 'needs_attention', 'issues'),
    'read_receipts': ('id', 'project_id', 'run_id', 'started_at', 'completed_at', 'complete', 'sources',
                      'missing_sources', 'fingerprint', 'error_code', 'quarantine_count'),
    'inspection_receipts': ('id', 'user_id', 'run_id', 'project_ids', 'tool', 'at', 'request_hash', 'result_hash'),
    'memory_metadata': ('id', 'topic', 'scope', 'project_id', 'status', 'version', 'updated_at'),
    'tool_calls': ('run_id', 'project_id', 'index', 'name', 'type', 'ok', 'code', 'at'),
}
_ENTITY_SOURCE = {
    'projects': ('central_records', 'project'), 'tasks': ('phase3_local', 'task_cache'),
    'runs': ('central_records', 'run'), 'operations': ('central_records', 'operation'),
    'health': ('central_records', 'project_health'), 'read_receipts': ('central_records', 'read_receipt'),
    'inspection_receipts': ('central_records', 'inspection_receipt'),
    'events': ('phase3_event_index', None), 'task_history': ('phase3_history', None),
    'tool_calls': ('central_records', 'run'),
}
_FILTERS = {'id', 'project_id', 'task_id', 'run_id', 'operation_id', 'status', 'after', 'before'}


class InspectionError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _fail(code, message):
    raise InspectionError(code, message)


def _spec(name, description, properties, required=()):
    return {'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}}}


def tools_specs():
    paging = {'offset': {'type': 'integer', 'minimum': 0, 'maximum': 10000},
              'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}}
    filters = {key: {'type': 'string'} for key in _FILTERS}
    return [
        _spec('central_inspect_overview', '只读检查授权项目及任务的已知状态。机构范围包括所有获准项目；fresh仅重新观察飞书，不同步处理或完成任务。',
              {'project_id': {'type': 'string'}, 'fresh': {'type': 'boolean'}}),
        _spec('central_inspect_code', '在当前RDOS代码副本中查找字面文本，或按相对路径和行号读取源码；返回文件哈希。不能读取运行目录、节点工作区或执行命令。',
              {'query': {'type': 'string'}, 'path': {'type': 'string'},
               'start_line': {'type': 'integer', 'minimum': 1}, 'end_line': {'type': 'integer', 'minimum': 1},
               'case_sensitive': {'type': 'boolean'}, 'limit': paging['limit']}),
        _spec('central_inspect_records', '限量联查运行、工具调用、操作、读取及恢复记录。日志仅限已登记文件；不能访问其他账号的私人对话。',
              {'entity': {'type': 'string', 'enum': [*_FIELDS, 'logs']}, 'filters': {'type': 'object', 'properties': filters, 'additionalProperties': False},
               'log': {'type': 'string', 'enum': sorted(_LOGS)}, **paging}, ['entity']),
        _spec('central_inspect_database', '查看允许实体的真实数据库结构及受控投影；使用固定只读连接。只接受实体、字段、过滤条件，不接受SQL或数据库路径。',
              {'entity': {'type': 'string', 'enum': list(_FIELDS)}, 'schema': {'type': 'boolean'},
               'fields': {'type': 'array', 'items': {'type': 'string'}},
               'filters': {'type': 'object', 'properties': filters, 'additionalProperties': False}, **paging}),
    ]


def _context(run, actor, project_id=None):
    # The same saved run and current actor checks as the normal tool boundary.
    from server import central_service
    saved = central_service._trusted_run(run, actor)
    current = central_service._run_actor(saved)
    if 'admin' not in current.get('roles', []) or saved.get('trigger') != 'manual':
        _fail('authorization_failed', '只读自检需要当前已认证管理员的手动请求')
    permitted = []
    for pid in saved.get('allowed_projects', []):
        runtime.project_for_actor(pid, current)
        permitted.append(pid)
    if project_id is not None:
        if not isinstance(project_id, str) or not project_id or project_id not in permitted:
            _fail('out_of_scope', '项目不属于当前运行的授权范围')
        permitted = [project_id]
    return saved, current, permitted


def _clean(value):
    from server.central_service import _safe_record
    if isinstance(value, str):
        value = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----', '[私钥已隐藏]', value)
    return _safe_record(value)


def _integer(value, default, minimum, maximum):
    value = default if value is None else value
    if type(value) is not int or not minimum <= value <= maximum:
        _fail('invalid_query', '读取数量或位置超出允许范围')
    return value


def _date(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, AttributeError):
        _fail('invalid_query', '时间筛选须使用明确的ISO日期时间')


def _filters(value):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - _FILTERS or any(not isinstance(v, str) or len(v) > 200 for v in value.values()):
        _fail('invalid_query', '查询条件未获准')
    for key in ('after', 'before'):
        if key in value:
            _date(value[key])
    return value


def _authorizer(action, first, second, database, origin):
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        allowed = _TABLES | {'sqlite_master'}
        return sqlite3.SQLITE_OK if first in allowed and database in ('main', None) else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_OK if (second or first or '').lower() in {'json_extract', 'json_valid', 'coalesce', 'ifnull', 'count'} else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA:
        return sqlite3.SQLITE_OK if (first in {'schema_version', 'query_only'} and second is None
                                     or first == 'table_info' and second in _TABLES) else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_DENY


@contextmanager
def _readonly_connection():
    # No caller-selected database or URI, and no normal db.connection() commit.
    path = db.db_path()
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    connection.enable_load_extension(False)
    connection.set_authorizer(_authorizer)
    deadline = time.monotonic() + 0.75
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        yield connection
    finally:
        connection.close()


def _scope_sql(project_ids, *, empty=False, column="json_extract(payload,'$.project_id')"):
    if not project_ids:
        return ('1=0', [])
    marks = ','.join('?' for _ in project_ids)
    return (('(' + column + f' IN ({marks})' + (f" OR {column}='' OR {column} IS NULL" if empty else '') + ')'), list(project_ids))


def _load(entity, actor, project_ids, *, include_organization=False, filters=None):
    if entity == 'memory_metadata':
        try:
            memory = importlib.import_module('server.central_memory')
            rows = memory.inspection_metadata(actor, project_ids)
        except (ImportError, AttributeError):
            _fail('inspection_source_unavailable', '中央记忆元数据读取服务尚未启用，不能据此判断没有记忆')
        return [{key: row[key] for key in _FIELDS[entity] if key in row} for row in rows[:_MAX_SCAN]], len(rows) > _MAX_SCAN
    table, kind = _ENTITY_SOURCE[entity]
    where, params = [], []
    if kind:
        where.append('kind=?'); params.append(kind)
    if entity == 'projects':
        clause, values = _scope_sql(project_ids, column='id')
    elif entity == 'task_history':
        clause, values = _scope_sql(project_ids)
        scoped_tasks = "SELECT id FROM phase3_local WHERE kind='task_cache' AND " + clause
        clause = 'task_id IN (' + scoped_tasks + ')'
    elif entity == 'inspection_receipts':
        clause, values = "json_extract(payload,'$.user_id')=?", [actor['id']]
    else:
        clause, values = _scope_sql(project_ids, empty=include_organization and entity in ('runs', 'tool_calls'))
    where.append(clause); params.extend(values)
    if entity in ('runs', 'tool_calls'):
        where.append("(json_extract(payload,'$.user_id')=? OR (json_extract(payload,'$.user_id')='system:scheduler' AND json_extract(payload,'$.trigger')='scheduled'))")
        params.append(actor['id'])
    # Exact identifiers can locate old evidence without scanning newer runs.
    for key, value in (filters or {}).items():
        if key == 'id' and entity != 'task_history':
            where.append('id=?'); params.append(value)
        elif key == 'run_id' and entity in ('runs', 'tool_calls'):
            where.append('id=?'); params.append(value)
        elif key == 'task_id' and entity in ('tasks', 'task_history'):
            where.append(('id' if entity == 'tasks' else 'task_id') + '=?'); params.append(value)
        elif key == 'operation_id' and entity == 'operations':
            where.append("(id=? OR json_extract(payload,'$.operation_id')=?)"); params.extend([value, value])
    with _readonly_connection() as conn:
        exists = conn.execute('SELECT name FROM sqlite_master WHERE type=? AND name=?', ('table', table)).fetchone()
        if not exists:
            return [], False
        order = 'sequence DESC' if entity == 'events' else 'task_id,version DESC' if entity == 'task_history' else 'updated_at DESC,id' if table in ('central_records', 'phase3_local') else 'id'
        columns = 'payload,sequence' if entity == 'events' else 'payload,task_id,version,payload_hash' if entity == 'task_history' else 'payload'
        rows = conn.execute(f'SELECT {columns} FROM {table} WHERE ' + ' AND '.join(where) + ' ORDER BY ' + order + ' LIMIT ?', [*params, _MAX_SCAN + 1]).fetchall()
    complete = len(rows) <= _MAX_SCAN
    output = []
    for row in rows[:_MAX_SCAN]:
        if len(row['payload']) > 2_000_000:
            complete = False
            continue
        item = json.loads(row['payload'])
        if entity == 'inspection_receipts' and not set(item.get('project_ids', [])).issubset(project_ids):
            continue
        if entity == 'events':
            item['sequence'] = row['sequence']
        if entity == 'task_history':
            item.update(task_id=row['task_id'], version=row['version'], payload_hash=row['payload_hash'])
        if entity == 'operations':
            # Business operation facts are shared; authorization source texts,
            # draft inputs and another administrator's chat are never included.
            task = item.get('prepared_task') or item.get('result') or {}
            if isinstance(task, dict):
                for key in ('task_id', 'requirements_version', 'record_revision'):
                    if key not in item and key in task:
                        item[key] = task[key]
            verification = item.get('verification')
            item['verification_status'] = verification.get('status') if isinstance(verification, dict) else ''
        if entity == 'tool_calls':
            for index, step in enumerate(item.get('steps', [])):
                if step.get('type') not in ('tool_call', 'tool_result'):
                    continue
                result = step.get('result') or {}
                output.append({'run_id': item['id'], 'project_id': item.get('project_id', ''), 'index': index,
                               'name': step.get('name', step.get('tool', '')), 'type': step['type'],
                               'ok': result.get('ok') if isinstance(result, dict) else step.get('ok'),
                               'code': result.get('code', '') if isinstance(result, dict) else '',
                               'at': item.get('completed_at') or item.get('started_at') or item.get('created_at', '')})
                if len(output) >= _MAX_SCAN:
                    return output, True
        else:
            output.append(item)
    return output, not complete


def _matches(item, filters):
    for key, value in filters.items():
        if key in ('after', 'before'):
            stamp = next((item.get(k) for k in ('at', 'updated_at', 'completed_at', 'created_at', 'started_at') if item.get(k)), None)
            if not stamp:
                return False
            try:
                date = _date(stamp)
            except InspectionError:
                return False
            if key == 'after' and date < _date(value) or key == 'before' and date > _date(value):
                return False
        elif key == 'task_id':
            if item.get('task_id') != value and not any(ref.get('task_id') == value for ref in item.get('task_refs', []) if isinstance(ref, dict)):
                return False
        elif key == 'operation_id':
            if item.get('operation_id') != value and item.get('id') != value:
                return False
        elif key == 'run_id':
            if item.get('run_id') != value and item.get('id') != value:
                return False
        elif str(item.get(key, '')) != value:
            return False
    return True


def _project_rows(entity, rows, fields):
    allowed = _FIELDS[entity]
    if fields is None:
        fields = [key for key in allowed if key != 'body']
    if not isinstance(fields, list) or not fields or len(fields) > len(allowed) or any(key not in allowed for key in fields):
        _fail('out_of_scope', '请求的数据库字段不在允许范围')
    output = []
    for item in rows:
        value = {key: item[key] for key in fields if key in item}
        if 'body' in value and len(value['body']) > 16000:
            value['body'] = value['body'][:16000]
            value['body_incomplete'] = True
        output.append(_clean(value))
    return output


def _query(entity, args, actor, project_ids, *, include_organization=False):
    if entity not in _FIELDS:
        _fail('out_of_scope', '数据库实体不在只读允许范围')
    filters = _filters(args.get('filters'))
    if 'project_id' in filters and filters['project_id'] not in project_ids:
        _fail('out_of_scope', '查询项目不属于当前运行')
    # Validate column selection before reading any rows.
    _project_rows(entity, [], args.get('fields'))
    offset = _integer(args.get('offset'), 0, 0, 10000)
    limit = _integer(args.get('limit'), 30, 1, 50)
    rows, bounded = _load(entity, actor, project_ids, include_organization=include_organization, filters=filters)
    match_filters = {key: value for key, value in filters.items() if not (key == 'project_id' and entity in ('task_history', 'inspection_receipts'))}
    rows = [row for row in rows if _matches({**row, 'project_id': row['id']} if entity == 'projects' else row, match_filters)]
    selected = _project_rows(entity, rows[offset:offset + limit], args.get('fields'))
    return {'entity': entity, 'items': selected, 'total_observed': len(rows), 'scan_complete': not bounded,
            'next_offset': offset + limit if offset + limit < len(rows) else None,
            'source': 'database_read_only', 'content_hash': store.digest(selected)}


def _schema(entity=None):
    entities = [entity] if entity else list(_FIELDS)
    if any(item not in _FIELDS for item in entities):
        _fail('out_of_scope', '数据库实体不在只读允许范围')
    result = []
    with _readonly_connection() as conn:
        version = conn.execute('PRAGMA schema_version').fetchone()[0]
        for name in entities:
            table = _ENTITY_SOURCE.get(name, ('central_records', None))[0]
            actual = conn.execute('PRAGMA table_info(' + table + ')').fetchall()
            result.append({'entity': name, 'source_table': table, 'available': bool(actual),
                           'allowed_fields': list(_FIELDS[name]),
                           'physical_columns': [{'name': row['name'], 'type': row['type']} for row in actual]})
    return {'schema_version': version, 'entities': result, 'source': 'database_schema_read_only'}


def _source(path):
    if not isinstance(path, str) or not path or len(path) > 240:
        _fail('out_of_scope', '请使用当前RDOS副本中的相对代码路径')
    raw = Path(path)
    if raw.is_absolute() or any(part in ('.', '..') or part.startswith('.') or part in _EXCLUDED for part in raw.parts):
        _fail('out_of_scope', '代码路径不在允许范围')
    if any(re.search(r'(?i)(credential|secret|\.env|panel_access)', part) for part in raw.parts):
        _fail('out_of_scope', '不能读取凭据或私有配置')
    candidate = _CODE / raw
    current = _CODE
    for part in raw.parts:
        current = current / part
        if current.is_symlink():
            _fail('out_of_scope', '代码读取不跟随符号链接')
    if not candidate.is_file() or not candidate.resolve().is_relative_to(_CODE) or candidate.suffix.lower() not in _SOURCE_TYPES:
        _fail('out_of_scope', '路径不是允许的代码文本')
    metadata = candidate.stat()
    if metadata.st_nlink != 1:
        _fail('out_of_scope', '代码读取不跟随硬链接')
    if metadata.st_size > 2_000_000:
        _fail('read_limit', '该文件超过只读检查的文本大小限制')
    raw_bytes = candidate.read_bytes()
    try:
        body = raw_bytes.decode('utf-8-sig')
    except UnicodeError:
        _fail('unsupported_source', '该文件不是UTF-8代码文本')
    return body.splitlines(), hashlib.sha256(raw_bytes).hexdigest()


def _code(args):
    if 'path' in args:
        if args.get('query'):
            _fail('invalid_query', '按段读取和代码搜索请分别调用')
        lines, version = _source(args['path'])
        start = _integer(args.get('start_line'), 1, 1, 1000000)
        end = _integer(args.get('end_line'), min(start + 79, 1000000), start, min(start + 159, 1000000))
        chosen, size, complete = [], 0, True
        for number in range(start, min(end, len(lines)) + 1):
            original = lines[number - 1]
            text = _clean(original[:2000])
            if size + len(text) > 32000:
                complete = False
                break
            chosen.append({'line': number, 'text': text, 'line_complete': len(original) <= 2000})
            size += len(text)
        return {'mode': 'read', 'path': args['path'], 'file_sha256': version, 'total_lines': len(lines),
                'lines': chosen, 'requested_range_complete': complete,
                'source': 'current_working_tree', 'version_basis': 'file_sha256'}
    query = args.get('query')
    if not isinstance(query, str) or not query or len(query) > 200 or '\n' in query or '\r' in query:
        _fail('invalid_query', '请输入最多200字的单行字面查找内容')
    case = args.get('case_sensitive', False)
    if type(case) is not bool:
        _fail('invalid_query', '大小写选项无效')
    limit = _integer(args.get('limit'), 30, 1, 50)
    needle = query if case else query.casefold()
    matches, scanned, stopped = [], 0, False
    for directory, folders, files in os.walk(_CODE, followlinks=False):
        folders[:] = sorted(folder for folder in folders if folder not in _EXCLUDED and not folder.startswith('.') and not (Path(directory) / folder).is_symlink())
        for filename in sorted(files):
            relative = str((Path(directory) / filename).relative_to(_CODE))
            try:
                lines, version = _source(relative)
            except (InspectionError, OSError):
                continue
            scanned += 1
            for number, line in enumerate(lines, 1):
                if needle in (line if case else line.casefold()):
                    matches.append({'path': relative, 'line': number, 'text': _clean(line[:1000]),
                                    'line_complete': len(line) <= 1000, 'file_sha256': version})
                    if len(matches) >= limit:
                        stopped = True
                        break
            if stopped or scanned >= 800:
                stopped = True
                break
        if stopped:
            break
    return {'mode': 'search', 'matches': matches, 'scanned_files': scanned, 'scan_complete': not stopped,
            'source': 'current_working_tree', 'version_basis': 'file_sha256', 'literal_search': True}


def _logs(args):
    name = args.get('log', 'server.log')
    if name not in _LOGS:
        _fail('out_of_scope', '日志未登记为可读来源')
    filters = _filters(args.get('filters'))
    # These files have no reliable per-user/per-project provenance. Exposing
    # raw server lines could bypass conversation ownership, so only operational
    # event metadata and narrowly classified server messages are returned.
    if set(filters) - {'after', 'before'}:
        _fail('unavailable_correlation', '该日志没有可靠的对象关联字段，请改查运行或操作记录')
    offset = _integer(args.get('offset'), 0, 0, 10000)
    limit = _integer(args.get('limit'), 30, 1, 50)
    root = db.db_path().parent
    if os.environ.get('RDOS_RUNTIME_MODE') == 'production':
        from server.production_runtime import require
        require()
        root = Path('/var/log/baite-rdos') if name == 'server.log' else root / 'runtime'
    path = root / ('panel.log' if os.environ.get('RDOS_RUNTIME_MODE') == 'production' and name == 'server.log' else name)
    if path.is_symlink() or path.resolve().parent != root.resolve():
        _fail('out_of_scope', '日志路径不属于当前运行目录')
    if not path.exists():
        return {'log': name, 'items': [], 'source_available': False, 'reason': '当前没有该日志', 'next_offset': None}
    size = path.stat().st_size
    with path.open('rb') as stream:
        seek = max(0, size - 262144)
        stream.seek(seek)
        data = stream.read(262144)
    lines = data.decode('utf-8', errors='replace').splitlines()
    if seek and lines:
        lines = lines[1:]
    observed = []
    for number, text in enumerate(lines, 1):
        line_hash = hashlib.sha256(text.encode()).hexdigest()
        if name.endswith('.jsonl'):
            try:
                row = json.loads(text)
            except ValueError:
                observed.append({'line_in_window': number, 'status': 'unparseable', 'content_hash': line_hash})
                continue
            row = {key: row[key] for key in ('at', 'event', 'channel', 'category', 'object_hash', 'state') if key in row}
            if not _matches(row, filters):
                continue
        else:
            # No arbitrary traceback/message text: it may quote a request.
            category = ('error' if re.search(r'(?i)error|exception|traceback', text) else
                        'warning' if 'warning' in text.lower() else
                        'startup' if re.search(r'(?i)started server|application startup|uvicorn running', text) else
                        'shutdown' if re.search(r'(?i)shutting down|shutdown complete|finished server', text) else 'other')
            stamp = re.search(r'\d{4}-\d\d-\d\d[T ][0-9:.]+(?:Z|[+-]\d\d:\d\d)?', text)
            row = {'category': category, **({'at': stamp.group()} if stamp else {})}
            exception = re.match(r'^([A-Za-z_][A-Za-z0-9_.]{0,100}(?:Error|Exception|Interrupt)):', text.strip())
            if exception:
                row['exception_type'] = exception.group(1)
            frame = re.search(r'File "([^"\n]+)", line (\d+), in ([A-Za-z0-9_.<>]+)', text)
            if frame:
                try:
                    relative = str(Path(frame.group(1)).relative_to(_CODE))
                    _, version = _source(relative)
                except (ValueError, InspectionError, OSError):
                    pass
                else:
                    row['code_frame'] = {'path': relative, 'line': int(frame.group(2)),
                                         'function': frame.group(3), 'current_file_sha256': version}
            if not _matches(row, filters):
                continue
        observed.append({**_clean(row), 'line_in_window': number, 'content_hash': line_hash})
    observed.reverse()
    return {'log': name, 'items': observed[offset:offset + limit], 'next_offset': offset + limit if offset + limit < len(observed) else None,
            'source_available': True, 'window_start_byte': seek, 'window_bytes': len(data), 'file_size': size,
            'window_sha256': hashlib.sha256(data).hexdigest(), 'scan_complete': seek == 0,
            'evidence_boundary': '旧日志只提供可核对的运行元数据；缺少对象关联时不能归因到某个任务或账号。'}


def _project_fact(pid, actor):
    from server import rdos_projects
    item = store.get('project', pid) or {}
    value = {key: item.get(key) for key in _FIELDS['projects'] if key in item}
    value['id'] = pid
    if rdos_projects.is_registered(pid):
        workspace = importlib.import_module('server.phase3_service').business_workspace(actor, pid)
        tasks = workspace['tasks']
        refresh = workspace.get('last_refresh') or {}
        return {'project': value, 'tasks': [_task_fact(task) for task in tasks],
                'last_full_read_at': refresh.get('at'), 'last_read_complete': refresh.get('complete'),
                'last_read_error': refresh.get('error') or '', 'source': 'verified_cache',
                'nodes': workspace.get('nodes', []), 'documents': workspace.get('documents', [])}
    tasks = [task for task in store.items('task') if task.get('project_id', store.PROJECT_ID) == pid]
    return {'project': value, 'tasks': [_task_fact(task) for task in tasks],
            'last_full_read_at': runtime.health(pid).get('last_full_read_at'), 'source': 'verified_cache', 'nodes': [], 'documents': []}


def _task_fact(task):
    value = {key: task[key] for key in _FIELDS['tasks'] if key in task}
    value['artifact_evidence'] = [{'document_id': ref.get('document_id') or ref.get('id'), 'revision': ref.get('revision'),
                                  'source_url': ref.get('source_url'), 'content_hash': ref.get('content_hash')}
                                 for ref in task.get('result_refs', []) if isinstance(ref, dict)]
    value['execution_reported'] = task.get('result_status')
    value['technical_verification'] = task.get('receipt_status') or 'not_observed'
    value['evidence_boundary'] = '成果引用与技术读回分别记录；引用存在不等于本次已读取成果正文。'
    return value


def _safe_failure(exc):
    """Keep actionable classifications, never connector output or exceptions' text."""
    code = getattr(exc, 'code', 'observation_failed')
    kind = type(exc).__name__
    return {'code': code if isinstance(code, str) and re.fullmatch(r'[a-z0-9_]{1,80}', code) else 'observation_failed',
            'error_type': kind if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', kind) else 'Exception'}


def result_brief(result):
    """Body-free evidence metadata retained when a large tool result is projected."""
    if not isinstance(result, dict):
        return {}
    evidence = result.get('evidence') or {}
    values = []
    for project in evidence.get('projects', []):
        issues = project.get('observation_issues', [])
        values.append({'project_id': project.get('project', {}).get('id'),
            'fresh_status': project.get('fresh_status', 'not_requested'),
            'task_count': len(project.get('tasks', [])), 'event_count': len(project.get('events', [])),
            'issue_count': len(issues),
            'issue_codes': dict(Counter(item.get('code', 'invalid_record') for item in issues)),
            'issue_types': dict(Counter(item.get('error_type', 'Unknown') for item in issues))})
    return {'ok': result.get('ok'), 'read_only': result.get('read_only'), 'observed_at': result.get('observed_at'),
        'inspection_receipt_id': result.get('inspection_receipt_id'),
        'complete': evidence.get('complete'), 'fresh': evidence.get('fresh'), 'projects': values,
        'source_errors': [{key: item.get(key) for key in ('source', 'code', 'error_type')}
                          for item in evidence.get('source_errors', [])]}


async def _overview(args, actor, project_ids):
    from server import rdos_projects, phase3_backend, phase3_service
    fresh = args.get('fresh', False)
    if type(fresh) is not bool:
        _fail('invalid_query', '最新读取选项无效')
    output = [_project_fact(pid, actor) for pid in project_ids]
    for project in output:
        pid = project['project']['id']
        project['health'] = runtime.health(pid)
    if fresh:
        backend = phase3_backend.get_backend()
        # A shared authority is read once. Never call refresh_registry,
        # _ingest_event, _derive_submission, reconciliation, or cache replacement.
        async def read_source(name, read):
            try:
                values = await asyncio.to_thread(read)
                if not isinstance(values, list):
                    _fail('incomplete_source', '任务或协作记录未完整返回')
                return values, None
            except Exception as exc:
                return None, {'source': name, **_safe_failure(exc)}
        task_source, event_source = await asyncio.gather(
            read_source('tasks', backend.list_tasks), read_source('events', backend.list_events))
        errors = [error for _, error in (task_source, event_source) if error]
        if errors:
            for project in output:
                project.update(fresh_status='failed', fresh_error='最新只读观察未完成；保留已知缓存，不将它当成本次成功读取。')
            return {'projects': output, 'fresh': True, 'complete': False,
                    'source': 'read_only_observation', 'source_errors': errors}
        raw_tasks, raw_events = task_source[0], event_source[0]
        from server import peer_tasks
        tasks_by_id = {value.get('task_id'): value for value in raw_tasks if isinstance(value, dict)}
        for project in output:
            pid = project['project']['id']
            if not rdos_projects.is_registered(pid):
                project.update(fresh_status='unsupported', fresh_error='该历史项目没有当前协作表适配，未用Default代替。')
                continue
            tasks, issues = [], []
            for raw in raw_tasks:
                if not isinstance(raw, dict) or raw.get('project_id') != pid:
                    continue
                try:
                    if raw.get('dispatch_source') == 'peer':
                        # validate_task verifies signatures; accept_task would
                        # also adopt it and write grants, which is not a read.
                        peer_tasks.validate_task(raw)
                        checked = raw
                    else:
                        checked = phase3_service._validate_task(raw)
                    tasks.append(_task_fact(checked))
                except (ValueError, KeyError, TypeError) as exc:
                    issues.append({'task_id': raw.get('task_id', ''), **_safe_failure(exc), 'message': '任务完整性或版本需核对；未处理或覆盖现有任务。'})
            events = []
            for raw in raw_events:
                if not isinstance(raw, dict) or raw.get('project_id') != pid:
                    continue
                try:
                    observation = None
                    if raw.get('event_type') in {'node_registry_snapshot', 'peer_enrollment_request', 'peer_capability_ack'}:
                        observation = peer_tasks.observe_system_event(raw, tasks=tasks_by_id)
                    else:
                        phase3_service._event_shape(raw)
                    event = {key: raw[key] for key in _FIELDS['events'] if key != 'body' and key in raw}
                    if observation:
                        event['system_observation'] = observation
                    events.append(event)
                except (ValueError, KeyError, TypeError) as exc:
                    issues.append({'event_id': raw.get('id', ''), **_safe_failure(exc), 'message': '记录格式或签名需核对；未摄取或推进任务。'})
            project.update(tasks=tasks, events=events, observation_issues=issues, fresh_status='partial' if issues else 'succeeded',
                           source='registered_task_authority_read_only', observed_at=db.utc_now())
    return {'projects': output, 'fresh': fresh, 'complete': all(p.get('fresh_status', 'succeeded') == 'succeeded' for p in output),
            'source': 'read_only_observation' if fresh else 'verified_cache',
            'capabilities': ['authorized_project_overview', 'literal_code_search', 'scoped_record_query', 'read_only_database_projection'],
            'limits': ['没有回执不能判断节点离线或未工作。', '只读观察不处理回执，不改变任务、技术核对或质量评价。']}


def _receipt(name, args, result, run, actor, project_ids):
    at = db.utc_now()
    key = 'inspection_' + store.digest([run['id'], name, args, at])[:28]
    store.put('inspection_receipt', key, {'id': key, 'user_id': actor['id'], 'run_id': run['id'],
              'project_ids': project_ids, 'tool': name, 'at': at,
              'request_hash': store.digest(args), 'result_hash': store.digest(result)})
    return key


async def execute(name, args, run, actor):
    try:
        if name not in INSPECTION_TOOLS or not isinstance(args, dict):
            _fail('not_allowed', '自检工具或参数未获准')
        spec = next(item['function']['parameters'] for item in tools_specs() if item['function']['name'] == name)
        if set(args) - set(spec['properties']) or any(key not in args for key in spec['required']):
            _fail('invalid_query', '自检参数包含未支持内容')
        filters = _filters(args.get('filters'))
        selected = args.get('project_id') or filters.get('project_id')
        saved, current, project_ids = _context(run, actor, selected)
        observed_at = db.utc_now()
        if name == 'central_inspect_overview':
            result = await _overview(args, current, project_ids)
            summary = ('最新只读观察未完整完成；已知状态和读取缺口分别保留。' if not result['complete'] else
                       '已读取' + str(len(result['projects'])) + '个授权项目的' + ('最新记录' if args.get('fresh') else '已知状态') + '。')
        elif name == 'central_inspect_code':
            result = _code(args)
            summary = '已读取当前代码及文件版本。'
        elif name == 'central_inspect_records':
            entity = args['entity']
            result = _logs(args) if entity == 'logs' else _query(entity, args, current, project_ids,
                                                               include_organization=not saved.get('project_id') and not selected)
            summary = ('该记录来源目前不存在，未据此判断系统正常。' if result.get('source_available') is False else
                       '已读取对应范围内的记录；未执行恢复或业务变更。')
        else:
            schema = args.get('schema', False)
            if type(schema) is not bool:
                _fail('invalid_query', 'Schema选项无效')
            if schema:
                result = _schema(args.get('entity'))
            else:
                result = _query(args.get('entity', 'tasks'), args, current, project_ids,
                                include_organization=not saved.get('project_id') and not selected)
            summary = '已从固定数据库只读连接取得允许的结构或记录。'
        result = _clean(result)
        receipt = _receipt(name, args, result, saved, current, project_ids)
        return {'ok': True, 'observed_at': observed_at, 'scope': {'project_ids': project_ids, 'user_id': current['id'],
                'kind': 'project_conversation' if saved.get('project_id') else 'organization_conversation',
                'represents_all_account_permissions': not bool(saved.get('project_id') or selected)},
                'summary': summary, 'evidence': result, 'inspection_receipt_id': receipt,
                'evidence_hash': store.digest(result), 'read_only': True}
    except (InspectionError, PermissionError, ValueError, OSError, sqlite3.Error) as exc:
        return {'ok': False, 'code': getattr(exc, 'code', 'authorization_failed' if isinstance(exc, PermissionError) else 'inspection_failed'),
                'error': _clean(str(exc)) if isinstance(exc, (InspectionError, PermissionError)) else '只读检查未完成，不能据此判定系统正常。',
                'read_only': True}
