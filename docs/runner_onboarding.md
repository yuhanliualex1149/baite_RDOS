# RDOS Runner 自助接入（候选版本）

适用：macOS；Windows 10 22H2 / Windows 11 x64。Windows ARM64、WSL、网络盘、移动盘、云盘同步目录不在本轮验收范围。**自动测试不替代 Windows 客户端和 WorkBuddy 实机验收，未经确认不要面向伙伴正式推广。**

这是[组织 onboarding](https://scn33386s7ui.feishu.cn/docx/TNZEdqDVvodfCixr4uEcqRqNn6c)的技术补充。飞书无法访问必须如实报告，不自动执行其中的 Skill 安装或研发任务；本轮不修改原文。

## 管理员交付什么

1. 创建独立 Runner，或复用已有节点。Workspace 可以留空；填写的路径仅为建议，不会在 Server 上检查或展开。
2. 在交付窗口下载个人配置，选择 macOS / Windows，复制不含 Token 的接入指令。
3. 私下交付配置文件。关闭窗口后无法重新读取旧 Token；“接入指令”按钮不轮换凭证。配置遗失才由管理员主动轮换。

个人配置格式保持不变：

```json
{
  "control_url": "https://control.example/baite-rdos",
  "runner_id": "rnr_example123",
  "runner_token": "由管理员交付，不要粘贴到聊天或公共文档",
  "workspace": ""
}
```

Token 不出现在命令参数、任务定义、接入指令或日志。伙伴不需要管理员账号或飞书 OAuth。

## Local Agent 从零接入

1. 阅读本说明，确认用户提供的是自己的个人配置；核对服务 URL（保留子路径）及 Runner ID。
2. 检查前置环境：Mac Python 3.10+，推荐 3.13；Windows 使用 [Python 官方](https://www.python.org/downloads/windows/) Python 3.13 x64 用户级安装。缺失时先说明安装和必要授权，不绕过系统安全保护，不修改 PowerShell ExecutionPolicy，不用管理员终端安装 Runner。
3. 检查现有安装及 Workspace，避免重复运行或接管不明工作目录。
4. 下载 Panel 指令中**完整 SHA**固定的 `deploy/install_runner.py`，核对同 SHA Git tree 的 blob Hash。不能使用 main/latest，不能把网络响应直接管道执行。安装器为独立标准库脚本，无需下载整个仓库或安装 Server。
5. 从下载目录执行以下命令，个人配置路径用本机实际值。Token 本身不能作为参数。

macOS：

```bash
python3.13 install_runner.py install --config "/absolute/path/to/personal-config.json"
```

Windows PowerShell（普通用户）：

```powershell
py -3.13 install_runner.py install --config "C:\absolute path\personal-config.json"
```

留空 Workspace 时默认为 `当前用户主目录/RDOS-Workspace/<runner_id>`；可加 `--workspace "本机专用目录绝对路径"`。现有配置的完整路径继续有效。建议路径来自其他电脑时必须明确改成本机路径；安装器不改写输入配置正文。

首次安装要求空目录或尚不存在的目录。重复安装保留工作文件、outbox 和历史；相同配置且进程正常时不重复启动。旧节点无需重新注册。

## 安装、权限与后台行为

| 项目 | macOS | Windows |
| --- | --- | --- |
| 程序目录 | `~/Library/Application Support/BaiteRDOS/runners/<id>` | `%LOCALAPPDATA%\BaiteRDOS\runners\<id>` |
| 后台 | 当前用户 LaunchAgent | 当前用户 Task Scheduler 登录触发 |
| 权限 | 私有目录 0700、配置 0600 | 当前用户与 SYSTEM 的 ACL，不向其他普通用户开放 |
| Python | 独立 venv，标准库 | Python 3.13 x64 独立 venv，固定 tzdata |
| 启动条件 | 用户登录 | 用户登录，InteractiveToken、普通权限、不保存密码 |

安装目录包含 `manage.py`、`config.json`、`.venv/`、`releases/<SHA>/` 和 `logs/`。后台定义只记录配置路径，不含 Token。安装器先只读验证凭证，再按 `/api/health.app_release` 的完整 SHA 下载并核对 Git blob Hash。运行清单明确限定为：

```text
runner/runner.py
runner/__init__.py
runner/platform_support.py
runner/requirements.lock
rdos_protocol.py
```

Windows 依赖以 `--require-hashes` 安装固定 `tzdata` wheel。Mac 可以兼容安装不含新模块的旧云端 SHA；Windows 遇到旧版本明确拒绝，不偷换候选版本或 main。程序升级/回滚不在本轮自动执行。

Windows 任务设置：忽略重复启动、失败重试、无限运行时间、电池供电允许运行、不主动唤醒。休眠或未登录期间不能工作；恢复后自动重连。这里不是开机未登录即可工作的系统服务。Windows 本轮只验收本地固定 NTFS；已知 OneDrive 路径和重解析点会拒绝，其他云盘目录仍由 Agent/用户检查。

## 资料入口与 Agent 读取约定

全局：`<workspace>/runtime.json`；项目：`<workspace>/projects/<project_id>/runtime.json`。

```json
{
  "layout_version": 1,
  "snapshot_dir": "本机绝对路径/.runner/releases/revision-7-唯一后缀",
  "revision": 7
}
```

项目快照实际位于 Workspace `.runner/project-releases/<project_id>/revision-*`。入口只允许指向本 Runner 的版本目录，不能任意访问其他目录。

Agent **每批先读取一次入口，并在该 snapshot_dir 中完成该批读取**：

- 全局 `shared/global_rules.md`、`shared/skills/`、`shared/selected_rag/`、`control/`。
- 项目 `shared/`、`control/project.json`、`control/todos.md`、`control/gate_records.md`。
- Agent 可写的工作区仍为 Workspace `work/`、`outbox/`，以及项目 `work/project_status.json`、`work/files/`，不在快照中写研发文件。

Windows 不创建符号链接；不要假设 Workspace 根目录有可读的 `shared/` 或 `control/`。Mac 保留这两个兼容链接，但新 Agent 也应优先使用入口。

快照和生成文本使用 UTF-8；生成文本采用 LF。下载正文保留原字节并校验 Hash。路径用规范化相对路径，冲突按大小写折叠及 Unicode NFC 检查；非法 Windows 文件名不擅自改名。校验、文件占用或入口替换失败都保留 last-known-good，错误进入同步状态。历史快照不自动删除。

## 验收与状态判断

必须同时确认：

1. `status` 显示进程运行。Windows 显示 Task 运行状态（不是单独 PID）；这是本机证据。
2. Panel 最近收到心跳，显示实际系统、版本、Workspace；Online 不等于同步成功。
3. `runtime.json` 指向完整快照，revision 与 Panel 一致，日志和同步健康无异常。
4. 经用户同意，原子写入以下虚构接入事件到 `outbox/<唯一ID>.json`，等待匹配 ACK 后自动归档到 `outbox/sent/`。不以反复生成新 ID 掩盖重试问题。

```json
{
  "event_id": "现场生成的唯一UUID",
  "type": "progress",
  "current_focus": "接入测试",
  "status": "working",
  "summary": "填写本次实际验证结果，不上传真实研发正文",
  "needs_collaboration": ""
}
```

不能访问 Panel 时如实说明，由管理员核对，不声称已经看见 Online。最终交付实际 Workspace、安装目录、版本、进程状态、同步结果、event_id/ACK 和待验证项；不输出 Token。

## 查询、启停与卸载

使用下载的安装器，或安装目录中的 `manage.py`；Python 用系统前置运行时，卸载时不要使用待删除目录内的 venv Python。

```powershell
py -3.13 install_runner.py status --runner-id rnr_example123
py -3.13 install_runner.py stop --runner-id rnr_example123
py -3.13 install_runner.py start --runner-id rnr_example123
py -3.13 install_runner.py uninstall --runner-id rnr_example123
```

Mac 将 `py -3.13` 换为 `python3.13`。仓库中旧 `deploy/install_macos_runner.py` 入口仍可调用；若单独下载旧入口，必须把同一 SHA 的 `install_runner.py` 放在旁边，推荐直接下载新入口。

`stop` 同时禁用登录自启动；`start` 恢复并避免重复进程。`--no-start` 只抑制本次安装启动，不停止已运行的服务。`status` 不发心跳或云端请求。

`uninstall` 只删除经身份核实的本节点后台任务、程序、安装内凭证和日志，保留 Workspace/工作文件；**不撤销云端 Token，不删除最初下载的配置**。如需撤销由管理员停用或轮换。运行目录有冲突或手动 Runner 占用时先停下排查，不删除未知目录。

## 候选版本测试与发布边界

本分支为 `codex/windows-runner-onboarding`，没有部署正式云端。现有生产健康接口仍可能指向旧 Mac 版本，因此不能拿候选 Windows 安装器对生产服务声称已完成接入。候选验证使用隔离 Control API/虚构凭证；真实测试需管理员明确提供候选测试服务和独立节点。

配置 `BAITE_APP_RELEASE` 为候选完整 SHA 后，测试 Panel 才生成固定版本链接；未设置时指令明确停止安装，不猜测版本。UI 始终标注 candidate，正式发布需另行确认。

自动测试：`python -m unittest discover -s tests -v`、`python tests/e2e_demo.py`。Mac 真 LaunchAgent 隔离测试：`python3.13 tests/macos_installer_smoke.py --local-runtime`。Windows CI 检查原生锁、ACL、junction、文件占用和任务注册；GitHub Windows Server runner **不能证明 Windows 10/11 客户端体验已验收**。

后续逐项执行 [Windows 10 清单](windows10_acceptance.md)、[Windows 11 清单](windows11_acceptance.md)。批准前不合并 main、不发布云端、不自动启动真实节点或轮换真实 Token。

技术依据：[Python zoneinfo 数据源](https://docs.python.org/3/library/zoneinfo.html#data-sources)、[Microsoft TaskSettings](https://learn.microsoft.com/en-us/windows/win32/taskschd/tasksettings)。
