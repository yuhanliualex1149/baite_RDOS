import unittest
from tests.phase1_support import run_guarded


class ReleaseMigrationTest(unittest.TestCase):
    def test_versioned_local_policy_and_memory_migration_preserves_custom_text(self):
        result = run_guarded('''
from server import central_settings as settings, central_memory as memory, next_release_migration as migration
settings.initialize(); memory.initialize()
old='执行完成、回执已读回和质量评价分别记录；只有发布时明确要求人工质量验收的任务才交管理员判断。'
settings.save_agent(admin,'保留本人定制职责。'+old,1,'old-policy')
saved=memory.apply_settings(admin,{'kind':'remember','topic':'任务收尾','content':old,'scope':'organization'},'old-memory',0)
before_tasks=store.items('task');before_messages=store.items('message')
first=migration.initialize()
policy=settings.policy_snapshot()
assert not first['reused'] and policy['version']==3
assert '保留本人定制职责。' in policy['content'] and migration.RULE in policy['content']
assert old not in policy['content'] and policy['source_kind']=='release_migration'
entry=next(item for item in store.items('central_memory') if item['topic']=='任务收尾')
assert entry['version']==2 and old not in entry['content']
assert any(item['content']==old for item in store.items('central_memory_history'))
assert migration.initialize()['reused'] and settings.policy_snapshot()==policy
assert store.items('task')==before_tasks and store.items('message')==before_messages
assert (root/'runtime/central/agent.md').read_text().strip()==policy['content']
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
