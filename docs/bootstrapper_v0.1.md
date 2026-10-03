# RDOS Runner Bootstrapper v0.1（内部测试）

## 用户接入

1. 管理员在 Runners 创建节点，选择 macOS / Windows，下载安装器，并通过私密渠道交付一次性接入码。接入码 24 小时有效；旧配置下载仍保留，但不是新用户默认流程。
2. 用户双击安装器，粘贴接入码；可选本机专用 Workspace。无需安装 Python、Homebrew、Git，也无需 WorkBuddy 执行安装命令。
3. 安装器从 `https://yjmt.cn/baite-rdos` 获取独立版本清单，仅下载同一 RDOS 域名的版本化 Runtime；核对架构、大小、SHA-256，再安装为当前用户后台服务。
4. 只有本次安装实例的心跳、全局/项目快照本地校验、诊断事件真实 outbox ACK 与服务端回执全部通过，才显示“接入完成”。失败可在同一安装器中重试或导出脱敏诊断。
5. WorkBuddy 在安装完成后读取 Workspace 根目录及各项目的 `runtime.json`，在同一批次固定读取其 `snapshot_dir`。WorkBuddy 不管理 Runner；Local Agent 自主决定具体工作。

Mac 本轮只在 Apple Silicon、macOS 26.6.2 验收。Windows 10 22H2/11 x64 为候选包，系统防护、登录自启、休眠恢复和长期运行待实机验收。未签名/未公证的内部包如被系统阻止，不移除 quarantine、不关闭安全防护，记录阻止情况并由用户按系统提示处理。

## 三种版本

- Bootstrapper：图形应用自身版本（v0.1.0）。
- Runner Runtime：独立的按平台完整运行目录，含解释器、时区数据和证书包；由清单固定版本和 Hash。
- Control Server：`/api/health.app_release` 仅用于服务端部署追踪；`/api/bootstrap/manifest.protocol_version` 用于接入兼容。

Runtime 源码位于 `runner/`；`bootstrapper/` 只负责接入、安装、系统后台运行、自测。旧 `deploy/install_runner.py` 保留给已有配置文件模式，不删除旧 Workspace 或节点。接入码认领时才替换该节点长期 Token；旧 Token 在成功认领前有效。卸载只清理本机程序和凭证、保留 Workspace；需管理员另行停用节点或轮换云端 Token。

## 构建和发布门槛

在目标平台使用固定版本构建依赖：

```text
python -m pip install -r bootstrapper/build-requirements.txt
python deploy/build_bootstrapper.py --version 0.1.0-candidate --output bootstrap-artifacts
python tests/packaged_smoke.py bootstrap-artifacts
```

Mac 产物为 `RDOS-Runner-macos-arm64-<version>.dmg` 和 `rdos-runtime-macos-arm64-<version>.tar.gz`；Windows 为 `.exe` 和 `.zip`。构建只在对应系统进行，不能交叉编译。候选分支 CI 运行原有测试、安装器及冻结成品烟测，上传两平台文件。安装器包与 Runtime 均不得包含真实节点凭证。

双平台产物核实后，用 `deploy/make_bootstrap_manifest.py --help` 的参数生成清单。先上传产物到 `/opt/baite-rdos/downloads/<version>/` 并逐个核对大小/Hash；更新 Nginx 的 RDOS 下载 location；备份在线数据库、环境配置和旧清单；再部署固定 Server 提交。只有服务健康、旧 Runner 仍在线、下载可访问时，才将清单原子切换到 `BAITE_BOOTSTRAP_MANIFEST_PATH` 指向的路径，开放工作台下载入口。不能用 `main/latest` 或 GitHub source tree 作为用户安装源。

发布前/后记录 `https://yjmt.cn/`、`/yxxz/`、`/fuquelai/` 和 `/baite-rdos/` 的既有状态码/内容基线。失败先撤回新清单，再依据 `DEPLOYMENT.md` 中已验证的代码、数据库恢复流程回滚；不要用旧代码直接打开已迁移的新库。新表与列使用 schema v6 增量迁移，启动前通过 SQLite backup API 自动备份。

## 实机验收记录模板

| 检查 | Mac arm64 本机 | Windows 10 x64 | Windows 11 x64 |
| --- | --- | --- | --- |
| 从浏览器下载并双击图形包 | 待验收 | 待验收 | 待验收 |
| 无开发环境下安装与系统授权 | 待验收 | 待验收 | 待验收 |
| 后台进程和登录自启 | 待验收 | 待验收 | 待验收 |
| 云端本次心跳、共享/项目快照 | 待验收 | 待验收 | 待验收 |
| 诊断事件 ACK 及 Panel 回执 | 待验收 | 待验收 | 待验收 |
| 休眠恢复、杀毒软件、长时间运行 | 待验收 | 待验收 | 待验收 |
| 卸载保留 Workspace | 待验收 | 待验收 | 待验收 |

自动测试通过不能替代以上人工实机验收；Windows 不应被称为正式可用。
