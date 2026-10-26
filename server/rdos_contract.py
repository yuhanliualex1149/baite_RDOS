"""Public business contract; deployment identity remains in the private registry.

This adapter changes representation, never task authority or requirements.
"""
from __future__ import annotations

import copy
import os

PROTOCOL_VERSION = 2
PUBLIC_PROJECT_ID = 'project_rdos'
PUBLIC_WORKSPACE_ID = 'ws_rdos'
DEFAULT_NODES = (
    ('node_lab_phase3_codex', 'node_codex'),
    ('node_lab_phase3_workbuddy', 'node_workbuddy'),
)
PRIVATE_KEYS = frozenset({'env_id', 'generation', 'identity_mode', 'source_environment', 'remote_actor'})


def identifiers(registry=None):
    registry = registry or {}
    if os.environ.get('RDOS_RUNTIME_MODE') == 'production':
        # Formal identifiers are already business identifiers. A missing
        # optional registry must never route project_rdos back into the lab.
        pairs = {registry.get('default_project_id', PUBLIC_PROJECT_ID): PUBLIC_PROJECT_ID}
    else:
        pairs = {registry.get('default_project_id', 'lab_project_phase3_collab'): PUBLIC_PROJECT_ID,
                 'mem_lab_admin_0001': 'member_admin', 'mem_lab_codex_0001': 'member_codex',
                 'mem_lab_workbuddy_0001': 'member_workbuddy'}
        pairs.update(DEFAULT_NODES)
    for node in registry.get('nodes', []):
        if node.get('node_id') and node.get('public_id'):
            pairs[node['node_id']] = node['public_id']
    return pairs


def _map(value, mapping, *, public=False):
    if isinstance(value, dict):
        return {key: _map(item, mapping, public=public) for key, item in value.items()
                if not (public and (key in PRIVATE_KEYS or key.startswith('_')))}
    if isinstance(value, list):
        return [_map(item, mapping, public=public) for item in value]
    if isinstance(value, str):
        return mapping.get(value, value)
    return copy.deepcopy(value)


def public_view(value, registry=None):
    return _map(value, identifiers(registry), public=True)


def internal_input(value, registry=None):
    return _map(value, {v: k for k, v in identifiers(registry).items()})


def encode(value, registry):
    result = public_view(value, registry)
    result.update(protocol_version=PROTOCOL_VERSION,
                  workspace_id=registry.get('workspace_id', PUBLIC_WORKSPACE_ID))
    return result


def decode(value, registry):
    projects = {PUBLIC_PROJECT_ID}
    for project in registry.get('projects', []):
        projects.add(project.get('public_id', project.get('id')))
    if (not isinstance(value, dict) or value.get('protocol_version') != PROTOCOL_VERSION
            or value.get('workspace_id') != registry.get('workspace_id', PUBLIC_WORKSPACE_ID)
            or value.get('project_id') not in projects
            or any(key in value for key in PRIVATE_KEYS)):
        raise ValueError('业务协议版本或工作区不匹配')
    result = internal_input(value, registry)
    result.pop('protocol_version', None)
    result.pop('workspace_id', None)
    result.update(env_id=registry['env_id'], generation=registry['generation'])
    if 'event_type' in result:
        identity_mode = registry.get('identity_mode', 'role_test')
        result['identity_mode'] = identity_mode
        if isinstance(result.get('verified_actor'), dict):
            result['verified_actor']['identity_mode'] = identity_mode
    return result
