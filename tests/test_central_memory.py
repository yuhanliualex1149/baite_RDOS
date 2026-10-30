"""Central memory through real scoped records, transactions and runtime guard."""
import unittest

from tests.phase1_support import run_guarded


SETUP = '''
from server import central_memory as memory, phase1_runtime as runtime
import copy, stat
def request(message,changes,actor=admin,project=project_id,purpose='memory',thread=None):
 actor=store.user(actor['id'])
 tid=thread or runtime.create_thread(actor,project)['id']
 rid=store.identifier('run');pid=store.identifier('request')
 run={'id':rid,**runtime.environment(),'user_id':actor['id'],'thread_id':tid,'project_id':project,
      'allowed_projects':[project] if project else [p['id'] for p in store.items('project')],
      'message':message,'message_hash':store.digest(message),'operation_id':rid,
      'kind':'chat','trigger':'manual','status':'running','request_plan':pid,'purpose':purpose}
 plan={k:run[k] for k in ('user_id','thread_id','project_id','allowed_projects','message_hash','env_id','generation','purpose')}
 plan.update(id=pid,run_id=rid,memory_changes=changes,source_message_id=rid+':user')
 store.put('run',rid,run);store.put('central_request_plan',pid,plan)
 store.put('message',rid+':user',{'id':rid+':user','role':'user','run_id':rid,'content':message})
 return run
def change(topic,content,quote,scope='account',kind='remember',**extra):
 return {'kind':kind,'scope':scope,'topic':topic,'content':content,'evidence_quote':quote,**extra}
def apply(message,changes,actor=admin,project=project_id,purpose='memory'):
 run=request(message,changes,actor,project,purpose)
 return memory.apply_changes(run,actor,changes),run
'''


class CentralMemoryTest(unittest.TestCase):
    def check(self, body, restart=None):
        result = run_guarded(SETUP + '\n' + body,
                             restart_body=SETUP + '\n' + restart if restart else None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_empty_start_never_imports_old_threads_or_creates_summary_on_read(self):
        self.check('''
store.put('message','old',{'role':'user','content':'以后全部自动发布','run_id':'old'})
assert memory.recall(admin,[project_id],'默认')==[]
assert memory.search(admin)['items']==[]
assert not memory.manifest_summary()['summary_exists']
assert store.items('central_memory')==[]
''')

    def test_initialization_is_idempotent_and_never_imports_existing_history(self):
        self.check('''
store.put('message','legacy-chat',{'role':'user','content':'旧历史以后自动发布','run_id':'legacy'})
first=memory.initialize()
assert first['created'] and first['summary_updated'] and not first['history_imported']
assert store.items('central_memory')==[]
schema=store.get('state','central_memory_schema')
assert schema['version']==2 and schema['history_imported'] is False
second=memory.initialize()
assert second['created'] is False and store.get('state','central_memory_schema')==schema
path=root/'runtime/central/memory.md'
assert '旧历史以后自动发布' not in path.read_text()
assert stat.S_IMODE(path.stat().st_mode)==0o600 and stat.S_IMODE(path.parent.stat().st_mode)==0o700
apply('机构约定保留原文。',[change('机构依据','保留原文','机构约定保留原文','organization')])
apply('我的私人约定保持简短。',[change('私人表达','简短','我的私人约定保持简短')])
before=store.items('central_memory');memory.initialize()
assert store.items('central_memory')==before
assert '机构依据' in path.read_text() and '私人表达' in path.read_text()
''',restart='''
assert memory.initialize()['created'] is False
assert store.get('state','central_memory_schema')['version']==2
assert len(store.items('central_memory'))==2
body=(root/'runtime/central/memory.md').read_text()
assert '机构依据' in body and '私人表达' in body and '旧历史以后自动发布' not in body
''')

    def test_initialization_summary_failure_is_non_blocking_and_retryable(self):
        self.check('''
before=store.items('task')
original=memory._write_summary
def failed():raise OSError('sensitive-path')
memory._write_summary=failed
result=memory.initialize()
assert result['created'] and not result['summary_updated'] and result['summary_error']
assert 'sensitive-path' not in json.dumps(result)
assert store.get('state','central_memory_schema')['version']==2
assert store.items('task')==before and store.items('central_memory')==[]
memory._write_summary=original
retry=memory.initialize()
assert retry['created'] is False and retry['summary_updated']
''')

    def test_full_view_and_missing_file_recovery_share_complete_content(self):
        self.check('''
memory.initialize()
for index in range(12):
 memory.apply_settings(admin,{'kind':'remember','topic':'持续约定'+str(index),
  'content':('保留完整业务约定。'*160)+str(index),'scope':'organization'},'complete-view-'+str(index),0)
expected=memory.model_snapshot(admin)['content']
assert len(expected)>12000 and '持续约定11' in expected
assert memory._summary_content()==expected
path=root/'runtime/central/memory.md'
assert path.read_text()==expected
path.unlink()
assert memory._create_summary_if_missing() and path.read_text()==expected
path.write_text('保留并发或人工写入的文件。')
assert not memory._create_summary_if_missing()
assert path.read_text()=='保留并发或人工写入的文件。'
''')

    def test_explicit_memory_deduplication_replacement_and_restart_recall(self):
        self.check('''
c=[change('表达','先给结论','以后先给结论')]
first,run=apply('以后先给结论。',c)
key=first['changes'][0]['id']
memory.stamp_refs(run,[key]);store.put('run',run['id'],run)
again=memory.apply_changes(run,admin,c)
assert again['changes'][0]['reused'] and len(store.items('central_memory'))==1
new,_=apply('以后先给结果和来源。',[change('表达','先给结果和来源','以后先给结果和来源')])
assert new['changes'][0]['id']==key and new['changes'][0]['version']==2
history=memory.search(admin,'表达',include_history=True)['history']
assert {i['content'] for i in history}=={'先给结论','先给结果和来源'}
assert memory.recall(admin,[project_id],'表达')[0]['content']=='先给结果和来源'
projected=memory.context_history([{'role':'user','content':run['message']},
 {'role':'assistant','run_id':run['id'],'content':'我会先给结论。'}],admin)
assert len(projected)==1 and '以后先给结论' not in projected[0]['content']
''',restart='''
assert memory.recall(admin,[project_id],'表达')[0]['content']=='先给结果和来源'
assert len(store.items('central_memory'))==1
''')

    def test_shared_memory_is_usable_across_admins_without_expanding_researcher_or_raw_sources(self):
        self.check('''
other=store.create_user('admin-b','另一位管理员',['admin'],'temporary-admin-password')['user']
reader=store.create_user('research-b','研发',['researcher'],'temporary-reader-password')['user']
apply('我的回复只要三句。',[change('个人主题A','三句','我的回复只要三句')])
apply('机构约定保留来源。',[change('机构约定','保留来源','机构约定保留来源','organization')])
apply('本项目本周先不做Review。',[change('阶段决定','本周先不做Review','本项目本周先不做Review','project')])
own=memory.search(admin)['items'];foreign=memory.search(other)['items'];limited=memory.search(reader)['items']
assert len(own)==3 and len(foreign)==3 and len(limited)==1
assert limited[0]['scope']=='organization'
assert any(i['topic']=='个人主题A' for i in foreign)
org=next(i for i in foreign if i['scope']=='organization')
assert all('quote' not in s and 'message_id' not in s for s in org['sources'])
assert len(memory.inspection_metadata(reader,[]))==1
assert len(memory.search(admin,allowed_projects=[])['items'])==2
try:memory.recall(reader,[project_id],'阶段')
except memory.MemoryError as exc:assert exc.status==403
else:raise AssertionError('project permission bypass')
''')

    def test_inferred_candidates_never_override_confirmed_memory(self):
        self.check('''
first,_=apply('我喜欢短回复。',[change('表达','短回复','我喜欢短回复',evidence_kind='inferred')])
assert first['changes'][0]['status']=='candidate' and memory.recall(admin,[],'表达')==[]
active,_=apply('以后都短回复。',[change('表达','短回复','以后都短回复')])
assert active['changes'][0]['status']=='active'
apply('这一次我又问了很多细节。',[change('表达','详细回复','这一次我又问了很多细节',evidence_kind='inferred')])
assert memory.recall(admin,[],'表达')[0]['content']=='短回复'
assert any(i['status']=='candidate' for i in memory.search(admin)['items'])
''')

    def test_forget_removes_recall_summary_and_related_history_without_business_rewrite(self):
        self.check('''
c=[change('表达','只给三句','以后只给三句')]
first,original=apply('这份任务继续讨论。以后只给三句。',c)
key=first['changes'][0]['id'];memory.stamp_refs(original,[key]);store.put('run',original['id'],original)
store.put('operation','business-done',{'status':'verified','result':'成果存在'})
before=store.get('operation','business-done')
forget,forget_run=apply('忘掉表达约定。',[change('表达','','忘掉表达约定',kind='forget')])
assert forget['changes'][0]['status']=='forgotten'
assert memory.recall(admin,[project_id],'表达')==[]
assert memory.search(admin,include_history=True)['history']==[]
history=[{'id':original['id']+':user','role':'user','run_id':original['id'],'content':original['message']},
 {'role':'assistant','run_id':original['id'],'content':'我会只给三句。'},
 {'role':'user','content':'继续核对成果位置。'}]
projected=memory.context_history(history,admin)
assert len(projected)==2 and '以后只给三句' not in json.dumps(projected,ensure_ascii=False)
assert '这份任务继续讨论' in projected[0]['content']
assert history[0]['content']==original['message']
assert store.get('operation','business-done')==before
conversation=[{'role':'assistant','run_id':original['id'],'content':None,'tool_calls':[{'id':'old-call'}]},
 {'role':'tool','tool_call_id':'old-call','content':'只给三句'},
 {'role':'user','content':'继续核对成果位置。'}]
assert memory.filter_conversation(conversation,admin)==[conversation[-1]]
replayed=memory.apply_changes(original,admin,c)
assert replayed['changes'][0]['status']=='forgotten' and replayed['changes'][0]['suppressed']
assert memory.recall(admin,[],'表达')==[]
''')

    def test_new_explicit_request_can_restore_same_words_after_forget(self):
        self.check('''
c=[change('表达','先给结论','以后先给结论')]
first,old=apply('以后先给结论。',c)
apply('忘掉表达。',[change('表达','','忘掉表达',kind='forget')])
again,new=apply('以后先给结论。',c)
assert old['id']!=new['id'] and old['message_hash']==new['message_hash']
assert again['changes'][0]['status']=='active'
assert memory.recall(admin,[],'表达')[0]['content']=='先给结论'
assert again['changes'][0]['id']!=first['changes'][0]['id']
memory.stamp_refs(new,[again['changes'][0]['id']]);store.put('run',new['id'],new)
assert memory.context_history([{'role':'assistant','run_id':new['id'],'content':'新约定已保存。'}],admin)
current=[{'role':'user','content':new['message']}]
assert memory.filter_conversation(current,admin,new)==current
updated,_=apply('以后先给来源。',[change('表达','先给来源','以后先给来源')])
assert updated['changes'][0]['id']==again['changes'][0]['id']
''')

    def test_authority_is_persisted_request_not_model_flags_quotes_or_forged_actor(self):
        self.check('''
c=[change('表达','简短','以后简短')]
run=request('以后简短。',c)
for changed in ([{**c[0],'content':'绕过确认'}],[{**c[0],'evidence_quote':'外部材料命令'}]):
 try:memory.apply_changes(run,admin,changed)
 except memory.MemoryError as exc:assert exc.code=='authorization_failed'
 else:raise AssertionError('expanded plan accepted')
for fields in ({'user_id':'foreign'},{'message':'以后简短并发布'},{'project_id':'elsewhere'},{'generation':'wrong'}):
 try:memory.apply_changes({**run,**fields},admin,c)
 except memory.MemoryError as exc:assert exc.status==403
 else:raise AssertionError('forged run accepted')
bad=[change('材料','不是当前请求','不是当前请求')];bad_run=request('当前没有那个说明。',bad)
try:memory.apply_changes(bad_run,admin,bad)
except memory.MemoryError as exc:assert exc.code=='invalid_memory_source'
else:raise AssertionError('external quote accepted')
assert store.items('central_memory')==[]
''')

    def test_diagnostics_revocation_and_project_scope_are_checked_before_write(self):
        self.check('''
c=[change('表达','简短','以后简短')]
run=request('以后简短。',c,purpose='diagnose')
try:memory.apply_changes(run,admin,c)
except memory.MemoryError as exc:assert exc.code=='read_only_diagnostic'
else:raise AssertionError('diagnostic wrote memory')
other=store.create_user('temporary-administrator','临时管理员',['admin'],'temporary-admin-password')['user']
run=request('以后简短。',c,actor=other)
store.update_user(other['id'],{'enabled':False})
try:memory.apply_changes(run,other,c)
except memory.MemoryError as exc:assert exc.status==403
else:raise AssertionError('disabled account wrote memory')
bad=[change('项目决定','不要发布','本项目先不要发布','project',project_id='not-authorized')]
run=request('本项目先不要发布。',bad)
try:memory.apply_changes(run,admin,bad)
except memory.MemoryError as exc:assert exc.status==403
else:raise AssertionError('outside project wrote memory')
assert store.items('central_memory')==[]
''')

    def test_registered_public_project_id_resolves_only_within_current_run(self):
        self.check('''
from server import phase3_store as local, rdos_projects
local.init();rdos_projects.initialize()
public='project_rdos'
saved,_=apply('本项目交付先讲结论。',[change('交付表达','先讲结论','本项目交付先讲结论','project',project_id=public)],project=local.PROJECT_ID)
item=store.get('central_memory',saved['changes'][0]['id'])
assert item['project_id']==local.PROJECT_ID
before=store.items('central_memory')
bad=[change('其他项目','不能写入','本项目不能写入','project',project_id=public)]
run=request('本项目不能写入。',bad,project=project_id)
try:memory.apply_changes(run,admin,bad)
except memory.MemoryError as exc:assert exc.code=='authorization_failed'
else:raise AssertionError('public project alias expanded run scope')
assert store.items('central_memory')==before
''')

    def test_summary_is_fixed_atomic_and_contains_shared_and_project_memory(self):
        self.check('''
apply('我的私人偏好是先给结论。',[change('私人主题','先给结论','我的私人偏好是先给结论')])
apply('本项目暂停Review。',[change('项目主题','暂停Review','本项目暂停Review','project')])
apply('机构约定引用原文。',[change('机构主题','引用原文','机构约定引用原文','organization')])
path=root/'runtime/central/memory.md'
body=path.read_text();assert '机构主题' in body and '引用原文' in body
assert '私人主题' in body and '项目主题' in body
assert stat.S_IMODE(path.stat().st_mode)==0o600
assert not path.with_name('memory.md.tmp').exists()
assert memory.manifest_summary()['active_count']==3
apply('忘掉机构主题。',[change('机构主题','','忘掉机构主题','organization',kind='forget')])
assert '机构主题' not in path.read_text()
''')

    def test_summary_failure_keeps_database_success_and_does_not_touch_business(self):
        self.check('''
store.put('operation','task-written',{'status':'verified'})
original=memory._write_summary
def failed():raise OSError('sensitive-path-and-token')
memory._write_summary=failed
saved,run=apply('以后先给结论。',[change('表达','先给结论','以后先给结论')])
assert saved['ok'] and not saved['summary_updated']
assert 'sensitive-path-and-token' not in json.dumps(saved)
assert memory.recall(admin,[],'表达')[0]['content']=='先给结论'
assert store.get('operation','task-written')=={'status':'verified'}
memory._write_summary=original
reused=memory.apply_changes(run,admin,[change('表达','先给结论','以后先给结论')])
assert reused['summary_updated'] and reused['changes'][0]['reused']
''')

    def test_forget_and_correction_summary_failure_retires_stale_file(self):
        self.check('''
old='SUMMARY_FORGOTTEN_BODY_SENTINEL'
apply('机构约定'+old,[change('机构摘要',old,'机构约定'+old,'organization')])
path=root/'runtime/central/memory.md'
assert old in path.read_text()
original=memory._write_summary
def failed():raise OSError('private-summary-failure')
memory._write_summary=failed
forgot,_=apply('忘掉机构摘要。',[change('机构摘要','','忘掉机构摘要','organization',kind='forget')])
assert forgot['ok'] and forgot['summary_removed'] and forgot['summary_stale'] is False
assert not path.exists() and memory.recall(admin,[],'机构摘要')==[]
assert 'private-summary-failure' not in json.dumps(forgot)
memory._write_summary=original
apply('机构表达保留旧样式。',[change('机构表达','旧样式','机构表达保留旧样式','organization')])
assert '旧样式' in path.read_text()
memory._write_summary=failed
corrected,_=apply('机构表达改为新样式。',[change('机构表达','新样式','机构表达改为新样式','organization',kind='correct')])
assert corrected['ok'] and corrected['summary_removed'] and not path.exists()
assert memory.recall(admin,[],'机构表达')[0]['content']=='新样式'
memory._write_summary=original
''')

    def test_summary_cleanup_failure_is_reported_as_stale_without_undoing_forget(self):
        self.check('''
apply('机构表达保留原文。',[change('机构表达','保留原文','机构表达保留原文','organization')])
path=root/'runtime/central/memory.md'
write,discard=memory._write_summary,memory._discard_summary
def failed():raise OSError('sensitive-cleanup-path')
memory._write_summary=failed;memory._discard_summary=failed
result,_=apply('忘掉机构表达。',[change('机构表达','','忘掉机构表达','organization',kind='forget')])
assert result['ok'] and result['summary_stale'] and not result['summary_removed']
assert '旧摘要可能仍含过期内容' in result['summary_error']
assert '已停用' not in result['summary_error'] and 'sensitive-cleanup-path' not in json.dumps(result)
assert path.exists() and memory.recall(admin,[],'机构表达')==[]
memory._write_summary=write;memory._discard_summary=discard
memory.initialize()
assert '机构表达' not in path.read_text()
''')

    def test_recall_is_bounded_and_transaction_rolls_back_entire_invalid_change_batch(self):
        self.check('''
for number in range(12):
 text='以后主题'+str(number)+'照此办理'
 apply(text,[change('主题'+str(number),'有界约定'+str(number),text)])
assert len(memory.recall(admin,[project_id],'主题'))==8
assert len(memory.recall(admin,[project_id],'主题',limit=3))==3
for number in range(3):
 quote='这是同一约定的又一次明确来源'+str(number)
 apply(quote,[change('主题0','有界约定0',quote)])
recalled=next(i for i in memory.recall(admin,[project_id],'主题0') if i['topic']=='主题0')
assert len(recalled['sources'])==2 and all('quote' not in item for item in recalled['sources'])
before=store.items('central_memory')
c=[change('新增主题','可以保存','这条可以保存'),change('第二项','非法引用','不存在的原文')]
run=request('这条可以保存。',c)
try:memory.apply_changes(run,admin,c)
except memory.MemoryError as exc:assert exc.code=='invalid_memory_source'
else:raise AssertionError('invalid batch committed')
assert store.items('central_memory')==before
''')

    def test_cross_admin_conflict_requires_explicit_correction(self):
        self.check('''
apply('机构回复保持简短。',[change('表达','简短','机构回复保持简短','organization')])
other=store.create_user('admin-b','管理员乙',['admin'],'temporary-admin-password')['user']
incoming=[change('表达','详细','机构回复一律详细','organization')]
run=request('机构回复一律详细。',incoming,actor=other)
try:memory.apply_changes(run,other,incoming)
except memory.MemoryError as exc:assert exc.code=='memory_conflict'
else:raise AssertionError('last writer silently replaced other authority')
apply('把表达约定改为详细。',[change('表达','详细','把表达约定改为详细','organization',kind='correct')],actor=other)
assert memory.recall(other,[],'表达')[0]['content']=='详细'
''')

    def test_replace_and_invalidate_preserve_explicit_history_without_forget_revival(self):
        self.check('''
saved,first=apply('以后先给结论。',[change('表达','先给结论','以后先给结论')])
key=saved['changes'][0]['id']
replaced,_=apply('将表达约定替换为先给来源。',[change('表达','先给来源','将表达约定替换为先给来源',kind='replace')])
assert replaced['changes'][0]['kind']=='replace' and replaced['changes'][0]['id']==key
assert memory.recall(admin,[],'表达')[0]['content']=='先给来源'
inactive,run=apply('这项表达约定暂时失效。',[change('表达','','这项表达约定暂时失效',kind='invalidate')])
assert inactive['changes'][0]['status']=='inactive'
assert memory.recall(admin,[],'表达')==[] and memory.search(admin,'表达')['items']==[]
old=memory.search(admin,'表达',include_history=True)['history']
assert {i['content'] for i in old}=={'先给结论','先给来源'}
assert not store.items('central_memory_suppression')
assert '以后先给结论' not in json.dumps(memory.context_history([{'role':'user','content':first['message']}],admin),ensure_ascii=False)
apply('忘掉表达约定。',[change('表达','','忘掉表达约定',kind='forget')])
apply('原记录继续保持失效。',[change('表达','','原记录继续保持失效',kind='invalidate',memory_id=key)])
assert memory.search(admin,include_history=True)['history']==[]
''')

    def test_deictic_change_resolves_unique_same_thread_memory_and_actual_scope(self):
        self.check('''
saved,first=apply('机构约定先给结论。',[change('表达','先给结论','机构约定先给结论','organization')])
c=[change('','','忘掉刚才那条',kind='forget')]
run=request('忘掉刚才那条。',c,thread=first['thread_id'])
result=memory.apply_changes(run,admin,c)
assert result['changes'][0]['id']==saved['changes'][0]['id']
assert store.get('central_memory',saved['changes'][0]['id'])['scope']=='organization'
assert memory.search(admin)['items']==[]
''')

    def test_deictic_multiple_displayed_memories_require_clarification(self):
        self.check('''
saved,first=apply('我的回复先给结论。',[change('表达','先给结论','我的回复先给结论')])
apply('我的依据保留原文。',[change('依据','保留原文','我的依据保留原文')])
shown=request('你记得什么？',[],thread=first['thread_id'])
memory.stamp_displayed(shown,memory.search(admin)['items']);store.put('run',shown['id'],shown)
c=[change('','','忘掉刚才那条',kind='forget')]
run=request('忘掉刚才那条。',c,thread=first['thread_id'])
try:memory.apply_changes(run,admin,c)
except memory.MemoryError as exc:assert exc.code=='memory_ambiguous'
else:raise AssertionError('ambiguous displayed memory guessed')
assert len(memory.search(admin)['items'])==2
foreign=request('忘掉刚才那条。',c)
try:memory.apply_changes(foreign,admin,c)
except memory.MemoryError as exc:assert exc.code=='memory_ambiguous'
else:raise AssertionError('other thread used as authority')
''')

    def test_deictic_displayed_version_conflict_does_not_apply_old_reference(self):
        self.check('''
saved,first=apply('以后先给结论。',[change('表达','先给结论','以后先给结论')])
shown=request('你记得表达约定吗？',[],thread=first['thread_id'])
memory.stamp_displayed(shown,memory.search(admin,'表达')['items']);store.put('run',shown['id'],shown)
apply('以后先给来源。',[change('表达','先给来源','以后先给来源',kind='correct')])
c=[change('','','忘掉刚才那条',kind='forget')]
run=request('忘掉刚才那条。',c,thread=first['thread_id'])
try:memory.apply_changes(run,admin,c)
except memory.MemoryError as exc:assert exc.code=='memory_conflict'
else:raise AssertionError('stale displayed memory changed')
assert memory.recall(admin,[],'表达')[0]['content']=='先给来源'
''')

    def test_two_admins_share_new_thread_correction_forget_and_restart(self):
        self.check('''
other=store.create_user('shared-admin-b','管理员乙',['admin'],'temporary-admin-password')['user']
saved,origin=apply('以后给人的汇报先讲结果。',[change('汇报表达','先讲结果','以后给人的汇报先讲结果')])
key=saved['changes'][0]['id']
assert store.get('central_memory',key)['scope']=='organization'
found=memory.recall(other,[project_id],'汇报表达')[0]
assert found['id']==key and found['content']=='先讲结果'
assert found['applicability']=={'kind':'account','account_id':admin['id']}
assert found['sources']==[{'user_id':admin['id']}]
assert '以后给人的汇报先讲结果' not in json.dumps(memory.search(other),ensure_ascii=False)
changed,current=apply('把汇报表达改为先讲结果和待决定事项。',
 [change('汇报表达','先讲结果和待决定事项','把汇报表达改为先讲结果和待决定事项',kind='correct')],actor=other)
assert changed['changes'][0]['id']==key and changed['changes'][0]['version']==2
assert store.get('central_memory',key)['applicability']=={'kind':'account','account_id':admin['id']}
assert memory.recall(admin,[],'汇报表达')[0]['content']=='先讲结果和待决定事项'
memory.stamp_refs(current,[key]);store.put('run',current['id'],current)
apply('忘掉汇报表达。',[change('汇报表达','','忘掉汇报表达',kind='forget')])
assert memory.search(other,'汇报表达',include_history=True)['items']==[]
assert memory.search(other,'汇报表达',include_history=True)['history']==[]
assert memory.context_history([{'role':'assistant','run_id':current['id'],
 'content':'先讲结果和待决定事项','memory_refs':[key]}],other)==[]
assert '汇报表达' not in (root/'runtime/central/memory.md').read_text()
replayed=memory.apply_changes(origin,admin,[change('汇报表达','先讲结果','以后给人的汇报先讲结果')])
assert replayed['changes'][0]['suppressed']
''',restart='''
other=next(u for u in store.users() if u['username']=='shared-admin-b')
assert memory.initialize()['schema_version']==2
assert memory.search(other,'汇报表达')['items']==[]
assert memory.recall(admin,[],'汇报表达')==[]
''')

    def test_legacy_account_migration_preserves_id_history_author_and_suppression(self):
        self.check('''
other=store.create_user('legacy-admin-b','管理员乙',['admin'],'temporary-admin-password')['user']
saved,source=apply('以后保留具体依据。',[change('依据','保留具体依据','以后保留具体依据')])
key=saved['changes'][0]['id'];old=store.get('central_memory',key)
old.update(scope='account',account_id=admin['id']);old.pop('audience',None)
store.put('central_memory',key,old)
store.put('central_memory_history',store.digest([key,old['version']]),old)
store.put('state','central_memory_schema',{'version':1,'history_imported':False})
suppression={'id':'legacy-forgotten','scope':'account','account_id':admin['id'],'project_id':'',
 'sources':[{'run_id':'legacy-run','quote_hash':'legacy-quote'}]}
store.put('central_memory_suppression',suppression['id'],suppression)
original_history=store.get('central_memory_history',store.digest([key,old['version']]))
result=memory.initialize();migrated=store.get('central_memory',key)
assert result['migrated_ids']==[key] and migrated['version']==old['version']+1
assert migrated['scope']=='organization' and migrated['account_id']==''
assert migrated['legacy_account_id']==admin['id'] and migrated['sources']==old['sources']
assert store.get('central_memory_history',store.digest([key,old['version']]))==original_history
assert store.get('central_memory_suppression',suppression['id'])==suppression
assert memory.recall(other,[],'依据')[0]['id']==key
before=store.items('central_memory');again=memory.initialize()
assert not again['migrated_ids'] and store.items('central_memory')==before
with db.connection() as conn:
 assert memory._source_suppressed(conn,{'run_id':'legacy-run','quote_hash':'legacy-quote'},'organization','','')
apply('把依据约定替换为标明版本。',[change('依据','标明版本','把依据约定替换为标明版本',kind='replace')],actor=other)
assert memory.recall(admin,[],'依据')[0]['content']=='标明版本'
''',restart='''
assert memory.initialize()['schema_version']==2
item=memory.recall(admin,[],'依据')[0]
assert item['content']=='标明版本' and item['version']==3
''')

    def test_legacy_conflicting_topics_are_not_silently_collapsed_or_overwritten(self):
        self.check('''
other=store.create_user('legacy-conflict-admin','管理员乙',['admin'],'temporary-admin-password')['user']
saved,source=apply('以后汇报保持简短。',[change('表达','简短','以后汇报保持简短')])
old=store.get('central_memory',saved['changes'][0]['id'])
old.update(scope='account',account_id=admin['id']);old.pop('audience',None)
store.put('central_memory',old['id'],old)
second={**old,'id':'legacy-other-expression','account_id':other['id'],'content':'详细','updated_by':other['id']}
store.put('central_memory',second['id'],second)
store.put('state','central_memory_schema',{'version':1,'history_imported':False})
memory.initialize()
assert {item['content'] for item in memory.search(other,'表达')['items']}=={'简短','详细'}
incoming=[change('表达','先讲结果','改为先讲结果',kind='correct')]
run=request('改为先讲结果。',incoming,actor=other)
try:memory.apply_changes(run,other,incoming)
except memory.MemoryError as exc:assert exc.code=='memory_conflict'
else:raise AssertionError('conflicting legacy memories were silently resolved')
assert len(memory.search(admin,'表达')['items'])==2
''')

    def test_equal_legacy_topics_merge_current_view_but_keep_ids_and_forget_sources(self):
        self.check('''
other=store.create_user('legacy-equal-admin','管理员乙',['admin'],'temporary-admin-password')['user']
saved,origin=apply('以后汇报先给结果。',[change('表达','先给结果','以后汇报先给结果')])
first=store.get('central_memory',saved['changes'][0]['id'])
first.update(scope='account',account_id=admin['id']);first.pop('audience',None)
store.put('central_memory',first['id'],first)
second={**first,'id':'legacy-same-expression','account_id':admin['id'],'updated_by':other['id'],
 'sources':[{**first['sources'][0],'run_id':'legacy-other-run','user_id':other['id']}]}
store.put('central_memory',second['id'],second)
memory.initialize()
items=memory.search(other,'表达')['items'];assert len(items)==1
key=items[0]['id'];assert len(store.items('central_memory'))==2
assert {s['user_id'] for s in store.get('central_memory',key)['sources']}=={admin['id'],other['id']}
apply('忘掉表达。',[change('表达','','忘掉表达',kind='forget')],actor=other)
assert memory.search(admin,'表达')['items']==[]
assert memory.search(admin,'表达',include_history=True)['history']==[]
with db.connection() as conn:
 assert memory._source_suppressed(conn,second['sources'][0],'organization','','')
''')


if __name__ == '__main__':
    unittest.main()
