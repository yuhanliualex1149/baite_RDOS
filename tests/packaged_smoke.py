"""Run real frozen binaries with no development Python variables or PATH entries."""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bootstrapper.core import extract_runtime


def run(binary: Path, argument: str, env: dict) -> None:
    result = subprocess.run([str(binary), argument], env=env, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=40, check=False)
    if result.returncode:
        raise AssertionError(f"{binary.name} failed ({result.returncode}): {result.stderr[-1000:]}")


if __name__ == "__main__":
    output = Path(sys.argv[1]).resolve()
    env = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX", "DYLD_LIBRARY_PATH"):
        env.pop(name, None)
    if sys.platform == "darwin":
        env["PATH"] = "/usr/bin:/bin"
        runtime = output / "dist" / "rdos-runner" / "rdos-runner"
        gui = output / "dist" / "RDOS Runner.app" / "Contents" / "MacOS" / "RDOS Runner"
        run(runtime, "--help", env)
        checked = subprocess.run([str(runtime), "--self-check"], env=env, capture_output=True,
                                 text=True, encoding="utf-8", timeout=40, check=True)
        if json.loads(checked.stdout)["runtime_version"] == "development":
            raise AssertionError("Frozen Runtime did not locate packaged release.json")
        run(gui, "--self-check", env)
        manager_help = subprocess.run([str(gui), "--manager", "--help"], env=env,
                                      capture_output=True, timeout=40, check=False)
        if manager_help.returncode:
            raise AssertionError("Frozen GUI lacks Manager entrypoint")
        stable_manager_help = subprocess.run([str(runtime), "--manager", "--help"], env=env,
                                             capture_output=True, timeout=40, check=False)
        if stable_manager_help.returncode:
            raise AssertionError("Frozen onedir Runtime lacks stable Manager entrypoint")
        for binary in (runtime, gui):
            linked = subprocess.run(["/usr/bin/otool", "-L", str(binary)], capture_output=True,
                                    text=True, check=True).stdout.split("\n", 1)[1]
            for forbidden in ("/opt/homebrew", "/Users/", "/Library/Frameworks/Python.framework"):
                if forbidden in linked:
                    raise AssertionError(f"Frozen binary linked to build machine path: {forbidden}")
        archive = next(output.glob("rdos-runtime-macos-arm64-*.tar.gz"))
        kind = "tar.gz"
    elif os.name == "nt":
        system_root = env.get("SystemRoot") or env.get("WINDIR") or r"C:\Windows"
        env["PATH"] = os.path.join(system_root, "System32") + os.pathsep + system_root
        runtime = output / "dist" / "rdos-runner" / "rdos-runner.exe"
        gui = output / "dist" / "RDOS Runner.exe"
        run(runtime, "--help", env)
        checked = subprocess.run([str(runtime), "--self-check"], env=env, capture_output=True,
                                 text=True, encoding="utf-8", timeout=40, check=True)
        if json.loads(checked.stdout)["runtime_version"] == "development":
            raise AssertionError("Frozen Runtime did not locate packaged release.json")
        run(gui, "--self-check", env)
        manager_help = subprocess.run([str(gui), "--manager", "--help"], env=env,
                                      capture_output=True, timeout=40, check=False)
        if manager_help.returncode:
            raise AssertionError("Frozen GUI lacks Manager entrypoint")
        stable_manager_help = subprocess.run([str(runtime), "--manager", "--help"], env=env,
                                             capture_output=True, timeout=40, check=False)
        if stable_manager_help.returncode:
            raise AssertionError("Frozen onedir Runtime lacks stable Manager entrypoint")
        archive = next(output.glob("rdos-runtime-windows-x64-*.zip"))
        kind = "zip"
    else:
        raise SystemExit("Native packaged smoke runs only on supported targets")
    with tempfile.TemporaryDirectory(prefix="rdos-relocated-") as temporary:
        moved = Path(temporary) / "runtime"
        extract_runtime(archive, moved, kind)
        run(moved / "rdos-runner" / runtime.name, "--help", env)
    print("Frozen Runtime and GUI bootstrapper start without Python in PATH; relocated archive runs")
