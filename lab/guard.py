"""Process-local restrictions; this is not an operating-system sandbox."""
from __future__ import annotations

import json
import os
import sys
import threading
import contextvars
import hashlib
import socket
import shutil
import weakref
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

_active = False
_runtime: Path | None = None
_local = threading.local()
_cli_executor = None
_model_token = None
_model_permit = contextvars.ContextVar("rdos_model_permit", default=False)
_MODEL_HOST = "ark.cn-beijing.volces.com"
_model_sockets = weakref.WeakSet()
_peer_crypto = None


def _model_stream(sock, address) -> bool:
    return (isinstance(sock, socket.socket)
            and sock.family in (socket.AF_INET, socket.AF_INET6)
            and sock.type == socket.SOCK_STREAM
            and isinstance(address, tuple) and len(address) >= 2 and address[1] == 443)


def _model_stream_sendmsg(sock, address) -> bool:
    """Permit buffered writes only on the exact model-authorized socket.

    CPython's selector transport can flush a stream with sendmsg from an event
    loop callback after the request ContextVar has been reset. Provenance is
    recorded at connect; a peer on port 443 alone never grants this capability.
    The weak set does not retain closed sockets or transfer trust on fd reuse.
    """
    if address is not None or sock not in _model_sockets:
        return False
    try:
        return _model_stream(sock, sock.getpeername())
    except (OSError, ValueError):
        return False


def register_model_token(token) -> None:
    global _model_token
    if _active or _model_token is not None:
        deny("model.late_registration")
    _model_token = token


def trusted_model_agent(agent) -> bool:
    return _model_token is not None and getattr(agent, "_lab_model_token", None) is _model_token


@contextmanager
def model_permit(agent):
    """One bounded Agent Plan request. Other sockets remain guarded."""
    require_active()
    if not trusted_model_agent(agent):
        deny("model.untrusted_agent")
    marker = _model_permit.set(True)
    try:
        yield
    finally:
        _model_permit.reset(marker)


def register_cli_executor(executor) -> None:
    """Only the configured phase-3 factory can register before installation."""
    global _cli_executor
    if _active:
        deny("phase3.late_executor")
    from server.phase3_feishu import trusted_executor
    if not trusted_executor(executor):
        deny("phase3.untrusted_executor")
    _cli_executor = executor


@contextmanager
def cli_permit(executor, argv: list[str], cwd: str, env: dict):
    """Authorize one exact Popen; never suspend the process/file/network hook."""
    require_active()
    if executor is not _cli_executor or executor is None or getattr(_local, "cli_permit", None):
        deny("phase3.cli_permit")
    executor.validate_argv(argv)
    if cwd != str(_runtime) or env != executor.child_env:
        deny("phase3.cli_environment")
    binary = Path(executor.binary)
    if binary.is_symlink() or hashlib.sha256(binary.read_bytes()).hexdigest() != executor.binary_hash:
        deny("phase3.cli_binary_changed")
    permit = {"argv": tuple(argv), "cwd": cwd, "env": dict(env), "used": False, "spawned": False}
    _local.cli_permit = permit
    try:
        yield
    finally:
        _local.cli_permit = None


def _consume_cli(event, args) -> bool:
    permit = getattr(_local, "cli_permit", None)
    if not permit:
        return False
    if event == "subprocess.Popen":
        executable, argv, cwd, env = args
        if (not permit["used"] and executable == permit["argv"][0] and tuple(argv) == permit["argv"]
                and cwd == permit["cwd"] and env == permit["env"]):
            permit["used"] = True
            return True
    # CPython may choose posix_spawn after the already checked Popen event.
    if event == "os.posix_spawn" and permit["used"] and not permit["spawned"]:
        executable, argv, env = args
        if executable == permit["argv"][0] and tuple(argv) == permit["argv"] and env == permit["env"]:
            permit["spawned"] = True
            return True
    return False


@contextmanager
def peer_crypto_permit(argv: list[str], payload: str):
    """One frozen crypto helper, with no command, module or path arguments."""
    require_active()
    if not _peer_crypto or tuple(argv) != _peer_crypto['argv'] or getattr(_local, 'cli_permit', None):
        deny('peer.crypto_command')
    try:
        value = json.loads(payload)
        action = value.get('action')
        fields = {'generate': {'action'}, 'sign': {'action','payload','private_key'},
                  'verify': {'action','payload','signature','public_key'}}
        if action not in fields or set(value) != fields[action] or len(payload.encode()) > 2_000_000:
            deny('peer.crypto_input')
        if any(not isinstance(v, str) for v in value.values()):
            deny('peer.crypto_input')
    except (ValueError, TypeError, AttributeError):
        deny('peer.crypto_input')
    for name, expected in _peer_crypto['hashes'].items():
        path = Path(name)
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            deny('peer.crypto_changed')
    # subprocess.run inherits the current environment unless its caller passes
    # an exact environment. This helper must never inherit NODE_OPTIONS.
    env = {'PATH':'/usr/bin:/bin', 'HOME':str(_runtime), 'TMPDIR':str(_runtime/'tmp')}
    _local.cli_permit = {'argv':tuple(argv), 'cwd':str(_runtime), 'env':env,
                         'used':False, 'spawned':False}
    try:
        yield env
    finally:
        _local.cli_permit = None


def require_active() -> None:
    if not _active or os.environ.get("RDOS_LAB_MODE") != "1":
        raise RuntimeError("此副本仅支持隔离实验入口：请使用 labctl.py start")


def deny(channel: str) -> None:
    if _runtime is not None and not getattr(_local, "logging", False):
        _local.logging = True
        try:
            path = _runtime / "blocked.jsonl"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            record = {"at": datetime.now(timezone.utc).isoformat(), "event": "blocked", "channel": channel,
                      "env_id": os.environ.get("RDOS_LAB_ENV_ID"), "generation": os.environ.get("RDOS_LAB_GENERATION")}
            with os.fdopen(fd, "a", encoding="utf-8") as output:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
        finally:
            _local.logging = False
    raise PermissionError("RDOS 实验环境已阻止：" + channel)


def install(root: Path, port: int) -> None:
    global _active, _runtime, _peer_crypto
    if _active:
        return
    root = root.resolve()
    _runtime = root / "runtime"
    read_roots = [root / "code", root / ".venv", _runtime, Path(sys.base_prefix).resolve(),
                  Path("/System/Library"), Path("/usr/lib"), Path("/usr/share"), Path("/private/etc")]
    read_files = {Path("/dev/null"), Path("/dev/urandom")}
    if _cli_executor is not None:
        read_files.add(Path(_cli_executor.binary))
    helper = root/'code/node_skills/rdos-workflow/scripts/peer_crypto.cjs'
    bundled = Path('/Users/alexliu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node')
    linux_node = Path('/www/server/nodejs/v26.1.0/bin/node')
    discovered = shutil.which('node')
    trusted_node = (bundled if bundled.is_file() else
                    linux_node if discovered and Path(discovered).resolve() == linux_node.resolve() else None)
    if helper.is_file() and trusted_node and trusted_node.is_file() and not helper.is_symlink():
        binary = trusted_node.resolve()
        _peer_crypto = {'argv':(str(binary),str(helper.resolve())),
                        'hashes':{str(binary):hashlib.sha256(binary.read_bytes()).hexdigest(),
                                  str(helper.resolve()):hashlib.sha256(helper.read_bytes()).hexdigest()}}
        read_files.add(binary)

    def allowed_path(value, *, write: bool = False) -> bool:
        if isinstance(value, int):
            return True
        try:
            raw = Path(os.fsdecode(value))
            # Audit events omit dir_fd, so relative mutation paths are ambiguous.
            if write and not raw.is_absolute():
                return False
            p = raw.absolute().resolve()
        except (TypeError, ValueError):
            return False
        if write:
            return p == _runtime or _runtime in p.parents
        if p.name in {".env", ".env.local"} or Path("/private/etc/baite-rdos") in p.parents:
            return False
        return p in read_files or any(p == r or r in p.parents for r in read_roots)

    def audit(event, args):
        if getattr(_local, "logging", False):
            return
        if event in {"socket.connect", "socket.connect_ex"}:
            sock = args[0]
            address = args[1]
            if not (_model_permit.get() and _model_stream(sock, address)):
                deny("network." + event.split(".")[-1])
            _model_sockets.add(sock)
        if event == "socket.sendmsg" and not _model_stream_sendmsg(args[0], args[1]):
            deny("network.sendmsg")
        if event == "socket.sendto":
            deny("network." + event.split(".")[-1])
        # asyncio resolves in a worker thread without propagating the request ContextVar.
        # The exact hostname alone is harmless; socket.connect remains request-scoped.
        if event == "socket.getaddrinfo" and args[0] not in ("127.0.0.1", None, _MODEL_HOST, _MODEL_HOST.encode()):
            deny("network.resolve")
        if event == "socket.bind":
            address = args[1]
            if not isinstance(address, tuple) or address[:2] != ("127.0.0.1", port):
                deny("network.bind")
        if event.startswith(("subprocess.", "os.exec", "os.spawn", "os.posix_spawn")) or event in {"os.system", "os.fork", "os.forkpty", "pty.spawn"}:
            if not _consume_cli(event, args):
                deny("process." + event)
        if event == "os.chdir":
            deny("filesystem.chdir")
        if event == "open":
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if not allowed_path(args[0], write=writing):
                deny("filesystem.write" if writing else "filesystem.read")
        if event == "sqlite3.connect":
            value = args[0]
            if isinstance(value, str) and value.startswith('file:'):
                # A single canonical read-only URI, never a user-supplied path
                # or a general URI exception (immutable/mode=rw/etc. denied).
                target = _runtime / 'panel.sqlite3'
                accepted = (value == target.as_uri() + '?mode=ro'
                            and not target.is_symlink() and allowed_path(target, write=True))
            else:
                accepted = allowed_path(value, write=True)
            if not accepted:
                deny("database.path")
        if event in {"os.listdir", "os.scandir"} and not allowed_path(args[0] if args else os.getcwd()):
            deny("filesystem.enumerate")
        if event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime"} and not allowed_path(args[0], write=True):
            deny("filesystem.mutation")
        if event in {"os.rename", "os.link", "os.symlink"}:
            if not all(allowed_path(arg, write=True) for arg in args[:2]):
                deny("filesystem.mutation")

    sys.addaudithook(audit)
    _active = True
