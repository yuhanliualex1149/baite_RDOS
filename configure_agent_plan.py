"""Interactively save the local Agent Plan credential without echoing it."""
from __future__ import annotations

import getpass
import os
from pathlib import Path
import tempfile


CONFIG = Path(__file__).resolve().parent / ".env.local"


def main() -> None:
    print("RDOS · Agent Plan 本机配置")
    print(f"保存位置：{CONFIG}")
    print("粘贴 API Key 后按回车。输入不会显示；不会上传或打印密钥。")
    try:
        key = getpass.getpass("Agent Plan API Key：").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n已取消，配置未修改。")
        return
    if not key or any(char.isspace() for char in key):
        raise SystemExit("密钥为空或包含空白，配置未修改。请重新运行。")
    lines = CONFIG.read_text(encoding="utf-8").splitlines() if CONFIG.exists() else []
    existing = {line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
                for line in lines if "=" in line and not line.lstrip().startswith("#")}
    values = {"BAITE_LLM_API_KEY": key}
    for name, default in (("BAITE_LLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/plan/v3"),
                          ("BAITE_LLM_MODEL", "deepseek-v4-pro"),
                          ("BAITE_LLM_THINKING", "enabled"),
                          ("BAITE_LLM_REASONING_EFFORT", "high"),
                          ("BAITE_LLM_MAX_COMPLETION_TOKENS", "4096")):
        if not existing.get(name):
            values[name] = default
    output = []
    for line in lines:
        name = line.split("=", 1)[0].strip()
        if name in values and not line.lstrip().startswith("#"):
            output.append(f"{name}={values.pop(name)}")
        else:
            output.append(line)
    output.extend(f"{name}={value}" for name, value in values.items())
    descriptor, temporary = tempfile.mkstemp(prefix=".env.local-", dir=CONFIG.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(output) + "\n")
        os.replace(temporary, CONFIG)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print("已保存。请重启本地 Panel，再到“全局设置”检查模型状态。")


if __name__ == "__main__":
    main()
