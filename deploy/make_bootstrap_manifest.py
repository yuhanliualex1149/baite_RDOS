"""Generate a candidate manifest after both platform artifacts have been verified."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def artifact(path: Path, base: str, version: str, kind: str | None = None) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    result = {"version": version, "url": base.rstrip("/") + "/" + path.name,
              "size": path.stat().st_size, "sha256": digest.hexdigest()}
    if kind:
        result.update({"archive": kind, "min_bootstrapper_version": "0.1.0"})
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--mac-runtime", type=Path, required=True)
    parser.add_argument("--mac-installer", type=Path, required=True)
    parser.add_argument("--windows-runtime", type=Path, required=True)
    parser.add_argument("--windows-installer", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = {"schema_version": 1, "protocol_version": "1", "channel": "internal-test",
                "platforms": {
                    "macos-arm64": {"runtime": artifact(args.mac_runtime, args.base_url, args.version, "tar.gz"),
                                    "bootstrapper": artifact(args.mac_installer, args.base_url, "0.1.0")},
                    "windows-x64": {"runtime": artifact(args.windows_runtime, args.base_url, args.version, "zip"),
                                    "bootstrapper": artifact(args.windows_installer, args.base_url, "0.1.0")},
                }}
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
