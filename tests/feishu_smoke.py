from __future__ import annotations

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from server.rag_sync import FeishuRagSource  # noqa: E402


def main() -> None:
    folder_token = os.environ.get("BAITE_SELECTED_RAG_FOLDER_TOKEN", "").strip()
    if not folder_token:
        raise SystemExit("请设置 BAITE_SELECTED_RAG_FOLDER_TOKEN")
    files, ignored = FeishuRagSource().fetch(folder_token)
    markdown_files = [item for item in files if item["source_type"] == "file"]
    documents = [item for item in files if item["source_type"] == "docx"]
    assert files, "当前资料文件夹没有可同步的 Markdown 或飞书文档"
    from server.rag_sync import _manifest
    _manifest(files)
    for item in files:
        assert item["source_token"]
        assert item["content_hash"]
        assert item["modified_time"]
        assert item["content"].strip()
        print(
            f"PASS {item['source_type']:5} {item['source_name']} "
            f"{item['content_hash'][:12]} {item['size_bytes']} bytes"
        )
    print(f"REAL FEISHU READ-ONLY SMOKE: PASS ({len(markdown_files)} Markdown, {len(documents)} documents, {len(ignored)} unsupported)")


if __name__ == "__main__":
    main()
