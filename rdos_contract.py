"""The small, versioned operating contract shared by Panel and Runner."""
from __future__ import annotations

from typing import Any

from rdos_protocol import content_hash


CONTRACT_VERSION = "1"
READ_ORDER = ["control/agent_contract.json", "shared/global_rules.md", "manifest.json"]
EVENTS = [
    "progress", "activity_record", "collaboration_request", "collaboration_response",
    "skill_change_proposal", "project_update", "installation_self_test",
]
AGENT_EVENTS = [event for event in EVENTS if event != "installation_self_test"]


def make_contract(guidance: str, rag_required: bool = True,
                  skill_policy: str = "recommended") -> dict[str, Any]:
    contract = make_contract_unchecked(guidance, rag_required, skill_policy)
    validate_contract(contract)
    return contract


def validate_contract(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "contract_version", "organization_guidance", "knowledge", "runtime",
        "workspace", "skills", "events", "human_decision",
    }:
        raise ValueError("Operating Contract 字段缺失或包含未知字段")
    if value["contract_version"] != CONTRACT_VERSION:
        raise ValueError("不支持的 Operating Contract 版本")
    guidance = value["organization_guidance"]
    if not isinstance(guidance, str) or not guidance.strip() or len(guidance) > 100_000:
        raise ValueError("组织说明不能为空或过长")
    if not isinstance(value["knowledge"], dict) or type(value["knowledge"].get("selected_rag_required")) is not bool:
        raise ValueError("Selected RAG 必读设置无效")
    if not isinstance(value["skills"], dict) or value["skills"].get("shared_skill_policy") not in {"recommended", "optional"}:
        raise ValueError("Shared Skill 使用政策无效")
    canonical = make_contract_unchecked(guidance, value["knowledge"]["selected_rag_required"],
                                        value["skills"]["shared_skill_policy"])
    if value != canonical:
        raise ValueError("Operating Contract 包含不允许更改的组织边界")
    return value


def make_contract_unchecked(guidance: str, rag_required: bool, skill_policy: str) -> dict[str, Any]:
    """Construct the canonical shape without recursively validating it."""
    return {
        "contract_version": CONTRACT_VERSION,
        "organization_guidance": guidance,
        "knowledge": {
            "selected_rag_authority": "feishu_current",
            "selected_rag_required": rag_required,
            "project_shared_context_authority": "project_feishu_folder",
            "legacy_default": False,
        },
        "runtime": {"required_read_order": READ_ORDER.copy(), "refresh_on_revision_change": True},
        "workspace": {
            "shared": "read_only", "control": "read_only", "work": "read_write",
            "outbox": "structured_event_only",
        },
        "skills": {
            "shared_skill_authority": "panel_published",
            "shared_skill_policy": skill_policy,
            "local_override_allowed": True,
            "proposal_required_for_shared_update": True,
        },
        "events": {"allowed": EVENTS.copy()},
        "human_decision": {
            "machine_can_approve_gate": False,
            "machine_can_publish_shared_skill": False,
            "machine_can_modify_selected_rag_source": False,
        },
    }


def contract_hash(contract: dict[str, Any]) -> str:
    validate_contract(contract)
    return content_hash(contract)


def render_rules(contract: dict[str, Any]) -> str:
    validate_contract(contract)
    rag = "必须先读取已同步的 Selected RAG" if contract["knowledge"]["selected_rag_required"] else "可按任务需要读取已同步的 Selected RAG"
    skill = "推荐参考已发布 Shared Skill" if contract["skills"]["shared_skill_policy"] == "recommended" else "可选用已发布 Shared Skill"
    return (
        "# 全局工作规则\n\n"
        f"- {rag}；其正式来源为飞书当前文件。\n"
        f"- {skill}；Local Agent 可自主选择本地版本和工作方法。\n"
        "- Shared Skill 的组织正式版本由系统管理员发布；改进通过 Proposal 提交。\n"
        "- 重大风险、对外发布和不可逆操作应交由人判断；Agent 不能批准 Gate。\n"
        "- shared/control 依组织协议只读；工作产物写入 work，结构化事件写入 outbox。\n\n"
        "## 组织补充说明\n\n"
        f"{contract['organization_guidance'].rstrip()}\n"
    )


def agent_projection(contract: dict[str, Any], revision: int, updated_at: str) -> dict[str, Any]:
    validate_contract(contract)
    return {
        "contract_version": CONTRACT_VERSION,
        "contract_hash": contract_hash(contract),
        "shared_revision": revision,
        "generated_at": updated_at,
        "organization_guidance": contract["organization_guidance"],
        "knowledge": contract["knowledge"],
        "runtime": contract["runtime"],
        "workspace": contract["workspace"],
        "skills": contract["skills"],
        "events": {"allowed": AGENT_EVENTS.copy()},
        "human_decision": contract["human_decision"],
    }
