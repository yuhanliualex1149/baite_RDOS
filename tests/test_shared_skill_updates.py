"""Full-byte institutional package updates, using only isolated local packages."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tests.test_rdos_skill import node,task,manifest,Transport


class SkillsTransport(Transport):
    def __init__(self):
        super().__init__(task())
        self.files={'SKILL.md':b'# Methods\n','scripts/check.py':b'print(1)\n'}
        self.unavailable=False
        self.reads=[]
    def skill_package(self,root,folder):
        self.reads.append((root,folder))
        if self.unavailable:raise node.WorkflowError('connection_failed','source unavailable')
        return {'name':'Methods','files':copy.deepcopy(self.files)}


class SharedSkillUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.transport=SkillsTransport();self.package=self.root/'installed-methods';self.package.mkdir()
        for name,data in self.transport.files.items():
            target=self.package/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
        self.config=self.package/'local-config.json';self.config.write_text('{"local":true}')
        value=manifest();value['shared_skills']={'root_folder_id':'institutional_skills'}
        self.workflow=node.Workflow(value,self.transport,node.State(self.root/'state.sqlite3'))
    def tearDown(self):
        self.workflow.state.db.close();self.temp.cleanup()
    def link(self):return self.workflow.link_shared_skill('folder_methods',self.package)
    def update(self):
        self.transport.files['scripts/check.py']=b'print(2)\n'
        return self.workflow.check_shared_skills()['skills'][0]
    def test_only_explicit_existing_package_is_mapped_and_no_empty_check_write(self):
        self.assertEqual(self.workflow.check()['shared_skills']['checked_count'],0)
        self.assertEqual(self.transport.reads,[])
        self.assertTrue(self.link()['linked'])
        self.assertTrue(self.link()['already_linked'])
        self.assertEqual(self.workflow.check_shared_skills()['skills'][0]['status'],'current')
        self.assertEqual(self.transport.append_count,0)
    def test_complete_package_hash_detects_script_change_not_just_skill_text(self):
        self.link();first=self.update()
        self.assertTrue(first['needs_prompt']);self.assertEqual(first['decision'],'pending')
        self.assertFalse(self.workflow.check_shared_skills()['skills'][0]['needs_prompt'])
        with self.assertRaises(node.WorkflowError) as error:self.workflow.apply_shared_skill('folder_methods',first['content_hash'])
        self.assertEqual(error.exception.code,'skill_update_not_approved')
        self.assertEqual((self.package/'scripts/check.py').read_bytes(),b'print(1)\n')
    def test_decline_and_unanswered_do_not_install_or_prompt_same_version_after_restart(self):
        self.link();first=self.update()
        self.workflow.decide_shared_skill('folder_methods',first['content_hash'],'decline')
        self.workflow.state.db.close();self.workflow.state=node.State(self.root/'state.sqlite3')
        again=self.workflow.check_shared_skills()['skills'][0]
        self.assertEqual(again['decision'],'decline');self.assertFalse(again['needs_prompt'])
        self.assertEqual((self.package/'scripts/check.py').read_bytes(),b'print(1)\n')
    def test_approved_exact_version_updates_and_preserves_config_backup_and_readback(self):
        self.link();first=self.update();digest=first['content_hash']
        self.workflow.decide_shared_skill('folder_methods',digest,'approve')
        result=self.workflow.apply_shared_skill('folder_methods',digest)
        self.assertTrue(result['readback_verified'])
        self.assertEqual((self.package/'scripts/check.py').read_bytes(),b'print(2)\n')
        self.assertEqual(self.config.read_text(),'{"local":true}')
        self.assertEqual(self.workflow.apply_shared_skill('folder_methods',digest),result)
        binding=self.workflow.state.get('operations','shared_skill:folder_methods')
        self.assertEqual((Path(binding['last_backup'])/'scripts/check.py').read_bytes(),b'print(1)\n')
        self.assertEqual(self.workflow.check_shared_skills()['skills'][0]['status'],'current')
    def test_source_changed_after_approval_needs_new_consent(self):
        self.link();first=self.update();self.workflow.decide_shared_skill('folder_methods',first['content_hash'],'approve')
        self.transport.files['scripts/check.py']=b'print(3)\n'
        with self.assertRaises(node.WorkflowError) as error:self.workflow.apply_shared_skill('folder_methods',first['content_hash'])
        self.assertEqual(error.exception.code,'skill_version_conflict')
        again=self.workflow.check_shared_skills()['skills'][0]
        self.assertTrue(again['needs_prompt']);self.assertEqual(again['decision'],'pending')
    def test_local_change_and_new_file_conflict_are_not_overwritten(self):
        self.link();first=self.update();self.workflow.decide_shared_skill('folder_methods',first['content_hash'],'approve')
        (self.package/'scripts/check.py').write_bytes(b'local important edit')
        with self.assertRaises(node.WorkflowError) as error:self.workflow.apply_shared_skill('folder_methods',first['content_hash'])
        self.assertEqual(error.exception.code,'local_skill_conflict')
        self.assertEqual((self.package/'scripts/check.py').read_bytes(),b'local important edit')
    def test_source_failure_is_incomplete_and_does_not_block_task_check(self):
        self.link();self.transport.unavailable=True
        result=self.workflow.check()
        self.assertEqual(result['shared_skills']['status'],'partial')
        self.assertEqual(result['shared_skills']['skills'][0]['status'],'check_failed')
        self.assertTrue(result['complete_read']);self.assertEqual(len(result['tasks']),1)
    def test_installed_update_with_lost_local_record_is_reconciled_after_restart(self):
        self.link();first=self.update();self.workflow.decide_shared_skill('folder_methods',first['content_hash'],'approve')
        with patch.object(self.workflow,'_finish_skill_update',side_effect=OSError('record save interrupted')):
            with self.assertRaises(OSError):self.workflow.apply_shared_skill('folder_methods',first['content_hash'])
        self.assertEqual((self.package/'scripts/check.py').read_bytes(),b'print(2)\n')
        self.workflow.state.db.close();self.workflow.state=node.State(self.root/'state.sqlite3')
        result=self.workflow.apply_shared_skill('folder_methods',first['content_hash'])
        self.assertTrue(result['readback_verified'])
        self.assertEqual(len(list(self.package.parent.glob('installed-methods.rdos-previous-*'))),1)
    def test_interruption_between_directory_moves_resumes_protected_full_stage(self):
        self.link();first=self.update();self.workflow.decide_shared_skill('folder_methods',first['content_hash'],'approve')
        original=Path.rename
        def interrupt_stage(path,target):
            if path.name.startswith('.rdos-skill-'):raise OSError('interrupted after old package moved')
            return original(path,target)
        with patch.object(Path,'rename',interrupt_stage):
            with self.assertRaises(OSError):self.workflow.apply_shared_skill('folder_methods',first['content_hash'])
        self.assertFalse(self.package.exists())
        self.assertTrue(self.workflow.apply_shared_skill('folder_methods',first['content_hash'])['readback_verified'])
        self.assertEqual(self.config.read_text(),'{"local":true}')

    def test_recovery_does_not_ignore_changed_protected_local_config(self):
        self.link();first=self.update();self.workflow.decide_shared_skill('folder_methods',first['content_hash'],'approve')
        with patch.object(self.workflow,'_finish_skill_update',side_effect=OSError('local state interrupted')):
            with self.assertRaises(OSError):self.workflow.apply_shared_skill('folder_methods',first['content_hash'])
        self.config.write_text('important user change after installation')
        with self.assertRaises(node.WorkflowError) as error:self.workflow.apply_shared_skill('folder_methods',first['content_hash'])
        self.assertEqual(error.exception.code,'local_skill_conflict')
        self.assertEqual(self.config.read_text(),'important user change after installation')

    def test_path_traversal_and_symlink_local_package_refused(self):
        self.transport.files['../outside']=b'bad'
        with self.assertRaises(ValueError):self.link()
        self.transport.files.pop('../outside');(self.package/'linked').symlink_to(self.config)
        with self.assertRaises(node.WorkflowError):self.link()
    def test_peer_recovery_error_does_not_block_ordinary_tasks_or_shared_check(self):
        self.link()
        with patch.object(self.workflow,'continue_peer_enrollment',side_effect=node.WorkflowError('invalid_signature','bad registry')):
            result=self.workflow.check()
        self.assertEqual(result['peer_enrollment']['status'],'recovery_pending')
        self.assertTrue(result['complete_read']);self.assertEqual(len(result['tasks']),1)


class SkillTransportBoundaryTests(unittest.TestCase):
    def test_unregistered_source_is_refused_before_listing(self):
        import sys
        transport=node.Feishu(manifest(),binary=sys.executable)
        with patch.object(transport,'_skill_children',side_effect=AssertionError('unregistered source read')):
            with self.assertRaises(node.WorkflowError):transport.skill_package('outside','folder_methods')
