"""Canonical hashes shared by the server and the standalone Runner."""
import hashlib
import hmac
import json


def content_hash(value: dict) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def seal_snapshot(snapshot: dict) -> dict:
    body = {key: value for key, value in snapshot.items() if key not in ("snapshot_hash", "changed")}
    return {**body, "snapshot_hash": content_hash(body)}


def verify_snapshot(snapshot: dict) -> None:
    expected = snapshot.get("snapshot_hash")
    if not isinstance(expected, str) or not hmac.compare_digest(expected, seal_snapshot(snapshot)["snapshot_hash"]):
        raise ValueError("完整快照 Hash 校验失败")
