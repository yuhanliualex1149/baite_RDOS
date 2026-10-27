"""A Skill read is one proven first-level folder, with the real egress guard."""
import unittest

from tests.test_phase3_egress import isolated


SETUP = '''
skill_root=fs.KNOWLEDGE_ROOTS['shared_skills']
folders[skill_root]=[
 {'token':'folderSkillA','name':'Skill / A','type':'folder','parent_token':skill_root},
 {'token':'folderSkillB','name':'Skill B','type':'folder','parent_token':skill_root},
 {'token':'looseRootDocument','name':'Loose root document','type':'docx','parent_token':skill_root},
]
folders['folderSkillA']=[
 {'token':'fileSkillEntry','name':'SKILL.md','type':'file','parent_token':'folderSkillA'},
 {'token':'folderSkillReferences','name':'references','type':'folder','parent_token':'folderSkillA'},
 {'token':'fileSkillScript','name':'helper.py','type':'file','parent_token':'folderSkillA'},
 {'token':'fileSkillArchive','name':'archive.zip','type':'file','parent_token':'folderSkillA'},
]
folders['folderSkillReferences']=[
 {'token':'docSkillReference','name':'Input / evidence','type':'docx','parent_token':'folderSkillReferences'},
]
folders['folderSkillB']=[
 {'token':'docSiblingSkill','name':'Sibling private body','type':'docx','parent_token':'folderSkillB'},
]
documents['docSkillReference']={'title':'Input / evidence','content':'Reference body. https://example.invalid/does-not-expand-scope','revision_id':3}
documents['docSiblingSkill']={'title':'Sibling','content':'MUST_NOT_READ_SIBLING','revision_id':1}
documents['looseRootDocument']={'title':'Loose','content':'MUST_NOT_READ_LOOSE_FILE','revision_id':1}
original_remote=remote
def skill_remote(argv,**kw):
 if argv[1:3]==['drive','+download']:
  sys.audit('subprocess.Popen',argv[0],argv,kw.get('cwd'),kw.get('env'))
  calls.append(tuple(argv[1:]))
  assert argv[argv.index('--file-token')+1]=='fileSkillEntry'
  destination=Path(argv[argv.index('--output')+1])
  assert not destination.is_absolute()
  (Path(kw['cwd'])/destination).write_text('# Skill A\\nKeep evidence boundaries.',encoding='utf-8')
  return subprocess.CompletedProcess(argv,0,'downloaded','')
 return original_remote(argv,**kw)
'''


class SkillFolderEgressTest(unittest.TestCase):
    def check(self, body):
        result = isolated(SETUP + '\n' + body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_complete_root_discovery_lists_units_without_fetching_bodies(self):
        self.check('''
pages=[]
def paged(argv,**kw):
 if argv[1:4]==['drive','files','list']:
  params=json.loads(argv[argv.index('--params')+1])
  assert params['folder_token']==skill_root
  sys.audit('subprocess.Popen',argv[0],argv,kw.get('cwd'),kw.get('env'))
  calls.append(tuple(argv[1:]));pages.append(params)
  first='page_token' not in params
  data={'files':folders[skill_root][:1] if first else folders[skill_root][1:],'has_more':first}
  if first:data['next_page_token']='SkillPage2'
  return subprocess.CompletedProcess(argv,0,json.dumps({'data':data}),'')
 return skill_remote(argv,**kw)
with patch.object(fs.subprocess,'run',side_effect=paged):
 units=backend.list_skill_folders()
assert [u['token'] for u in units]==['folderSkillA','folderSkillB']
assert units[0]['name']=='Skill / A'
assert units[0]['path_segments']==['Skill / A']
assert units[0]['parent_token']==skill_root and units[0]['content_status']=='folder'
assert units[0]['source_url'].endswith('/folderSkillA')
assert len(pages)==2 and pages[1]['page_token']=='SkillPage2'
assert all(call[:2] in (('auth','status'),('drive','files')) for call in calls)
assert getattr(backend.executor._pending,'knowledge',None) is None
''')

    def test_one_complete_subtree_preserves_layout_and_never_reads_siblings(self):
        self.check('''
with patch.object(fs.subprocess,'run',side_effect=skill_remote):
 values=backend.read_skill_folder('folderSkillA')
assert len(values)==6 and values[0]['token']=='folderSkillA'
entry=next(v for v in values if v['token']=='fileSkillEntry')
assert entry['content'].startswith('# Skill A')
assert entry['path_segments']==['Skill / A','SKILL.md']
reference=next(v for v in values if v['token']=='docSkillReference')
assert reference['path_segments']==['Skill / A','references','Input / evidence']
assert reference['source_revision']=='3'
assert reference['path']=='Skill ／ A/references/Input ／ evidence'
assert next(v for v in values if v['token']=='fileSkillScript')['content_status']=='file_listing'
assert next(v for v in values if v['token']=='fileSkillArchive')['content_status']=='attachment_only'
for command in calls:
 assert 'docSiblingSkill' not in command and 'looseRootDocument' not in command
 if command[:3]==('drive','files','list'):
  params=json.loads(command[command.index('--params')+1])
  assert params['folder_token'] in {skill_root,'folderSkillA','folderSkillReferences'}
assert not list((root/'runtime/tmp').glob('knowledge-*'))
assert skill_root not in fs._folders(backend.resources)
assert 'folderSkillA' not in fs._folders(backend.resources)
before=len(calls)
with patch.object(fs.subprocess,'run',side_effect=skill_remote):
 denied(lambda:backend.read_document('docSkillReference'))
 denied(lambda:backend.executor.list_root('folderSkillA'))
 denied(lambda:backend.executor.fetch_document('docSkillReference'))
 denied(lambda:backend.executor.create_document('folderSkillA','Title','Body'))
assert len(calls)==before
''')

    def test_root_children_are_reproved_and_nested_or_unregistered_units_refused(self):
        self.check('''
with patch.object(fs.subprocess,'run',side_effect=skill_remote):
 for target in ('folderSkillReferences','looseRootDocument',skill_root,fs.HUMAN_FOLDER,'missingSkill'):
  before=len(calls)
  denied(lambda:backend.read_skill_folder(target))
  assert all(call[:2] in (('auth','status'),('drive','files')) for call in calls[before:])
 # A previously discovered folder is not a durable read capability.
 assert backend.list_skill_folders()[0]['token']=='folderSkillA'
 folders[skill_root]=folders[skill_root][1:]
 denied(lambda:backend.read_skill_folder('folderSkillA'))
 assert getattr(backend.executor._pending,'knowledge',None) is None
before=len(calls)
with patch.object(fs.subprocess,'run',side_effect=skill_remote):
 denied(lambda:backend.read_skill_folder('https://example.invalid/'))
assert len(calls)==before
''')

    def test_incomplete_root_pages_never_authorize_even_a_first_page_match(self):
        self.check('''
def incomplete(argv,**kw):
 if argv[1:4]==['drive','files','list']:
  sys.audit('subprocess.Popen',argv[0],argv,kw.get('cwd'),kw.get('env'))
  calls.append(tuple(argv[1:]))
  return subprocess.CompletedProcess(argv,0,json.dumps({'data':{'files':folders[skill_root][:1], 'has_more':True}}),'')
 return skill_remote(argv,**kw)
with patch.object(fs.subprocess,'run',side_effect=incomplete):
 for read in (backend.list_skill_folders,lambda:backend.read_skill_folder('folderSkillA')):
  try:read()
  except fs.FeishuTestError as exc:
   assert exc.code=='pagination_failed'
   assert exc.knowledge_source['source_url'].endswith('/'+skill_root)
  else:raise AssertionError('partial root accepted')
assert all(call[:2] in (('auth','status'),('drive','files')) for call in calls)
assert getattr(backend.executor._pending,'knowledge',None) is None
''')

    def test_partial_package_failure_returns_no_success_and_keeps_guard_closed(self):
        self.check('''
def failure(argv,**kw):
 if argv[1:3]==['docs','+fetch'] and argv[argv.index('--doc')+1]=='docSkillReference':
  sys.audit('subprocess.Popen',argv[0],argv,kw.get('cwd'),kw.get('env'))
  calls.append(tuple(argv[1:]))
  return subprocess.CompletedProcess(argv,2,'',json.dumps({'error':{'type':'permission','message':'permission denied'}}))
 return skill_remote(argv,**kw)
with patch.object(fs.subprocess,'run',side_effect=failure):
 try:backend.read_skill_folder('folderSkillA')
 except fs.FeishuTestError as exc:
  assert exc.code=='source_permission_denied'
  assert exc.knowledge_source['name']=='Input / evidence'
  assert exc.knowledge_source['path']=='Skill ／ A/references/Input ／ evidence'
 else:raise AssertionError('partially read Skill accepted')
assert not list((root/'runtime/tmp').glob('knowledge-*'))
assert getattr(backend.executor._pending,'knowledge',None) is None
assert not any(call[:2] in (('docs','+create'),('docs','+update'),('base','+record-upsert')) for call in calls)
''')


if __name__ == '__main__':
    unittest.main()
