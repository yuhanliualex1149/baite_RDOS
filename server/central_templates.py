"""Approved, local wording for member instructions.

Only these prose fragments are editable. Identity, project scope, feedback
destination, write markers, permissions and approval rules stay in the code
that assembles an instruction. There is no registered Feishu template source.
"""
from __future__ import annotations

import json
import re

from server import central_store as store, db


FEEDBACK_DIVIDER = "\n\n[反馈栏目]\n"
_FEEDBACK_HEADINGS = (
    "当前进度与下一步", "本期成果／阶段性产出", "卡点与协作需求",
    "Human Gate 相关线索", "Skill／RAG 实际使用", "本地 Skill 更新",
)


DEFAULTS = {
    "onboarding.0": (
        "先检查已有飞书连接并复用。读取本步材料，在指定反馈位置只追加最少的连接检查：成员、标识、时间、读取来源和读写结果；真实读回。"
        "不要上传工作内容，也不要因本步测试扩大资料范围。若连接失败，按连接帮助定位具体缺口。"
    ),
    "onboarding.1": (
        "按本步材料与本人确认身份、当前工作范围和允许共享范围；已有且未变化的确认可复用，新增范围才再次确认。"
        "核对已有记忆、资料入口和连接，只补缺失或过时项，并实际保存与读回。反馈脱敏的确认依据和结果；不迁移原文件，也不上传私人路径或记忆正文。"
    ),
    "onboarding.3": (
        "按本步材料建立或更新本机现有定时任务，保留已有任务标识，不重复创建。先确认当前工作与共享范围仍符合本人此前确认；"
        "新增范围请本人决定。每日按本人及实际获分配项目读取当前要求；每周只在有真实工作或问题时，按该项目现行反馈说明整理，"
        "不生成空日报。若尚未分配项目，不把接入测试项目当成业务项目。当前有关新要求须进入工作提醒与执行，不止是文件巡检。"
        "核对计划标识、频率、运行条件及目标项目选择后，在普通 Agent 对话中手动运行一次相同的读取→反馈写入→读回流程，"
        "并记录实际结果；仅配置而未执行不算完成。首次按时自动触发另记，不能用中央定时检查代替。"
    ),
    "project.instructions": (
        "读取项目背景、资料和适用于本人的当前任务。复用机构接入；仅对这个项目补确认本地工作与共享范围，"
        "并把项目、任务入口和正确反馈位置关联到已有本地记忆及定时检查。不要重做机构接入。"
    ),
    "project.feedback": (
        "按实际项目反馈真实变化：平时有重要卡点可随时记录，通常每周汇总一次。"
        "写明周期起止日期和时区、与上次相比的变化；没有新事实就不写空周报。"
        "以下六项只填写有内容的部分，不要求凑齐；写入时删去占位语和空栏目。成果给出名称、版本或日期、共享位置和当前状态；"
        "评审与确认写明对象、版本、提出意见的人及可分享的决定来源，尚未决定就标为线索。"
        "区分人工完成、Agent 参与和未核实的信息；Skill/RAG 区分已读取与实际使用，"
        "本地 Skill 改动不自动成为机构方法。"
        + FEEDBACK_DIVIDER +
        "#### 1. 当前进度与下一步\n<工作推进到了哪里，与上次相比有什么变化；下一步是什么>\n"
        "#### 2. 本期成果／阶段性产出\n<成果名称、版本或日期、获准共享的链接、草稿／待评审／已确认状态>\n"
        "#### 3. 卡点与协作需求\n<已阻塞或潜在风险，需要谁提供什么支持及影响>\n"
        "#### 4. Human Gate 相关线索\n<评审或取舍的对象和版本、谁提出意见、实际决定与来源；不确定就写待确认>\n"
        "#### 5. Skill／RAG 实际使用\n<用于什么工作、已读取还是实际使用、有效或失效之处；无法核实调用情况就写未知>\n"
        "#### 6. 本地 Skill 更新\n<改了什么、为什么、是否试用与是否建议分享；没有更新就写未更新>"
    ),
    "project.periodic": (
        "沿用本人已建立的本地定时任务和标识，不重新接入或另建计划。每日读取适用于本人和实际项目的当前有效要求；"
        "有变化时带入正在做的工作，没有变化就不生成日报。每周只在有真实进展、问题或待判断事项时整理反馈；重要卡点可随时追加。"
    ),
}

_PROTECTED_LINE = re.compile(
    r"(?im)^\s*(?:成员ID|项目ID|反馈ID|身份标记|本次运行标识|接入阶段|接入路径|反馈位置|适用成员|任务入口|项目资料|反馈模板版本)\s*[:：]|\[RDOS\s*写回标识\s*[:：]"
)


def is_template(template_id: str) -> bool:
    return template_id in DEFAULTS


def _snapshot(template_id: str, row: dict | None) -> dict:
    if not is_template(template_id):
        raise ValueError("没有这个可修改的指令模板")
    content = row["content"] if row else DEFAULTS[template_id]
    version = row["version"] if row else 1
    return {"id": template_id, "version": version, "content": content,
            "content_hash": store.digest(content), "source_url": f"rdos-template:{template_id}"}


def snapshot(template_id: str) -> dict:
    """Read the one active version; code defaults are version 1 until approved."""
    if not is_template(template_id):
        raise ValueError("没有这个可修改的指令模板")
    return _snapshot(template_id, store.get("instruction_template", template_id))


def text(template_id: str) -> str:
    return snapshot(template_id)["content"]


def feedback_parts() -> tuple[str, str]:
    """One active version supplies both the guidance and six work sections."""
    return text("project.feedback").split(FEEDBACK_DIVIDER, 1)


def versions_for(purpose: str, stage: int = 0) -> dict[str, int]:
    """The version set that an instruction capability was minted against."""
    if purpose == "onboarding":
        ids = [f"onboarding.{stage}"] if stage in (0, 1, 3) else []
    elif purpose == "project":
        ids = ["project.instructions", "project.feedback"]
    else:
        ids = []
    return {item: snapshot(item)["version"] for item in ids}


def _valid_content(value: str, template_id: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 6000:
        raise ValueError("指令模板需有正文，且不超过 6000 字")
    if "\x00" in value or _PROTECTED_LINE.search(value):
        raise ValueError("成员、项目、反馈位置和写回字段由系统生成，不能写进可修改模板")
    if template_id == "project.feedback":
        if value.count(FEEDBACK_DIVIDER) != 1:
            raise ValueError("反馈说明只能保留一处反馈栏目分隔")
        guidance, sections = value.split(FEEDBACK_DIVIDER, 1)
        headings = re.findall(r"(?m)^####\s+\d+\.\s+([^\n]+)$", sections)
        if not guidance.strip() or len(headings) != 6 or any(
            sum(name in heading for heading in headings) != 1 for name in _FEEDBACK_HEADINGS
        ) or any(sum(name in heading for name in _FEEDBACK_HEADINGS) != 1 for heading in headings
        ) or re.search(r"(?m)^#{1,3}\s", sections):
            raise ValueError("反馈模板需保留六类栏目各一处，栏目可调整顺序和说明")
    return value.strip()


def apply_approved(template_id: str, base_hash: str, old_text: str, new_text: str,
                   operation_id: str, actor_id: str) -> dict:
    """Atomically activate an admin-approved patch, with local retry identity.

    Caller obtains approval through the existing proposal endpoint. This
    method checks the administrator again because it is the mutation boundary.
    A local SQLite commit has a definite outcome, unlike a remote write.
    """
    if not is_template(template_id):
        raise ValueError("没有这个可修改的指令模板")
    if not operation_id or len(operation_id) > 150:
        raise ValueError("缺少有效发布标识")
    if not isinstance(old_text, str) or not old_text:
        raise ValueError("请先选中当前模板中需要修改的原文")
    if not isinstance(new_text, str) or not new_text.strip():
        raise ValueError("模板修改内容不能为空")
    from server import phase1_operations as ops
    operation = store.get('operation', operation_id) or {}
    authorization = operation.get('authorization') or {}
    proposal = next((p for p in store.items('proposal') if p.get('publication_operation_id') == operation_id), None)
    if not proposal or (proposal.get('target_id'), proposal.get('base_hash'), proposal.get('old_text'), proposal.get('new_text')) != (template_id, base_hash, old_text, new_text):
        raise PermissionError('模板生效必须来自已确认的同一份草稿')
    ops.require_confirmation(operation_id, actor_id=actor_id, project_id=proposal['project_id'],
                             target_kind='template', target_id=template_id, draft_hash=proposal['draft_hash'],
                             base={'hash': base_hash, 'version': proposal.get('base_version')})
    request_hash = store.digest([template_id, base_hash, old_text, new_text, actor_id])
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = conn.execute("SELECT roles_json,enabled FROM central_users WHERE id=?", (actor_id,)).fetchone()
        if not actor or not actor["enabled"] or "admin" not in json.loads(actor["roles_json"]):
            raise PermissionError("当前账号不能发布指令模板")
        op_row = conn.execute("SELECT payload FROM central_records WHERE kind='template_activation' AND id=?",
                              (operation_id,)).fetchone()
        if op_row:
            operation = json.loads(op_row[0])
            if operation["request_hash"] != request_hash:
                raise ValueError("同一发布标识不能用于不同模板内容")
            return operation["result"]
        row = conn.execute("SELECT payload FROM central_records WHERE kind='instruction_template' AND id=?",
                           (template_id,)).fetchone()
        current = _snapshot(template_id, json.loads(row[0]) if row else None)
        if (base_hash != current["content_hash"] or current["content"].count(old_text) != 1
                or current['version'] != authorization.get('base', {}).get('version')):
            raise ValueError("模板来源已变化，请重新读取、修改并审阅当前版本")
        changed = _valid_content(current["content"].replace(old_text, new_text, 1), template_id)
        if changed == current["content"]:
            raise ValueError("草稿没有修改模板内容")
        result = _snapshot(template_id, {"content": changed, "version": current["version"] + 1})
        now = db.utc_now()
        row_value = {"id": template_id, "content": changed, "version": result["version"],
                     "updated_at": now, "updated_by": actor_id, "operation_id": operation_id}
        conn.execute("INSERT INTO central_records(kind,id,payload,updated_at) VALUES('instruction_template',?,?,?) "
                     "ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                     (template_id, json.dumps(row_value, ensure_ascii=False), now))
        op_value = {"id": operation_id, "request_hash": request_hash, "result": result,
                    "template_id": template_id, "actor_id": actor_id, "created_at": now}
        conn.execute("INSERT INTO central_records(kind,id,payload,updated_at) VALUES('template_activation',?,?,?)",
                     (operation_id, json.dumps(op_value, ensure_ascii=False), now))
    return result
