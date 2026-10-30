"""Local, reversible retirement of unpublished drafts and their authorizations."""
from __future__ import annotations
import copy
import json
from server import db, central_store as store


class DraftError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _write(conn, table, kind, identifier, item):
    conn.execute(f'INSERT INTO {table}(kind,id,payload,updated_at) VALUES(?,?,?,?) '
                 'ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at',
                 (kind, identifier, json.dumps(item, ensure_ascii=False), db.utc_now()))


def _linked_operation(op, kind, item):
    binding = op.get('authorization') or {}
    if kind == 'draft':
        if op.get('action') not in ('phase3_task:start','phase3_task:update'):
            return False
        base = binding.get('base') or {}
        request = {'task_id':item['task_id'], 'action':op['action'].split(':')[-1],
                   'payload':{'draft_id':item['id'],'expected_hash':item['draft_hash']},
                   'actor_id':binding.get('actor_id'), 'expected_revision':base.get('record_revision'),
                   'expected_requirements_version':base.get('requirements_version')}
        return binding.get('input_hash') == store.digest(request)
    return (op.get('id') == item.get('publication_operation_id') or
            (binding.get('draft_hash') == item.get('draft_hash') and
             binding.get('target_id') == item.get('target_id' if kind == 'proposal' else 'task_id')))


async def transition(kind, identifier, action, actor, operation_id, expected_hash):
    """Delete/restore under the publishing lock; invalidate whole pending plans."""
    from server import phase1_runtime as runtime, phase1_operations as writes
    from server.central_service import WRITE_LOCK
    if kind not in {'draft','task_draft','proposal'} or action not in {'delete','restore'}:
        raise DraftError('invalid_action','不支持此草稿操作')
    user = runtime.actor(actor)
    if 'admin' not in user.get('roles',[]):
        raise DraftError('authorization_failed','此操作需要管理员身份')
    table = 'phase3_local' if kind == 'draft' else 'central_records'
    request = {'kind':kind,'id':identifier,'action':action,'actor_id':user['id'],'expected_hash':expected_hash}
    audit_id = 'draft_lifecycle:' + operation_id
    async with WRITE_LOCK:
        if kind == 'draft' and action == 'delete':
            from server.phase3_write_recovery import recover_before_draft_delete
            await recover_before_draft_delete(identifier, expected_hash, actor, audit_id)
        with db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            # Recheck inside the publishing lock and the same transaction, even
            # for institution drafts without a project and idempotent replays.
            from server.central_memory import _actor, MemoryError
            try:
                user = _actor(actor, conn, write=True)
            except MemoryError as exc:
                raise DraftError(exc.code, str(exc)) from exc
            old_audit=conn.execute("SELECT payload FROM central_records WHERE kind='draft_lifecycle' AND id=?",(audit_id,)).fetchone()
            if old_audit:
                saved=json.loads(old_audit[0])
                if saved['request_hash'] != store.digest(request):
                    raise DraftError('operation_conflict','同一操作标识不能用于不同草稿内容')
                return saved['result']
            row=conn.execute(f'SELECT payload FROM {table} WHERE kind=? AND id=?',(kind,identifier)).fetchone()
            if not row:raise DraftError('not_found','草稿不存在')
            item=json.loads(row[0])
            if item.get('draft_hash')!=expected_hash:
                raise DraftError('version_conflict','草稿已变化，请重新查看')
            project_id=item.get('project_id')
            if project_id:
                runtime.project_for_actor(project_id,user)
            operations=[json.loads(row[0]) for row in conn.execute("SELECT payload FROM central_records WHERE kind='operation'")]
            linked=[op for op in operations if _linked_operation(op,kind,item)]
            if action=='delete':
                if item.get('status') not in {'draft','rejected'}:
                    raise DraftError('invalid_transition','只能删除未发布草稿')
                from server.phase3_write_recovery import retired_unwritten
                if item.get('publication') or any((op.get('dispatched_at') or op.get('prepared_fields') or op.get('status')=='succeeded')
                                                and not retired_unwritten(op) for op in linked):
                    raise DraftError('write_uncertain','草稿已经发布或结果待核对，请先核对原发布')
                item.update(status='deleted',deleted_at=db.utc_now(),deleted_by=user['id'],
                            restore_status=item.get('status','draft'))
                for op in linked:
                    _write(conn,'central_records','draft_revoked_operation',op['id'],
                           {'id':op['id'],'draft_id':identifier,'revoked_at':db.utc_now(),'source':audit_id})
                # A multi-action confirmation is indivisible. Never execute the
                # remaining subset after one of its drafts was removed.
                for source_kind in ('central_pending_request','central_request_plan'):
                    records=conn.execute('SELECT id,payload FROM central_records WHERE kind=?',(source_kind,)).fetchall()
                    for record in records:
                        value=json.loads(record['payload'])
                        if any((a.get('payload') or {}).get('draft_id')==identifier for a in value.get('task_actions',[])):
                            value.update(status='invalidated',invalidated_at=db.utc_now(),invalidated_by=audit_id)
                            _write(conn,'central_records',source_kind,record['id'],value)
            else:
                if item.get('status')!='deleted':
                    raise DraftError('invalid_transition','只有已删除草稿可以恢复')
                item.update(status='draft',restored_at=db.utc_now(),restored_by=user['id'])
                # New approval needs a new operation, even for identical prose.
                for key in ('publication_operation_id','confirmed_by','confirmed_at'):
                    item.pop(key,None)
            item['lifecycle_revision']=item.get('lifecycle_revision',0)+1
            _write(conn,table,kind,identifier,item)
            _write(conn,'central_records','draft_lifecycle',audit_id,
                   {'id':audit_id,'request_hash':store.digest(request),'request':request,
                    'at':db.utc_now(),'result':copy.deepcopy(item)})
            return item


def ensure_not_revoked(operation_id):
    if store.get('draft_revoked_operation',operation_id):
        raise DraftError('authorization_failed','原草稿授权已撤销，请重新确认恢复后的草稿')


def migrate_local_drafts():
    """Version local unpublished projections only; never performs Feishu I/O."""
    from server import phase3_service as tasks, central_service
    changed=[]
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        for table,kind in (('phase3_local','draft'),('central_records','task_draft')):
            for row in conn.execute(f'SELECT id,payload FROM {table} WHERE kind=?',(kind,)).fetchall():
                original=json.loads(row['payload'])
                if original.get('status') not in ('draft','deleted','rejected'):continue
                item=copy.deepcopy(original)
                fields=item.get('payload') if kind=='draft' else item
                if not isinstance(fields,dict):continue
                dirty=False
                if fields.get('task_type')=='review':fields['task_type']='action';dirty=True
                if (fields.get('completion_rule') or {}).get('kind') in ('human','human_review'):
                    fields['completion_rule']={'kind':'receipt_recorded'};dirty=True
                if not dirty:continue
                migration_id='workflow-v2:'+kind+':'+row['id']+':'+store.digest(original)
                _write(conn,'central_records','draft_workflow_history',migration_id,
                       {'id':migration_id,'before':original,'at':db.utc_now(),'version':2})
                if kind=='draft':
                    item['draft_hash']=store.digest({key:item[key] for key in ('task_id','payload','base_revision','base_requirements_version','change_reason')})
                else:item['draft_hash']=central_service.task_draft_hash(item)
                item['workflow_version']=2
                _write(conn,table,kind,row['id'],item)
                for source_kind in ('central_pending_request','central_request_plan'):
                    for source in conn.execute('SELECT id,payload FROM central_records WHERE kind=?',(source_kind,)).fetchall():
                        value=json.loads(source['payload'])
                        if any((a.get('payload') or {}).get('draft_id')==row['id'] for a in value.get('task_actions',[])):
                            value.update(status='invalidated',invalidated_at=db.utc_now(),invalidated_by=migration_id)
                            _write(conn,'central_records',source_kind,source['id'],value)
                changed.append({'kind':kind,'id':row['id']})
    return {'version':2,'changed':changed}
