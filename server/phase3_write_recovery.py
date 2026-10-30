"""Retire only a proven old local refusal, without erasing its send history.

Called under the task publishing lock by the administrator's draft deletion.
General response loss and journal absence cannot unlock an uncertain write.
"""
from __future__ import annotations

import asyncio
import json

from server import db, central_store as central, phase3_store as local


def _retirement_binding(op):
    # Authorization checks and timestamps may append on an attempted replay.
    # They must not invalidate an already recorded, immutable retirement proof.
    keys=('id','action','payload_hash','authorization_hash','status','error_code',
          'dispatched_at','task_dispatched','event_dispatched','prepared_task','prepared_event',
          'retired_at','retired_by','retirement_source')
    return central.digest({key:op.get(key) for key in keys})


def retired_unwritten(op):
    receipt = central.get('unwritten_task_recovery', op['id']) or {}
    return (op.get('status') == 'conflict' and op.get('error_code') == 'retired_unwritten'
        and receipt.get('authorization_hash') == op.get('authorization_hash')
        and central.digest(op.get('authorization') or {}) == op.get('authorization_hash')
        and receipt.get('retirement_binding_hash') == _retirement_binding(op)
        and receipt.get('proof', {}).get('transport_attempted') is False)


async def recover_before_draft_delete(identifier, expected_hash, actor, audit_id):
    from server import phase3_service as tasks, phase3_backend as backends
    from server import phase1_operations as writes
    from server.draft_lifecycle import _linked_operation
    from server.central_memory import _actor
    user = tasks._admin(actor)
    draft = local.get('draft', identifier)
    if not draft or draft.get('status') != 'draft' or draft.get('draft_hash') != expected_hash:
        return
    backend = backends.get_backend()
    inspect = getattr(backend, 'unwritten_legacy_upgrade_evidence', None)
    if inspect is None:
        return
    operations = [op for op in central.items('operation') if _linked_operation(op, 'draft', draft)]
    for op in operations:
        if (op.get('status') != 'uncertain' or op.get('action') != 'phase3_task:update'
            or op.get('error_code') != 'write_failed' or not op.get('task_dispatched') or op.get('event_dispatched')
            or len(op.get('attempts', [])) != 1 or op['attempts'][0].get('mode') != 'write'
            or op.get('active_attempt') or (op['id'] in writes.ACTIVE_WRITES and not writes.ACTIVE_WRITES[op['id']].done())):
            continue
        binding = op.get('authorization') or {}
        prepared = op.get('prepared_task') or {}
        event = op.get('prepared_event') or {}
        base = binding.get('base') or {}
        if (central.digest(binding) != op.get('authorization_hash') or not event.get('id')
            or prepared.get('task_id') != draft['task_id']
            or prepared.get('record_revision') != base.get('record_revision', -1) + 1
            or prepared.get('requirements_version') != base.get('requirements_version', -1) + 1):
            continue
        proof = await asyncio.to_thread(inspect, prepared, op['id'] + ':task', op['id'] + ':event')
        if not proof or proof.get('transport_attempted') is not False:
            continue
        # This is an authoritative read plus the normal identity, requirements,
        # immutable-authority and rollback checks, never a cache-only inference.
        remote = await tasks._read_task(draft['task_id'])
        tasks._check_expected(remote, base.get('record_revision'), base.get('requirements_version'))
        head = local.get('task_head', draft['task_id']) or {}
        remote_hash = central.digest(backends.payload(remote))
        if head.get('content_hash') != remote_hash:
            continue
        events = await asyncio.to_thread(backend.list_events)
        if any(e.get('id') == event['id'] or e.get('operation_id') == op['id'] + ':event' for e in events):
            continue
        proof = {**proof, 'remote_task_hash':remote_hash, 'remote_revision':remote['record_revision'],
            'remote_requirements_version':remote['requirements_version'], 'observed_at':db.utc_now()}
        with db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current_user = _actor(user, conn, write=True)
            if 'admin' not in current_user.get('roles', []):
                continue
            def get(table, kind, key):
                row = conn.execute(f'SELECT payload FROM {table} WHERE kind=? AND id=?',(kind,key)).fetchone()
                return json.loads(row[0]) if row else None
            latest = get('central_records','operation',op['id'])
            current_draft = get('phase3_local','draft',identifier)
            if latest != op or current_draft != draft or get('phase3_local','task_head',draft['task_id']) != head:
                continue
            snapshots=[]
            for kind,key in (('task_authority',tasks._authority_key(prepared)),('trusted_event',event['id'])):
                value=get('phase3_local',kind,key)
                if not value or value.get('operation_id') != op['id']:
                    continue
                # Only this operation's own unconfirmed projections may retire.
                expected=central.digest(backends.payload(prepared if kind=='task_authority' else event))
                if value.get('hash') != expected:
                    raise ValueError('原写入计划与未确认索引不一致，保留原记录待核对')
                snapshots.append({'kind':kind,'id':key,'payload':value})
            at=db.utc_now()
            retired={**op, 'status':'conflict', 'error_code':'retired_unwritten',
                'needs_reconciliation':False,'retired_at':at,'retired_by':current_user['id'],
                'retirement_source':audit_id,'updated_at':at}
            receipt={'id':op['id'],'at':at,'actor_id':current_user['id'],'source':audit_id,
                'authorization_hash':op['authorization_hash'],'before_operation':op,
                'retired_operation_hash':central.digest(retired),'retirement_binding_hash':_retirement_binding(retired),'proof':proof,
                'retired_projections':snapshots}
            conn.execute("INSERT INTO central_records VALUES('unwritten_task_recovery',?,?,?)",
                (op['id'],json.dumps(receipt,ensure_ascii=False),at))
            for item in snapshots:
                conn.execute('DELETE FROM phase3_local WHERE kind=? AND id=?',(item['kind'],item['id']))
            conn.execute("UPDATE central_records SET payload=?,updated_at=? WHERE kind='operation' AND id=?",
                (json.dumps(retired,ensure_ascii=False),at,op['id']))
