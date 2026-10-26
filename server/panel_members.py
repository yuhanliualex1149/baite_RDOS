"""Member cards join accounts to registered nodes without changing node work."""
from __future__ import annotations

import os

from server import central_store as store, phase3_store as local, rdos_projects


def _public_node(node):
    node_id = node.get('public_id') or node['node_id']
    return {
        'node_id': node_id, 'public_id': node_id, 'member_id': node.get('member_id', ''),
        'display_name': node.get('display_name') or node.get('label') or node_id,
        'label': node.get('label') or node.get('display_name') or node_id,
        'enabled': node.get('enabled', True),
        'onboard_url': os.environ.get('BAITE_PUBLIC_URL', '').rstrip('/') + '/nodes/' + node_id + '/start',
    }


def associated_nodes(member_id):
    # Keep registration independent from account enabled state. Deactivating a
    # Panel account neither proves nor requests that its external Agent stopped.
    return [_public_node(node) for node in local.items('node') if node.get('member_id') == member_id]


def users_view(actor):
    rdos_projects._admin(actor)
    return {'items': [{**member, 'nodes': associated_nodes(member['id'])} for member in store.users()]}


def visible_nodes(actor):
    """Registry projection for the project selector; no organization-wide leak."""
    user = rdos_projects._reader(actor)
    nodes = local.node_registry()
    if 'admin' not in user['roles']:
        allowed = set()
        for project_id in rdos_projects.readable_project_ids(user):
            allowed.update(rdos_projects.project_node_ids(project_id))
        nodes = [node for node in nodes if node['node_id'] in allowed]
    return [_public_node(node) for node in nodes]
