"""Build self-contained platform artifacts on the target OS, not on the user's machine."""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]


def build(version: str, output: Path) -> dict:
    if sys.platform == "darwin" and platform.machine().lower() == "arm64":
        key = "macos-arm64"
    elif sys.platform == "win32" and platform.machine().lower() in {"amd64", "x86_64"}:
        key = "windows-x64"
    else:
        raise SystemExit("只能在 Apple Silicon Mac 或 Windows x64 构建")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    work = output / "build"
    work.mkdir(exist_ok=True)
    release = work / "release.json"
    release.write_text(json.dumps({"release": version}) + "\n", encoding="utf-8")
    separator = ";" if sys.platform == "win32" else ":"
    common = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--paths", str(PROJECT),
              "--distpath", str(output / "dist"), "--workpath", str(work / "pyinstaller"),
              "--specpath", str(work)]
    runtime = common + ["--onedir", "--name", "rdos-runner", "--collect-data", "tzdata",
                        "--collect-data", "certifi", "--add-data", str(release) + separator + ".",
                        str(PROJECT / "bootstrapper" / "runner_entry.py")]
    subprocess.run(runtime, cwd=PROJECT, check=True)
    gui = common + ["--windowed", "--name", "RDOS Runner", "--collect-data", "certifi"]
    gui += ["--onefile"] if sys.platform == "win32" else ["--onedir", "--osx-bundle-identifier", "cn.yjmt.rdos.bootstrapper"]
    gui.append(str(PROJECT / "bootstrapper" / "gui.py"))
    subprocess.run(gui, cwd=PROJECT, check=True)
    dist = output / "dist"
    folder = dist / "rdos-runner"
    if key == "macos-arm64":
        archive = output / f"rdos-runtime-{key}-{version}.tar.gz"
        with tarfile.open(archive, "w:gz", dereference=False) as target:
            target.add(folder, arcname="rdos-runner", recursive=True)
        dmg = output / f"RDOS-Runner-{key}-{version}.dmg"
        with tempfile.TemporaryDirectory(prefix="rdos-dmg-", dir=output) as staging:
            candidate = Path(staging) / "runner.dmg"
            subprocess.run(["hdiutil", "create", "-volname", "RDOS Runner", "-srcfolder",
                            str(dist / "RDOS Runner.app"), "-format", "UDZO", str(candidate)], check=True)
            candidate.replace(dmg)
        installer = dmg
    else:
        archive = output / f"rdos-runtime-{key}-{version}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for path in folder.rglob("*"):
                if path.is_file():
                    target.write(path, path.relative_to(dist))
        installer = output / f"RDOS-Runner-{key}-{version}.exe"
        shutil.copy2(dist / "RDOS Runner.exe", installer)
    return {"platform": key, "runtime": str(archive), "bootstrapper": str(installer)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    print(json.dumps(build(options.version, options.output), ensure_ascii=False))
