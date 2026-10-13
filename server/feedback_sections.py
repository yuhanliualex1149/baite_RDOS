"""Split a shared Feishu document into individual feedback records.

Headings are helpful but optional: some Local Agents write a plain
``连接检查：...`` line, and several such records can share one document.
"""
from __future__ import annotations

import re


# A feedback entry can contain its own Markdown headings (including six
# project-work categories). Only headings that identify another *record* may
# close the current entry. Repeated 记录类型 / 反馈ID fields below also form a
# boundary for older documents without headings.
_HEADING = re.compile(r"^#{2,3}\s+(?:成员标识|连接检查|接入反馈|项目反馈|工作反馈|第[零一二三0-9]+阶段)(?:\s|[:：]|$)")
_RECORD_TITLE = re.compile(r"^(?:连接检查|接入反馈|项目反馈|工作反馈)\s*[:：]")
_RECORD_TYPE = re.compile(r"^(?:[-*]\s*)?(?:\*\*)?(?:记录类型|record_type)(?:\*\*)?\s*[:：]")
_FEEDBACK_ID = re.compile(r"^(?:[-*]\s*)?(?:\*\*)?(?:反馈ID|feedback_id)(?:\*\*)?\s*[:：]")


def records(text: str) -> list[str]:
    """Keep field values inside one record; never borrow from adjacent entries."""
    result: list[str] = []
    current: list[str] = []
    has_type = False
    has_id = False

    def flush() -> None:
        nonlocal current, has_type, has_id
        if any(line.strip() for line in current):
            result.append("\n".join(current).strip())
        current, has_type, has_id = [], False, False

    for line in text.splitlines():
        stripped = line.strip()
        heading = bool(_HEADING.match(stripped))
        title = bool(_RECORD_TITLE.match(stripped))
        record_type = bool(_RECORD_TYPE.match(stripped))
        feedback_id = bool(_FEEDBACK_ID.match(stripped))
        # A fresh heading/title or a repeated identifying field begins another
        # record. A marker alone is not a boundary: readback can follow it.
        if ((heading and stripped.startswith("## ") and any(part.strip() for part in current))
                or (heading or title) and (has_type or has_id)
                or record_type and has_type
                or feedback_id and has_id):
            flush()
        current.append(line)
        has_type |= record_type
        has_id |= feedback_id
    flush()
    return result
