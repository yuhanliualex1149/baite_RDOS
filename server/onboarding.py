"""Credential-free installation instructions; never rotate or recover a Token."""
from __future__ import annotations

import os
import re
from pathlib import PurePosixPath, PureWindowsPath

ORGANIZATION_DOC = "https://scn33386s7ui.feishu.cn/docx/TNZEdqDVvodfCixr4uEcqRqNn6c"
REPOSITORY = "https://github.com/yuhanliualex1149/baite_RDOS"


def validate_client_workspace(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if any(ord(char) < 32 for char in value):
        raise ValueError("Workspace 含控制字符")
    # Never expand or resolve another computer's path on the Control Server.
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value)
    if (windows.is_absolute() and ".." not in windows.parts) or (posix.is_absolute() and ".." not in posix.parts):
        return value
    raise ValueError("Workspace 必须留空或填写 Windows / POSIX 绝对路径，不接受 ~、环境变量或 ..")


def instructions(node: str, platform: str, public_url: str) -> dict:
    release = os.environ.get("BAITE_APP_RELEASE", "development")
    pinned = bool(re.fullmatch(r"[0-9a-f]{40}", release))
    installer_url = f"https://raw.githubusercontent.com/yuhanliualex1149/baite_RDOS/{release}/deploy/install_runner.py" if pinned else None
    docs_url = f"{REPOSITORY}/blob/{release}/docs/runner_onboarding.md" if pinned else None
    python = "py -3.13" if platform == "windows" else "python3.13"
    runtime = "Windows 10 22H2 / Windows 11 x64，官方 Python 3.13 x64 用户级安装；不支持 ARM64" if platform == "windows" else "macOS，Python 3.10+，推荐 3.13"
    text = f"""请协助我为现有 RDOS 节点完成本机接入，不创建新节点。

这是候选版本测试指令，尚未完成 Windows 10/11 与 WorkBuddy 实机验收，不作为正式推广入口。
服务地址：{public_url.rstrip('/')}
节点 ID：{node}
平台：{runtime}
固定发布 SHA：{release if pinned else '未配置，停止安装并联系管理员'}
安装器：{installer_url or '不可用；不允许改用 main/latest'}
技术文档：{docs_url or '不可用；等待管理员配置固定发布 SHA'}
组织 onboarding：{ORGANIZATION_DOC}

1. 先阅读技术文档与组织 onboarding。飞书无法访问时明确告诉我，不得声称已读取；不要自动安装其中的 Skill 或执行无关研发任务。
2. 让我提供管理员私下交付的个人配置 JSON。不要让我粘贴 Token 到聊天、命令参数或日志。核对其中 control_url 和 runner_id 与上面一致；如配置遗失，联系管理员主动轮换，不能重新读取旧 Token。
3. 检查操作系统、Python 和现有安装。只下载上述固定 SHA 的安装器，核对 GitHub 同 SHA tree 的 blob Hash；验证失败立即停止。缺少 Python 时，引导我安装官方用户级版本，不改系统安全策略。
4. 安装前确认 Workspace 为本机专用目录，优先使用安装器默认值；Windows 使用本地固定 NTFS，避开网络盘、移动盘和云盘同步目录。已有完整配置仍可使用 --workspace 明确指定本机目录，不覆盖工作文件。
5. 从下载目录执行：{python} install_runner.py install --config "<个人配置.json绝对路径>"
   安装器按服务 /api/health 的完整发布 SHA 下载并校验 Runner，不下载 main/latest；遇版本不支持、权限或授权问题请报告，不绕过保护。不运行 start_local.py，不启动本地 Control Server。
6. 检查 status、Panel 心跳、同步 revision 和健康状态。全局资料先读 Workspace/runtime.json，项目资料先读 Workspace/projects/<project_id>/runtime.json；每批读取固定使用该入口的 snapshot_dir，不混读不同版本。项目 work/files 不属于同步快照。
7. 经我确认后用虚构摘要提交一次最小 Progress 事件，使用唯一 event_id；仅在匹配 ACK 后 outbox 文件进入 sent 才算回传通过。不要提交真实研发正文。
8. 管理命令：{python} install_runner.py status --runner-id {node}；start / stop / uninstall 使用同样参数。重复安装应复用节点。卸载保留 Workspace，云端 Token 不会因此撤销。

请最终报告：Python 版本、安装位置、实际 Workspace、进程状态、心跳与同步状态、ACK 验证结果，以及仍需我授权或待验收的事项。不要输出 Token。Panel 管协作，Runner 管同步，Local Agent 管具体工作。
"""
    return {"runner_id": node, "platform": platform, "channel": "candidate", "installable": pinned,
            "release": release, "installer_url": installer_url, "documentation_url": docs_url,
            "organization_url": ORGANIZATION_DOC, "instructions": text}
