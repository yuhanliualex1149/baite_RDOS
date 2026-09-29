"""Install a per-user LaunchAgent without embedding credentials in its plist."""
import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path


def build_plist(python: Path, code: Path, config: Path, logs: Path) -> dict:
    return {
        "Label": "cn.yjmt.rdos.runner.yuhan",
        "ProgramArguments": [str(python), str(code / "runner" / "runner.py"), "--config", str(config),
                             "--poll-seconds", "5", "--log-directory", str(logs)],
        "WorkingDirectory": str(code), "RunAtLoad": True, "KeepAlive": True,
        "ThrottleInterval": 10, "ProcessType": "Background", "Umask": 0o077,
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--code", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path.home() / "Library/LaunchAgents/cn.yjmt.rdos.runner.yuhan.plist")
    parser.add_argument("--render-only", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This installer supports macOS only")
    os.umask(0o077)
    python, code, config = args.python.expanduser().absolute(), args.code.expanduser().resolve(strict=True), args.config.expanduser().resolve(strict=True)
    if not python.is_file() or not (code / "runner/runner.py").is_file():
        parser.error("Python or Runner does not exist")
    config.chmod(0o600)
    logs = args.logs.expanduser().resolve()
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = args.output.expanduser().absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".plist.tmp")
    with temporary.open("wb") as stream:
        plistlib.dump(build_plist(python, code, config, logs), stream)
    temporary.chmod(0o600)
    os.replace(temporary, output)
    subprocess.run(["plutil", "-lint", str(output)], check=True)
    if not args.render_only:
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/cn.yjmt.rdos.runner.yuhan"], check=False, capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(output)], check=True)
        subprocess.run(["launchctl", "print", f"{domain}/cn.yjmt.rdos.runner.yuhan"], check=True)
    print(f"LaunchAgent: {output}")


if __name__ == "__main__":
    main()
