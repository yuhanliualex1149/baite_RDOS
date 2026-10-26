"""Provision only the approved phase-3 objects. Requires explicit --apply.

Run with the lab's dedicated Python, while its Panel is stopped. Every remote
mutation is journaled before dispatch, then independently reconciled. A lost
response never triggers a second create, including after restarting this tool.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from lab import config
from server import phase3_feishu as fs


def client_guide(resources):
    """Executable remote instructions; public resource IDs, no private paths."""
    env_id, generation = resources["env_id"], resources["generation"]
    event = {"id": "evt_STABLE_NODE_TASK_VERSION_ACTION_1", "operation_id": "op_STABLE_NODE_TASK_VERSION_ACTION_1",
             "env_id": env_id, "generation": generation, "project_id": fs.PROJECT_ID,
             "event_type": "submission", "claimed_node_id": "node_lab_phase3_codex", "identity_mode": "role_test",
             "task_refs": [{"task_id": "TASK_ID_FROM_CURRENT_RECORD", "requirements_version": 1}],
             "recipients": ["agent", "human"], "body": "本次实际完成内容、依据及尚未核实项的完整说明；长篇产物另存原生文档。",
             "artifact_refs": [{"document_id": "NEW_NATIVE_DOCUMENT_TOKEN", "revision": 1,
                                "content_hash": "SHA256_OF_EXACT_FULL_FETCHED_CONTENT"}],
             "origin": "node_report", "created_at": "REPLACE_WITH_ACTUAL_ISO8601_TIME"}
    fields = {"事件ID": event["id"], "类型": event["event_type"], "自报节点": event["claimed_node_id"],
              "任务引用": fs._json(event["task_refs"]), "操作ID": event["operation_id"], "记录数据": fs._json(event)}
    base = fs.TENANT + "/base/" + resources["base_token"]
    return f'''# RDOS3 role_test 客户端执行说明

待讨论，不授权任务开始。本人必须先在Panel明确开始具体任务。收到本说明本身不等于收到制作任务。

## 唯一资源与身份

- 项目：`{fs.PROJECT_ID}`
- 环境：`{env_id}`；代次：`{generation}`。每条事件都原样填写这三个范围字段。
- 任务登记：{base}?table={resources['tasks_table']}
- 协作记录（两个节点唯一写回表）：{base}?table={resources['events_table']}
- Base token：`{resources['base_token']}`；任务表：`{resources['tasks_table']}`；事件表：`{resources['events_table']}`。
- project产物根：{fs.TENANT}/drive/folder/{fs.HUMAN_FOLDER}
- agent产物根：{fs.TENANT}/drive/folder/{fs.AGENT_FOLDER}
- 节点只能自报为`node_lab_phase3_codex`或`node_lab_phase3_workbuddy`，分别对应本次实际打开的客户端。两个客户端共用现有飞书user账号；可核实的是共享调用账号，节点归属与独立成员隔离均未验证。不要自报已验证身份或本人批准。

只用自己已有的飞书工具或原生CLI访问上述资源。不要读取Panel文件、其他本机工作区、localhost任务/反馈接口、六份旧草案或正式业务资料；不启动Runner、不新增定时任务、不修改权限、账号连接或全局规则。

## 先读取有效任务

读取任务表全部页的`记录数据`文本，解析完整JSON；可读列仅方便浏览，不替代JSON。只处理project_id/env_id/generation与本说明一致、executor_node_ids包含本节点的任务。无适用任务就如实回报没有任务，不创建任务、不开始制作。

任务要求在goal、input_refs、deliverable、output_zone、completion_rule等字段中。requirements_version是业务要求版，record_revision是记录修订，来源/产物还有各自文档版本；三者不能混用。关键操作前及提交前重新读当前任务。只在business_status=active时新增执行；暂停或关闭后停止新增工作并按真实状态提交paused_ack/closed_ack，不宣称远程进程已被终止。旧要求的晚到成果保留并注明旧版，不拿它满足新版。节点不得修改任务表、目标、要求、状态或批准字段。

CLI读取示例（可使用等价已有工具；不要改变资源）：

```text
lark-cli base +record-list --as user --base-token {resources['base_token']} --table-id {resources['tasks_table']} --format json --limit 200 --offset 0
```

按返回has_more/数量继续offset分页，不只读首页。CLI可能返回items/records对象，也可能返回fields、record_id_list与data二维数组，须按字段名与record_id配对恢复完整记录。

## 原生文档与确切版本

进展、问题、Review、更正、读取、开始、提交及停止回执的**完整正文都写入同一张协作记录表**，不得用文档协作代替写回。长篇制作成果可以另建原生文档，表内的 artifact_refs 必须引用独立读回的确切版本和完整正文哈希。Agent Review 本身不要求另建文档，其完整评审意见写在 event_type=review 的 body，artifact_refs 用 []。

仅读取任务input_refs明确引用的本轮新文档，独立核对引用中的revision和content_hash。新产物只能建在任务output_zone对应的根；保留旧稿，新要求版另建新产物。没有并发保证时另存新版本，不覆盖人类原文。

客户端新产物必须使用这个精确标题前缀，否则中央不会自动读取其正文：

```text
RDOS3 role_test {fs.PROJECT_ID} TASK_ID vREQUIREMENTS_VERSION NODE_ID [P3-20位小写十六进制标识] 简短产物名
```

TASK_ID、REQUIREMENTS_VERSION、NODE_ID取当前任务和本客户端实际节点；v与数字相邻。先确定稳定creation_operation_id，同一次创建一直复用它。标识算法为SHA256(canonical_json([env_id, creation_operation_id]))的前20位小写十六进制。canonical_json递归按键排序、不转义中文、逗号和冒号后无空格（Python：json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))）。

用现有飞书工具建原生文档，标题必须完整。CLI v2创建时，Markdown正文开头用`<title>经HTML转义的完整标题</title>`明确标题，后接空行和完整正文；使用`docs +create --api-version v2 --as user --parent-token 指定根token --doc-format markdown --content -`，正文从stdin输入。

创建后独立完整读取文档；不要从创建返回值或摘要推断成功。CLI完整读取：

```text
lark-cli docs +fetch --api-version v2 --as user --doc NEW_DOCUMENT_TOKEN --format json --doc-format markdown --detail simple --scope full
```

此处scope=full读取全文；detail=simple仅表示Markdown不带XML块属性，不能换成outline/keyword/range/section。当前原生CLI不接受Markdown与detail=full的组合。

保存完整返回document.content（或实际协议完整content字段）与revision_id。content_hash必须是该完整字符串UTF-8字节的SHA256，保留标题、所有空白、换行与正文，不能对去标题后的body、Markdown文件、CLI输出JSON整体或屏幕摘要计算。事件artifact_refs填写document_id、实际revision、该content_hash。原生导出可能含标题；完整实际导出正文与哈希是版本证据。

中央仅在一次受控同步时枚举这两个根的元数据，排除旧对象，并且只在有效事件关联已分配任务、当前要求版、正确产物根及上述标题后，独立完整读取新产物。新建文档不会自动批准任务，标题也不能伪造本人授权。

## 协作事件与表格列

事件类型支持read、started、progress、submission、review、blocker、clarification、correction、paused_ack、closed_ack、check_result、project_feedback。一般project_feedback可不关联任务；其他执行回执必须带合法task_refs。一次事件可关联多个任务及各自要求版。所有回执和评审的完整正文均写在body；长篇制作成果可留在文档并引用确切版本。

以下为结构示例，必须替换占位值、任务要求版、实际时间与实际产物版本；不要照抄成已完成证据。read/started/停止回执等没有产物时artifact_refs用[]。Agent Review使用event_type=review，完整意见在body、artifact_refs为[]，不代表本人专业验收；制作结果使用submission。完成规则以当前任务completion_rule为准：human要本人确认确切文档产物，human_review要本人确认协作记录中的确切Review事件；artifact_present/source_versions_match也只能由中央独立核对满足预授权规则后结束。

```json
{json.dumps(event, ensure_ascii=False, indent=2)}
```

一条事件新建一条事件表记录，不覆盖旧行。记录数据字段是上述完整JSON字符串；可读列必须与其保持精确一致：事件ID=id、类型=event_type、自报节点=claimed_node_id、任务引用=canonical_json(task_refs)、操作ID=operation_id。所有这些列都是文本。以下是与示例JSON完全对应的fields示例：

```json
{json.dumps(fields, ensure_ascii=False, indent=2)}
```

写入示例：`lark-cli base +record-upsert --as user --base-token {resources['base_token']} --table-id {resources['events_table']} --json '<上述真实fields JSON>'`。可使用已有飞书工具做同样的新增。不要向任务表写节点回执。

id和operation_id在同一逻辑事件中稳定不变；发送前先独立查表，若已存在同ID同完整JSON则复用，若同ID不同内容则停止并报告冲突。同一次创建/写入超时或返回不明时，只能继续查询原稳定标识及完整正文，不得再次create、换新ID绕过或宣布成功。尚查不到就保留未核实状态。更正必须新增事件并用correction_of指向原事件ID，保留原记录。

写后用独立record-list或record-get重新读取完整fields与记录数据，核对原始事件所有字段和稳定标识；写响应或可读列匹配都不足以证明完整正文成功。随后报告实际事件ID、产物URL与确切版本，以及未核实项，等待本人通过Panel同步和决定。
'''


def seed_documents(resources=None):
    documents = (
        ("human", "阶段3 待讨论需求", "# 待讨论需求\n\n本轮为两个测试客户端协作验证，尚未开始任何制作任务。\n\n拟讨论的演示需求：介绍一个虚构学习产品，先包含两个使用场景，待本人从Panel明确开始后制作初稿。\n\n目标读者：希望了解产品用途的人。交付物：便于人类阅读的飞书原生文档。\n\n这份讨论稿不是任务批准；有效要求以本轮任务登记的当前要求版本为准。"),
        ("human", "阶段3 项目说明", "# 阶段3 测试项目\n\n这是独立本地实验副本的协作测试。飞书任务登记及有效变更记录是业务主记录，文档保留完整正文与版本。\n\nCodex与WorkBuddy是同一负责人使用的两个测试客户端，身份性质为role_test；共用现有飞书user账号，不证明两位真实成员或成员级权限隔离。\n\n制作、Agent Review、本人专业验收分别记录。任务调整保留旧要求与旧产物；取消只发停止要求，不宣称远程进程已停止。\n\n本轮不启用真实模型、定时任务或正式业务资源。"),
        ("human", "阶段3 人类任务说明", "# 人类任务说明\n\n建置时尚无已批准任务；后续以任务登记当前记录为准。\n\n本人从Panel核对具体草稿、要求版本和产物版本后执行开始、调整、暂停、恢复、验收关闭或取消。\n\n待讨论需求及Agent Review不构成本人批准。人修改产物也不自动改变任务要求。\n\n后续说明是带来源版本的派生文档；有效要求以任务登记当前版为准。"),
        ("agent", "阶段3 Agent执行说明", "# Agent执行说明\n\n这是本轮协作测试的窄范围入口，不是全局规则。两个客户端仅通过指定飞书任务登记、协作记录及本轮新文档交换业务内容。\n\n先读取当前任务完整记录、要求版本、分配节点和允许产物位置。无适用任务则只报告没有任务，不自建任务、不宣布开始。\n\n协作记录支持读取、开始、进展、成果、卡点、澄清、Agent Review、更正与停止回执。事件保留稳定ID、任务及要求版本，产物引用绑定确切文档版本和正文哈希。\n\n任务要求或状态由本人经Panel管理；节点不修改目标、要求版本或批准状态。暂停或取消后停止新增工作，并按真实情况回执。\n\n旧要求的晚到产物保留归档，不自动满足新要求。不要用“老板同意”的自报文字替代可信会话决定。\n\n禁止读取本轮根内六份旧草案、正式资源、其他项目、凭据和Panel本地文件；不使用localhost任务或反馈接口，不新建定时任务。"),
        ("human", "文档并发验证", "# 文档并发验证\n\n这是专用临时并发验证正文；待讨论，不授权任务开始。\n\nP3_CAS_PROBE_BASE\n\n保留这段验证前后的原文。"),
    )
    result = tuple((zone, "RDOS3 role_test " + title, content + "\n\n待讨论，不授权任务开始。")
                   for zone, title, content in documents)
    if resources is not None:
        result = (*result[:3], ("agent", result[3][1], client_guide(resources)), *result[4:])
    return result


class Provisioner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root = Path(cfg["root"])
        self.path = self.root / "runtime/phase3_resources.json"
        self.journal_path = self.root / "runtime/phase3_setup_operations.json"
        binary_hash = hashlib.sha256(Path(config.PHASE3_CLI).read_bytes()).hexdigest()
        if cfg.get("phase3", {}).get("enabled") and cfg["phase3"]["cli_sha256"] != binary_hash:
            fs._fail("binary_changed", "建置CLI与已固定校验值不同，不能静默重新固定")
        cfg["phase3"] = {"enabled": True, "cli_path": config.PHASE3_CLI,
                         "cli_sha256": binary_hash,
                         "registry_path": str(self.path)}
        if self.path.exists():
            self.resources = config.read_json(self.path)
            fs._validate_registry(self.resources, cfg, ready=False)
            if self.resources.get("cli_sha256") not in (None, binary_hash):
                fs._fail("binary_changed", "资源登记的建置CLI校验值不同")
        else:
            state = config.instance(cfg)
            self.resources = {"schema_version": 1, "state": "provisioning", "env_id": cfg["env_id"],
                              "generation": state["generation"], "project_id": fs.PROJECT_ID, "backend": "feishu_test",
                              "human_folder": fs.HUMAN_FOLDER, "agent_folder": fs.AGENT_FOLDER,
                              "base_token": "", "tasks_table": "", "events_table": "", "documents": [],
                              "account_fingerprint": "0" * 64, "excluded_object_ids": []}
        self.resources["cli_sha256"] = binary_hash
        self.executor = fs._CLIExecutor(fs._TOKEN, cfg, self.resources, setup=True)
        self.backend = fs.FeishuBackend(cfg, self.resources, self.executor)
        self.operations = config.read_json(self.journal_path) if self.journal_path.exists() else {}

    def save(self):
        config.write_json(self.path, self.resources)
        config.write_json(self.journal_path, self.operations)

    def operation(self, key, action, discover):
        old = self.operations.get(key)
        existing = fs._visible(discover) if old else discover()
        if existing:
            self.operations[key] = {"state": "verified", "object_id": existing}
            self.save()
            return existing
        if old:
            fs._fail("write_uncertain", "建置操作尚未核对，已保留标识；不会重复创建：" + key)
        self.operations[key] = {"state": "sent"}
        self.save()
        try:
            action()
        except fs.FeishuTestError as exc:
            if exc.code != "write_uncertain":
                raise
        existing = fs._visible(discover)
        if not existing:
            self.operations[key]["state"] = "uncertain"
            self.save()
            fs._fail("write_uncertain", "建置可能已提交，未独立读回；再次运行只核对：" + key)
        self.operations[key] = {"state": "verified", "object_id": existing}
        self.save()
        return existing

    def root_object(self, folder, name, kind):
        matches = [f for f in self.executor.list_root(folder) if f.get("name") == name and f.get("type") == kind]
        if len(matches) > 1:
            fs._fail("remote_conflict", "同一建置标识有多个对象，须核对")
        if matches and matches[0]["token"] in self.resources.get("excluded_object_ids", []):
            fs._fail("out_of_scope", "同名旧资源不能用于本轮建置")
        return fs._identifier(matches[0]["token"]) if matches else None

    def table_object(self, name):
        matches = [t for t in self.executor.tables() if t.get("name") == name]
        if len(matches) > 1:
            fs._fail("remote_conflict", "指定Base有同名重复表")
        return fs._identifier(matches[0].get("table_id") or matches[0].get("id")) if matches else None

    def run(self):
        # No login, refresh authorization, scopes, configuration or permissions
        # are changed. auth status reports the existing cached user identity.
        fingerprint = fs.account_fingerprint(self.executor._run(["auth", "status"]))
        old = self.resources["account_fingerprint"]
        if old != "0" * 64 and old != fingerprint:
            fs._fail("account_changed", "建置账号与固定账号不同")
        self.resources["account_fingerprint"] = fingerprint
        if not self.resources.get("baseline_captured"):
            # List metadata only. Old document bodies are never fetched.
            existing = self.executor.list_root(fs.HUMAN_FOLDER) + self.executor.list_root(fs.AGENT_FOLDER)
            self.resources["excluded_object_ids"] = [f["token"] for f in existing]
            self.resources["baseline_captured"] = True
        self.save()
        name = "RDOS阶段3协作登记 [P3-" + fs._hash(self.resources["env_id"])[:12] + "]"
        if not self.resources["base_token"]:
            self.resources["base_token"] = self.operation("base", lambda: self.executor.create_base(name),
                lambda: self.root_object(fs.AGENT_FOLDER, name, "bitable"))
            self.save()
        elif self.root_object(fs.AGENT_FOLDER, name, "bitable") != self.resources["base_token"]:
            fs._fail("out_of_scope", "登记Base未在指定Agent根独立找到")
        for key, name in (("tasks_table", "阶段3任务登记"), ("events_table", "阶段3协作记录")):
            if not self.resources[key]:
                self.resources[key] = self.operation(key, lambda name=name: self.executor.create_table(name),
                                                    lambda name=name: self.table_object(name))
                self.save()
            elif self.table_object(name) != self.resources[key]:
                fs._fail("out_of_scope", "登记表与本轮Base不一致")
        for key, names in (("tasks_table", fs.TASK_FIELDS), ("events_table", fs.EVENT_FIELDS)):
            table = self.resources[key]
            for field_name in names:
                def discover(table=table, field_name=field_name):
                    data = self.executor.fields(table)
                    fields = data.get("items", data.get("fields", []))
                    for field in fields:
                        name = field if isinstance(field, str) else field.get("field_name", field.get("name"))
                        if name == field_name:
                            if isinstance(field, dict) and field.get("type") not in (None, 1, "text", "Text"):
                                fs._fail("invalid_fields", "阶段3指定字段必须是文本")
                            return field.get("field_id", field.get("id", field_name)) if isinstance(field, dict) else field_name
                    return None
                self.operation(key + ".field." + field_name,
                               lambda table=table, field_name=field_name: self.executor.create_field(table, field_name), discover)
        for index, (zone, title, content) in enumerate(seed_documents(self.resources)):
            document = self.backend.create_document(zone, title, content, "phase3-seed-document-" + str(index + 1))
            if index == 4:
                self.resources["cas_probe_document"] = document["id"]
                self.save()
        self.probe_revision_conflict(self.resources["cas_probe_document"])
        # Read both complete tables and all complete document bodies. This does
        # not constitute a real client run or human acceptance.
        self.backend.list_tasks()
        self.backend.list_events()
        for document in self.resources["documents"]:
            self.backend.read_document(document["id"])
        physical_tables = self.executor.tables()
        self.resources["state"] = "ready"
        self.resources["business_table_count"] = 2
        self.resources["native_table_count"] = len(physical_tables)
        self.resources["unexposed_native_table_count"] = sum(t.get("table_id", t.get("id")) not in
            (self.resources["tasks_table"], self.resources["events_table"]) for t in physical_tables)
        self.resources["cli_sha256"] = self.cfg["phase3"]["cli_sha256"]
        self.save()
        return {"state": "ready", "documents": len(self.resources["documents"]), "tables": 2,
                "business_tables": 2, "physical_tables": len(physical_tables),
                "account_fingerprint": fingerprint, "registry_path": str(self.path),
                "cas_verified": self.resources.get("cas_verified", False),
                "phase3_configuration": self.cfg["phase3"]}

    def probe_revision_conflict(self, identifier):
        """Exercise current and stale revisions only on the new probe document."""
        if identifier != self.resources.get("cas_probe_document"):
            fs._fail("out_of_scope", "并发验证只能使用本轮专用新文档")
        key = "document_revision_probe"
        probe = self.operations.get(key)
        if probe and probe.get("state") == "verified":
            if not probe.get("cas_verified"):
                self.backend.create_document("human", "RDOS3 role_test 并发验证保留版",
                                             fs._body(probe["first_expected"], probe["title"]), "phase3-cas-preserved-version")
            return
        current = self.backend.read_document(identifier)
        if probe is None:
            if current["content"].count("P3_CAS_PROBE_BASE") != 1:
                fs._fail("probe_conflict", "专用验证文档缺少唯一原始片段")
            probe = {"state": "prepared", "title": current["title"], "base_revision": current["revision"], "before": current["content"],
                     "first_expected": current["content"].replace("P3_CAS_PROBE_BASE", "P3_CAS_PROBE_CURRENT", 1)}
            self.operations[key] = probe
            self.resources["cas_verified"] = False
            self.save()
        if probe["state"] == "prepared":
            probe["state"] = "current_sent"
            self.save()
            try:
                self.executor.update_document(identifier, probe["base_revision"], "P3_CAS_PROBE_BASE", "P3_CAS_PROBE_CURRENT", probe=True)
            except fs.FeishuTestError as exc:
                if exc.code != "write_uncertain":
                    raise
            current = self.backend.read_document(identifier)
        if probe["state"] == "current_sent":
            if current["content"] != probe["first_expected"] or str(current["revision"]) == str(probe["base_revision"]):
                fs._fail("write_uncertain", "并发验证首写未独立读回，保留状态且不重发")
            probe.update(state="current_verified", first_revision=current["revision"], first_hash=current["content_hash"])
            self.save()
        rejected = False
        if probe["state"] == "current_verified":
            probe["state"] = "stale_sent"
            self.save()
            try:
                response = self.executor.update_document(identifier, probe["base_revision"], "P3_CAS_PROBE_CURRENT", "P3_CAS_PROBE_STALE_ACCEPTED", probe=True)
                rejected = response.get("_revision_rejected") is True
                probe["rejection_evidence"] = response.get("_response_hash", "")
            except fs.FeishuTestError:
                # A timeout or unspecified rejection is not a CAS guarantee.
                rejected = False
            probe["explicit_revision_rejection"] = rejected
            self.save()
        after = self.backend.read_document(identifier)
        unchanged = after["content"] == probe["first_expected"] and str(after["revision"]) == str(probe["first_revision"])
        self.resources["cas_verified"] = bool(probe.get("explicit_revision_rejection") and unchanged)
        probe.update(state="verified", final_revision=after["revision"], final_hash=after["content_hash"],
                     preserved_after_stale=unchanged, cas_verified=self.resources["cas_verified"])
        self.save()
        if not self.resources["cas_verified"]:
            self.backend.create_document("human", "RDOS3 role_test 并发验证保留版",
                                         fs._body(probe["first_expected"], probe["title"]), "phase3-cas-preserved-version")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="创建本轮已批准的新资源；缺此参数只显示计划")
    parser.add_argument("--enable", action="store_true", help="成功核对后在实验lab.json启用phase3适配层")
    args = parser.parse_args()
    cfg = config.load()
    if not args.apply:
        print(json.dumps({"mode": "plan_only", "base_count": 1, "table_count": 2, "document_count": len(seed_documents()),
                          "roots": [fs.HUMAN_FOLDER, fs.AGENT_FOLDER], "old_document_bodies": "never_read"}, ensure_ascii=False))
        return
    process_path = Path(cfg["root"]) / "runtime/process.json"
    if process_path.exists():
        try:
            process = config.read_json(process_path)
            if process.get("env_id") != cfg["env_id"] or type(process.get("pid")) is not int:
                raise ValueError()
            os.kill(process["pid"], 0)
        except (ProcessLookupError, ValueError):
            pass
        else:
            raise SystemExit("Panel仍在运行；请先用实验labctl停止，再进行资源建置")
    try:
        provisioner = Provisioner(cfg)
        result = provisioner.run()
        if args.enable:
            config.write_json(Path(cfg["root"]) / "config/lab.json", cfg)
            result["enabled"] = True
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except fs.FeishuTestError as exc:
        print(json.dumps({"state": "stopped", "code": exc.code, "message": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
