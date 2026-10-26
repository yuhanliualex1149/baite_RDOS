"""Dry-run conversion of the original Feishu task rows to RDOS 2.0 facts.

This module never calls Feishu or changes a database. A published legacy row is
not proof that its work is still outstanding, so a separate evidence decision
is required before it can become an active 2.0 task.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


LEGACY_FIELDS = frozenset({
    "任务ID", "项目ID", "发布状态", "任务性质", "阶段", "写回标识", "适用成员",
    "版本", "任务类别", "关联任务", "任务说明",
})
DEFAULT_PROJECT = "project_rdos"
BAITE_AI_PROJECT = "prj_0d9b2a0ecec486cdf8"
FINANCE_PROJECT = "rdos-self"
WEEKLY_TASK = "RDOS-77CBA792EB4D"


@dataclass(frozen=True)
class Mapping:
    human_folders: dict[str, str]
    owner_ids: dict[str, str]
    member_nodes: dict[str, str]
    event_table_id: str


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def business_digest(value: Any) -> str:
    """Match server.rdos_rules.digest for requirement fingerprints."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def one(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        if len(value) != 1 or not isinstance(value[0], str):
            raise ValueError("legacy multi-select cell must contain one value")
        return value[0]
    if not isinstance(value, str):
        raise ValueError("legacy cell has an unexpected type")
    return value


def title(description: str, task_id: str) -> str:
    for line in description.splitlines():
        candidate = re.sub(r"^#{1,6}\s*", "", line).strip()
        if candidate:
            return candidate[:100]
    return task_id


def convert(record_id: str, row: dict, decision: str, mapping: Mapping) -> dict:
    """Return a proposal and blockers; never infer completion from publish state.

    decision is one of disabled, completed, ongoing, or pending_evidence. Only
    ongoing is executable. It must come from a separately reviewed old-feedback
    ledger, not from the legacy task row itself.
    """
    if not record_id or not isinstance(row, dict) or set(row) != LEGACY_FIELDS:
        raise ValueError("legacy row identity or fields are incomplete")
    task_id, member_id = one(row["任务ID"]), one(row["适用成员"])
    original_project = one(row["项目ID"])
    publication, nature = one(row["发布状态"]), one(row["任务性质"])
    description = one(row["任务说明"])
    if not task_id or not description or publication not in ("已发布", "停用"):
        raise ValueError("legacy task identity, body, or publication state is invalid")
    if decision not in ("disabled", "completed", "ongoing", "pending_evidence"):
        raise ValueError("evidence decision is required")
    if publication == "停用" and decision != "disabled":
        raise ValueError("a disabled legacy row cannot be reactivated by conversion")
    if publication == "已发布" and decision == "disabled":
        raise ValueError("a published legacy row cannot be silently disabled")
    try:
        version = int(one(row["版本"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("legacy requirements version is invalid") from exc
    if version < 1:
        raise ValueError("legacy requirements version is invalid")
    # The two disabled onboarding rows predate project registration.  Their
    # original empty scope remains in migration_source; Default is only an
    # archival routing value and can never make them executable.
    current_project = (DEFAULT_PROJECT if original_project in (BAITE_AI_PROJECT, '')
                       else original_project)
    node_id = mapping.member_nodes.get(member_id, "")
    folder = mapping.human_folders.get(current_project, "")
    owner_id = mapping.owner_ids.get(current_project, "")
    blockers: list[str] = []
    if not original_project and decision != 'disabled':
        blockers.append("unscoped_legacy_project")
    if not node_id and decision not in ('disabled', 'pending_evidence'):
        blockers.append("unverified_member_node_mapping")
    if (not folder or not owner_id) and decision != 'disabled':
        blockers.append("project_mapping_incomplete")
    if not mapping.event_table_id and decision != 'disabled':
        blockers.append("event_table_not_created")
    if decision == "pending_evidence":
        blockers.append("completion_or_continuation_not_proven")
    periodic = nature == "周期"
    if periodic and task_id != WEEKLY_TASK:
        blockers.append("periodic_rules_need_review")
    if decision == "ongoing" and blockers:
        raise ValueError("cannot activate task with unresolved migration blockers: " + ",".join(blockers))
    source_hash = digest(row)
    operation_id = "migrate_" + digest([record_id, task_id, source_hash])[:32]
    # Existing content remains authoritative. These fields are defaults for
    # previously absent 2.0 columns, not an invented rewrite of instructions.
    requirement = {
        "title": title(description, task_id), "goal": description,
        "owner_id": owner_id or mapping.owner_ids.get(DEFAULT_PROJECT, ''),
        "executor_node_ids": [node_id] if node_id else [],
        "input_refs": [], "deliverable": description,
        "output_zone": "project", "dispatch_to": ["agent"], "return_to": ["agent"],
        "completion_rule": {"kind": "receipt_recorded"}, "due_at": "",
        "editable_document_ids": [], "task_type": "action",
        "deliverable_audience": ["admin", "project_members"],
    }
    if periodic and task_id == WEEKLY_TASK:
        requirement['periodicity'] = {'unit': 'week', 'timezone': 'Asia/Shanghai',
                                      'deadline_weekday': 4, 'deadline_time': '17:00'}
    status = "active" if decision == "ongoing" else "closed" if decision in ("disabled", "completed") else "paused"
    task = {
        "id": task_id, "task_id": task_id, "project_id": current_project,
        **requirement, "requirements_version": version, "record_revision": 1,
        "requirements_hash": business_digest(requirement), "business_status": status,
        "operation_id": operation_id, "grant_operation_id": operation_id,
        "migration_source": {
            "record_id": record_id, "legacy_project_id": original_project,
            "legacy_publication": publication, "legacy_nature": nature,
            "source_fields_sha256": source_hash, "evidence_decision": decision,
            "legacy_writeback_marker": one(row["写回标识"]),
        },
    }
    columns = {
        "标题": task["title"], "状态": status,
        "执行节点": node_id, "要求版本": str(version),
        "记录修订": "1", "操作ID": operation_id,
        "记录数据": canonical_json(task), "当前项目ID": current_project,
    }
    return {"record_id": record_id, "task_id": task_id,
            "source_fields_sha256": source_hash, "proposal": task,
            "new_columns": columns, "blockers": blockers,
            "human_folder": folder, "event_table_id": mapping.event_table_id}
