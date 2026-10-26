"""Folder-level Skill snapshots with real SQLite and the unchanged guard."""
import unittest
from tests.phase1_support import run_guarded


SETUP = '''
from server import shared_knowledge as knowledge, knowledge_skill_folders as packages
from server.phase3_feishu import KNOWLEDGE_ROOTS
class Source:
 def __init__(self):
  self.calls=[];self.failed=set();self.root_failure=False
  self.folders=[{'token':token,'name':name,'type':'folder','parent_token':KNOWLEDGE_ROOTS['shared_skills'],
     'content_status':'folder','path':name,'path_segments':[name]} for token,name in [('folderAlpha01','Alpha Skill'),('folderBeta002','Beta Skill'),('folderLog0003','Skill Maintaining Log')]]
  self.data={
    'folderAlpha01':[self.folders[0],{'token':'fileAlphaSkill','name':'SKILL.md','type':'file','parent_token':'folderAlpha01','content_status':'full_text','content':'# Alpha\\n第一版','path':'Alpha Skill/SKILL.md'},
      {'token':'folderAlphaRefs','name':'references','type':'folder','parent_token':'folderAlpha01','content_status':'folder','path':'Alpha Skill/references'},
      {'token':'fileAlphaRef01','name':'basis.md','type':'file','parent_token':'folderAlphaRefs','content_status':'full_text','content':'引用依据','path':'Alpha Skill/references/basis.md'}],
    'folderBeta002':[self.folders[1],{'token':'fileBetaSkill','name':'SKILL.md','type':'file','parent_token':'folderBeta002','content_status':'full_text','content':'# Beta','path':'Beta Skill/SKILL.md'}],
    'folderLog0003':[self.folders[2],{'token':'docMaintaining','name':'维护记录','type':'docx','parent_token':'folderLog0003','content_status':'full_text','content':'维护记录原文','source_revision':2,'path':'Skill Maintaining Log/维护记录'}]}
 def list_skill_folders(self):
  self.calls.append('roots')
  if self.root_failure:raise knowledge.KnowledgeError('pagination_failed','unsafe-sensitive-output')
  return list(self.folders)
 def read_skill_folder(self,token):
  self.calls.append(token)
  if token in self.failed:raise knowledge.KnowledgeError('source_permission_denied','unsafe-sensitive-output')
  return self.data[token]
 def read_knowledge(self,category):
  self.calls.append(category)
  return []
source=Source()
'''


class SkillFolderKnowledgeTest(unittest.TestCase):
    def check(self, body, restart=None):
        result = run_guarded(SETUP + '\n' + body, restart_body=(SETUP + '\n' + restart) if restart else None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_context_removed_and_one_folder_is_one_skill_with_fixed_tree(self):
        self.check('''
result=knowledge.sync('shared_skills',admin,'initial',backend=source)
assert result['sync_result']['status']=='succeeded',result
assert {s['category'] for s in result['sources']}=={'selected_rag','shared_skills'}
assert len(result['items'])==3 and all(i['type']=='skill_package' for i in result['items'])
alpha=next(i for i in result['items'] if i['name']=='Alpha Skill')
detail=knowledge.read_item(alpha['id'],admin)
assert detail['content']=='# Alpha\\n第一版'
assert detail['item']['entrypoint_status']=='ready' and detail['item']['file_count']==2
assert [f['relative_path'] for f in detail['files']]==['references','references/basis.md','SKILL.md']
ref=next(f for f in detail['files'] if f['name']=='basis.md')
assert ref['relative_segments']==['references','basis.md']
assert knowledge.read_item(ref['id'],admin,ref['version'],skill_id=alpha['id'],package_version=alpha['version'])['content']=='引用依据'
log=next(i for i in result['items'] if i['name']=='Skill Maintaining Log')
assert knowledge.read_item(log['id'],admin)['item']['entrypoint_status']=='missing'
assert knowledge.model_summary(admin)['available_skill_count']==3
assert knowledge.model_summary(admin)['available_text_count']==4
found=knowledge.model_catalog(admin,category='shared_skills',query='basis.md')
assert found['total_matches']==1 and found['items'][0]['id']==alpha['id']
''')

    def test_nested_change_updates_only_whole_package_and_old_version_remains(self):
        self.check('''
first=knowledge.sync('shared_skills',admin,'first',backend=source)
alpha=next(i for i in first['items'] if i['name']=='Alpha Skill');beta=next(i for i in first['items'] if i['name']=='Beta Skill')
old=knowledge.read_item(alpha['id'],admin)
source.data['folderAlpha01'][-1]['content']='修改后的引用依据'
source.calls=[]
second=knowledge.sync('shared_skills',admin,'only-alpha',backend=source,skill_id=alpha['id'])
assert source.calls==['roots','folderAlpha01']
new=next(i for i in second['items'] if i['id']==alpha['id'])
assert new['version']!=alpha['version']
assert next(i for i in second['items'] if i['id']==beta['id'])['version']==beta['version']
assert knowledge.read_item(alpha['id'],admin,alpha['version'])['files']==old['files']
old_ref=next(f for f in old['files'] if f['name']=='basis.md')
assert knowledge.read_item(old_ref['id'],admin,old_ref['version'],skill_id=alpha['id'],package_version=alpha['version'])['content']=='引用依据'
again=knowledge.sync('shared_skills',admin,'alpha-again',backend=source,skill_id=alpha['id'])
assert next(i for i in again['items'] if i['id']==alpha['id'])['version']==new['version']
store.put('test','folder_version',{'id':alpha['id'],'version':alpha['version']})
''', restart='''
ref=store.get('test','folder_version')
assert knowledge.read_item(ref['id'],admin,ref['version'])['content']=='# Alpha\\n第一版'
assert source.calls==[]
''')

    def test_partial_package_failure_does_not_replace_failed_version(self):
        self.check('''
first=knowledge.sync('shared_skills',admin,'first',backend=source)
versions={i['name']:i['version'] for i in first['items']}
source.data['folderAlpha01'][1]['content']='本轮失败不得提前激活'
source.data['folderBeta002'][1]['content']='Beta新版'
source.failed.add('folderAlpha01')
result=knowledge.sync('shared_skills',admin,'partial',backend=source)
assert result['sync_result']['status']=='partial'
items={i['name']:i for i in result['items']}
assert items['Alpha Skill']['version']==versions['Alpha Skill'] and items['Alpha Skill']['sync_status']=='failed'
assert items['Beta Skill']['version']!=versions['Beta Skill']
assert 'unsafe-sensitive-output' not in json.dumps(result)
source.failed.clear();source.root_failure=True
source.folders=source.folders[:1]
bad=knowledge.sync('shared_skills',admin,'bad-root-page',backend=source)
assert bad['sync_result']['status']=='failed'
assert all(i['available'] for i in bad['items'])
''')

    def test_folder_rename_stable_identity_removal_and_no_cross_package_file(self):
        self.check('''
first=knowledge.sync('shared_skills',admin,'first',backend=source)
alpha=next(i for i in first['items'] if i['name']=='Alpha Skill');beta=next(i for i in first['items'] if i['name']=='Beta Skill')
beta_file=knowledge.read_item(beta['id'],admin)['files'][0]
try:knowledge.read_item(beta_file['id'],admin,beta_file['version'],skill_id=alpha['id'],package_version=alpha['version'])
except knowledge.KnowledgeError as exc:assert exc.status==403
else:raise AssertionError('cross-package file read')
source.folders[0]['name']='Alpha 改名'
source.data['folderAlpha01'][0]['name']='Alpha 改名'
renamed=knowledge.sync('shared_skills',admin,'renamed',backend=source,skill_id=alpha['id'])
assert next(i for i in renamed['items'] if i['id']==alpha['id'])['name']=='Alpha 改名'
source.folders=[source.folders[0],source.folders[2]]
removed=knowledge.sync('shared_skills',admin,'removed',backend=source)
assert next(i for i in removed['items'] if i['id']==beta['id'])['available'] is False
assert knowledge.read_item(beta['id'],admin,beta['version'])['content']=='# Beta'
''')

    def test_existing_complete_flat_cache_projects_without_remote_reads(self):
        self.check('''
for token in source.data:
 for item,body in knowledge._normalize('shared_skills',source.data[token]):
  store.put('knowledge_item',item['id'],item)
  store.put('knowledge_content',store.digest([item['id'],item['version']]),{'item':item,'content':body})
items=knowledge.catalog(admin)['items']
assert len(items)==3 and all(item['type']=='skill_package' for item in items)
assert source.calls==[]
alpha=next(i for i in items if i['name']=='Alpha Skill')
assert knowledge.read_item(alpha['id'],admin)['content']=='# Alpha\\n第一版'
''')

    def test_legacy_removed_file_is_not_current_and_other_unit_failure_survives(self):
        self.check('''
for token in source.data:
 for item,body in knowledge._normalize('shared_skills',source.data[token]):
  if item['name']=='basis.md':item['available']=False
  store.put('knowledge_item',item['id'],item)
  store.put('knowledge_content',store.digest([item['id'],item['version']]),{'item':item,'content':body})
alpha=next(i for i in knowledge.catalog(admin)['items'] if i['name']=='Alpha Skill')
assert not any(f['name']=='basis.md' for f in knowledge.read_item(alpha['id'],admin)['files'])
knowledge.sync('shared_skills',admin,'full',backend=source)
source.failed.add('folderAlpha01')
knowledge.sync('shared_skills',admin,'failed-alpha',backend=source,skill_id=alpha['id'])
beta=next(i for i in knowledge.catalog(admin)['items'] if i['name']=='Beta Skill')
done=knowledge.sync('shared_skills',admin,'ok-beta',backend=source,skill_id=beta['id'])
assert done['sync_result']['status']=='succeeded'
shared=next(s for s in done['sources'] if s['category']=='shared_skills')
assert shared['status']=='partial' and shared['last_error']
assert next(i for i in done['items'] if i['id']==alpha['id'])['sync_status']=='failed'
''')

    def test_single_missing_folder_marks_only_that_package_unavailable(self):
        self.check('''
first=knowledge.sync('shared_skills',admin,'first',backend=source)
alpha=next(i for i in first['items'] if i['name']=='Alpha Skill')
others={i['id']:i for i in first['items'] if i['id']!=alpha['id']}
source.folders=source.folders[1:]
source.calls=[]
missing=knowledge.sync('shared_skills',admin,'missing-alpha',backend=source,skill_id=alpha['id'])
assert missing['sync_result']['status']=='failed'
assert missing['sync_result']['error_code']=='skill_not_found'
assert source.calls==['roots']
removed=next(i for i in missing['items'] if i['id']==alpha['id'])
assert removed['available'] is False and removed['sync_status']=='failed'
assert removed['version']==alpha['version'] and removed['error_code']=='skill_not_found'
assert {i['id']:i for i in missing['items'] if i['id']!=alpha['id']}==others
assert knowledge.read_item(alpha['id'],admin,alpha['version'])['content']=='# Alpha\\n第一版'
''')


if __name__ == '__main__':
    unittest.main()
