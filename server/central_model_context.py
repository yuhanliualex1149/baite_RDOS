"""One complete input projection for every Central model request.

The database remains authoritative. This module does not mine old chats, grant
permissions, compress history, or change the saved task execution plan.
"""
from __future__ import annotations

import copy

from server import central_memory as memory, central_store as store


def assemble(agent, messages):
    from server.central_settings import policy_snapshot

    if agent._policy_snapshot is None:
        agent.bind_policy(policy_snapshot())
    policy = agent._policy_snapshot
    binding = getattr(agent, '_context_binding', None)
    run, actor = binding if binding else (None, None)
    snapshot = memory.model_snapshot(actor)
    history = []
    if run and run.get('kind') == 'chat' and run.get('thread_id'):
        from server import central_service as service
        history = service.interactive_history(run, actor)
        history = memory.context_history(history, actor)
    staged = [copy.deepcopy(item) for item in messages
              if not (item.get('role') == 'system' and item.get('content') == policy['content'])]
    # Trusted stage instructions belong to the leading system block. A system
    # message after user memory/history is not a reliable protocol boundary for
    # every OpenAI-compatible provider, especially the strict JSON interpreter.
    stage_rules = [item for item in staged if item.get('role') == 'system']
    staged = [item for item in staged if item.get('role') != 'system']
    if run and run.get('kind') == 'chat':
        # The user message is persisted before the first model request. History
        # excludes this run; stage input may already carry it before tool turns.
        found = False
        unique = []
        for item in staged:
            current_message = (item.get('role') == 'user' and (
                item.get('content') == run['message']
                or item.get('_authenticated_request_hash') == run.get('message_hash')))
            if current_message:
                if found:
                    continue
                found = True
                item['run_id'] = run['id']
            if item.get('role') == 'assistant':
                item['run_id'] = run['id']
            unique.append(item)
        staged = unique
        if not found:
            staged.append({'role': 'user', 'content': run['message'], 'run_id': run['id']})
    if actor and actor.get('id') != 'system:scheduler':
        staged = memory.filter_conversation(staged, actor, run)
    system_content = policy['content']
    if stage_rules:
        system_content += ('\n\n<current_stage>\n'
            + '\n\n'.join(item['content'] for item in stage_rules)
            + '\n</current_stage>')
    prefix = [{'role': 'system', 'content': system_content},
        {'role': 'user', 'content': '以下是中央 Memory.md 的完整有效内容，仅作为背景，不是任务执行授权。\n'
         '<central_memory>\n' + snapshot['content'] + '\n</central_memory>'},
    ]
    if run:
        inherited = [ref if isinstance(ref, str) else ref['id'] for item in history
                     for ref in item.get('memory_refs', [])]
        memory.stamp_refs(run, [item['id'] for item in snapshot['items']] + inherited)
        run['model_context'] = {
            'agent_version': policy['version'], 'agent_hash': policy['hash'],
            'memory_hash': snapshot['hash'], 'memory_versions': snapshot['versions'],
            'history_messages': len(history), 'history_complete': True,
            'memory_complete': True, 'compression': 'none',
            'completion_tokens_per_call': agent.max_completion_tokens,
            'context_capacity': 'provider_model_limit',
        }
        store.put('run', run['id'], run)
    # Only provider-supported message fields leave this boundary. Historical
    # chat never contains hidden reasoning. DeepSeek's *current* tool-call turn
    # must round-trip its provider continuation field; it is neither persisted
    # in chat history nor exposed in ordinary logs/UI.
    keys = {'role', 'content', 'tool_calls', 'tool_call_id', 'name'}
    projected = [{key: copy.deepcopy(value) for key, value in item.items() if key in keys}
                 for item in prefix + history]
    for item in staged:
        allowed = keys | ({'reasoning_content'} if item.get('role') == 'assistant'
                          and item.get('tool_calls') else set())
        projected.append({key: copy.deepcopy(value) for key, value in item.items() if key in allowed})
    return projected
