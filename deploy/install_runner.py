"""Per-user RDOS installer: macOS Python 3.10+ / Windows Python 3.13 x64."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import venv
import xml.etree.ElementTree as ET
from pathlib import Path

REPOSITORY = "yuhanliualex1149/baite_RDOS"
LEGACY_FILES = ("runner/runner.py", "rdos_protocol.py")
RUNTIME_FILES = (*LEGACY_FILES, "runner/__init__.py", "runner/platform_support.py", "runner/requirements.lock")
WINDOWS = os.name == "nt"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("服务返回重定向；请管理员确认 control_url，不转发 Token")


def fetch(url: str, token: str = "") -> bytes:
    headers = {"User-Agent": "RDOS-installer", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise ValueError(f"HTTP {exc.code}：请检查服务地址、节点凭证或下载访问权限") from None
    except urllib.error.URLError:
        raise ValueError("网络或 TLS 校验失败；未关闭证书验证，请检查网络和 Python 证书") from None


def runner_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,100}", value):
        raise ValueError("runner_id 格式不安全")
    return value


def service_label(node: str) -> str:
    return "org.baite.rdos.runner." + runner_id(node)


def install_root() -> Path:
    if WINDOWS:
        return Path(os.environ["LOCALAPPDATA"]) / "BaiteRDOS/runners"
    return Path.home() / "Library/Application Support/BaiteRDOS/runners"


def plist_path(node: str) -> Path:
    return Path.home() / "Library/LaunchAgents" / (service_label(node) + ".plist")


def powershell(script: str, values: dict | None = None, check: bool = True):
    # No execution-policy override; paths are environment data, never script text.
    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
         "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); " + script],
        env={**os.environ, **(values or {})}, capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if check and result.returncode:
        # These commands only receive paths/task names, never credentials or config contents.
        raise ValueError("Windows 后台任务或文件权限操作失败：" + result.stderr.strip()[:2000])
    return result


def protect_path(path: Path) -> None:
    if not WINDOWS:
        path.chmod(0o700 if path.is_dir() else 0o600)
        return
    powershell("""
$p=$env:RDOS_PRIVATE_PATH; $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
$directory=[IO.Directory]::Exists($p)
if ($directory) { $acl=[Security.AccessControl.DirectorySecurity]::new(); $inherit='ContainerInherit,ObjectInherit' }
else { $acl=[Security.AccessControl.FileSecurity]::new(); $inherit='None' }
$acl.SetOwner($sid); $acl.SetAccessRuleProtection($true,$false)
foreach ($id in @($sid.Value,'S-1-5-18')) {
  $rule=[Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($id), 'FullControl', $inherit, 'None', 'Allow')
  $acl.AddAccessRule($rule)
}
if($directory){[IO.Directory]::SetAccessControl($p,$acl)}else{[IO.File]::SetAccessControl($p,$acl)}
""", {"RDOS_PRIVATE_PATH": str(path)})


def reject_links(path: Path) -> None:
    for part in (path, *path.parents):
        if not WINDOWS and str(part) in {"/var", "/tmp"}:
            continue
        if part.is_symlink() or (part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("安装与 Workspace 路径不能包含符号链接或重解析点")


def check_workspace_idle(work: Path) -> None:
    path = work / ".runner/runner.lock"
    reject_links(path)
    if not path.exists():
        return
    with path.open("a+b") as lock:
        try:
            if WINDOWS:
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Workspace 有手动运行的 Runner，请先停止，不修改或卸载安装") from None


def wait_workspace_idle(work: Path) -> None:
    # bootout/Task.Stop can return before the terminating process releases its handles.
    for attempt in range(50):
        try:
            check_workspace_idle(work)
            return
        except ValueError:
            if attempt == 49:
                raise
            time.sleep(0.1)


def require_ntfs(path: Path) -> None:
    import ctypes
    anchor = path.anchor
    filesystem = ctypes.create_unicode_buffer(32)
    if (anchor.startswith("\\\\") or ctypes.windll.kernel32.GetDriveTypeW(anchor) != 3
            or not ctypes.windll.kernel32.GetVolumeInformationW(anchor, None, 0, None, None, None, filesystem, 32)
            or filesystem.value != "NTFS"):
        raise ValueError("首版只支持本地固定 NTFS 目录；不支持网络盘或移动盘")
    for key in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        cloud = os.environ.get(key)
        if cloud and (Path(cloud).resolve() == path.resolve() or Path(cloud).resolve() in path.resolve().parents):
            raise ValueError("请使用不受云盘同步管理的本地专用 Workspace")


def build_task(node: str, python: Path, code: Path, config: Path, logs: Path) -> bytes:
    sid = powershell("[Security.Principal.WindowsIdentity]::GetCurrent().User.Value").stdout.strip()
    if not re.fullmatch(r"S-1-[0-9-]+", sid):
        raise ValueError("无法获取当前 Windows 用户 SID")
    task = ET.Element("Task", {"version": "1.2", "xmlns": "http://schemas.microsoft.com/windows/2004/02/mit/task"})
    def add(parent, tag, text=None, **attrs):
        item = ET.SubElement(parent, tag, attrs)
        item.text = text
        return item
    registration = add(task, "RegistrationInfo")
    add(registration, "Description", str(config))  # Identity/installation check, no credentials.
    trigger = add(add(task, "Triggers"), "LogonTrigger")
    add(trigger, "Enabled", "true"); add(trigger, "UserId", sid)
    principal = add(add(task, "Principals"), "Principal", id="RunnerUser")
    add(principal, "UserId", sid); add(principal, "LogonType", "InteractiveToken")
    add(principal, "RunLevel", "LeastPrivilege")
    settings = add(task, "Settings")
    for key, value in {
        "MultipleInstancesPolicy": "IgnoreNew", "DisallowStartIfOnBatteries": "false",
        "StopIfGoingOnBatteries": "false", "AllowHardTerminate": "true", "StartWhenAvailable": "true",
        "RunOnlyIfNetworkAvailable": "false", "AllowStartOnDemand": "true", "Enabled": "true",
        "RunOnlyIfIdle": "false", "WakeToRun": "false", "ExecutionTimeLimit": "PT0S",
    }.items():
        add(settings, key, value)
    restart = add(settings, "RestartOnFailure")
    add(restart, "Interval", "PT1M"); add(restart, "Count", "999")
    action = add(add(task, "Actions", Context="RunnerUser"), "Exec")
    add(action, "Command", str(python))
    add(action, "Arguments", subprocess.list2cmdline([str(code / "runner/runner.py"), "--config", str(config),
                                                     "--poll-seconds", "5", "--log-directory", str(logs)]))
    add(action, "WorkingDirectory", str(code))
    return ET.tostring(task, encoding="utf-16", xml_declaration=True)


def windows_task(node: str, action: str, xml: Path | None = None, check: bool = False):
    prefix = "$s=New-Object -ComObject 'Schedule.Service'; $s.Connect(); $f=$s.GetFolder('\\'); $n=$env:RDOS_TASK_NAME; "
    commands = {
        "register": "$xml=[IO.File]::ReadAllText($env:RDOS_TASK_XML); $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; $null=$f.RegisterTask($n,$xml,6,$sid,$null,3,$null)",
        "status": "$t=$f.GetTask($n); $instances=$t.GetInstances(0); $instance=$null; if($instances.Count -gt 0){$instance=$instances.Item(1).InstanceGuid}; @{running=($t.State -eq 4); instance_id=$instance; enabled=$t.Enabled; config=$t.Definition.RegistrationInfo.Description; last_result=$t.LastTaskResult} | ConvertTo-Json -Compress",
        "start": "$t=$f.GetTask($n); $t.Enabled=$true; if ($t.State -ne 4) { $null=$t.Run($null) }",
        "stop": "$t=$f.GetTask($n); $t.Enabled=$false; $t.Stop(0); for($i=0; $i -lt 50 -and $t.GetInstances(0).Count -gt 0; $i++){Start-Sleep -Milliseconds 100}; if($t.GetInstances(0).Count -gt 0){throw 'Task did not stop'}",
        "delete": "$f.DeleteTask($n,0)",
    }
    return powershell(prefix + commands[action], {"RDOS_TASK_NAME": service_label(node),
                                                   "RDOS_TASK_XML": str(xml or "")}, check=check)


def uninstall(node: str, root: Path) -> None:
    directory = root.expanduser().resolve() / runner_id(node)
    reject_links(directory)
    target = directory / "config.json"
    if not target.is_file():
        raise ValueError("没有可核实的安装；不删除未知目录")
    config = json.loads(target.read_text(encoding="utf-8"))
    work = Path(config["workspace"]).resolve()
    if config.get("runner_id") != node or work == directory or directory in work.parents or work in directory.parents:
        raise ValueError("安装身份或 Workspace 边界不匹配；不执行卸载")
    info = service_info(node)
    if WINDOWS:
        if info.returncode == 0 and json.loads(info.stdout).get("config") != str(target):
            raise ValueError("后台任务属于其他安装；不执行卸载")
        if info.returncode == 0:
            stop_service(node)
            windows_task(node, "delete", check=True)
    else:
        path = plist_path(node)
        if path.exists() and str(target) not in plistlib.loads(path.read_bytes()).get("ProgramArguments", []):
            raise ValueError("后台任务属于其他安装；不执行卸载")
        stop_service(node)
        path.unlink(missing_ok=True)
    wait_workspace_idle(work)
    # Exact validated installation only; Workspace and downloaded original config are untouched.
    shutil.rmtree(directory)
    print(f"已移除后台任务、程序和安装内凭证；保留 Workspace：{work}。云端 Token 未撤销，原始下载配置需另行保管或移除。")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".rdos-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_config(path: Path, workspace: Path | None) -> dict:
    path = path.expanduser()
    reject_links(path.absolute())
    protect_path(path)
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("配置必须是 JSON 对象")
    for key in ("runner_id", "runner_token", "control_url"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f"配置缺少 {key}")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,512}", config["runner_token"]):
        raise ValueError("runner_token 格式非法；请使用管理员下载的原始配置")
    runner_id(config["runner_id"])
    url = urllib.parse.urlsplit(config["control_url"])
    if (url.username or url.password or url.query or url.fragment or not url.hostname
            or not (url.scheme == "https" or
                    (url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"}))):
        raise ValueError("control_url 必须使用 HTTPS（本机测试可使用 loopback HTTP），且不能含凭证或查询参数")
    selected = (workspace or Path(config.get("workspace") or Path.home() / "RDOS-Workspace" / config["runner_id"])).expanduser()
    reject_links(selected.absolute())
    if not selected.is_absolute() or selected.resolve() in (Path(selected.anchor), Path.home().resolve()):
        raise ValueError("请通过 --workspace 指定专用工作目录的绝对路径，不能使用主目录或根目录")
    if WINDOWS:
        require_ntfs(selected)
    return {"runner_id": config["runner_id"], "runner_token": config["runner_token"],
            "control_url": config["control_url"].rstrip("/"), "workspace": str(selected.resolve())}


def git_blob_hash(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def download_runtime(config: dict) -> tuple[str, dict[str, bytes]]:
    health = json.loads(fetch(config["control_url"] + "/api/health"))
    release = health.get("app_release", "")
    if not isinstance(release, str) or not re.fullmatch(r"[0-9a-f]{40}", release):
        raise ValueError("云端未提供完整发布 SHA，停止安装；不猜测 main/latest 版本")
    # Read-only validation: no heartbeat, event or administrator credentials needed.
    fetch(config["control_url"] + "/api/runner/sync", config["runner_token"])
    tree = json.loads(fetch(f"https://api.github.com/repos/{REPOSITORY}/git/trees/{release}?recursive=1"))
    if tree.get("truncated"):
        raise ValueError("发布清单不完整")
    entries = {item["path"]: item for item in tree.get("tree", [])}
    files = {}
    names = RUNTIME_FILES
    if "runner/platform_support.py" not in entries:
        if WINDOWS:
            raise ValueError("云端发布版本尚不支持 Windows；请等待管理员验收发布，不回退 main/latest")
        names = LEGACY_FILES
    for name in names:
        entry = entries.get(name, {})
        if entry.get("type") != "blob" or entry.get("mode") not in {"100644", "100755"}:
            raise ValueError(f"发布中缺少普通文件：{name}")
        content = fetch(f"https://raw.githubusercontent.com/{REPOSITORY}/{release}/{name}")
        if git_blob_hash(content) != entry.get("sha"):
            raise ValueError(f"下载校验失败：{name}")
        if name.endswith(".py"):
            compile(content, name, "exec")
        files[name] = content
    return release, files


def build_plist(node: str, python: Path, code: Path, config: Path, logs: Path) -> dict:
    return {
        "Label": service_label(node),
        "ProgramArguments": [str(python), str(code / "runner/runner.py"), "--config", str(config),
                             "--poll-seconds", "5", "--log-directory", str(logs)],
        "WorkingDirectory": str(code), "RunAtLoad": True, "KeepAlive": True,
        "ThrottleInterval": 10, "ProcessType": "Background", "Umask": 0o077,
        "StandardOutPath": str(logs / "launchd.log"),
        "StandardErrorPath": str(logs / "launchd-error.log"),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"},
    }


def service_info(node: str) -> subprocess.CompletedProcess:
    if WINDOWS:
        return windows_task(node, "status")
    return subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{service_label(node)}"],
                          capture_output=True, text=True)


def stop_service(node: str) -> None:
    if WINDOWS:
        info = service_info(node)
        windows_task(node, "stop", check=True)
        if info.returncode == 0:
            config = json.loads(Path(json.loads(info.stdout)["config"]).read_text(encoding="utf-8"))
            if config.get("runner_id") != node:
                raise ValueError("后台任务的配置身份不匹配")
            wait_workspace_idle(Path(config["workspace"]))
        return
    target = f"gui/{os.getuid()}/{service_label(node)}"
    subprocess.run(["launchctl", "disable", target], check=True, capture_output=True)
    if service_info(node).returncode == 0:
        subprocess.run(["launchctl", "bootout", target], check=True, capture_output=True)


def start_service(node: str) -> None:
    if WINDOWS:
        windows_task(node, "start", check=True)
        return
    path = plist_path(node)
    if not path.is_file():
        raise ValueError("尚未安装该节点；请先执行 install")
    target = f"gui/{os.getuid()}/{service_label(node)}"
    subprocess.run(["launchctl", "enable", target], check=True, capture_output=True)
    info = service_info(node)
    if info.returncode:
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)],
                       check=True, capture_output=True)
    elif not re.search(r"\bpid = \d+", info.stdout):
        subprocess.run(["launchctl", "kickstart", target], check=True, capture_output=True)


def install(config_path: Path, workspace: Path | None, root: Path, start: bool = True) -> Path:
    config = load_config(config_path, workspace)
    node = config["runner_id"]
    reject_links(root.expanduser().absolute())
    directory = root.expanduser().resolve() / node
    reject_links(directory)
    target_config = directory / "config.json"
    work = Path(config["workspace"])
    if work == directory or directory in work.parents or work in directory.parents:
        raise ValueError("Workspace 与程序安装目录必须分离")
    previous = None
    if target_config.exists():
        previous = json.loads(target_config.read_text(encoding="utf-8"))
        for key in ("runner_id", "control_url", "workspace"):
            if previous.get(key) != config[key]:
                raise ValueError(f"已有安装的 {key} 不同；请先检查，不自动覆盖或迁移")
    elif work.exists() and any(work.iterdir()):
        raise ValueError("首次安装要求空 Workspace；请选择新目录，不自动接管已有工作文件")
    path = directory / "task.xml" if WINDOWS else plist_path(node)
    if path.exists():
        saved = {} if WINDOWS else plistlib.loads(path.read_bytes())
        if not WINDOWS and (saved.get("Label") != service_label(node) or str(target_config) not in saved.get("ProgramArguments", [])):
            raise ValueError("该节点已有另一处安装，停止以避免重复服务")
    if WINDOWS:
        info = service_info(node)
        if info.returncode == 0 and json.loads(info.stdout).get("config") != str(target_config):
            raise ValueError("该节点已有另一处安装，停止以避免重复服务")
    # Detect a manually launched Runner too, before replacing its configuration.
    info = service_info(node)
    managed_running = info.returncode == 0 and (json.loads(info.stdout).get("running") if WINDOWS else re.search(r"\bpid = \d+", info.stdout))
    if not managed_running:
        check_workspace_idle(work)
    release, files = download_runtime(config)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    protect_path(directory)
    files = {**files, "release.json": (json.dumps({"release": release}) + "\n").encode()}
    code = directory / "releases" / release
    # Validate all downloads before installation; never overwrite an existing release.
    if code.exists():
        for name, content in files.items():
            if not (code / name).is_file() or (code / name).read_bytes() != content:
                raise ValueError("本地程序校验失败；保留原安装，请检查后使用干净安装目录")
    else:
        code.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix=".download-", dir=code.parent) as temporary:
            stage = Path(temporary) / "runtime"
            for name, content in files.items():
                atomic_write(stage / name, content)
            stage.rename(code)
    python = directory / (".venv/Scripts/python.exe" if WINDOWS else ".venv/bin/python")
    if not python.exists():
        venv.EnvBuilder(with_pip=WINDOWS).create(directory / ".venv")
    if WINDOWS:
        subprocess.run([str(python), "-m", "pip", "--isolated", "install", "--disable-pip-version-check",
                        "--require-hashes", "--only-binary=:all:", "-r", str(code / "runner/requirements.lock")],
                       check=True, capture_output=True)
    subprocess.run([str(python), str(code / "runner/runner.py"), "--help"],
                   check=True, capture_output=True)
    logs = directory / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    protect_path(logs)
    service = (build_task(node, python.with_name("pythonw.exe"), code, target_config, logs) if WINDOWS
               else plistlib.dumps(build_plist(node, python, code, target_config, logs)))
    unchanged = (previous == config and path.exists() and path.read_bytes() == service)
    if not unchanged and service_info(node).returncode == 0:
        stop_service(node)
        wait_workspace_idle(work)
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    if WINDOWS:
        protect_path(work)
    atomic_write(target_config, (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode())
    atomic_write(path, service)
    atomic_write(directory / "manage.py", Path(__file__).read_bytes())
    if WINDOWS:
        if not unchanged or service_info(node).returncode != 0:
            windows_task(node, "register", path, check=True)
    else:
        subprocess.run(["plutil", "-lint", str(path)], check=True, capture_output=True)
    if start:
        start_service(node)
    return directory


def status(node: str, root: Path) -> dict:
    directory = root.expanduser().resolve() / node
    installed = (directory / "config.json").is_file()
    info = service_info(node)
    task = json.loads(info.stdout) if WINDOWS and info.returncode == 0 else {}
    pid = re.search(r"\bpid = (\d+)", info.stdout) if not WINDOWS and info.returncode == 0 else None
    result = {"runner_id": node, "installed": installed, "service_loaded": info.returncode == 0,
              "running": task.get("running", bool(pid)), "pid": int(pid.group(1)) if pid else None,
              "instance_id": task.get("instance_id"),
              "last_task_result": task.get("last_result"),
              "logs": str(directory / "logs"),
              "note": "本机进程状态不是云端 Online 或同步成功证明；请同时查看 Panel 心跳和同步健康"}
    if installed:
        config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        result["workspace"] = config["workspace"]
        state = Path(config["workspace"]) / ".runner/state.json"
        if state.is_file():
            try:
                saved = json.loads(state.read_text(encoding="utf-8"))
                result.update(revision=saved.get("revision"), sync_failure_count=saved.get("failure_count"))
            except (ValueError, OSError):
                result["sync_state"] = "unreadable"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "status", "start", "stop", "uninstall"))
    parser.add_argument("--config", type=Path, help="管理员交付的专属 JSON；不要传入 Token 字符串")
    parser.add_argument("--workspace", type=Path, help="本机专用 Workspace，覆盖配置中的原路径")
    parser.add_argument("--runner-id", help="status/start/stop 使用的节点 ID")
    parser.add_argument("--root", type=Path, default=install_root(), help="安装根目录（所有节点的父目录）")
    parser.add_argument("--no-start", action="store_true", help="安装但暂不启动；不要用于停止已运行的服务")
    args = parser.parse_args()
    if WINDOWS:
        if (sys.version_info[:2] != (3, 13) or platform.machine().lower() not in {"amd64", "x86_64"}
                or "ARM" in os.environ.get("PROCESSOR_ARCHITEW6432", "").upper() or sys.maxsize <= 2**32):
            parser.error("Windows 需要官方 Python 3.13 x64 用户级安装；ARM64 暂不支持")
        if sys.getwindowsversion().build < 19045:
            parser.error("需要 Windows 10 22H2 或 Windows 11")
    elif sys.platform != "darwin" or sys.version_info < (3, 10):
        parser.error("macOS 需要 Python 3.10+（推荐 3.13）；不自动安装系统 Python")
    os.umask(0o077)
    try:
        if args.action == "install":
            if not args.config:
                parser.error("install 需要 --config")
            directory = install(args.config, args.workspace, args.root, not args.no_start)
            print(f"安装完成：{directory}\n管理入口：{directory / 'manage.py'}")
            print("安装完成不等于联通验收；请检查 status、同步日志和 outbox ACK。")
        else:
            if not args.runner_id:
                parser.error("此操作需要 --runner-id")
            node = runner_id(args.runner_id)
            if args.action == "start":
                start_service(node)
            elif args.action == "stop":
                stop_service(node)
            elif args.action == "uninstall":
                uninstall(node, args.root)
            print(json.dumps(status(node, args.root), ensure_ascii=False, indent=2))
    except subprocess.CalledProcessError:
        raise SystemExit("本地程序或后台任务校验失败；检查 Python、当前用户会话及日志，不绕过系统安全策略") from None
    except (ValueError, OSError) as exc:
        if type(exc) is ValueError:
            raise SystemExit(str(exc)) from None
        raise SystemExit("配置格式、文件权限或本地环境异常；请检查路径与 JSON，勿粘贴 Token 到日志或聊天") from None


if __name__ == "__main__":
    main()
