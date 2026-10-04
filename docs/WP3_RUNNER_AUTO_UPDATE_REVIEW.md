# WP3 Runner Runtime Auto Update Review

## 结论与边界

候选实现将后台服务的稳定入口改为安装目录内保留的 Runner Manager。Manager 只管理 Runtime 子进程和程序版本；Workspace 根目录的 `runtime.json` 继续只指向资料快照。Manager 本轮不自更新，也不接管正在运行的真实旧节点。

此文档记录候选代码的本机隔离验证。正式云端仍是旧版，尚未发布 Bootstrap Manifest；真实 Mac 重启验收须在约定窗口用隔离节点执行。Windows 10/11 真人实机、正式云端部署与真实旧节点切换均未验收。

## 实施内容

- Mac 保留冻结图形程序为稳定 Manager；Windows 从首次下载的 onedir Runtime 保留独立的 Manager 副本，避免 onefile 安装器的父子进程影响后台停止。两端都通过 `--manager` 托管可更新的 Runtime；Windows 子进程还绑定随 Manager 退出而关闭的 Job Object。新安装的后台服务指向保留路径；发现仍指向旧 Runtime 的同名服务时拒绝静默接管。
- 安装目录中的私有 `runtime-state.json` 持久记录 current、previous、candidate、版本 Hash、切换阶段、失败候选和子进程实例。更新下载到暂存目录，校验大小、SHA-256、安全解包和候选独立 `--self-check` 后才切换。相同版本不同 Hash 不覆盖。
- 启动时及每 12 小时检查同域 Manifest，按平台、协议、Contract 和最低 Manager 版本过滤。断网或清单暂不可达时继续运行当前 Runtime；失败的同一版本与 Hash 暂停自动重试，清单变化或管理员点击“重试 Runtime 更新”才解除。
- 切换时停止已核实身份的子进程，不禁用后台服务。新版本必须回传本次进程实例的心跳和同步状态，且本地全局及参与项目快照均通过校验，才记为成功。超时、崩溃或不健康时回到 previous；Manager 在切换中重启也从持久状态回退。
- Runner 的 heartbeat、sync-status 与安装状态加入可选实例字段；旧请求仍兼容。Runner 认证接口回传更新状态；管理员卡片只展示当前／目标版本、状态、时间和错误，并提供明确重试入口。
- 旧脚本安装器仍能识别旧版源码清单；候选源码新增 Contract/Agent 模块时会一并下载，不改真实旧节点。

## 已执行的隔离验证

| 检查 | 结果 |
| --- | --- |
| 全套单元回归 | 82 tests passed，5 skipped；覆盖正常升级、重复检查、坏 Hash／坏程序／模拟 Windows 文件占用、无新心跳、不健康快照、切换中断恢复、断网、版本不兼容、管理员重试、实例匹配和增量数据库备份。 |
| 三节点旧协议端到端 | `tests/e2e_demo.py` 通过，旧式 Runner 的同步、事件 ACK、离线恢复与 Token 轮换未回退。 |
| 旧 Mac 安装器隔离回归 | `tests/macos_installer_smoke.py --local-runtime` 通过；测试服务和文件已清理。另对旧 LaunchAgent 配置副本演练了拒绝静默接管和还原，未操作真实服务。 |
| 冻结成品 | 本机 Apple Silicon、macOS 26.6.2 构建候选 Runtime 和 DMG；`tests/packaged_smoke.py` 在精简 PATH、无开发 Python 环境变量下通过，已解包搬移运行并检查动态链接。 |
| 冻结 Runtime E2E | `tests/frozen_runtime_e2e.py` 用隔离 Panel 和虚构凭证通过心跳、完整快照、诊断事件与 ACK。 |
| 冻结 Manager E2E | `tests/manager_frozen_e2e.py` 通过：旧冻结 Runtime → 校验新版本 → 新实例健康 → ACK → 模拟坏候选回退 → 冻结 Manager 自身拉起并托管 Runtime。全程未连接正式云端。 |
| 浏览器工作台 | 本机 Chrome 完成节点接入回归，以及 Runtime 更新状态、错误和管理员重试按钮检查；无非预期 console error/warn。 |

## 待验收与发布门槛

- 候选分支的 macOS arm64／Windows x64 CI 冻结成品测试：待远端 CI 结果。CI 不等于 Windows 真人实机。
- 约定窗口内对**隔离测试节点**做真实 Mac 重启，验证 Manager 登录自启、持久状态与离线恢复：未执行。
- Windows 10/11 普通用户、任务计划程序、文件占用、系统防护和休眠恢复：未执行。
- 正式云端部署、真实旧 Mac 节点接管与生产回滚：未执行；本轮不切换。

只有上述发布门槛另行确认后，才可将此候选视为正式接入方案。当前 WP3 代码与本机隔离测试已就绪，但不宣称正式上线。
