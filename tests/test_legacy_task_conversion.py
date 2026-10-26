from __future__ import annotations

import unittest

from cutover.legacy_task_conversion import Mapping, convert
from server import rdos_rules


ROW = {
    "任务ID": "RDOS-OLD-01", "项目ID": "prj_0d9b2a0ecec486cdf8",
    "发布状态": ["已发布"], "任务性质": ["一次性"], "阶段": None,
    "写回标识": "old-marker", "适用成员": "member-x", "版本": "3",
    "任务类别": "work", "关联任务": None,
    "任务说明": "# 原来的目标\n保留全部原始要求。",
}
MAP = Mapping({"project_rdos": "ZBQXfgblvlE3NrdrZ2JcNNhfnxh"},
              {"project_rdos": "admin-1"}, {"member-x": "node-x"}, "tbl-events")


class LegacyConversionTest(unittest.TestCase):
    def test_no_inferred_reexecution(self):
        result = convert("rec-old", ROW, "pending_evidence", MAP)
        self.assertEqual(result["proposal"]["business_status"], "paused")
        self.assertIn("completion_or_continuation_not_proven", result["blockers"])
        self.assertEqual(result["proposal"]["requirements_version"], 3)
        self.assertEqual(result["proposal"]["migration_source"]["legacy_project_id"], ROW["项目ID"])
        self.assertEqual(result["new_columns"]["当前项目ID"], "project_rdos")

    def test_explicit_ongoing_preserves_identity_and_requirement_hash(self):
        first = convert("rec-old", ROW, "ongoing", MAP)
        second = convert("rec-old", ROW, "ongoing", MAP)
        task = first["proposal"]
        self.assertEqual(first, second)
        self.assertEqual(task["task_id"], ROW["任务ID"])
        self.assertEqual(task["record_revision"], 1)
        self.assertEqual(task["project_id"], "project_rdos")
        self.assertEqual(task["requirements_hash"], rdos_rules.digest(rdos_rules.requirements(task)))
        self.assertEqual(task["goal"], ROW["任务说明"])

    def test_disabled_cannot_be_reactivated(self):
        row = {**ROW, "发布状态": ["停用"]}
        with self.assertRaises(ValueError):
            convert("rec-old", row, "ongoing", MAP)
        self.assertEqual(convert("rec-old", row, "disabled", MAP)["proposal"]["business_status"], "closed")

    def test_unscoped_disabled_onboarding_stays_archived(self):
        row = {**ROW, "项目ID": None, "适用成员": None,
               "发布状态": ["停用"], "任务性质": ["每次接入执行"]}
        result = convert("rec-onboarding", row, "disabled", MAP)
        self.assertEqual(result['proposal']['project_id'], 'project_rdos')
        self.assertEqual(result['proposal']['business_status'], 'closed')
        self.assertEqual(result['proposal']['executor_node_ids'], [])
        self.assertEqual(result['proposal']['migration_source']['legacy_project_id'], '')
        self.assertEqual(result['blockers'], [])

    def test_unknown_member_cannot_activate(self):
        row = {**ROW, "适用成员": "unverified-member"}
        with self.assertRaises(ValueError):
            convert("rec-old", row, "ongoing", MAP)

    def test_approved_weekly_task_keeps_parent_active(self):
        row = {**ROW, "任务ID": "RDOS-77CBA792EB4D", "任务性质": ["周期"]}
        result = convert("rec-old", row, "ongoing", MAP)
        self.assertEqual(result["proposal"]["business_status"], "active")
        self.assertEqual(result['proposal']['requirements_version'], 4)
        self.assertEqual(result['proposal']['migration_source']['legacy_requirements_version'], 3)
        self.assertEqual(result['proposal']['title'], 'Default 项目周报')
        self.assertIn(row['任务说明'], result['proposal']['goal'])
        self.assertEqual(result["proposal"]["periodicity"], {
            "unit": "week", "timezone": "Asia/Shanghai",
            "deadline_weekday": 4, "deadline_time": "17:00",
        })
        self.assertEqual(result["proposal"]["requirements_hash"],
                         rdos_rules.digest(rdos_rules.requirements(result["proposal"])))


if __name__ == "__main__":
    unittest.main()
