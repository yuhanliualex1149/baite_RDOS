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
    assert len(files) == 6, f"预期当前第一层 6 项，实际 {len(files)} 项"
    assert len(markdown_files) == 5, f"预期 5 个 Markdown，实际 {len(markdown_files)} 个"
    assert len(documents) == 1, f"预期 1 个飞书文档，实际 {len(documents)} 个"
    assert not ignored, f"当前正式文件夹出现未纳入项：{ignored}"
    for item in files:
        assert item["source_token"]
        assert item["content_hash"]
        assert item["modified_time"]
        assert item["content"].strip()
        print(
            f"PASS {item['source_type']:5} {item['source_name']} "
            f"{item['content_hash'][:12]} {item['size_bytes']} bytes"
        )
    print("REAL FEISHU READ-ONLY SMOKE: PASS (6/6)")


if __name__ == "__main__":
    main()
