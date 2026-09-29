# RDOS 本地试跑计划

## 开始前

- Control Panel 已启动，能用唯一系统管理员账号登录；
- 已在 Runners 页面创建 `user1 / user2 / user3`；
- 三份一次性配置已下载，并分别启动三个 Runner 终端；
- 飞书个人 OAuth 当前有效。

## 试跑步骤

1. 在 Runners 页面确认三个节点都是 Online，runner_id、Token 与 Workspace 相互隔离。
2. 在 Selected RAG 点击“立即同步”，确认当前 6 项进入 active snapshot，并同步到三个 Workspace。
3. 在 Global Rules 发布一条规则，确认 revision 增加，三个节点的 `shared/global_rules.md` 更新。
4. 让 user1 生成 Progress 与 Activity，确认 Overview 展示，但 Work Queue 不增加待办。
5. 让 user1 向 user2 发普通核对请求；user2 Confirm 或 Return，确认双方 Workspace 更新，管理员不用介入。
6. 再发一个普通请求，由 user2 选择 Escalate，确认进入“待判断”，管理员执行 Confirm 或 Return。
7. 让 user1 提交 Skill Proposal，在 Shared Skills 比较当前与候选内容并发布，确认三个节点获得新版本。
8. 再提交一份 Proposal 并退回，确认来源节点在同步内容中看见反馈。
9. 停止 user3，等待超过 10 秒，确认显示 Offline；重新启动，确认自动恢复与补传。
10. 轮换 user3 Token，确认旧配置被拒绝；下载新配置后可以恢复。
11. 暂停 Control Panel，再生成一个 outbox 事件；确认文件不进入 `sent/`。恢复 Panel 后确认自动补传。
12. 全程确认 Selected RAG 页面只有查看和“立即同步”，不能编辑飞书正文。

## 结束判断

通过的含义是：管理员协调、Runner 同步、Local Agent 自主工作的边界已经能运行。它不代表真实 Agent、正式云部署、多管理员、完整 Workflow 或飞书写入已经完成。
