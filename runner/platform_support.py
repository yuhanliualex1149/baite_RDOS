"""Small native filesystem boundary shared by manual and background Runners."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

WINDOWS = os.name == "nt"


def is_linklike(path: Path) -> bool:
    """Includes Windows junctions and other reparse points, without following them."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def reject_link_ancestors(path: Path) -> None:
    for part in (path, *path.parents):
        if not WINDOWS and str(part) in {"/var", "/tmp"}:
            continue  # macOS system aliases, not user-controlled links.
        if is_linklike(part):
            raise ValueError("路径含符号链接或 Windows 重解析点，拒绝越界访问")


def workspace_lock(path: Path):
    reject_link_ancestors(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.open("a+b")
    try:
        if WINDOWS:
            import msvcrt
            # locking() locks from the current file position, including beyond EOF.
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise ValueError("此 Workspace 已有 Runner 正在运行") from None
    return lock  # Closing the handle releases the native lock.


def portable_filename(name: str) -> str:
    if (not name or name in {".", ".."} or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or name.endswith((" ", "."))
            or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])", name.split(".")[0])):
        raise ValueError(f"不支持的跨平台文件名：{name}")
    return name


def release_directory(root: Path) -> Path:
    if root.parent.name == "projects" and re.fullmatch(r"prj_[A-Za-z0-9_-]+", root.name):
        return root.parent.parent / ".runner" / "project-releases" / root.name
    return root / ".runner" / "releases"


def owned_release(root: Path, release: Path) -> Path:
    reject_link_ancestors(release_directory(root))
    expected = release_directory(root).resolve()
    if not release.is_absolute() or release.parent.resolve() != expected or not release.name.startswith("revision-"):
        raise ValueError("runtime.json 只能指向此 Runner 的版本目录")
    # On macOS /var is a system alias for /private/var. Resolve the trusted base first.
    candidate = expected / release.name
    reject_link_ancestors(candidate)
    if not candidate.is_dir():
        raise ValueError("活动快照不存在")
    return candidate
