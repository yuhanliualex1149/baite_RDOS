"""The three one-time tasks readable by both original and RDOS 2.0 clients.

Only these fixed IDs receive a legacy-column projection.  The 2.0 task and
its authorization/readback remain the authority; these columns are a bridge
for the original scheduled readers, not a second task publication path.
"""
from __future__ import annotations

from cutover import production_config as formal


UPGRADES = {
    'RDOS-2-UPGRADE-YUHAN': {
        'node_id': 'node_yuhan', 'member_id': 'mem_p6BbcYJrwAcv_M5Y',
        'legacy_project_id': 'prj_0d9b2a0ecec486cdf8', 'label': '雨寒 Codex',
    },
    'RDOS-2-UPGRADE-XIAOFEI': {
        'node_id': 'node_xiaofei', 'member_id': 'mem_x_rLn9VBAe_z8xol',
        'legacy_project_id': 'rdos-self', 'label': '潇斐',
    },
    'RDOS-2-UPGRADE-DUODUO': {
        'node_id': 'node_duoduo', 'member_id': 'mem_g3nKLUxRovywCSTk',
        'legacy_project_id': 'rdos-self', 'label': '多多',
    },
}


def instructions(task_id: str) -> str:
    item = UPGRADES[task_id]
    node = item['node_id']
    entry = formal.PUBLIC_URL + '/nodes/' + node + '/start'
    extra = ('原先“只读、不写回”的本节点限制须经本任务核对后，改为按新版规范将实际进展、问题和结果追加到原 Base 的协作记录表；不再新增旧反馈文档。'
             if node == 'node_duoduo' else
             '升级后的实际进展、问题和结果追加到原 Base 的协作记录表；旧反馈文档只保留历史。')
    return (f'为 {item["label"]} 原位更新 RDOS 工作协作方式。\n'
            f'从现有定时任务自然读取本条任务；入口：{entry} 。读取入口中的节点配置，按其 SHA256 校验并启用当前 RDOS 执行 Skill。'
            '只使用已获准的飞书连接和现有客户端能力，不安装新的后台服务。\n'
            '原位更新本节点已有的每日任务检查和每周反馈安排，使其调用当前 Skill；保留两条安排各自的 ID、时间、时区、启停状态和会话／工区绑定，不重建安排。'
            f'{extra}\n'
            '先核对旧任务及定时安排，再修改；完成后把实际 Skill 版本、两条安排的保留情况及必要问题写入同一协作记录表，并独立读回。'
            '若本地持久规则、飞书授权或终端权限拒绝上述修改，暂停升级并如实报告，不绕过规则或把已发布任务当作已完成。')


def task_payload(task_id: str) -> dict:
    """Inputs for the existing authenticated task-draft/action service."""
    item = UPGRADES[task_id]
    return {
        'task_id': task_id, 'title': '更新 RDOS 工作协作方式',
        'goal': instructions(task_id), 'deliverable': 'Skill 已启用，原有两条定时安排原位更新；升级回执已在协作记录表独立读回。',
        'executor_node_ids': [item['node_id']], 'task_type': 'action',
        'completion_rule': {'kind': 'receipt_recorded'},
        'output_zone': 'agent', 'dispatch_to': ['agent'], 'return_to': ['agent'],
        'deliverable_audience': ['admin'],
    }


def legacy_columns(task: dict, operation_id: str) -> dict:
    """Project a verified task into the original eleven-column reader format."""
    item = UPGRADES.get(task.get('task_id'))
    if not item:
        return {}
    if (task.get('project_id') != formal.DEFAULT_PROJECT
            or task.get('executor_node_ids') != [item['node_id']]
            or task.get('title') != '更新 RDOS 工作协作方式'
            or task.get('goal') != instructions(task['task_id'])
            or task.get('task_type') != 'action'
            or task.get('completion_rule') != {'kind': 'receipt_recorded'}
            or task.get('business_status') not in {'active', 'paused', 'pending_review', 'closed'}):
        raise ValueError('升级任务的执行对象或要求与固定切换方案不一致')
    # The original status and nature columns are Feishu single-selects.  The
    # existing CLI accepts their option text and returns a one-item array.
    return {
        '项目ID': item['legacy_project_id'],
        '发布状态': '已发布' if task['business_status'] in {'active', 'pending_review'} else '停用',
        '任务性质': '一次性',
        '写回标识': operation_id,
        '适用成员': item['member_id'],
        '版本': str(task['requirements_version']),
        '任务类别': 'work',
        '任务说明': instructions(task['task_id']),
    }
