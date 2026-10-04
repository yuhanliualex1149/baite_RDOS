"""Agent-facing views of a validated Operating Contract; no Agent is launched here."""
from __future__ import annotations

from typing import Any


ENTRY_VERSION = 1
ROOT_ENTRY = (
    "# RDOS Workspace entry\n\n"
    "Before working, read `runtime.json` in this Workspace. Use its `snapshot_dir` "
    "for the entire reading session, then read `control/READ_FIRST.md` and "
    "`control/agent_contract.json` inside that snapshot. Re-read `runtime.json` "
    "for a new session; do not mix files from different snapshots.\n\n"
    "The Runner only synchronizes data. The Local Agent chooses its own methods.\n"
)


def render_read_first(agent: dict[str, Any]) -> str:
    """Render one dynamic view for both Codex and WorkBuddy."""
    required = {"contract_version", "contract_revision", "contract_hash", "shared_revision", "generated_at",
                "organization_guidance", "knowledge", "workspace", "skills", "events",
                "human_decision"}
    if not required.issubset(agent):
        raise ValueError("Agent Contract 缺少入口字段")
    rag = "必读" if agent["knowledge"]["selected_rag_required"] else "按需读取"
    skill = "推荐" if agent["skills"]["shared_skill_policy"] == "recommended" else "可选"
    allowed = ", ".join(agent["events"]["allowed"])
    return (
        "# RDOS — READ FIRST\n\n"
        f"- Contract version: {agent['contract_version']}\n"
        f"- Contract revision: {agent['contract_revision']}\n"
        f"- Contract hash: {agent['contract_hash']}\n"
        f"- Shared revision: {agent['shared_revision']}\n"
        f"- Generated at: {agent['generated_at']}\n\n"
        "先读取 Workspace 根目录的 `runtime.json`，固定使用其中的 `snapshot_dir` "
        "完成这一批资料读取。不要把不同 revision 的文件混合使用。"
        "该快照根目录的 `manifest.json` 是共享资料清单。\n\n"
        "## 资料与工作边界\n\n"
        f"- Selected RAG（飞书当前文件）：{rag}；位置：快照内 `shared/selected_rag/`。\n"
        f"- Shared Skill（Panel 已发布）：{skill}；位置：快照内 `shared/skills/`。"
        "Local Agent 自主选择 Skill、Workflow 和工作方法。\n"
        "- 项目资料：`projects/<project_id>/runtime.json` 指向各项目自己的快照；"
        "项目飞书文件夹是正式来源。\n"
        "- `shared/` 和 `control/` 按协作协议只读，并有 Runner 完整性校验；"
        "同一系统用户下不构成 OS 强制隔离。\n"
        "- 工作文件写入 Workspace 的 `work/` 或相应项目的 `work/`；"
        "结构化结果写入 Workspace 根目录 `outbox/*.json`，由 Runner 上传并在 ACK 后归档。\n"
        f"- Agent 可提交的事件类型：{allowed}。\n"
        "- 重大风险、对外发布、不可逆操作及 Human Gate 由人判断。"
        "Agent 不得批准 Gate、发布 Shared Skill 或修改 Selected RAG 正式来源。\n\n"
        "## 组织补充说明\n\n"
        f"{agent['organization_guidance'].rstrip()}\n"
    )
