"""The member-facing project feedback wording shared by project and periodic work."""
from __future__ import annotations

def template_version() -> str:
    """Version of the active feedback wording, used by every project entry."""
    from server import central_templates
    return f"project-feedback-v{central_templates.snapshot('project.feedback')['version']}"


def guidance_text() -> str:
    from server import central_templates
    return central_templates.feedback_parts()[0]


def periodic_requirement(project: dict, member: dict) -> str:
    """Text for an ordinary task draft, reviewed through the existing publish flow."""
    from server import central_store as store, central_templates
    feedback_template = central_templates.snapshot("project.feedback")
    periodic_template = central_templates.snapshot("project.periodic")
    feedback_guidance, sections = feedback_template["content"].split(central_templates.FEEDBACK_DIVIDER, 1)

    feedback = project.get("feedback_url") or (
        f"{store.FEISHU_HOST}/docx/{project.get('feedback_id') or project.get('feedback_doc_id')}"
        if project.get("feedback_id") or project.get("feedback_doc_id") else ""
    )
    if not project.get("id") or not member.get("id") or not feedback:
        raise ValueError("项目、成员和已建立的项目反馈入口是起草周期要求的必要条件。")
    return f"""{project.get('name') or '当前项目'}：项目反馈与定期检查

适用成员：{member['id']}；项目：{project['id']}；反馈位置：{feedback}
反馈模板版本：project-feedback-v{feedback_template['version']}
周期要求模板版本：project.periodic@v{periodic_template['version']}

{periodic_template['content']}
{feedback_guidance}
反馈栏目沿用同版本项目说明，填有事实的部分即可：
{sections}
按项目现行说明的同版本格式写入上述反馈位置。关联实际任务和版本；无任务可反馈获准项目工作。每次写入用稳定唯一的反馈ID，结果不确定先查远端，写后读回；不上传私人路径、原始对话、memory 或凭据。反馈实际读取、手动执行和首次真正按时触发分别说明，不能把已发布要求当作本人已执行。
"""


def record_template(*, member_id: str, identity_mode: str, run_label: str, project_id: str) -> str:
    """Headings below record level keep all six sections in one Feishu record."""
    from server import central_templates
    feedback_template = central_templates.snapshot("project.feedback")
    sections = feedback_template["content"].split(central_templates.FEEDBACK_DIVIDER, 1)[1]
    return f"""## 成员标识 {member_id} 本次运行标识 {run_label}
### 项目反馈
记录类型：project_update
成员ID：{member_id}
项目ID：{project_id}
身份标记：{identity_mode}
本次运行标识：{run_label}
反馈ID：{run_label}-{project_id}-<本条唯一后缀>
反馈周期：<起止日期和时区；临时反馈可写实际覆盖时间>
报告时间：<实际形成反馈的时间和时区>
反馈模板版本：project-feedback-v{feedback_template['version']}
更正原反馈ID：<仅更正旧反馈时填写，否则写无>
关联任务及版本：<有则逐项填写实际任务ID和读取的版本；无则写未关联任务>

{sections}

来源与尚未核实部分：<可分享的依据和仍未知的地方>
[RDOS 写回标识:<与本条反馈ID相同>]
"""
