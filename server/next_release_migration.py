"""Idempotent database-local configuration upgrade; never calls Feishu or a model."""
from __future__ import annotations

import copy
from server import central_memory as memory, central_settings as settings, central_store as store, db

MIGRATION = 'next_fixes_120_business_rules_v2'
CONTEXT_RULE = ('每次中央模型调用使用完整 Agent.md、完整有效 Memory.md 和当前对话全部历史；'
                '不按相关性或最近条数裁剪，不自动压缩。历史与记忆只提供背景，不能代替当前执行授权。')
RULE = ('RDOS 不设置质量审阅或验收流程；审阅意见属于普通任务。有效执行结果与独立读回后结束任务，'
        '管理员也可标记完成，不能伪造节点回执。节点完成安装、定时安排及登记申请后，'
        '中央登记处理和首次自然触发另行跟踪，不要求重做已完成工作。')


def rewrite(content):
    pairs = {
        '，人工质量验收规则不变': '',
        '正常运行按权限和相关性检索少量记忆，不同时加载整个历史和 memory.md。': CONTEXT_RULE,
        '执行完成、回执已读回和质量评价分别记录；只有发布时明确要求人工质量验收的任务才交管理员判断。':
            '执行完成与回执已读回分别记录。RDOS 不设置质量审阅或验收流程。',
        'Review 是审阅任务类别，其意见保存在协作记录中，不自动产生人工批准或人工待办。':
            '审阅材料并提交意见属于普通任务，意见保存在协作记录中。',
        '不产生人工质量验收，也不自动成为管理员待办': '不自动成为管理员待办',
        '专业质量和人的验收另行说明。成果存在、技术核对未完成和质量未判断是三件事。':
            '成果存在与技术核对未完成分别说明。RDOS 不代替团队判断专业成果质量。',
        '只有任务明确约定人工质量验收时才等待对应决定': '任务执行不设置质量验收门槛',
    }
    for old, new in pairs.items():
        content = content.replace(old, new)
    return content


def policy_content(content):
    content = rewrite(content)
    if RULE not in content:
        content += '\n\n' + RULE
    if CONTEXT_RULE not in content:
        content += '\n\n' + CONTEXT_RULE
    return content


def initialize(*, regenerate_views=True):
    """Keep the previous versions and mark the real release-migration source."""
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if memory._get(conn, 'state', MIGRATION):
            return {'reused': True}
        now = db.utc_now()
        policy = memory._get(conn, 'state', 'central_agent_policy')
        changed = []
        if policy:
            content = policy_content(policy['content'])
            if content != policy['content']:
                memory._put(conn, 'central_agent_policy_history', str(policy['version']), policy)
                policy.update(content=content, version=policy['version'] + 1,
                              hash=store.digest(content), updated_at=now,
                              source_kind='release_migration', migration_id=MIGRATION)
                memory._put(conn, 'state', 'central_agent_policy', policy)
                memory._put(conn, 'central_agent_policy_history', str(policy['version']), policy)
        for item in memory._all(conn, 'central_memory'):
            if item.get('status') != 'active':
                continue
            content = rewrite(item['content'])
            if content == item['content']:
                continue
            before = copy.deepcopy(item)
            memory._history(conn, before)
            item.update(content=content, version=item['version'] + 1, updated_at=now,
                        change_kind='release_migration', migration_id=MIGRATION)
            item.setdefault('sources', []).append({'source_kind': 'release_migration',
                                                  'migration_id': MIGRATION, 'at': now})
            memory._put(conn, 'central_memory', item['id'], item)
            memory._history(conn, item)
            changed.append(item['id'])
        memory._put(conn, 'state', MIGRATION, {'at': now, 'memory_ids': changed,
                                             'source_kind': 'release_migration'})
    # The database is authoritative. Initialization regenerates fixed views and
    # exposes its existing file-error state rather than altering external data.
    if regenerate_views:
        settings.initialize()
        memory.initialize()
    return {'reused': False, 'memory_ids': changed}
