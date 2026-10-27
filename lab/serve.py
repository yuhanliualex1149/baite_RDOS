"""Only supported application entrypoint in this experimental checkout."""
from __future__ import annotations

import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-id", required=True)
    args = parser.parse_args()
    from lab import config
    cfg = config.load()
    state = config.instance(cfg)
    runtime = config.ROOT / "runtime"
    if os.environ.get("RDOS_LAB_INSTANCE_ID") != args.instance_id:
        raise SystemExit("实验启动实例标识不匹配")
    expected = {"RDOS_LAB_MODE": "1", "RDOS_LAB_ROOT": str(config.ROOT), "RDOS_LAB_RUNTIME": str(runtime),
                "RDOS_LAB_ENV_ID": cfg["env_id"], "RDOS_LAB_GENERATION": state["generation"],
                "BAITE_DB_PATH": str(runtime / "panel.sqlite3"),
                "BAITE_PUBLIC_URL": f'http://127.0.0.1:{cfg["port"]}'}
    if any(os.environ.get(k) != v for k, v in expected.items()):
        raise SystemExit("启动环境与实验配置不一致")
    if any(k in os.environ for k in ("ARK_API_KEY", "BAITE_LLM_API_KEY", "BAITE_LARK_CLI", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")):
        raise SystemExit("拒绝继承真实外部调用配置")
    credentials = config.read_json(runtime / "credentials.json")
    os.environ.update(BAITE_ADMIN_USERNAME=credentials["admin_username"], BAITE_ADMIN_PASSWORD=credentials["admin_password"],
                      BAITE_SESSION_SECRET=credentials["session_secret"], BAITE_DISABLE_EXTERNAL_SYNC="true",
                      BAITE_ENABLE_LEGACY_SYNC="false", TMPDIR=str(runtime / "tmp"))
    from server.lab_central_backend import configure
    configure(cfg)
    from server.lab_model_provider import configure as configure_model
    configure_model(cfg)
    from server.phase3_feishu import configure as configure_phase3
    configure_phase3(cfg)
    from lab.guard import install
    install(config.ROOT, cfg["port"])
    import uvicorn
    uvicorn.run("server.main:app", host="127.0.0.1", port=cfg["port"], access_log=False,
                proxy_headers=False, log_level="warning", workers=1)


if __name__ == "__main__":
    main()
