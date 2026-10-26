#!/usr/bin/env python3
"""One explicit node action per invocation; reads only this node's access file."""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_access(node_dir: Path) -> dict:
    path = node_dir / ".lab-access.json"
    if path.is_symlink():
        raise ValueError("节点配置不能是符号链接")
    access = json.loads(path.read_text(encoding="utf-8"))
    parsed = urllib.parse.urlsplit(access["base_url"])
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
            or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("实验服务地址必须是带指定端口的 http://127.0.0.1")
    if access.get("node_id") not in ("node_lab_codex", "node_lab_workbuddy"):
        raise ValueError("节点配置中的身份无效")
    if not isinstance(access.get("token"), str) or len(access["token"]) < 24 or not access.get("env_id") or not str(access.get("generation", "")):
        raise ValueError("节点配置缺少有效凭据、环境或代次")
    access["base_url"] = access["base_url"].rstrip("/")
    return access


def request(access: dict, path: str, payload: dict | None = None) -> dict:
    if not path.startswith("/api/lab/"):
        raise ValueError("只允许访问实验节点接口")
    headers = {"Authorization": "Bearer " + access["token"], "X-RDOS-Env-ID": str(access["env_id"]),
               "X-RDOS-Generation": str(access["generation"]), "Content-Type": "application/json"}
    req = urllib.request.Request(access["base_url"] + path, headers=headers,
                                 data=json.dumps(payload).encode("utf-8") if payload is not None else None)
    # Ignore OS/environment proxies and never follow HTTP redirects.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=15) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise ValueError("实验服务返回重定向；客户端已拒绝跟随") from None
        raise ValueError(f"实验请求失败（HTTP {exc.code}）；请核对当前节点、操作标识与服务状态") from None
    if result.get("env_id") != str(access["env_id"]) or str(result.get("generation")) != str(access["generation"]) or result.get("node_id") != access["node_id"] or result.get("source_environment") != "local_mock":
        raise ValueError("响应的环境、代次、节点或来源不匹配")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="RDOS 阶段 0：单次本地节点请求")
    parser.add_argument("--node-dir", "--workspace", dest="node_dir", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("context")
    commands.add_parser("tasks")
    commands.add_parser("read").add_argument("document_id")
    commands.add_parser("read-feedback").add_argument("feedback_id")
    submit = commands.add_parser("submit")
    submit.add_argument("--operation-id", required=True)
    submit.add_argument("--file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        node_dir = args.node_dir.resolve()
        access = load_access(node_dir)
        payload = None
        if args.command == "context":
            path = "/api/lab/context"
        elif args.command == "tasks":
            path = "/api/lab/tasks"
        elif args.command == "read":
            path = "/api/lab/documents/" + urllib.parse.quote(args.document_id, safe="")
        elif args.command == "read-feedback":
            path = "/api/lab/feedback/" + urllib.parse.quote(args.feedback_id, safe="")
        else:
            source = (node_dir / args.file).resolve()
            if node_dir not in source.parents or source.name == ".lab-access.json":
                raise ValueError("反馈文件必须位于当前节点目录内，且不能是节点凭据文件")
            content = source.read_text(encoding="utf-8")
            if access["token"] in content:
                raise ValueError("反馈正文含节点凭据，已拒绝发送")
            path = "/api/lab/feedback"
            payload = {"operation_id": args.operation_id, "content": content, "node_id": access["node_id"]}
        print(json.dumps(request(access, path, payload), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        # Do not print raw exception strings, response bodies, URLs or credentials.
        print("实验节点请求未完成；请核对当前节点配置、localhost 服务、操作标识及反馈文件。未自动重试。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
