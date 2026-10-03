"""Credential-free installation instructions; never rotate or recover a Token."""
from __future__ import annotations

import os
import json
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath

ORGANIZATION_DOC = "https://scn33386s7ui.feishu.cn/docx/TNZEdqDVvodfCixr4uEcqRqNn6c"


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
    installer_url = None
    manifest_path = os.environ.get("BAITE_BOOTSTRAP_MANIFEST_PATH", "")
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8")) if manifest_path else {}
        key = "macos-arm64" if platform == "macos" else "windows-x64"
        installer_url = manifest["platforms"][key]["bootstrapper"]["url"]
    except (OSError, KeyError, ValueError):
        pass
    text = f"""RDOS 节点 {node} 的本机接入说明：
1. 管理员私下提供一次性接入码；不要把接入码、Token 或配置文件贴进聊天。
2. 从工作台下载图形安装器：{installer_url or '尚未发布，请等待管理员开放入口'}。
3. 用户双击安装器、粘贴接入码并完成系统正常授权。无需 Python、Homebrew、Git、Terminal 或 WorkBuddy 代装。
4. 安装器显示“接入完成”后，WorkBuddy 再读取本机 Workspace/runtime.json 及各项目 runtime.json；每批读取固定使用入口指向的快照目录。
5. WorkBuddy 自主安排研发工作，将结构化结果写入 outbox；Runner 只做同步与回传。安装与管理在图形安装器内完成。
组织 onboarding：{ORGANIZATION_DOC}。无法读取时明确说明，勿声称已读取。
"""
    return {"runner_id": node, "platform": platform, "channel": "internal-test",
            "installable": installer_url is not None, "installer_url": installer_url,
            "organization_url": ORGANIZATION_DOC, "instructions": text}
