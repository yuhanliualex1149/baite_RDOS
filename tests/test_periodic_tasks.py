"""A recurring parent remains actionable next week without duplicating a week."""
from __future__ import annotations

from datetime import datetime, timezone
import unittest

from server import rdos_rules


class PeriodicRuleTests(unittest.TestCase):
    def setUp(self):
        self.task = {'task_id': 'RDOS-77CBA792EB4D', 'project_id': 'project_rdos',
                     'business_status': 'active', 'requirements_version': 1,
                     'record_revision': 2, 'task_type': 'action', 'goal': '每周工作反馈',
                     'owner_id': 'member_admin', 'deliverable': '每周五反馈',
                     'executor_node_ids': ['node_yuhan'],
                     'completion_rule': {'kind': 'receipt_recorded'},
                     'periodicity': {'unit': 'week', 'timezone': 'Asia/Shanghai',
                                     'deadline_weekday': 4, 'deadline_time': '17:00'}}

    def test_week_changes_without_changing_parent_requirement_version(self):
        friday = datetime(2026, 10, 23, 8, 30, tzinfo=timezone.utc)
        monday = datetime(2026, 10, 26, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(rdos_rules.period_key(self.task, friday), '2026-W43')
        self.assertEqual(rdos_rules.period_key(self.task, monday), '2026-W44')
        previous = {'task_id': self.task['task_id'], 'requirements_version': 1,
                    'period_key': '2026-W43', 'completed_steps': ['submission_verified']}
        plan = rdos_rules.execution_plan(self.task, 'node_yuhan', previous, now=monday)
        self.assertEqual(plan['period_key'], '2026-W44')
        self.assertEqual(plan['reason'], 'ready')
        self.assertIn('submit_result', plan['allowed_actions'])
        same_week = rdos_rules.execution_plan(self.task, 'node_yuhan', previous, now=friday)
        self.assertEqual(same_week['reason'], 'already_submitted')

    def test_late_previous_week_receipt_is_allowed_but_future_is_not(self):
        monday = datetime(2026, 10, 26, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(rdos_rules.valid_result_period({'period_key': '2026-W43'}, self.task, monday), '2026-W43')
        with self.assertRaises(rdos_rules.RuleViolation):
            rdos_rules.valid_result_period({'period_key': '2026-W45'}, self.task, monday)

    def test_nonperiodic_task_cannot_claim_a_week(self):
        task = {key: value for key, value in self.task.items() if key != 'periodicity'}
        with self.assertRaises(rdos_rules.RuleViolation):
            rdos_rules.valid_result_period({'period_key': '2026-W44'}, task)

    def test_periodicity_participates_in_requirement_fingerprint(self):
        baseline = rdos_rules.requirements(self.task)
        changed = {**self.task, 'periodicity': {**self.task['periodicity'], 'deadline_time': '16:30'}}
        self.assertNotEqual(rdos_rules.digest(baseline), rdos_rules.digest(rdos_rules.requirements(changed)))


if __name__ == '__main__':
    unittest.main()
