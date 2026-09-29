from __future__ import annotations

from typing import Any, Dict, List


WORKFLOW_VERSION = "v0.3"
WORKFLOW_SOURCE_URL = (
    "https://scn33386s7ui.feishu.cn/file/"
    "KCpIbdTHAo73vvxwkFhcdIOanUe?from=from_copylink"
)
WORKFLOW_SOURCE_TOKEN = "KCpIbdTHAo73vvxwkFhcdIOanUe"
WORKFLOW_SOURCE_HASH = (
    "d816ef17874cb8dff4415fb3c4778782fbd44afe77ead7acd52b90b84f079ca3"
)


CORE_NODES: List[Dict[str, str]] = [
    {"code": "C0", "name": "建立边界与硬约束", "output": "任务、范围、约束与责任边界"},
    {"code": "C1", "name": "定义目标与适用边界", "output": "目标、对象、场景与风险边界"},
    {"code": "C2", "name": "建立研发架构", "output": "能力、课程、产品或项目架构"},
    {"code": "C3", "name": "并行生成原型", "output": "内容、机制、工具与实施支持原型"},
    {"code": "C4", "name": "修改、版本化与复审", "output": "新版、分支与未决问题"},
    {"code": "C5", "name": "形成交付包与实施准备", "output": "可被他人实施的交付包"},
    {"code": "C6", "name": "真实实施与现场判断", "output": "执行记录、变更与用户体验"},
    {"code": "C7", "name": "反馈路由与结果解释", "output": "修改输入、报告与下一周期问题"},
]

GATES: List[Dict[str, str]] = [
    {"code": "G0", "name": "范围 / 立项", "checks": "对象、交付、预算、周期、风险和合作责任"},
    {"code": "G1", "name": "目标 / 架构", "checks": "目标是否具体、可观察并能由产品承载"},
    {"code": "G2", "name": "内容 / 机制 / 风险", "checks": "适配、准确性、安全、规则与认知负荷"},
    {"code": "G3", "name": "设计 / 合规 / 制作", "checks": "品牌、空间、合同、招采和可制作性"},
    {"code": "G4", "name": "实施 readiness", "checks": "人员、材料、场地、平台与备选方案"},
    {"code": "G5", "name": "现场继续 / 调整", "checks": "安全、理解、时间、参与和临时变更"},
    {"code": "G6", "name": "版本发布", "checks": "反馈关闭、适用范围与材料一致性"},
    {"code": "G7", "name": "评估 / 报告 / 验收", "checks": "指标、数据、解释和交付是否成立"},
]

DEVELOPMENT_MODES = [
    {"value": "new_product", "label": "新产品 / 无可靠母版"},
    {"value": "existing_new_funder", "label": "既有母版 / 新资方或年度"},
    {"value": "existing_new_context", "label": "既有母版 / 新人群或场域"},
    {"value": "known_issue_iteration", "label": "旧版问题明确 / 迭代"},
]

PRODUCT_TYPES = [
    {"value": "course", "label": "课程"},
    {"value": "picture_book", "label": "绘本"},
    {"value": "game", "label": "游戏 / 桌游 / 沙盘"},
    {"value": "camp_pbl", "label": "营会 / PBL"},
    {"value": "large_event", "label": "嘉年华 / 大型活动"},
    {"value": "space_system", "label": "空间 / 行为系统"},
    {"value": "digital_remote", "label": "数字 / 远程支教"},
    {"value": "video", "label": "视频"},
    {"value": "venue_study", "label": "场馆研学"},
    {"value": "compliance_trust", "label": "合规 / 信托"},
    {"value": "other", "label": "其他"},
]

DELIVERY_SCALES = [
    {"value": "single_site", "label": "单点 / 小规模"},
    {"value": "multi_site", "label": "多地 / 伙伴复制"},
    {"value": "external_supplier", "label": "外部供应商 / 实物制作"},
    {"value": "outcome_evidence", "label": "需要成效说明"},
]

NODE_STATUSES = ("not_started", "in_progress", "complete", "blocked", "not_applicable")
TODO_STATUSES = ("open", "in_progress", "blocked", "done")


def catalog() -> Dict[str, Any]:
    return {
        "version": WORKFLOW_VERSION,
        "source_url": WORKFLOW_SOURCE_URL,
        "expected_hash": WORKFLOW_SOURCE_HASH,
        "core_nodes": CORE_NODES,
        "gates": GATES,
        "development_modes": DEVELOPMENT_MODES,
        "product_types": PRODUCT_TYPES,
        "delivery_scales": DELIVERY_SCALES,
        "boundary": (
            "这是基于历史证据形成的参考模型。Panel 用它汇总事实和提示缺口，"
            "不强制 Local Agent 的执行顺序。"
        ),
    }
