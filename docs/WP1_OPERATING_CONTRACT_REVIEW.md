# WP1 Operating Contract Review

状态：本地自测通过；未部署正式云端，未触碰现有真实 Runner。

## Schema 与权威来源

- `rdos_contract.py` 定义唯一 v1 结构及严格校验。`contract_version` 是格式版本，`contract_revision` 是发布序号，`shared_revision` 是全局资料序号；Hash 使用规范化 JSON。
- 管理员仅能修改组织说明、Selected RAG 是否必读、Shared Skill 的推荐／可选政策。飞书当前文件、项目飞书文件夹、Panel 已发布 Skill 的权威边界及 Human Decision 禁令均锁定。
- 原有七种事件全部保留；Agent-facing projection 不包含 Runner 专用安装诊断事件。
- 原 Markdown 规则在 v6→v7 迁移时完整进入 `organization_guidance`；此后 `global_rules.md` 只由 Contract 确定性生成。

## API、数据库与本地输出

- `GET/PUT /api/admin/global-contract`；历史查询与恢复。PUT 需要当前 `expected_contract_revision`，无内容变化不增加 revision；恢复是创建新版本，不改写历史。旧 `/api/admin/global-rules` GET 保留生成视图，PUT 明确返回 409。
- SQLite v7 增加 `global_contract_history`，当前指针保存在 settings；迁移前使用现有 SQLite backup API 自动备份。
- 共享快照含完整 Contract 及兼容旧 Runner 的生成式 `global_rules.content`。新版 Runner 验证版本、Hash、受保护字段和文件后，在同一原子快照内生成 `shared/global_contract.json`、`control/agent_contract.json`、`shared/global_rules.md`。全局 `runtime.json` 与快照 manifest 包含版本、修订号、Hash 和共享 revision。
- `shared/control` 的只读目前是组织协议与完整性校验，不是同一用户下的操作系统权限强制。

## 自测证据

- `python -m unittest discover -s tests -q`：71 项通过、5 项平台条件跳过。
- `tests/e2e_demo.py`：3 个隔离 Runner 的同步、协作、回传、离线、重启和旧能力通过。
- `tests/browser_onboarding.py`：本机 Chrome 真实浏览器发布和恢复 Contract、旧接入流程、无页面 error/warn 通过。
- `tests/frozen_runtime_e2e.py`：前一版冻结 Runtime 对新版服务端快照仍可心跳、同步、完成诊断 ACK，验证旧 Runner 兼容。
- 专项测试覆盖旧文本迁移与备份、无变化不 bump、历史恢复、过期提交与额外字段拒绝、坏 Hash／缺字段／未知版本不替换 last-known-good。

## 下一包前提与未解决项

WP1 自测门槛通过，可以进入 WP2。WorkBuddy 普通任务模式是否自动读取项目入口尚无实机证据；WP2 必须先验证，失败即停在该包。正式云端仍为旧版本，本包未发布。
