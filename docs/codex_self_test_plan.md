# RDOS Codex 自测计划

## 通过标准

1. 未登录不能访问管理 API；管理员密码为带盐 Hash，登录、退出、过期与修改密码有效。
2. Runner Token 只能代表自己的节点；停用或轮换后旧 Token 立即失效。
3. 可动态创建 3 个及第 4 个节点，业务代码没有固定节点枚举或真人姓名。
4. RAG 第一层 Markdown 和 docx 可同步；子文件夹不递归，其他类型明确展示。
5. 快照先完整校验再切换；失败保留 last-known-good，第 10 次进入 `needs_admin`。
6. Global Rules 只有管理员发布；Skill Proposal 去重并支持 Publish / Return。
7. Activity 只接收元数据，额外 Prompt 或完整工作内容字段被拒绝。
8. 普通协作不进管理员待办；Escalate、风险、对外发布、不可逆操作进入待判断。
9. Panel 和 Runner 重启可恢复；未 ACK 的 outbox 不归档；网络恢复后补传。
10. 浏览器登录、创建节点、规则、Skill、RAG、异常、Token 操作无 error/warn。

## 自动化命令

```bash
python -m unittest tests.test_api tests.test_runner tests.test_rag_sync -v
python tests/e2e_demo.py
```

单元测试使用隔离临时数据库和模拟飞书适配器，不修改正式数据。E2E 会启动真实 Control Server 与 3 个通用 Runner，验证原子同步、事件、协作、离线、Token 轮换、重启与补传。

## 真实飞书只读 Smoke Test

```bash
BAITE_SELECTED_RAG_FOLDER_TOKEN=KQjyf6KrxlZ24MdkHirc9TP0n2d \
python tests/feishu_smoke.py
```

验收：第一层当前 6 项全部读取，其中 5 个 Markdown 下载、1 个 docx 转 Markdown；记录 token、更新时间、Hash，不执行任何飞书写操作。

## 静态边界检查

```bash
rg -n '潇斐|多多|雨寒|xiaofei|duoduo' server runner tests simulate_agent.py start_local.py
rg -n 'prompt|chain.of.thought|思考过程' server runner
```

第一条应无结果。第二条只能出现在明确拒绝或边界说明中，不能有采集字段或页面。
