"""Enrollment, verified runtime installation and native per-user service control."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import re
import secrets
import shutil
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath


VERSION = "0.1.0"
PROTOCOL = "1"
ORIGIN = "https://yjmt.cn/baite-rdos"
RUNNER_ID = re.compile(r"^rnr_[A-Za-z0-9_-]{12,100}$")


def _version_tuple(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("发布清单的最低安装器版本无效")
    return tuple(int(part) for part in value.split("."))


def platform_key() -> str:
    machine = platform.machine().lower()
    if sys.platform == "darwin" and machine == "arm64":
        return "macos-arm64"
    if os.name == "nt" and machine in {"amd64", "x86_64"}:
        return "windows-x64"
    raise ValueError("首版只支持 Apple Silicon Mac 或 Windows 10/11 x64")


def installation_root(node: str) -> Path:
    if not RUNNER_ID.fullmatch(node):
        raise ValueError("接入码中的节点 ID 无效")
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "BaiteRDOS" / "runners" / node
    return Path.home() / "Library" / "Application Support" / "BaiteRDOS" / "runners" / node


def default_workspace(node: str) -> Path:
    return Path.home() / "RDOS-Workspace" / node


def _reject_link_ancestors(path: Path) -> None:
    for part in (path, *path.parents):
        if os.name != "nt" and str(part) in {"/var", "/tmp"}:
            continue
        if part.is_symlink() or (part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("安装目录或 Workspace 含符号链接/重解析点")


def _check_windows_workspace(path: Path) -> None:
    if os.name != "nt":
        return
    import ctypes
    filesystem = ctypes.create_unicode_buffer(32)
    anchor = path.anchor
    kernel = ctypes.windll.kernel32
    if (anchor.startswith("\\\\") or kernel.GetDriveTypeW(anchor) != 3
            or not kernel.GetVolumeInformationW(anchor, None, 0, None, None, None, filesystem, 32)
            or filesystem.value != "NTFS"):
        raise ValueError("首版只支持本地固定 NTFS Workspace")
    for key in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        cloud = os.environ.get(key)
        if cloud and (Path(cloud) == path or Path(cloud) in path.parents):
            raise ValueError("Workspace 不能位于云盘同步目录")


def node_from_code(code: str) -> str:
    node, dot, secret = code.strip().partition(".")
    if not dot or not RUNNER_ID.fullmatch(node) or len(secret) < 32:
        raise ValueError("接入码格式不正确")
    return node


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("下载地址发生重定向，已停止；请联系管理员")


def _request(url: str, data: dict | None = None, token: str = ""):
    headers = {"Accept": "application/json", "User-Agent": "RDOS-Bootstrapper/" + VERSION}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = json.dumps(data).encode("utf-8") if data is not None else None
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        if getattr(sys, "frozen", False):
            import certifi
            opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(
                context=ssl.create_default_context(cafile=certifi.where())))
        else:
            opener = urllib.request.build_opener(NoRedirect())
        return opener.open(request, timeout=30)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"服务返回 HTTP {exc.code}；请核对接入码、网络和节点状态") from None
    except urllib.error.URLError as exc:
        raise ValueError(f"无法连接 RDOS 服务：{exc.reason}") from None


def api_json(origin: str, path: str, data: dict | None = None, token: str = "") -> dict:
    with _request(origin.rstrip("/") + path, data, token) as response:
        return json.load(response)


def private_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, temporary = tempfile.mkstemp(prefix=".rdos-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        protect_path(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _clean_env() -> dict:
    env = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONPATH", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH"):
        env.pop(name, None)
    if getattr(sys, "frozen", False):
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    if os.name == "nt" and getattr(sys, "frozen", False):
        import ctypes
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    return env


def system_call(args: list[str], check: bool = True):
    result = subprocess.run(args, env=_clean_env(), capture_output=True, text=True, check=False)
    if check and result.returncode:
        raise ValueError(f"系统后台服务操作失败：{result.stderr.strip()[:700] or result.stdout.strip()[:700]}")
    return result


def protect_path(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o700 if path.is_dir() else 0o600)
        return
    # The private path is environment data, never interpolated into PowerShell code.
    script = """$p=$env:RDOS_PRIVATE_PATH; $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
$d=[IO.Directory]::Exists($p)
if($d){$acl=[Security.AccessControl.DirectorySecurity]::new();$inherit='ContainerInherit,ObjectInherit'}
else{$acl=[Security.AccessControl.FileSecurity]::new();$inherit='None'}
$acl.SetOwner($sid);$acl.SetAccessRuleProtection($true,$false)
foreach($id in @($sid.Value,'S-1-5-18')){
 $rule=[Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($id),'FullControl',$inherit,'None','Allow')
 $acl.AddAccessRule($rule)
}
if($d){[IO.Directory]::SetAccessControl($p,$acl)}else{[IO.File]::SetAccessControl($p,$acl)}"""
    env = _clean_env()
    env["RDOS_PRIVATE_PATH"] = str(path)
    result = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                            env=env, capture_output=True, text=True)
    if result.returncode:
        raise ValueError("无法设置 Windows 私有目录权限：" + result.stderr.strip()[:500])


def _safe_download_url(origin: str, url: str) -> None:
    base = urllib.parse.urlsplit(origin)
    target = urllib.parse.urlsplit(url)
    if (base.scheme != "https" or target.scheme != "https" or base.netloc != target.netloc
            or not target.path.startswith(base.path.rstrip("/") + "/downloads/")
            or target.query or target.fragment):
        raise ValueError("下载地址不在可信 RDOS 发行目录")


def download_verified(origin: str, artifact: dict, destination: Path) -> None:
    url = artifact["url"]
    _safe_download_url(origin, url)
    expected_size = int(artifact["size"])
    expected_hash = str(artifact["sha256"])
    if expected_size <= 0 or not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
        raise ValueError("发布清单缺少有效大小或 SHA-256")
    digest = hashlib.sha256()
    total = 0
    with _request(url) as response, destination.open("wb") as output:
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            total += len(chunk)
            if total > expected_size:
                raise ValueError("下载大小超过发布清单")
            digest.update(chunk)
            output.write(chunk)
    if total != expected_size or digest.hexdigest() != expected_hash:
        raise ValueError("下载文件大小或 SHA-256 不匹配，未安装")


def _member_name(name: str, seen: set[str]) -> PurePosixPath:
    if not name or "\\" in name or ":" in name or name.startswith("/"):
        raise ValueError("安装包包含不安全路径")
    path = PurePosixPath(name)
    if path.parts[0] != "rdos-runner" or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("安装包包含路径穿越")
    key = str(path).casefold()
    if key in seen:
        raise ValueError("安装包包含重名文件")
    seen.add(key)
    return path


def extract_runtime(archive: Path, destination: Path, kind: str) -> None:
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    seen: set[str] = set()
    if kind == "tar.gz":
        with tarfile.open(archive, "r:gz") as source:
            members = source.getmembers()
            if sum(item.size for item in members if item.isfile()) > 1024 * 1024 * 1024:
                raise ValueError("Runtime 解压体积过大")
            for member in members:
                path = _member_name(member.name.rstrip("/"), seen)
                if member.isdir() or member.isfile():
                    continue
                if not member.issym():
                    raise ValueError("安装包含不支持的特殊文件")
                target = PurePosixPath(member.linkname)
                if target.is_absolute():
                    raise ValueError("安装包链接越界")
                resolved = os.path.normpath(str(path.parent / target))
                if resolved.startswith("../") or resolved == "..":
                    raise ValueError("安装包链接越界")
            for member in (item for item in members if not item.issym()):
                target = destination / member.name
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.extractfile(member) as incoming, target.open("wb") as output:
                        shutil.copyfileobj(incoming, output)
                    target.chmod(member.mode & 0o755)
            for member in (item for item in members if item.issym()):
                target = destination / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(member.linkname)
            root = destination.resolve()
            for member in (item for item in members if item.issym()):
                try:
                    if not (destination / member.name).resolve(strict=True).is_relative_to(root):
                        raise ValueError("安装包链接越界")
                except (OSError, RuntimeError) as exc:
                    raise ValueError("安装包包含损坏链接") from exc
    elif kind == "zip":
        with zipfile.ZipFile(archive) as source:
            if sum(item.file_size for item in source.infolist()) > 1024 * 1024 * 1024:
                raise ValueError("Runtime 解压体积过大")
            for member in source.infolist():
                _member_name(member.filename.rstrip("/"), seen)
                if stat.S_IFMT(member.external_attr >> 16) == stat.S_IFLNK:
                    raise ValueError("Windows 安装包不能包含链接")
            for member in source.infolist():
                target = destination / member.filename
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(member) as incoming, target.open("wb") as output:
                        shutil.copyfileobj(incoming, output)
    else:
        raise ValueError("不支持的 Runtime 压缩格式")


def service_label(node: str) -> str:
    return "org.baite.rdos.runner." + node


def _plist_path(node: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / (service_label(node) + ".plist")


def _windows_task(node: str, action: str, xml: Path | None = None):
    task_name = service_label(node)
    if action == "register":
        return system_call(["schtasks.exe", "/create", "/tn", task_name, "/xml", str(xml), "/f"])
    if action == "start":
        system_call(["schtasks.exe", "/change", "/tn", task_name, "/enable"])
        return system_call(["schtasks.exe", "/run", "/tn", task_name])
    if action == "stop":
        system_call(["schtasks.exe", "/change", "/tn", task_name, "/disable"], check=False)
        return system_call(["schtasks.exe", "/end", "/tn", task_name], check=False)
    if action == "delete":
        return system_call(["schtasks.exe", "/delete", "/tn", task_name, "/f"])
    return system_call(["schtasks.exe", "/query", "/tn", task_name], check=False)


def _windows_task_xml(executable: Path, config: Path, logs: Path, manager: bool = False) -> bytes:
    identity = system_call(["whoami.exe", "/user", "/fo", "csv", "/nh"]).stdout
    match = re.search(r"S-1-[0-9-]+", identity)
    if not match:
        raise ValueError("无法确认当前 Windows 用户 SID")
    sid = match.group(0)
    task = ET.Element("Task", {"version": "1.2", "xmlns": "http://schemas.microsoft.com/windows/2004/02/mit/task"})
    def add(parent, name, value=None, **attrs):
        element = ET.SubElement(parent, name, attrs)
        element.text = value
        return element
    add(add(task, "RegistrationInfo"), "Description", str(config))
    trigger = add(add(task, "Triggers"), "LogonTrigger")
    add(trigger, "Enabled", "true")
    add(trigger, "UserId", sid)
    principal = add(add(task, "Principals"), "Principal", id="RunnerUser")
    add(principal, "UserId", sid)
    add(principal, "LogonType", "InteractiveToken")
    add(principal, "RunLevel", "LeastPrivilege")
    settings = add(task, "Settings")
    for name, value in {"MultipleInstancesPolicy": "IgnoreNew", "DisallowStartIfOnBatteries": "false",
                        "StopIfGoingOnBatteries": "false", "StartWhenAvailable": "true",
                        "WakeToRun": "false", "ExecutionTimeLimit": "PT0S", "Enabled": "true"}.items():
        add(settings, name, value)
    restart = add(settings, "RestartOnFailure")
    add(restart, "Interval", "PT1M")
    add(restart, "Count", "999")
    action = add(add(task, "Actions", Context="RunnerUser"), "Exec")
    add(action, "Command", str(executable))
    arguments = (["--manager"] if manager else []) + ["--config", str(config), "--poll-seconds", "5", "--log-directory", str(logs)]
    add(action, "Arguments", subprocess.list2cmdline(arguments))
    add(action, "WorkingDirectory", str(executable.parent))
    return ET.tostring(task, encoding="utf-16", xml_declaration=True)


def register_service(node: str, executable: Path, config: Path, logs: Path, manager: bool = False) -> None:
    if os.name == "nt":
        current = _windows_task_status(node)
        if current and current["config"] != str(config):
            raise ValueError("同名后台任务属于其他安装；请先由管理员核实")
        if current:
            if manager and (current.get("executable") != str(executable) or "--manager" not in current.get("arguments", "")):
                raise ValueError("已有后台任务仍指向旧 Runtime；需先隔离演练 Manager 接管")
            return
        xml = config.parent / "service.xml"
        xml.write_bytes(_windows_task_xml(executable, config, logs, manager))
        protect_path(xml)
        _windows_task(node, "register", xml)
        return
    path = _plist_path(node)
    path.parent.mkdir(parents=True, exist_ok=True)
    arguments = [str(executable)] + (["--manager"] if manager else []) + ["--config", str(config), "--poll-seconds", "5",
                                    "--log-directory", str(logs)]
    service = {"Label": service_label(node),
               "ProgramArguments": arguments,
               "WorkingDirectory": str(executable.parent), "RunAtLoad": True, "KeepAlive": True,
               "ThrottleInterval": 10, "ProcessType": "Background", "Umask": 0o077,
               "StandardOutPath": str(logs / "launchd.log"), "StandardErrorPath": str(logs / "launchd-error.log")}
    if path.exists():
        existing = plistlib.loads(path.read_bytes())
        previous_args = existing.get("ProgramArguments", [])
        if str(config) not in previous_args:
            raise ValueError("同名后台服务属于其他安装；请先由管理员核实")
        if manager and (previous_args[0] != str(executable) or "--manager" not in previous_args):
            raise ValueError("已有后台服务仍指向旧 Runtime；需先隔离演练 Manager 接管")
    with path.open("wb") as output:
        plistlib.dump(service, output)
    path.chmod(0o600)


def service_running(node: str) -> bool:
    if os.name == "nt":
        current = _windows_task_status(node)
        return bool(current and current["running"])
    result = system_call(["launchctl", "print", f"gui/{os.getuid()}/{service_label(node)}"], check=False)
    return result.returncode == 0 and re.search(r"\bpid = \d+", result.stdout) is not None


def _windows_task_status(node: str) -> dict | None:
    script = """[Console]::OutputEncoding=[Text.UTF8Encoding]::new();
$s=New-Object -ComObject 'Schedule.Service';$s.Connect();
try{$t=$s.GetFolder('\\').GetTask($env:RDOS_TASK_NAME);
 @{running=($t.State -eq 4);config=$t.Definition.RegistrationInfo.Description;executable=$t.Definition.Actions.Item(1).Path;arguments=$t.Definition.Actions.Item(1).Arguments}|ConvertTo-Json -Compress}
catch{exit 3}"""
    env = _clean_env()
    env["RDOS_TASK_NAME"] = service_label(node)
    result = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                            env=env, capture_output=True, text=True, encoding="utf-8", check=False)
    return json.loads(result.stdout) if result.returncode == 0 else None


def start_service(node: str) -> None:
    if os.name == "nt":
        if not service_running(node):
            _windows_task(node, "start")
        return
    target = f"gui/{os.getuid()}/{service_label(node)}"
    system_call(["launchctl", "enable", target])
    if not service_running(node):
        info = system_call(["launchctl", "print", target], check=False)
        if info.returncode:
            system_call(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(_plist_path(node))])
        else:
            system_call(["launchctl", "kickstart", target])


def stop_service(node: str) -> None:
    if os.name == "nt":
        _windows_task(node, "stop")
        return
    target = f"gui/{os.getuid()}/{service_label(node)}"
    system_call(["launchctl", "disable", target])
    if system_call(["launchctl", "print", target], check=False).returncode == 0:
        system_call(["launchctl", "bootout", target])


def _wait_workspace_idle(work: Path) -> None:
    lock_path = work / ".runner" / "runner.lock"
    if not lock_path.exists():
        return
    for _ in range(50):
        with lock_path.open("a+b") as handle:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                pass
        time.sleep(0.1)
    raise ValueError("Workspace 仍有 Runner 正在运行；未卸载")


def uninstall(node: str) -> str:
    root = installation_root(node)
    _reject_link_ancestors(root)
    config_path = root / "config.json"
    if not config_path.is_file():
        raise ValueError("未找到可核实的安装")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("runner_id") != node or not config.get("installation_id"):
        raise ValueError("安装身份不匹配，不删除任何文件")
    work = Path(config["workspace"]).resolve()
    if root.resolve() == work or root.resolve() in work.parents or work in root.resolve().parents:
        raise ValueError("Workspace 与程序目录重叠，不执行卸载")
    if os.name == "nt":
        xml = root / "service.xml"
        if not xml.is_file() or str(config_path) not in xml.read_text(encoding="utf-16"):
            raise ValueError("后台任务身份无法核实")
        stop_service(node)
        _windows_task(node, "delete")
    else:
        plist = _plist_path(node)
        if plist.is_file():
            if str(config_path) not in plistlib.loads(plist.read_bytes()).get("ProgramArguments", []):
                raise ValueError("后台服务身份无法核实")
            stop_service(node)
            plist.unlink()
    _wait_workspace_idle(work)
    shutil.rmtree(root)
    return f"已移除本机程序和凭证，Workspace 保留在 {work}。云端 Token 仍有效，请管理员停用节点或轮换凭证。"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_local_snapshot(root: Path, workspace: Path) -> int:
    entry = json.loads((root / "runtime.json").read_text(encoding="utf-8"))
    if entry.get("layout_version") != 1:
        raise ValueError("本地资料入口版本不匹配")
    release = Path(entry["snapshot_dir"]).resolve()
    expected = (workspace / ".runner" / ("project-releases" if root != workspace else "releases")).resolve()
    if root != workspace:
        expected = expected / root.name
    if release.parent != expected or not release.name.startswith("revision-"):
        raise ValueError("本地资料入口指向其他目录")
    snapshot = json.loads((release / "snapshot.json").read_text(encoding="utf-8"))
    expected_hash = snapshot.get("snapshot_hash", "")
    body = {key: value for key, value in snapshot.items() if key not in ("snapshot_hash", "changed")}
    actual_hash = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                            separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    if expected_hash != actual_hash:
        raise ValueError("本地快照 Hash 不匹配")
    manifest = json.loads((release / "local-manifest.json").read_text(encoding="utf-8"))
    for relative, digest in manifest.items():
        candidate = release / relative
        if candidate.is_symlink() or candidate.resolve().parent == release.parent or not candidate.resolve().is_relative_to(release):
            raise ValueError("本地资料文件越界")
        if not candidate.is_file() or _hash_file(candidate) != digest:
            raise ValueError("本地资料文件校验失败：" + relative)
    return int(entry["revision"])


def _wait_self_test(config: dict, root: Path, event_id: str, progress) -> None:
    origin, token = config["control_url"], config["runner_token"]
    workspace = Path(config["workspace"])
    node = config["runner_id"]
    outbox = workspace / "outbox"
    deadline = time.monotonic() + 180
    queued = False
    while time.monotonic() < deadline:
        if not service_running(node):
            progress("等待后台 Runner 启动")
            time.sleep(2)
            continue
        progress("检查心跳和资料同步")
        status = api_json(origin, "/api/runner/installation-status", token=token)
        if status["installation_id"] != config["installation_id"] or not status["heartbeat_at"]:
            time.sleep(2)
            continue
        if status["sync_health"] != "healthy":
            time.sleep(2)
            continue
        try:
            revision = _verified_local_snapshot(workspace, workspace)
            global_state = api_json(origin, "/api/runner/sync?known_revision=" + str(revision), token=token)
            if global_state.get("changed") or int(status["shared_revision"]) != revision:
                time.sleep(2)
                continue
            projects = api_json(origin, "/api/runner/projects/sync", token=token).get("projects", [])
            for project in projects:
                _verified_local_snapshot(workspace / "projects" / project["id"], workspace)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            time.sleep(2)
            continue
        event_file = outbox / (event_id + ".json")
        sent_file = outbox / "sent" / event_file.name
        rejected_file = outbox / "rejected" / event_file.name
        if rejected_file.exists():
            raise ValueError("诊断事件被服务端拒绝，请导出诊断并联系管理员")
        if not queued and not sent_file.exists() and not event_file.exists():
            outbox.mkdir(parents=True, exist_ok=True)
            private_write(event_file, {"event_id": event_id, "type": "installation_self_test",
                                       "installation_id": config["installation_id"],
                                       "shared_revision": revision, "project_count": len(projects)})
            queued = True
        progress("等待诊断事件 ACK")
        if sent_file.exists():
            final = api_json(origin, "/api/runner/installation-status", token=token)
            evidence = final.get("self_test") or {}
            if evidence.get("event_id") == event_id and evidence.get("shared_revision") == revision:
                return
        time.sleep(2)
    raise ValueError("自测尚未完成：后台心跳、资料同步或诊断 ACK 超时；可稍后点击重新自测")


def install(code: str, workspace: Path | None = None, progress=lambda message: None,
            origin: str = ORIGIN) -> dict:
    node = node_from_code(code)
    key = platform_key()
    root = installation_root(node)
    _reject_link_ancestors(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    protect_path(root)
    existing_config = root / "config.json"
    if existing_config.exists():
        old = json.loads(existing_config.read_text(encoding="utf-8"))
        if not old.get("installation_id"):
            raise ValueError("发现旧版脚本安装；v0.1 不自动迁移，请先确认旧服务和 Workspace")
    selected = (workspace or default_workspace(node)).expanduser()
    if not selected.is_absolute():
        raise ValueError("Workspace 必须是绝对路径")
    _reject_link_ancestors(selected)
    work = selected.resolve()
    if not work.is_absolute() or work == root.resolve() or root.resolve() in work.parents or work in root.resolve().parents:
        raise ValueError("Workspace 必须是与程序目录分离的本机专用目录")
    _check_windows_workspace(work)
    if not existing_config.exists() and work.exists() and any(work.iterdir()):
        raise ValueError("所选 Workspace 非空；请选新的专用目录，避免覆盖现有工作")
    progress("检查发行清单")
    manifest = api_json(origin, "/api/bootstrap/manifest")
    if str(manifest.get("protocol_version")) != PROTOCOL:
        raise ValueError("服务端同步协议与安装器不兼容")
    artifact = manifest["platforms"][key]["runtime"]
    if _version_tuple(artifact["min_bootstrapper_version"]) > _version_tuple(VERSION):
        raise ValueError("安装器版本过旧，请下载新版")
    version = str(artifact["version"])
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", version):
        raise ValueError("Runtime 版本无效")
    runtime = root / "runtime" / version
    executable = runtime / ("rdos-runner/rdos-runner.exe" if os.name == "nt" else "rdos-runner/rdos-runner")
    pending_path = root / "pending.json"
    if existing_config.exists() and not pending_path.exists():
        old = json.loads(existing_config.read_text(encoding="utf-8"))
        if old["runner_id"] != node or old["workspace"] != str(work):
            raise ValueError("已有安装身份或 Workspace 不同；未覆盖现有工作文件")
        state_path = root / "runtime-state.json"
        if not state_path.is_file():
            raise ValueError("发现旧版直接运行 Runtime 的服务；请先隔离演练 Manager 接管")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        version = state["current"]
        if not (root / "runtime" / version / executable.relative_to(runtime)).is_file():
            raise ValueError("已有安装的当前 Runtime 主程序缺失")
        progress("复用已有安装并重新自测")
        start_service(node)
        event_file = root / "self-test.json"
        event_id = str(uuid.uuid4())
        private_write(event_file, {"event_id": event_id})
        _wait_self_test(old, root, event_id, progress)
        retain_manager()
        return {"runner_id": node, "workspace": str(work), "runtime_version": version,
                "installation_id": old["installation_id"], "self_test_event_id": event_id}
    if not executable.is_file():
        progress("下载并校验独立 Runtime")
        (root / "runtime").mkdir(exist_ok=True)
        archive = root / ("runtime-download-" + version + (".zip" if os.name == "nt" else ".tar.gz"))
        download_verified(origin, artifact, archive)
        stage = Path(tempfile.mkdtemp(prefix=".runtime-", dir=root / "runtime"))
        try:
            extract_runtime(archive, stage / "package", artifact["archive"])
            candidate = stage / "package" / ("rdos-runner/rdos-runner.exe" if os.name == "nt" else "rdos-runner/rdos-runner")
            if not candidate.is_file():
                raise ValueError("Runtime 缺少主程序")
            if os.name != "nt" and not os.access(candidate, os.X_OK):
                raise ValueError("Runtime 主程序不可执行")
            os.replace(stage / "package", runtime)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            archive.unlink(missing_ok=True)
    _wait_workspace_idle(work)
    progress("保存安装身份并认领节点")
    fingerprint = hashlib.sha256(code.encode("utf-8")).hexdigest()
    if pending_path.exists():
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        if pending.get("code_fingerprint") != fingerprint:
            raise ValueError("此节点存在另一条未完成的安装；请先核实，不能覆盖凭证")
    else:
        pending = {"code_fingerprint": fingerprint, "installation_id": uuid.uuid4().hex,
                   "candidate_token": "brt_" + secrets.token_urlsafe(48)}
        private_write(pending_path, pending)
    api_json(origin, "/api/bootstrap/claim", {"runner_id": node, "enrollment_code": code,
             "installation_id": pending["installation_id"], "candidate_token": pending["candidate_token"]})
    config = {"control_url": origin, "runner_id": node, "runner_token": pending["candidate_token"],
              "workspace": str(work), "installation_id": pending["installation_id"],
              "bootstrapper_version": VERSION, "protocol_version": PROTOCOL}
    if existing_config.exists():
        old = json.loads(existing_config.read_text(encoding="utf-8"))
        if old.get("installation_id") != config["installation_id"] or old.get("workspace") != config["workspace"]:
            raise ValueError("已有安装身份或 Workspace 不同；未覆盖现有工作文件")
    private_write(existing_config, config)
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    protect_path(work)
    logs = root / "logs"
    logs.mkdir(exist_ok=True, mode=0o700)
    protect_path(logs)
    from bootstrapper.manager import initial_state
    state_path = root / "runtime-state.json"
    if not state_path.exists():
        private_write(state_path, initial_state(version, str(artifact.get("sha256", ""))))
    retained = retain_manager()
    if retained is None:
        raise ValueError("后台安装需要冻结图形安装器，不能从源码直接注册 Runtime")
    manager_executable = ((retained / "Contents" / "MacOS" / "RDOS Runner")
                          if os.name != "nt" else retained)
    progress("注册并启动后台 Runner")
    register_service(node, manager_executable, existing_config, logs, manager=True)
    start_service(node)
    event_path = root / "self-test.json"
    if event_path.exists():
        event_id = json.loads(event_path.read_text(encoding="utf-8"))["event_id"]
    else:
        event_id = str(uuid.uuid4())
        private_write(event_path, {"event_id": event_id})
    _wait_self_test(config, root, event_id, progress)
    pending_path.unlink(missing_ok=True)
    return {"runner_id": node, "workspace": str(work), "runtime_version": version,
            "installation_id": config["installation_id"], "self_test_event_id": event_id}


def status(node: str) -> dict:
    root = installation_root(node)
    path = root / "config.json"
    if not path.is_file():
        return {"installed": False, "running": False}
    config = json.loads(path.read_text(encoding="utf-8"))
    state_path = root / "runtime-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    return {"installed": True, "running": service_running(node), "workspace": config["workspace"],
            "installation_id": config.get("installation_id", ""),
            "current_version": state.get("current", ""), "target_version": state.get("target", ""),
            "update_state": state.get("phase", "legacy"), "update_error": state.get("error", "")}


def installed_nodes() -> list[str]:
    parent = installation_root("rnr_" + "0" * 12).parent
    if not parent.is_dir():
        return []
    nodes = []
    for path in parent.iterdir():
        if not RUNNER_ID.fullmatch(path.name) or path.is_symlink():
            continue
        try:
            config = json.loads((path / "config.json").read_text(encoding="utf-8"))
            if config.get("runner_id") == path.name and config.get("installation_id"):
                nodes.append(path.name)
        except (OSError, ValueError):
            continue
    return sorted(nodes)


def retain_manager() -> Path | None:
    """Keep a launchable GUI after the download/DMG has been removed."""
    if not getattr(sys, "frozen", False):
        return None
    executable = Path(sys.executable).resolve()
    if os.name == "nt":
        destination = Path(os.environ["LOCALAPPDATA"]) / "BaiteRDOS" / "RDOS Runner.exe"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if executable != destination and not destination.exists():
            shutil.copy2(executable, destination)
            protect_path(destination)
        return destination
    bundle = next((parent for parent in executable.parents if parent.suffix == ".app"), None)
    if bundle is None:
        raise ValueError("找不到图形安装器应用包，无法保留管理入口")
    destination = Path.home() / "Applications" / "RDOS Runner.app"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if bundle != destination and not destination.exists():
        temporary = Path(tempfile.mkdtemp(prefix=".rdos-manager-", dir=destination.parent))
        staging = temporary / "RDOS Runner.app"
        try:
            shutil.copytree(bundle, staging, symlinks=True)
            os.replace(staging, destination)
        finally:
            shutil.rmtree(temporary)
    return destination
