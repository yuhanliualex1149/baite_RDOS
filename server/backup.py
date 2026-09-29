"""Consistent online SQLite backups, including committed WAL pages."""
import argparse
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path


def backup_database(source: Path, destination: Path) -> dict:
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if source == destination or destination.exists():
        raise ValueError("备份目标必须是新的独立文件")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".backup-", suffix=".db", dir=destination.parent)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(temporary)) as target:
                original.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("备份完整性检查失败")
                has_settings = target.execute("SELECT 1 FROM sqlite_master WHERE name='settings'").fetchone()
                version = target.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone() if has_settings else None
        os.replace(temporary, destination)
        return {"created_at": datetime.now(timezone.utc).isoformat(), "schema_version": version[0] if version else None,
                "app_release": os.environ.get("BAITE_APP_RELEASE", "development"), "database": destination.name}
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="RDOS 在线一致性备份")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--retain-days", type=int, default=14)
    args = parser.parse_args()
    if args.retain_days < 1:
        parser.error("retain-days must be positive")
    source = Path(os.environ["BAITE_DB_PATH"])
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = directory / ("rdos-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".db")
    metadata = backup_database(source, destination)
    metadata_path = destination.with_suffix(".json")
    descriptor = os.open(metadata_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(metadata, output, indent=2)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.retain_days)).timestamp()
    for old in directory.glob("rdos-*.db"):
        if not old.is_symlink() and old.stat().st_mtime < cutoff and old.with_suffix(".json").is_file():
            old.unlink()
            old.with_suffix(".json").unlink()
    print(json.dumps(metadata))


if __name__ == "__main__":
    main()
