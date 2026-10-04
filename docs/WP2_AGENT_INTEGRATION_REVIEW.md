# WP2 Agent Integration Review

状态：隔离本地自测通过；未部署云端，未改动正在运行的真实 Runner。

## 实现边界

- Runner 只把已校验的 Operating Contract 投影为 Agent 可读文件，不启动或控制 WorkBuddy、Codex，也不选择 Skill／Workflow。
- 全局快照同时包含 `control/agent_contract.json` 与 `control/READ_FIRST.md`。两者由同一 Contract 投影生成，记录 Contract 版本／修订号／Hash、共享 revision、生成时间、资料入口、目录边界、outbox 事件和 Human Gate 禁令。
- Workspace 根的 `AGENTS.md` 与 `CODEBUDDY.md` 是稳定定位器：先读根 `runtime.json`，再固定该次 `snapshot_dir` 内的文件。动态规则不复制到根文件。
- 用户已有同名入口时绝不覆盖；Runner 在本地诊断输出冲突文件名。刷新不接触 `work/` 或项目业务文件。
- 新快照的 `manifest.json` 标注 Agent Integration 版本，其 Hash 纳入本地清单；入口 Markdown 与 JSON 投影不一致、文件损坏或未知 Integration 版本均不能作为健康快照激活。旧 WP1 快照仍可恢复。

## 客户端实测

使用仅有虚构资料、没有凭证的独立测试 Workspace。提示词只有：“读取这个 Workspace 并开始工作。先概述你理解的工作环境和约束，不修改文件。”未补充 RDOS 背景。

| 客户端 | 结果 | 证据摘要 |
| --- | --- | --- |
| WorkBuddy 5.6.2 普通任务 | 通过 | 自动找到根入口、`runtime.json` 和快照说明，准确复述版本、资料／工作／outbox 位置和人工判断边界。 |
| WorkBuddy 5.6.2 代码开发 | 通过 | 同一新会话提示下读取入口链，并指出 `/tmp` 与 `/private/tmp` 是同一 macOS 路径。 |
| WorkBuddy 5.6.2 项目内本地任务 | 通过 | 在没有项目自定义指令的隔离项目中选定同一 Workspace，准确复述 Contract 版本、revision、目录边界及 Gate 禁令。 |
| Codex CLI 全新只读会话 | 通过 | 自动先读 `runtime.json`，再读快照 `READ_FIRST.md`，复述版本和边界，未写文件。 |
| Runner 实际生成的快照 + WorkBuddy 普通任务 | 通过 | 自动读取 Contract、生成式 Rules、manifest、Selected RAG 和 Skill，准确复述 revision 17、Hash、事件类型和 Human Gate 禁令。 |

WorkBuddy 测试使用了本机已安装客户端；未将实际 RDOS 业务资料或凭证交给它。项目模式创建了名为“RDOS入口测试”的空白测试项目，作为验收记录保留；不是 RDOS 正式项目。Codex CLI 的用户级插件初始化出现与此测试无关的警告，但只读任务完成。

## 自动验证

- `python -m unittest discover -s tests -q`：74 项通过、5 项平台条件跳过。
- `tests/e2e_demo.py`：三节点同步、协作、事件 ACK、离线恢复等既有闭环通过。
- 专项测试覆盖首次同步、Contract 更新、根入口不复制动态规则、用户同名文件不覆盖、Runner 恢复、损坏的动态入口回退到上一健康快照。

## 判断与下一包

WP2 的零背景自动发现门槛通过，可以进入 WP3。这里的“只读”仍是协议与篡改检测，不是对同一系统用户的 OS 强制隔离。WorkBuddy／Codex 的本机结果不能替代其他用户设备的验收；正式云端和真实旧节点仍未切换。
