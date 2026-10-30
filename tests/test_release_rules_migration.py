"""Explicit release migration on isolated real records; no live resources."""
import subprocess
import sys
import unittest
from pathlib import Path

from tests.phase1_support import run_guarded


HELPER_SOURCE = (Path(__file__).resolve().parents[1] / 'deploy/migrate-next-release.py').read_text()
SETUP = 'HELPER_SOURCE=' + repr(HELPER_SOURCE) + '\n' + '''
import types,copy
from unittest.mock import patch
from server import db, central_settings as settings, central_memory as memory, next_release_migration as migration
helper=types.ModuleType('release_rules_migration')
exec(compile(HELPER_SOURCE,'deploy/migrate-next-release.py','exec'),helper.__dict__)
settings.initialize();memory.initialize()
old_rule='执行完成、回执已读回和质量评价分别记录；只有发布时明确要求人工质量验收的任务才交管理员判断。'
settings.save_agent(admin,'保留本人自定义职责。'+old_rule,1,'release-fixture-policy')
memory.apply_settings(admin,{'kind':'remember','topic':'任务规则','content':old_rule,'scope':'organization'},'release-fixture-rule',0)
memory.apply_settings(admin,{'kind':'remember','topic':'持续经验','content':'保留本人已有经验。','scope':'organization'},'release-fixture-custom',0)
files={'agent':root/'runtime/central/agent.md','memory':root/'runtime/central/memory.md'}
database=db.db_path()
def views():
 return {'agent':(settings.policy_snapshot()['content']+'\\n').encode(),'memory':memory._summary_content().encode()}
def expected():
 values=[copy.deepcopy(item) for item in store.items('central_memory') if item.get('status')=='active']
 for item in values:item['content']=migration.rewrite(item['content'])
 return {'agent':(migration.policy_content(settings.policy_snapshot()['content'])+'\\n').encode(),
  'memory':memory._render_memory(values).encode()}
def run(migrate=None,snapshot=None):
 return helper.run_migration(database,files,snapshot or (lambda:helper.capture(database,files,views)),expected,
  migrate or (lambda:migration.initialize(regenerate_views=False)),views,old_release='a'*40,candidate_release='b'*40)
'''


class ExplicitReleaseMigrationTest(unittest.TestCase):
    def check(self, body):
        result = run_guarded(SETUP + body, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_explicit_migration_updates_only_known_generated_views_and_preserves_records(self):
        self.check('''
before_tasks=store.items('task');before_messages=store.items('message')
targets=expected();result=run()
assert result['completed'] and result['verified_files']==2 and result['preserved_custom_files']==0
assert all(path.read_bytes()==targets[name] for name,path in files.items())
assert '保留本人自定义职责。' in settings.policy_snapshot()['content']
assert next(x for x in store.items('central_memory') if x['topic']=='持续经验')['content']=='保留本人已有经验。'
assert store.items('task')==before_tasks and store.items('message')==before_messages
assert old_rule not in str(views()) and store.get('state',migration.MIGRATION)
assert '保留本人' not in json.dumps(result,ensure_ascii=False)
assert helper._load_plan(database)['old_release']=='a'*40
''')

    def test_manual_files_are_preserved_while_effective_database_rules_are_migrated(self):
        self.check('''
for name,path in files.items():path.write_text('本人手工编辑-'+name)
before={name:path.read_bytes() for name,path in files.items()}
result=run()
assert result['completed'] and result['preserved_custom_files']==2 and result['verified_files']==0
assert all(path.read_bytes()==before[name] for name,path in files.items())
assert migration.RULE in settings.policy_snapshot()['content']
assert old_rule not in memory.model_snapshot(admin)['content']
''')

    def test_database_commit_then_lost_response_resumes_original_plan_once(self):
        self.check('''
def lost():
 migration.initialize(regenerate_views=False)
 raise OSError('synthetic response lost after actual database commit')
try:run(migrate=lost)
except OSError:pass
else:raise AssertionError('fault missing')
assert helper._load_plan(database)['phase']=='prepared' and store.get('state',migration.MIGRATION)
version=settings.policy_snapshot()['version'];history=store.items('central_memory_history')
assert run()['completed']
assert settings.policy_snapshot()['version']==version and store.items('central_memory_history')==history
assert helper._load_plan(database)['candidate_release']=='b'*40
''')

    def test_file_rename_then_lost_progress_resumes_without_rewriting_first_file(self):
        self.check('''
save=helper._save_plan
def lose_progress(database,plan):
 if plan['files']['agent']['state']=='verified':raise OSError('progress save lost after actual file rename')
 return save(database,plan)
with patch.object(helper,'_save_plan',side_effect=lose_progress):
 try:run()
 except OSError:pass
 else:raise AssertionError('fault missing')
assert helper._load_plan(database)['files']['agent']['state']=='pending'
before=helper.file_snapshot(files['agent'])
assert run()['completed'] and helper.file_snapshot(files['agent'])==before
''')

    def test_changed_file_is_preserved_and_original_plan_retained(self):
        self.check('''
def changed():
 migration.initialize(regenerate_views=False)
 files['agent'].write_text('本人在停服窗口新增的意见。')
try:run(migrate=changed)
except helper.MigrationFailure as exc:assert str(exc)=='migration_file_conflict'
else:raise AssertionError('changed file overwritten')
assert files['agent'].read_text()=='本人在停服窗口新增的意见。'
plan=helper._load_plan(database)
assert plan['phase']=='database_migrated' and plan['files']['agent']['error']=='migration_file_conflict'
try:run()
except helper.MigrationFailure:pass
else:raise AssertionError('retry ignored original plan')
assert files['agent'].read_text()=='本人在停服窗口新增的意见。'
''')

    def test_same_bytes_replaced_inode_is_also_a_conflict(self):
        self.check('''
original=files['agent'].read_bytes()
def replaced():
 migration.initialize(regenerate_views=False)
 temporary=files['agent'].with_name('manual-replacement.md');temporary.write_bytes(original)
 os.replace(temporary,files['agent'])
try:run(migrate=replaced)
except helper.MigrationFailure as exc:assert str(exc)=='migration_file_conflict'
else:raise AssertionError('replacement inode accepted')
assert files['agent'].read_bytes()==original
''')

    def test_atomic_write_checks_again_immediately_before_replace(self):
        self.check('''
original=helper.file_snapshot(files['agent']);before=files['agent'].read_bytes();native=helper.file_snapshot
def changed(path):
 if path==files['agent']:path.write_text('another writer during staged file write')
 return native(path)
with patch.object(helper,'file_snapshot',side_effect=changed):
 try:helper._atomic_write(files['agent'],b'new generated view',original)
 except helper.MigrationFailure:pass
 else:raise AssertionError('late write was overwritten')
assert files['agent'].read_text()=='another writer during staged file write'
assert not list(files['agent'].parent.glob('agent.md.release-*'))
''')

    def test_changed_database_result_is_not_projected_over_old_files(self):
        self.check('''
before={name:path.read_bytes() for name,path in files.items()}
def changed():
 migration.initialize(regenerate_views=False)
 value=settings.policy_snapshot();value['content']='本人新增职责。';store.put('state','central_agent_policy',value)
try:run(migrate=changed)
except helper.MigrationFailure as exc:assert str(exc)=='migration_result_conflict'
else:raise AssertionError('unexpected DB result accepted')
assert all(path.read_bytes()==before[name] for name,path in files.items())
assert settings.policy_snapshot()['content']=='本人新增职责。'
''')

    def test_completed_plan_does_not_rewrite_future_user_edits(self):
        self.check('''
assert run()['completed']
files['agent'].write_text('上线后本人新编辑。')
result=run(migrate=lambda:(_ for _ in ()).throw(AssertionError('replayed migration')))
assert result['reused'] and files['agent'].read_text()=='上线后本人新编辑。'
''')

    def test_snapshot_database_conflict_creates_no_plan_or_marker(self):
        self.check('''
def changed_snapshot():
 snapshot=helper.capture(database,files,views)
 value=settings.policy_snapshot();value['content']+='本人新增。';store.put('state','central_agent_policy',value)
 return snapshot
try:run(snapshot=changed_snapshot)
except helper.MigrationFailure as exc:assert str(exc)=='migration_database_conflict'
else:raise AssertionError('stale snapshot accepted')
assert helper._load_plan(database) is None and not store.get('state',migration.MIGRATION)
''')

    def test_database_only_migration_does_not_regenerate_any_file(self):
        self.check('''
for path in files.values():path.write_text('保留手工正文。')
before={name:helper.file_snapshot(path) for name,path in files.items()}
assert not migration.initialize(regenerate_views=False)['reused']
assert all(helper.file_snapshot(path)==before[name] for name,path in files.items())
''')

    def test_command_entry_rejects_any_caller_supplied_path(self):
        helper = Path(__file__).resolve().parents[1] / 'deploy/migrate-next-release.py'
        result = subprocess.run([sys.executable, '-B', str(helper), '/arbitrary/file'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('/arbitrary/file', result.stderr)
        self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
