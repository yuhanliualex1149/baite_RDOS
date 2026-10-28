"""Versioned Central duties and database-backed memory settings.

The fixed runtime files are generated views, not arbitrary file editors. These
endpoints record a real authenticated settings action rather than a fake chat.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import threading
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from server import central_memory as memory, central_store as store, db, rdos_contract
from server.central_api import admin
from server.lab_routes import SanitizedLabRoute

router = APIRouter(prefix='/api/central/settings', tags=['central-settings'], route_class=SanitizedLabRoute)
_FILE_LOCK = threading.Lock()
_POLICY_ID = 'central_agent_policy'
_BASELINE = Path(__file__).with_name('central_context') / 'agent.md'


class SettingsError(ValueError):
    def __init__(self, code, message, status=409, **details):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    operation_id: str = Field(min_length=1, max_length=120, pattern=r'^[A-Za-z0-9_.:-]+$')


class AgentInput(Input):
    content: str = Field(min_length=1, max_length=40000)
    expected_version: int = Field(ge=1)


class MemoryInput(Input):
    operation: Literal['remember', 'correct', 'replace', 'invalidate', 'forget']
    expected_version: int = Field(ge=0)
    memory_id: str = Field(default='', max_length=120)
    topic: str = Field(default='', max_length=160)
    content: str = Field(default='', max_length=2000)
    scope: Literal['organization', 'project', 'account'] = 'organization'
    project_id: str = Field(default='', max_length=100)


def _failure(exc):
    raise HTTPException(exc.status, _project_projection({
        'code': exc.code, 'message': str(exc), **exc.details})) from exc


def _project_projection(value):
    """Convert identity fields only; authored Markdown and memory text are exact."""
    if isinstance(value, dict):
        return {key: rdos_contract.public_view(item) if key == 'project_id'
                else _project_projection(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_project_projection(item) for item in value]
    return copy.deepcopy(value)


def _baseline():
    if os.environ.get('RDOS_RUNTIME_MODE') == 'production':
        from server.production_runtime import require
        content = require().agent_md.read_text(encoding='utf-8').strip()
    else:
        content = _BASELINE.read_text(encoding='utf-8').strip()
    return {'id': _POLICY_ID, 'version': 1, 'content': content, 'hash': store.digest(content),
            'source_kind': 'bundled_policy', 'updated_at': '', 'updated_by': ''}


def policy_snapshot():
    """Read-only frozen input for a new run; retries pass their saved snapshot."""
    return copy.deepcopy(store.get('state', _POLICY_ID) or _baseline())


def _policy_path():
    if os.environ.get('RDOS_RUNTIME_MODE') == 'production':
        from server.production_runtime import require
        path = require().agent_md
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise SettingsError('policy_file_path', '中央职责文件异常')
        return path
    runtime = Path(os.environ['RDOS_LAB_RUNTIME'])
    if not runtime.is_absolute() or runtime.is_symlink() or runtime.resolve() != db.db_path().resolve().parent:
        raise SettingsError('policy_file_path', '中央职责文件位置不属于当前运行目录')
    directory = runtime / 'central'
    if directory.is_symlink() or directory.exists() and not directory.is_dir():
        raise SettingsError('policy_file_path', '中央职责文件目录异常')
    path = directory / 'agent.md'
    if path.is_symlink() or path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise SettingsError('policy_file_path', '中央职责文件异常')
    return path


def _write_policy():
    # Always re-read the current database version while holding the file lock:
    # a delayed older save must not replace a newer generated file.
    with _FILE_LOCK:
        value = policy_snapshot()
        path = _policy_path()
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.parent.chmod(0o700)
        temporary = path.with_name('agent.md.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                output.write(value['content'] + '\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)


def _file_state(value):
    try:
        path = _policy_path()
        matching = path.is_file() and path.read_text(encoding='utf-8').rstrip('\n') == value['content']
    except Exception:
        matching = False
    if matching:
        return {'file_updated': True}
    if os.environ.get('RDOS_RUNTIME_MODE') == 'production':
        return {'file_updated': False, 'file_error':
            '数据库中的职责仍生效；现有职责文件与数据库不一致，重启会保留文件。请在 Panel 核对并明确保存希望采用的内容。'}
    return {'file_updated': False, 'file_error':
        '数据库中的职责有效；受限职责文件尚未更新，请稍后重试保存或重启。'}


def initialize():
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        current = memory._get(conn, 'state', _POLICY_ID)
        if not current:
            current = {**_baseline(), 'updated_at': db.utc_now()}
            memory._put(conn, 'state', _POLICY_ID, current)
            memory._put(conn, 'central_agent_policy_history', str(current['version']), current)
    # An existing formal file may contain an administrator's direct edits.
    # Startup is not an authorized settings change: preserve those bytes even
    # when they differ from the database projection. Explicit Panel saves still
    # call _write_policy and update the file after their DB transaction.
    if os.environ.get('RDOS_RUNTIME_MODE') != 'production' or not _policy_path().exists():
        try:
            _write_policy()
        except Exception:
            pass
    return {**current, **_file_state(current)}


def get_agent(actor):
    memory._actor(actor, write=True)
    current = policy_snapshot()
    return {**current, **_file_state(current)}


def save_agent(actor, content, expected_version, operation_id):
    if (not isinstance(content, str) or not content.strip() or len(content) > 40000 or '\x00' in content
            or type(expected_version) is not int or expected_version < 1
            or not isinstance(operation_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}', operation_id)):
        raise SettingsError('invalid_policy', '职责内容、版本或操作标识无效', 422)
    content = content.strip()
    fingerprint = store.digest({'content': content, 'expected_version': expected_version})
    key = store.digest(['agent', operation_id])
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        user = memory._actor(actor, conn, write=True)
        current = memory._get(conn, 'state', _POLICY_ID) or _baseline()
        previous = memory._get(conn, 'central_settings_operation', key)
        if previous:
            if previous['actor_id'] != user['id'] or previous['input_hash'] != fingerprint:
                raise SettingsError('operation_conflict', '同一操作标识不能保存不同职责内容')
            # Return the current version, never resurrect a superseded policy.
            reused = True
        else:
            if current['version'] != expected_version:
                raise SettingsError('policy_conflict', '职责已被更新，请核对当前版本后再保存',
                                    current_version=current['version'], current=current)
            reused = current['content'] == content
            if not reused:
                memory._put(conn, 'central_agent_policy_history', str(current['version']), current)
                current = {'id': _POLICY_ID, 'version': current['version'] + 1,
                           'content': content, 'hash': store.digest(content), 'updated_at': db.utc_now(),
                           'updated_by': user['id'], 'source_kind': 'panel_settings'}
                memory._put(conn, 'state', _POLICY_ID, current)
                memory._put(conn, 'central_agent_policy_history', str(current['version']), current)
            memory._put(conn, 'central_settings_operation', key, {'id': key,
                'operation_id': operation_id, 'actor_id': user['id'], 'input_hash': fingerprint,
                'action': 'save_agent', 'source_kind': 'panel_settings', 'version': current['version'],
                'at': db.utc_now()})
    try:
        _write_policy()
    except Exception:
        pass
    return {**current, 'saved': True, 'reused': reused, **_file_state(current)}


def memory_preview(actor):
    memory._actor(actor, write=True)
    try:
        path = memory._summary_path()
        content = path.read_text(encoding='utf-8')
        if not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError('invalid summary')
        if content != memory._summary_content():
            return {'content': '', 'hash': '', 'available': False,
                    'summary_updated_at': None, 'generated': False,
                    'error': '现有记忆文件与数据库中的有效记忆不一致；文件已保留，但直接编辑不会自动生效。请在 Panel 核对后明确保存所需记忆。'}
        return {'content': content, 'hash': store.digest(content), 'available': True,
                'summary_updated_at': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                'generated': True}
    except Exception:
        return {'content': '', 'hash': '', 'available': False, 'summary_updated_at': None,
                'generated': True, 'error': '记忆摘要暂不可用；数据库中的记忆仍然有效。'}


@router.get('/agent')
def agent_get(actor: dict = Depends(admin)):
    try:
        return get_agent(actor)
    except (SettingsError, memory.MemoryError) as exc:
        _failure(exc)


@router.put('/agent')
def agent_put(payload: AgentInput, actor: dict = Depends(admin)):
    try:
        return save_agent(actor, **payload.model_dump())
    except (SettingsError, memory.MemoryError) as exc:
        _failure(exc)


@router.get('/memories')
def memories_get(include_inactive: bool = Query(default=True), actor: dict = Depends(admin)):
    try:
        return _project_projection(memory.settings_items(actor, include_inactive=include_inactive))
    except memory.MemoryError as exc:
        _failure(exc)


@router.post('/memories')
def memories_post(payload: MemoryInput, actor: dict = Depends(admin)):
    value = payload.model_dump()
    value['project_id'] = rdos_contract.internal_input(value.get('project_id', ''))
    operation_id, expected = value.pop('operation_id'), value.pop('expected_version')
    value['kind'] = value.pop('operation')
    try:
        return _project_projection(memory.apply_settings(actor, value, operation_id, expected))
    except memory.MemoryError as exc:
        _failure(exc)


@router.get('/memory-preview')
def preview_get(actor: dict = Depends(admin)):
    return memory_preview(actor)
