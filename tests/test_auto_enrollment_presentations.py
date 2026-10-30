"""Automatic registration has no ordinary human-approval presentation step."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
import zipfile

from tests.phase1_support import run_guarded
from tests.test_node_onboarding import SETUP


ROOT = Path(__file__).resolve().parents[1]


class AutomaticEnrollmentPresentationTest(unittest.TestCase):
    def test_generated_setup_task_describes_automatic_registration_and_separate_readiness(self):
        result = run_guarded(SETUP + '''
async def scenario():
    node=new_node();assign(node)
    prepared=await node_onboarding.prepare(node['node_id'],admin)
    task=prepared['task'];goal=task['goal']
    assert '自动核验并完成登记' in goal and '不需要人工审核' in goal
    assert '中央签名' in goal and '互派就绪单独跟踪' in goal
    assert '申请步骤完成后不必等待' in goal
    assert all(word in goal for word in ('持久安装','09:00','16:30','Asia/Shanghai','Shared Skills','独立读回'))
    assert not any(word in goal for word in ('管理员在成员卡片','登记码','确认节点归属'))
    assert task['executor_node_ids']==[node['node_id']]
    assert task['business_status']=='active' and task['completion_rule']=={'kind':'receipt_recorded'}
    again=await node_onboarding.prepare(node['node_id'],admin)
    assert again['task']['task_id']==task['task_id'] and len(backend3.list_tasks())==1
asyncio.run(scenario())
''', timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_delivered_skill_removes_identity_review_but_keeps_uncertain_write_confirmation(self):
        from lab.build_node_skill import package_bytes
        manifest = {'protocol_version': 2, 'workspace_id': 'isolated_workspace',
                    'project_id': 'isolated_project', 'node_id': 'isolated_node',
                    'base_token': 'isolated_base'}
        content, _ = package_bytes(manifest)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            skill = archive.read('rdos-workflow/SKILL.md').decode()
            guide = archive.read('rdos-workflow/references/execution.md').decode()
        self.assertIn('自动完成登记，不需要人工审核', skill)
        self.assertIn('中央随既有同步自动核验并完成登记', guide)
        for text in (skill, guide):
            self.assertNotIn('管理员在成员卡片', text)
            self.assertNotIn('申请本身仅进入待确认，不自动绑定', text)
            self.assertIn('私钥', text)
            self.assertIn('互派就绪', text)
            self.assertIn('明确同意', text)
        self.assertIn('不是登记审核', skill)
        self.assertIn('不是节点登记审核', guide)
        self.assertIn('公钥与持有证明', guide)
        self.assertIn('不同公钥冲突', guide)

    def render(self, scenarios):
        bundled = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'
        binary = str(bundled) if bundled.is_file() and os.access(bundled, os.X_OK) else shutil.which('node')
        self.assertTrue(binary, 'Existing Node runtime is required; this test installs nothing')
        program = r'''
const fs=require('node:fs'),vm=require('node:vm');
const script=fs.readFileSync(process.argv[1],'utf8');
const start=script.indexOf('  function peerBindingButton('),end=script.indexOf('  function renderPeople(',start);
if(start<0||end<start)throw Error('Registration renderer not found');
const code=script.slice(start,end),scenarios=JSON.parse(fs.readFileSync(0,'utf8'));
const output=scenarios.map(entry=>vm.runInNewContext(code+';peerBindingButton(target)',{
  target:entry.target,rodos:{nodes:entry.nodes},list:value=>Array.isArray(value)?value:[],
  esc:value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]))
}));
process.stdout.write(JSON.stringify(output));
'''
        result = subprocess.run([binary, '-e', program, str(ROOT / 'server/static/central.js')],
                                input=json.dumps(scenarios), text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_registration_states_have_no_confirmation_controls_or_codes(self):
        cases = [
            ({}, '互派未启用'),
            ({'pending_requests': [{'request_id': 'request_one', 'status': 'pending',
                                   'confirmation_code': 'DO_NOT_PROJECT_CODE'}]}, '待自动登记'),
            ({'pending_requests': [{'request_id': 'request_one', 'status': 'registry_pending'}]}, '正在登记'),
            ({'bound': True}, '登记完成（互派待就绪）'),
            ({'bound': True, 'ready': True}, '互派已启用'),
            ({'registration_status': 'failed', 'registration_error': '登记记录暂未完整读回'}, '登记异常'),
            ({'pending_requests': [{'request_id': 'request_one', 'status': 'pending',
                                   'automatic_status': 'conflict', 'message': '与已有公钥不同'}]}, '登记异常'),
        ]
        output = self.render([{'target': 'node_one', 'nodes': [
            {'node_id': 'node_one', 'runtime_available': True, **value}]} for value, _ in cases])
        for text, (_, expected) in zip(output, cases):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)
                for marker in ('data-peer-approve', '确认节点归属', '确认绑定', '登记码', 'DO_NOT_PROJECT_CODE'):
                    self.assertNotIn(marker, text)

    def test_existing_ready_node_keeps_readiness_while_new_application_failure_is_escaped(self):
        scenarios = [
            {'target': 'node_one', 'nodes': [{'node_id': 'node_one', 'runtime_available': True,
              'bound': True, 'ready': True, 'registration_status': 'conflict',
              'registration_error': '<img src=x onerror=alert(1)>',
              'pending_requests': [{'status': 'pending', 'automatic_status': 'conflict',
                                   'message': 'private_machine_payload'}]}]},
            {'target': 'node_two', 'nodes': [{'node_id': 'node_one', 'pending_requests': [
                {'status': 'pending', 'request_id': 'node_one_request'}]},
                {'node_id': 'node_two', 'runtime_available': True, 'pending_requests': []}]}
        ]
        first, second = self.render(scenarios)
        self.assertIn('互派已启用', first)
        self.assertIn('登记异常：&lt;img', first)
        self.assertNotIn('<img', first)
        self.assertNotIn('private_machine_payload', first)
        self.assertIn('互派未启用', second)
        self.assertNotIn('node_one_request', second)

