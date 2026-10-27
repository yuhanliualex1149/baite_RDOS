"""The original scheduled clients can read the same authorized 2.0 row."""
from __future__ import annotations

import unittest
from unittest import mock
import json
import tempfile

from cutover import upgrade_tasks
from server import phase3_feishu, rdos_contract


class UpgradeTaskTests(unittest.TestCase):
    def test_three_fixed_nodes_keep_original_members_and_single_readable_row(self):
        self.assertEqual(len(upgrade_tasks.UPGRADES), 3)
        self.assertEqual({v['node_id'] for v in upgrade_tasks.UPGRADES.values()},
                         {'node_yuhan', 'node_xiaofei', 'node_duoduo'})
        for task_id, item in upgrade_tasks.UPGRADES.items():
            payload = upgrade_tasks.task_payload(task_id)
            task = {**payload, 'project_id': 'project_rdos',
                    'requirements_version': 1, 'business_status': 'active'}
            legacy = upgrade_tasks.legacy_columns(task, 'publish-' + task_id)
            self.assertEqual(legacy['适用成员'], item['member_id'])
            self.assertEqual(legacy['项目ID'], item['legacy_project_id'])
            self.assertEqual(legacy['发布状态'], '已发布')
            self.assertEqual(legacy['任务说明'], payload['goal'])
            self.assertIn('/nodes/' + item['node_id'] + '/start', legacy['任务说明'])
            self.assertIn('保留两条安排各自的 ID、时间、时区、启停状态和会话／工区绑定', legacy['任务说明'])
            closed = upgrade_tasks.legacy_columns({**task, 'business_status': 'closed'}, 'close-' + task_id)
            self.assertEqual(closed['发布状态'], '停用')
            self.assertEqual(closed['任务说明'], legacy['任务说明'])

    def test_mismatched_node_or_instructions_cannot_project_to_old_reader(self):
        task_id = 'RDOS-2-UPGRADE-DUODUO'
        task = {**upgrade_tasks.task_payload(task_id), 'project_id': 'project_rdos',
                'requirements_version': 1, 'business_status': 'active'}
        for change in ({'executor_node_ids': ['node_xiaofei']},
                       {'goal': 'skip skill validation'},
                       {'project_id': 'rdos-self'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                upgrade_tasks.legacy_columns({**task, **change}, 'op')
        self.assertEqual(upgrade_tasks.legacy_columns({'task_id': 'other'}, 'op'), {})

    def test_formal_cli_accepts_only_matching_legacy_projection(self):
        task_id = 'RDOS-2-UPGRADE-YUHAN'
        task = {**upgrade_tasks.task_payload(task_id), 'project_id': 'project_rdos',
                'requirements_version': 1, 'record_revision': 1,
                'business_status': 'active', 'operation_id': 'upgrade-op'}
        registry = {'base_token': 'E34dbYDEXaCRnDsMQDBcMvyMnjb',
                    'tasks_table': 'tblvJU3CwPt6eAaw', 'events_table': 'tblEvents1234',
                    'workspace_id': 'ws_rdos', 'default_project_id': 'project_rdos', 'env_id': 'rdos_formal',
                    'generation': '0' * 32, 'projects': [], 'documents': []}
        wire = rdos_contract.encode(task, registry)
        fields = {'任务ID': task_id, '记录数据': json.dumps(wire, ensure_ascii=False),
                  **upgrade_tasks.legacy_columns(task, 'upgrade-op')}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(phase3_feishu, '_FORMAL', True):
            executor = phase3_feishu._CLIExecutor(phase3_feishu._TOKEN,
                {'root': directory, 'phase3': {'cli_path': '/tmp/fake-cli', 'cli_sha256': '0'*64}}, registry)
            args = ['base', '+record-upsert', '--as', 'user', '--base-token', registry['base_token'],
                    '--table-id', registry['tasks_table'], '--json', json.dumps(fields, ensure_ascii=False)]
            executor._validate_args(args)
            bad = {**fields, '适用成员': 'another-member'}
            args[-1] = json.dumps(bad, ensure_ascii=False)
            with self.assertRaises(phase3_feishu.FeishuTestError):
                executor._validate_args(args)


if __name__ == '__main__':
    unittest.main()
