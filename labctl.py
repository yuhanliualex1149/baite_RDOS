#!/usr/bin/env python3
"""Manage only this checkout's local experiment; never loads production env."""
from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from lab import config

ROOT = config.ROOT
CODE = ROOT / "code"
PYTHON = ROOT / ".venv/bin/python"
NODES = {"node_lab_codex": ("codex", "mem_lab_codex_0001"),
         "node_lab_workbuddy": ("workbuddy", "mem_lab_workbuddy_0001")}


def now():
    return datetime.now(timezone.utc).isoformat()


def available_port():
    for port in range(18760, 18860):
        try:
            with socket.socket() as candidate:
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                candidate.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
    raise RuntimeError("实验端口范围均被占用；未停止任何现有服务")


def prepare_nodes(cfg, state, credentials):
    base_url = f'http://127.0.0.1:{cfg["port"]}'
    command = f'"{PYTHON}" "{CODE / "lab/client.py"}"'
    for node_id, (folder, member) in NODES.items():
        workspace = config.checked_path(ROOT / "nodes" / folder, ROOT, tree=True)
        workspace.mkdir(mode=0o700, exist_ok=True)
        config.write_json(workspace / ".lab-access.json", {
            "base_url": base_url, "node_id": node_id, "token": credentials["node_tokens"][node_id],
            "env_id": cfg["env_id"], "generation": state["generation"]})
        instructions = f'''# RDOS 本地实验节点

本工作区仅用于 RDOS 2.0 阶段 0。你是 `{node_id}`，虚构测试成员 `{member}`，身份为 role_test。
这是独立测试会话，不沿用旧百特项目身份、任务、文档入口或写回授权。开发会话和脚本自测不代表本会话完成。

## 当前入口与边界

- 工作目录：`{workspace}`；可写本目录中的测试反馈文件。
- 实验地址：`{base_url}`；唯一资料入口为下面客户端的 context 命令返回的本地接口。
- 只用 `{CODE / "lab/client.py"}` 访问本地模拟服务。只读该客户端代码和此工作区内的指令、生成配置；不修改代码或运行目录。
- `.lab-access.json` 是本节点的私有实验凭据，只让客户端脚本读取，不输出或复制凭据。
- 不使用真实飞书、lark-cli、旧业务链接或其他连接器；不搜索原项目、其他成员工作区或本机其他资料。
- 不创建定时任务，不修改用户级 AGENTS、memory、Skill 或客户端全局配置；无需安装任何 Agent 或 Runner。
- 若入口缺失、服务拒绝、代次不匹配或权限不足，停止该操作并报告；不要寻找正式环境作为替代。
- 本工作区规则不构成系统级权限隔离；仍在同一电脑和同一使用者权限下运行。

## 本次独立实测

在本工作区运行客户端，先报告实际工作目录、节点和入口，再完成当前测试任务。所有参数与命令说明以 `--help` 为准：

```sh
{command} --workspace "{workspace}" context
{command} --workspace "{workspace}" tasks
{command} --workspace "{workspace}" read lab_doc_intro
```

读完材料后，用你自己的话写一份 `feedback.txt`，包含实际读到的内容、完成动作、使用 local_mock 后端和未验证项，不复制预设标准答案，不自称真实飞书已通过。
提交时使用你本次生成的唯一 operation_id，并记录返回的反馈 ID：

```sh
{command} --workspace "{workspace}" submit --operation-id "phase0-{folder}-YOUR_UNIQUE_ID" --file feedback.txt
{command} --workspace "{workspace}" read-feedback RETURNED_FEEDBACK_ID
```

独立读回后核对正文、节点、成员和提交结果。最终报告：使用的客户端、新会话说明、材料ID/版本、反馈ID、读回匹配结果和未验证项；不要输出凭据。
不要代替另一节点执行或把脚本模拟称为真实客户端实测。完成后停止，等待用户指令。
'''
        # These generated files have fixed ownership. Preserve any user edits.
        path = workspace / "AGENTS.md"
        if not path.exists():
            path.write_text(instructions, encoding="utf-8")
        start = workspace / "START_HERE.md"
        if not start.exists():
            start.write_text("请在本专用工作区开启一个全新会话，读取 AGENTS.md，再完成其中的阶段 0 最小测试。不要使用旧会话上下文或正式业务入口。\n", encoding="utf-8")


def prepare_runtime(cfg):
    runtime = config.checked_path(ROOT / "runtime", ROOT, tree=True)
    if runtime.exists():
        state = config.instance(cfg)
        credentials = config.read_json(runtime / "credentials.json")
    else:
        runtime.mkdir(mode=0o700)
        (runtime / "tmp").mkdir(mode=0o700)
        state = {"env_id": cfg["env_id"], "generation": uuid.uuid4().hex, "created_at": now()}
        credentials = {"admin_username": "lab-admin", "admin_password": secrets.token_urlsafe(24),
                       "session_secret": secrets.token_urlsafe(48),
                       "node_tokens": {node: secrets.token_urlsafe(32) for node in NODES}}
        config.write_json(runtime / "instance.json", state)
        config.write_json(runtime / "credentials.json", credentials)
        (runtime / "PANEL_ACCESS.txt").write_text(
            "仅用于本地实验 Panel，勿粘贴到聊天或其他客户端。\n"
            f"地址：http://127.0.0.1:{cfg['port']}\n账号：{credentials['admin_username']}\n密码：{credentials['admin_password']}\n", encoding="utf-8")
        (runtime / "PANEL_ACCESS.txt").chmod(0o600)
    prepare_nodes(cfg, state, credentials)
    return state


def initialize():
    path = ROOT / "config/lab.json"
    if not path.exists():
        config.checked_path(ROOT / "config", ROOT, tree=True)
        cfg = {"schema_version": 1, "environment": "rdos_local_lab", "env_id": "rdos2_lab_" + secrets.token_hex(6),
               "root": str(ROOT), "port": available_port(),
               "backends": {"documents": "local_mock", "model": "local_mock"},
               "external_resources": {"enabled": False, "folder": "", "documents": [], "task_table": "", "feedback": "", "recipient": ""}}
        config.write_json(path, cfg)
    cfg = config.load()
    prepare_runtime(cfg)
    return status(cfg)


def owned_process(cfg):
    path = ROOT / "runtime/process.json"
    if not path.exists():
        return None
    state = config.read_json(path)
    if state.get("env_id") != cfg["env_id"] or state.get("generation") != config.instance(cfg)["generation"]:
        raise ValueError("进程记录的环境标识不匹配")
    pid = state.get("pid")
    if type(pid) is not int or pid <= 1:
        raise ValueError("实验进程记录无效")
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False)
    if not result.stdout.strip():
        return None
    expected = f"{PYTHON} -B -m lab.serve --instance-id {state['instance_id']}"
    if result.stdout.strip() != expected:
        raise ValueError("PID 已不属于本轮实验进程；拒绝停止或覆盖")
    return state


def status(cfg=None):
    cfg = cfg or config.load()
    state = config.instance(cfg)
    proc = owned_process(cfg)
    return {"environment": "RDOS 本地实验环境", "env_id": cfg["env_id"], "generation": state["generation"],
            "running": bool(proc), "pid": proc["pid"] if proc else None,
            "address": f'http://127.0.0.1:{cfg["port"]}', "data_dir": str(ROOT / "runtime"),
            "documents_backend": "project_scoped" if cfg.get("phase3", {}).get("enabled") else "local_mock",
            "phase3_documents_backend": "feishu_test" if cfg.get("phase3", {}).get("enabled") else "local_mock",
            "model_backend": cfg["backends"]["model"], "external_access": bool(cfg.get("phase3", {}).get("enabled")),
            "client_acceptance": "需分别核对真实新会话；服务记录不自动证明客户端身份"}


def start():
    cfg = config.load()
    state = prepare_runtime(cfg)
    if owned_process(cfg):
        return status(cfg)
    with socket.socket() as probe:
        # Match Uvicorn's listener: completed connections in TIME_WAIT must
        # not prevent a safe restart. An active listener still rejects bind.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", cfg["port"]))
        except OSError as exc:
            raise RuntimeError("实验端口已被占用；未停止或更换现有服务") from exc
    instance_id = uuid.uuid4().hex
    runtime = ROOT / "runtime"
    env = {"PATH": str(PYTHON.parent) + ":/usr/bin:/bin", "LANG": "en_US.UTF-8", "PYTHONNOUSERSITE": "1",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(CODE), "TMPDIR": str(runtime / "tmp"),
           "RDOS_LAB_MODE": "1", "RDOS_LAB_ROOT": str(ROOT), "RDOS_LAB_RUNTIME": str(runtime),
           "RDOS_LAB_ENV_ID": cfg["env_id"], "RDOS_LAB_GENERATION": state["generation"],
           "RDOS_LAB_INSTANCE_ID": instance_id,
           "BAITE_DB_PATH": str(runtime / "panel.sqlite3"), "BAITE_PUBLIC_URL": f'http://127.0.0.1:{cfg["port"]}'}
    if cfg.get("phase3", {}).get("enabled"):
        # Existing native Feishu user connection is the sole scoped child exit.
        # HOME is required to locate that connection; provider/proxy env is not inherited.
        env["HOME"] = os.environ["HOME"]
    with (runtime / "server.log").open("ab") as log:
        proc = subprocess.Popen([str(PYTHON), "-B", "-m", "lab.serve", "--instance-id", instance_id], cwd=CODE,
                                env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    record = {"env_id": cfg["env_id"], "generation": state["generation"], "pid": proc.pid,
              "instance_id": instance_id, "started_at": now()}
    config.write_json(runtime / "process.json", record)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("实验服务启动失败；诊断见 runtime/server.log（不含请求访问日志）")
        try:
            with opener.open(env["BAITE_PUBLIC_URL"] + "/api/lab/info", timeout=1) as response:
                info = json.load(response)
            if info.get("env_id") == cfg["env_id"] and info.get("generation") == state["generation"]:
                return status(cfg)
        except (OSError, ValueError):
            time.sleep(0.15)
    stop()
    raise RuntimeError("实验启动健康检查超时，已只停止本次实验进程")


def stop():
    cfg = config.load()
    proc = owned_process(cfg)
    if proc:
        os.kill(proc["pid"], signal.SIGTERM)
        for _ in range(60):
            if owned_process(cfg) is None:
                break
            time.sleep(0.1)
        else:
            # Revalidate ownership immediately before escalation.
            if owned_process(cfg):
                os.kill(proc["pid"], signal.SIGKILL)
                time.sleep(0.2)
    return status(cfg)


def reset(confirm):
    cfg = config.load()
    if cfg.get("phase3", {}).get("enabled"):
        raise ValueError("阶段3保留远端对象与操作核对链，禁止 reset；请使用配对代码/数据库备份恢复。未清理任何数据")
    if confirm != cfg["env_id"]:
        raise ValueError("reset 必须以 --confirm 提供当前实验 env_id；未清理任何数据")
    if owned_process(cfg):
        raise ValueError("请先停止实验实例，再确认重置")
    old = config.instance(cfg)
    runtime = config.checked_path(ROOT / "runtime", ROOT, tree=True)
    if runtime != ROOT / "runtime" or runtime.resolve() != runtime:
        raise ValueError("运行目录不是预期的实验目录")
    evidence = config.checked_path(ROOT / "evidence", ROOT, tree=True)
    archive = evidence / ("reset-" + old["generation"])
    archive.mkdir(mode=0o700, exist_ok=False)
    for name in ("events.jsonl", "blocked.jsonl", "instance.json"):
        path = runtime / name
        if path.is_file():
            shutil.copyfile(path, archive / name)
    # This exact fixed directory has been checked for identity, links, and ownership.
    shutil.rmtree(runtime)
    prepare_runtime(cfg)
    config.write_json(archive / "reset.json", {"reset_at": now(), "old_generation": old["generation"],
                      "new_generation": config.instance(cfg)["generation"], "removed_directory": str(runtime)})
    return status(cfg)


def reset_password(username=None):
    """Interactive local recovery; no provider setup, seed, reset or client writes."""
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise ValueError("密码恢复只接受本机终端交互，请在终端运行；不能通过参数或管道传入密码。")
    cfg = config.load()
    state = config.instance(cfg)
    if owned_process(cfg):
        raise ValueError("请先使用 labctl.py stop 停止 Panel，再恢复密码；未修改任何账号。")
    runtime = config.checked_path(ROOT / "runtime", ROOT, tree=True)
    database = config.checked_path(runtime / "panel.sqlite3", ROOT)
    if not database.is_file():
        raise ValueError("现有 Panel 数据库不存在；密码恢复不会创建或初始化数据库。")
    credentials = config.read_json(config.checked_path(runtime / "credentials.json", ROOT))
    session_secret = credentials.get("session_secret") if isinstance(credentials, dict) else None
    if not isinstance(session_secret, str) or len(session_secret) < 24:
        raise ValueError("本机运行配置不完整；未修改账号。")
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
            if username is None:
                candidate = conn.execute("SELECT username FROM central_users WHERE id='mem_lab_admin_0001'").fetchone()
                username = candidate[0] if candidate else None
            row = conn.execute("SELECT roles_json FROM central_users WHERE username=? AND enabled=1", (username,)).fetchone()
            if not row or "admin" not in json.loads(row[0]):
                raise ValueError("未找到启用的管理员账号，请使用 --username 指定现有管理员。")
    except (sqlite3.DatabaseError, json.JSONDecodeError) as exc:
        raise ValueError("现有 Panel 账号数据库无效；恢复不会初始化或替换数据。") from exc
    # Prompt before installing the guard: getpass opens /dev/tty, while the
    # application guard deliberately only permits registered runtime files.
    new_password = getpass.getpass("新密码（至少 10 个字符）：", stream=sys.stderr)
    confirmation = getpass.getpass("再次输入新密码：", stream=sys.stderr)
    os.environ.update(RDOS_LAB_MODE="1", RDOS_LAB_ROOT=str(ROOT), RDOS_LAB_RUNTIME=str(runtime),
        RDOS_LAB_ENV_ID=cfg["env_id"], RDOS_LAB_GENERATION=state["generation"],
        BAITE_DB_PATH=str(database), BAITE_PUBLIC_URL=f'http://127.0.0.1:{cfg["port"]}',
        BAITE_SESSION_SECRET=session_secret)
    from lab import guard
    guard.install(ROOT, cfg["port"])
    from server.central_accounts import recover_password
    recover_password(username, new_password, confirmation)
    return {"ok": True, "username": username, "message": "密码已重置，请重新登录。"}


def main():
    class SafeArgumentParser(argparse.ArgumentParser):
        def error(self, message):
            # argparse normally repeats invalid values, including a password
            # mistakenly supplied as an argument. Never echo those values.
            self.print_usage(sys.stderr)
            self.exit(2, "命令或参数无效；请用 --help 查看用法。密码只能在终端交互输入。\n")

    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "start", "status", "stop", "reset", "reset-password"))
    parser.add_argument("--confirm", help="reset 时显式确认实验 env_id")
    parser.add_argument("--username", help="reset-password 时指定现有管理员；默认使用初始管理员账号")
    args, unknown = parser.parse_known_args()
    if unknown:
        parser.error("存在不支持的参数；密码只能在终端交互输入。")
    if args.username is not None and args.action != "reset-password":
        parser.error("--username 只适用于 reset-password")
    if args.confirm is not None and args.action != "reset":
        parser.error("--confirm 只适用于 reset")
    lock = ROOT / ".labctl.lock"
    if lock.is_symlink():
        raise SystemExit("拒绝链接锁文件")
    with lock.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            result = (reset(args.confirm) if args.action == "reset" else reset_password(args.username)
                if args.action == "reset-password" else {"init": initialize, "start": start, "status": status, "stop": stop}[args.action]())
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            raise SystemExit(1)
        except (EOFError, KeyboardInterrupt):
            print(json.dumps({"ok": False, "error": "已取消密码输入，未提交密码修改。"}, ensure_ascii=False), file=sys.stderr)
            raise SystemExit(1)


if __name__ == "__main__":
    main()
