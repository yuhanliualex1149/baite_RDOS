"""Pure selection of peer task physical rows. No web, model or crypto imports."""

class PeerRecordError(ValueError):
    def __init__(self,code,message):
        self.code=code
        super().__init__(message)

def select_task_rows(values):
    if not values:return []
    if not any(value.get('dispatch_source')=='peer' for value in values):return values
    # Ordinary legacy Feishu writes do not depend on the peer service or DB.
    from server import central_store as central, phase3_store as local, phase3_backend as backends
    taskid=values[0].get('task_id')
    canonical_row=local.get('peer_canonical',taskid)
    if canonical_row:
        chosen=[v for v in values if v.get('record_id')==canonical_row['record_id']]
        if not chosen:raise PeerRecordError('task_missing','已接纳互派任务的主记录消失')
        for value in values:
            if value in chosen:continue
            if value.get('record_revision')!=1 or central.digest(backends.payload(value))!=canonical_row['creation_hash']:
                raise PeerRecordError('task_conflict','相同任务ID出现异内容物理记录')
        return chosen
    if any(backends.payload(value)!=backends.payload(values[0]) for value in values[1:]):
        raise PeerRecordError('task_conflict','相同互派任务ID出现不同内容')
    return [min(values,key=lambda value:value.get('record_id',''))]


def event_readback_payload(value, registry):
    """Normalize only the public peer event decoder's top-level identity default.

    The wire decoder enriches these public records with a runtime identity mode.
    Keep scope, proof, and nested verified actors intact; a conflicting explicit
    mode is a genuine mismatch rather than metadata to discard.
    """
    import copy
    meta = {'record_id', 'source_url', 'readback_verified', 'source_environment', 'remote_actor'}
    result = {k: copy.deepcopy(v) for k, v in value.items() if k not in meta and not k.startswith('_')}
    if result.get('event_type') in {'node_registry_snapshot', 'peer_enrollment_request', 'peer_capability_ack'}:
        result.setdefault('identity_mode', registry.get('identity_mode', 'role_test'))
    return result
