# RDOS Runner Bootstrapper v0.1 候选交付记录（2026-10-03）

状态：**双平台自动测试通过；未部署到正式云端；Mac/Windows 真实安装尚未验收。**

固定源码提交：`1aa680196dd2f990ec600fe8d1bb33a604e8a2bf`（分支 `codex/runner-bootstrapper`）。
双平台 CI：[GitHub Actions run 37094504917](https://github.com/yuhanliualex1149/baite_RDOS/actions/runs/37094504917)。构建产物保留至 2026-10-17；正式发布前应另行长期保存。

## 候选成品

发布目录建议：`/opt/baite-rdos/downloads/0.1.0-candidate/`。
以下是 **CI 构建的文件本身**，不是 GitHub artifact 外层 ZIP 的校验值：

| 文件 | 字节 | SHA-256 |
| --- | ---: | --- |
| `RDOS-Runner-macos-arm64-0.1.0-candidate.dmg` | 13533573 | `35824fc4879760890d6cd516954340792ee2e361f61c6c3a095c6ad63088d210` |
| `rdos-runtime-macos-arm64-0.1.0-candidate.tar.gz` | 8532266 | `033ddc97b7107b4e64bf4e91790e942cdc80d50aa2b0e67b576db5b87419194a` |
| `RDOS-Runner-windows-x64-0.1.0-candidate.exe` | 12665115 | `482734ff9caddec60fdbc196be06ad1f6bfa159621c59cb76c20ea57479615cf` |
| `rdos-runtime-windows-x64-0.1.0-candidate.zip` | 9590059 | `c5c57a276494be715876449f10c5f9b7d99f82ce3f8b85664ef78cfc2c57deff` |

按上述文件生成的 `bootstrap-artifacts/manifest.json` 保留在本地构建目录，不提交到 Git。它指向 `https://yjmt.cn/baite-rdos/downloads/0.1.0-candidate/`，只有实际上传并校验完四个文件后才能启用。

## 当前在线基线

- RDOS `/api/health`：`200`，`app_release=1fa97ff2a35972bddc9f9a5ca47d52f11b2f9d51`。
- 同域路径：`/` 为 `200`，`/fuquelai/` 为 `200`，`/baite-rdos/` 为 `200`，`/yxxz/` 为 `404`。后者是发布前观察值，不能归因于本候选版。
- 现有 Mac Runner 保持原样运行；尚未签发或认领真实测试节点接入码。

## 云端发布与实机验收待办

1. 已在本机 `known_hosts` 核验服务器 IP `39.96.86.168` 的主机密钥，但现有 SSH 用户密钥无部署权限；需取得新的授权凭证或由运维代执行。不要跳过主机密钥检查。先确认线上 Nginx、服务配置、数据库路径及当前回滚版本。
2. 上传四个候选成品到版本化下载目录，在服务器上再次核对字节和 SHA-256；此时不更新活动 Manifest。
3. 记录同域各路径发布前基线，备份在线 SQLite（使用 SQLite backup API）、环境配置、Nginx 配置和旧 Manifest；在隔离数据库演练 schema v6 迁移及旧 Runner 兼容。
4. 部署上述固定提交及下载路由，验证健康、登录、原有 Runner 心跳、项目和资料同步；之后才原子切换活动 Manifest，核对四个 HTTPS 下载地址。
5. 管理员创建**独立测试节点**，从浏览器下载 Mac DMG，在当前 Mac 上双击安装、完成系统授权和完整 Self Test；不要复用或轮换旧 Runner Token。WorkBuddy 只在安装完成后读取 Workspace。
6. 失败时先撤回新 Manifest；若需回滚 Server/数据库，按已验证的部署恢复流程成对处理。不得用旧代码直接打开迁移后的新库。
7. Windows 10 22H2 / 11 x64 普通用户安装、系统防护、登录自启、休眠恢复和长时间运行仍标记**待实机验收**。

未完成步骤不得写成“云端已更新”或“Mac 已实际接入”。
