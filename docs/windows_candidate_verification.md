# Windows Runner 与自助接入候选版本验证

日期：2026-09-29。分支：`codex/windows-runner-onboarding`。

## 交付结论

候选代码已实现，双平台 CI 全部通过，达到本轮候选版本交付标准；未标记正式使用验收。Windows 10 22H2 / Windows 11 x64 与真实 WorkBuddy 测试仍须分别执行。

代码验证基线：`61f4f3c35a8d7548a8b8abd00092dd8510913f48`。

## 本轮实际改动

- 共用 Runner 核心和 HTTP 协议；增加 Windows 原生文件锁、安全路径/重解析点检查、全局与项目 `runtime.json` 原子入口。Mac 保留兼容链接与旧工作文件。
- 统一安装管理入口 `deploy/install_runner.py`，保留旧 Mac 入口；Windows Python 3.13 x64 独立环境、固定 Hash 的 tzdata、当前用户 ACL 和 Task Scheduler。安装按云端完整 SHA 校验，不能回退 main/latest。
- Panel Workspace 可选并支持 Windows/POSIX 建议路径；heartbeat 的实际系统、版本、Workspace 与管理员配置分开；增量 schema 5 迁移前备份。
- 创建/轮换后的交付窗口可下载配置、选择平台、复制不含凭证的指令；已有节点可重新生成指令，不重复注册或读取旧 Token。入口明确标注候选版。
- 接入说明、Windows 10/11 两份实机清单、macOS/Windows CI 和真实浏览器流程测试。

## 自动测试证据

最终代码 CI：[双平台候选运行](https://github.com/yuhanliualex1149/baite_RDOS/actions/runs/36562592282)。

| 检查 | macOS | Windows |
| --- | --- | --- |
| 60 项 API/既有业务/文件系统/安装器测试 | 55 项执行，5 项 Windows 专用跳过；通过 | 49 项执行，11 项 Mac 专用跳过；通过 |
| 三个隔离 Runner E2E | 通过 | 通过 |
| 原生后台安装、同步、重复安装、启停、更新虚构凭证、ACK、卸载保留工作 | 通过（LaunchAgent） | 通过（Task Scheduler） |
| Chromium 创建/下载/复制/关闭清凭证/已有节点/轮换流程 | 通过，无意外 console error/warn | 通过，无意外 console error/warn |

Windows 使用 GitHub `windows-2022` x64 / Python 3.13；macOS 使用 `macos-14` arm64 / Python 3.13。平台专用测试分别执行，不把跳过计为验收。Windows CI 为托管 Server 环境，不能证明 Windows 10/11 普通用户、组织安全策略或 WorkBuddy 的行为。

本机也通过完整 60 项测试（5 项 Windows 专用跳过）、三 Runner E2E、Chromium 流程、Python 3.13 真 LaunchAgent 安装生命周期、JavaScript 语法和 diff 检查。所有安装测试使用临时目录、模拟 Control API 和虚构凭证；不会向正式云端发送测试事件。

覆盖的重点包括：旧配置与空 heartbeat、节点身份隔离、路径和权限、原生锁、中文/空格目录、同一 Workspace 互斥、损坏与入口替换失败保留旧快照、Windows 文件占用/junction、时区/工作日、断网/ACK 丢失/去重、Token 更新及卸载保留 Workspace。

## CI 暴露并修正的问题

1. Windows PowerShell 模块加载环境导致 `Set-Acl` 失败：改为原生 .NET ACL API，不修改执行策略或放宽权限。
2. 后台停止返回时进程仍可能占用文件锁：等待任务实例退出及 Workspace 锁释放后再重启/卸载。
3. NTFS 入口替换期间读取可能短暂遇到共享冲突：入口读取增加有限重试，不回退到半套快照或扩大权限。
4. 浏览器复制是异步动作，Windows 剪贴板可能返回 CRLF：等待复制成功后，统一换行再逐字核对；未跳过复制功能测试。

## 未执行与发布限制

- [Windows 10 实机清单](windows10_acceptance.md)：全部待验收。
- [Windows 11 实机清单](windows11_acceptance.md)：全部待验收。
- 普通用户从零安装、注销登录、电池/休眠恢复、Defender/组织防护、至少 24 小时运行、真实 WorkBuddy 接入尚未验证。
- 未访问或修改组织飞书 onboarding 原文；这里只提供技术补充，不声称已经读取在线文档。
- 未合并 main、未部署正式云端、未轮换真实 Token、未启动用户真实 Runner。仅创建并清理了隔离测试后台任务。既有本机清理状态保持不变。
- 本次只读检查正式 `/api/health` 仍报告旧 release `1fa97ff2a35972bddc9f9a5ca47d52f11b2f9d51`；Windows 安装器对不支持的新模块缺失版本会拒绝安装，不能拿旧正式服务冒充候选验收环境。
- 真实验收应由管理员明确提供隔离候选服务与独立节点；不要自行部署或对生产节点执行安装试验。

操作说明见 [Runner 自助接入](runner_onboarding.md)。实机通过并取得用户确认后，才进入合并、数据库备份、云端部署及旧 Mac Runner 兼容验证。
