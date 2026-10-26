# RDOS 2.0 正式切换操作闸门

本文件面向发布操作者。`codex/rdos2-release-prep` 是候选分支；发布脚本只接受该分支的精确 SHA。任何一步未证实即保留旧服务，记录现状，不把旧数据库覆盖到可能出现新回执的库上。

1. 在发布准备分支完成测试并推送后，以服务器管理员身份调用 `deploy/rdos2-stage-release.sh <40位SHA>`。它从该分支拉取、在隔离 Linux 构建用户下安装锁定依赖并运行发布测试，生成只读的 `/opt/baite-rdos/releases/<SHA>`，不切换服务。仅核对脚本的 `Staged and verified` 结果不等于正式可用。
2. 旧服务运行时，先以既有 `baite-rdos` 用户运行该 release 的 `python -B -m cutover.rehearse_store`，它在一致性库副本验证真实旧账号、项目与元数据迁移，不访问远端。通过后以同一用户及其 `HOME=/var/lib/baite-rdos` 执行 `python -B -m cutover.build_resources`，只生成受限候选登记、中央 `agent.md` 和空 `memory.md`。复制 `deploy/rdos2.env.example` 为 root:root 0600 的 `/etc/baite-rdos/rdos2.env`；现有密钥仍由原 `/etc/baite-rdos/rdos.env` 提供，严禁打印或复制到终端输出。运行 `cutover.migrate_cli --check --resources <候选登记>`，对照同一时点的原表、反馈和 SQLite 备份，批准逐行决策；未经核对的旧任务不能自动视为进行中或已完成。
3. 短时停止旧服务后，用同一服务用户执行 `cutover.migrate_cli --prepare-schema --resources <候选登记> --source-sha256 <刚核对的原表哈希>`。只在原 Base 建一张“协作记录”表并追加 2.0 字段；所有旧列、原任务 ID 和物理行保留。重新 `--check --decisions <已批准决策文件>` 取得绑定新表 ID 的 plan hash，再以 `--apply --source-sha256 ... --plan-sha256 ... --decisions ...` 写入原行、独立读回并安装现有 SQLite 的授权记录。收到不确定写入时先核对原对象，不换 ID 重发。
4. 把 `/var/lib/baite-rdos/runtime/rdos2-resources.ready.json` 以 root:baite-rdos 0640 安装到 `/etc/baite-rdos/rdos2-resources.json`（服务只读，密钥环境文件仍 root:root 0600）。执行 `deploy/rdos2-activate-release.sh <同一SHA>`；脚本先检查正式配置、数据库迁移标记和既有飞书登录，再备份并切换原 systemd 槽位。需分别核对原公开地址登录、原 Base/任务表/视图、三位节点入口、真实模型、协作表读写及读回。自动维护初次保持关闭，确认后再按既定管理规则启用。
5. 只有服务和双表读写闸门通过，才在已认证 Panel 中用现有任务草稿/发布服务，按 `cutover.upgrade_tasks.task_payload` 分别发布雨寒 Codex、潇斐、多多三条稳定 ID 的一次性升级任务。后端仅为这三条已授权 2.0 任务在**同一物理行**投影原 11 列，供旧定时读取。核对原行、目标成员和版本后记录“已发布，待自然读取”。每个节点实际安装、原位更新安排、写新协作表并独立读回后，才分别停止其旧文档新增反馈。旧文档始终保留历史。

定时安排的原 ID、时间、时区、启停状态和会话／工区绑定必须保留。节点本地规则拒绝从任务自行升级时暂停该节点，不用人工把任务正文转发到会话中。发布、节点读取、升级回执和最终切换是四种不同证据。
