# macOS Runner：Local Agent 从零接入

这是 [Agent onboarding](https://scn33386s7ui.feishu.cn/docx/TNZEdqDVvodfCixr4uEcqRqNn6c) 的技术补充，不替代组织知识与工作规范。只安装 Runner，不启动 Panel，不规定 Agent 如何研发。

候选版本已统一到 [跨平台接入说明](runner_onboarding.md) 和 `deploy/install_runner.py`。下文保留 Mac 操作约定；新版本优先读取 `runtime.json`。旧 `install_macos_runner.py` 只是兼容入口，单独下载时需在旁边放置同一 SHA 的 `install_runner.py`，不能再当成独立安装器。

## 人工只需提供

1. 管理员为伙伴创建独立节点，按需分配项目，并私下交付下载的 JSON 配置。
2. 伙伴将配置保存到本机，向 Agent 提供配置路径及本说明；Token 不粘贴进聊天或公共文档。
3. 遇到系统授权时由伙伴确认。Python 不足时 Agent 应说明安装方案并按本机权限执行，不绕过系统保护。

已有节点可以继续用。若旧配置已清理或失效，管理员在该节点轮换 Token 并下载新 JSON；不需要新建重复节点。配置中的旧 Workspace 路径可以通过安装参数改为本机新路径，源配置文件不会被改写。

## 安装器如何取得

独立脚本是仓库的 `deploy/install_runner.py`，可以单独交付，不依赖开发目录或完整仓库。只能从经过确认的完整 SHA 下载；不能误用 main 上的旧个人专用脚本。

安装器发布后可由 Agent 下载这一份脚本，再按下述流程执行。Agent 应检查下载来源与代码，不使用来历不明的脚本或把下载内容直接管道送入 Shell。

Runner 程序由安装器自动获取：读取已配置 Control Server 的 `/api/health` 发布 SHA，从公开仓库下载同一提交的明确运行清单（详见跨平台说明），核对 Git blob Hash，再安装到独立目录。不使用 main/latest 代替云端版本，不把 Token 发送给 GitHub。

## Local Agent 执行顺序

1. 检查 macOS 和 Python 版本。需要 Python 3.10+，推荐 3.12/3.13。macOS 自带 Python 3.9 不满足安装器要求。优先复用已有可靠 Python；缺少时只处理 Python 前置条件，不安装 Server、Docker、飞书 OAuth 或其他无关依赖。
2. 检查配置中的 `runner_id` 和 `control_url`。云端子路径必须保留，例如 `https://yjmt.cn/baite-rdos`。不要显示 `runner_token`；把输入配置权限收紧到 0600。
3. 选择专用 Workspace 的绝对路径，例如当前用户下的 `RDOS-Workspace`。首次安装要求空目录或不存在的目录，不覆盖其他工作。与程序安装目录分离。
4. 检查同一节点是否已有安装、后台服务或手动运行进程；复用或明确报告冲突，不再启动第二个 Runner。默认安装路径如下。
5. 执行安装器。示例中的路径必须由 Agent 替换为本机真实绝对路径，Python 命令替换为已验证的版本：

```bash
python3.13 /absolute/path/to/install_runner.py install \
  --config /absolute/path/to/runner-config.json \
  --workspace /absolute/path/to/RDOS-Workspace
```

安装器会验证凭证、下载并校验程序、创建不带 pip 的独立 Python 环境、保存私有配置、生成 LaunchAgent 并启动。Python 系统运行时仍须保留；不要卸载正在使用的 Python。脚本不安装系统级依赖。

默认程序目录：`当前用户/Library/Application Support/BaiteRDOS/runners/<runner_id>/`。

```text
<runner_id>/
  config.json           # 0600，含私有 Token
  manage.py             # 安装器的本地副本，可查询和启停
  .venv/
  releases/<云端 SHA>/
    runner/runner.py
    rdos_protocol.py
  logs/
```

服务名：`org.baite.rdos.runner.<runner_id>`。plist 在当前用户的 `Library/LaunchAgents/`；其中不保存 Token。服务会在用户登录时运行，不是开机未登录时的系统服务。

`--no-start` 仅表示本次安装不启动，不用于停止已经运行的服务；生成的 plist 仍配置登录启动。普通用户不需要指定 `--root`；它用于明确指定另一处安装根目录，同一节点不应有两处安装。

## 验收，不只看安装命令成功

1. `status` 显示 `running: true`，Panel 对应节点近期有心跳并显示 Online。
2. Workspace 出现 `shared/global_rules.md`、`control/sync.json`；确认 revision 和日志无同步校验错误。历史文件存在本身不是此次同步成功证据。
3. Agent 在 `work/` 写入接入测试记录，再以唯一 UUID 为 `event_id`，向 `outbox/` 原子写入一个事件（先写临时文件再改名为 `.json`）：

```json
{
  "event_id": "现场生成的唯一 UUID",
  "type": "progress",
  "current_focus": "Local Agent 接入测试",
  "status": "working",
  "summary": "根据本次真实验证情况填写，不要宣称未验证的事项成功",
  "needs_collaboration": ""
}
```

4. 等待该文件进入 `outbox/sent/`，表示 Runner 收到了匹配 event_id 的 ACK。若进入 `rejected/`，读取原因；若未回传，排查日志，不通过不停生成新 event_id 掩盖失败。
5. Agent 交付节点 ID、Workspace、进程状态、revision、测试 event_id/ACK 和未完成项。无法查看 Panel 时应明确说明，并由管理员确认 Online，不假装已登录查看。

未加入项目时没有项目目录是正常现象。`shared/`、`control/`、`.runner/` 由 Runner 管理；Agent 不能修改。加入项目后按项目 `control/` 信息工作，维护项目 `work/project_status.json`。不要求为了安装去写飞书、安装所有 Skill 或提交虚假项目日报。

## 查看、停止、恢复

可使用交付的安装器或安装目录中的 `manage.py`。以下路径和 ID 要替换为本机实际值：

```bash
python3.13 /absolute/path/to/manage.py status --runner-id rnr_example123
python3.13 /absolute/path/to/manage.py stop --runner-id rnr_example123
python3.13 /absolute/path/to/manage.py start --runner-id rnr_example123
```

stop 同时卸载服务并禁用登录自启动；start 会重新启用和加载，已运行时不启动第二个进程。status 不启动任何服务，也不请求云端。

进程 Running 与 Panel Online 是两件事；Online 也不等于同步健康。云端默认超过 60 秒无心跳显示 Offline，断网或休眠也会导致 Offline。日志在安装目录 `logs/runner.log`，后台启动异常另看 `launchd-error.log`。

重复执行 install 会保留 Workspace 和历史；配置不变且已运行时不重启。更新 Token 时，以新配置和原 Workspace 再执行 install。不同服务地址、不同 Workspace 或已有非本安装器管理的旧服务，需先检查迁移，不自动接管或删除。

## 自动测试

```bash
python3.13 -m unittest tests.test_macos_installer -v
python3.13 tests/macos_installer_smoke.py --release <云端完整 SHA>
```

Smoke 测试使用临时目录、虚构凭证、本机模拟 API，真实下载发布文件并操作一次性 launchd 服务，验证首次安装、重复安装、启停、同步、ACK，最后清理临时服务。它不访问管理员 API、不向真实云端发送心跳或事件。真实 WorkBuddy 接入验收仍需用户执行。
