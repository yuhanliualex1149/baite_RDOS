"""Frozen runtime entrypoint with an offline compatibility self-check."""
import json
import sys
from pathlib import Path

from rdos_contract import CONTRACT_VERSION
from runner.runner import main


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        import certifi
        import tzdata
        release = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])) / "release.json"
        version = json.loads(release.read_text(encoding="utf-8"))["release"] if release.is_file() else "development"
        print(json.dumps({"runtime_version": version, "protocol_versions": ["1"],
                          "contract_versions": [CONTRACT_VERSION]}))
    else:
        main()
