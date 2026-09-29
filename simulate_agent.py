from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from runner.runner import load_config


def event_for(args: argparse.Namespace, runner_id: str) -> Dict[str, Any]:
    event_id = str(uuid.uuid4())
    if args.scenario == "progress":
        return {
            "event_id": event_id,
            "type": "progress",
            "current_focus": args.name or "验证本地协作链路",
            "status": "working",
            "summary": args.summary or "已完成一次模拟进度回传。",
            "needs_collaboration": "",
        }
    if args.scenario == "activity":
        return {
            "event_id": event_id,
            "type": "activity_record",
            "kind": args.kind,
            "name": args.name or "Local Working Method",
            "version": args.version,
            "purpose": args.summary or "记录本地执行方法的元数据，不上传工作过程。",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
    if args.scenario == "skill-proposal":
        return {
            "event_id": event_id,
            "type": "skill_change_proposal",
            "skill_name": args.name or "Example Shared Skill",
            "base_version": args.base_version,
            "proposed_version": args.version or "v0.2",
            "summary": args.summary or "用于验证 Proposal 的提交、比较和发布流程。",
            "content": "# Example Shared Skill\n\n这是模拟 Agent 提交的完整候选内容。\n",
        }
    if args.scenario == "collaboration-request":
        if not args.target_runner_id:
            raise ValueError("collaboration-request 需要 --target-runner-id")
        return {
            "event_id": event_id,
            "type": "collaboration_request",
            "to_runner_id": args.target_runner_id,
            "category": args.category,
            "topic": args.name or "核对一项资料",
            "summary": args.summary or "请确认这项资料是否完整。",
            "reference": "workspace/files/example.md",
        }
    if args.scenario == "collaboration-response":
        if not args.collaboration_id:
            raise ValueError("collaboration-response 需要 --collaboration-id")
        return {
            "event_id": event_id,
            "type": "collaboration_response",
            "collaboration_id": args.collaboration_id,
            "action": args.action,
            "note": args.summary or "已完成模拟处理。",
        }
    raise ValueError(f"未知场景：{args.scenario}")


def main() -> None:
    parser = argparse.ArgumentParser(description="生成一个 Local Agent 模拟 outbox 事件")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "scenario",
        choices=(
            "progress",
            "activity",
            "skill-proposal",
            "collaboration-request",
            "collaboration-response",
            "project-status",
        ),
    )
    parser.add_argument("--name", default="")
    parser.add_argument("--summary", default="")
    parser.add_argument("--kind", choices=("skill", "workflow", "method"), default="method")
    parser.add_argument("--version", default="")
    parser.add_argument("--base-version", default="v0.1")
    parser.add_argument("--target-runner-id", default="")
    parser.add_argument(
        "--category",
        choices=("ordinary", "risk", "external_release", "irreversible"),
        default="ordinary",
    )
    parser.add_argument("--collaboration-id", type=int)
    parser.add_argument("--action", choices=("confirm", "return", "escalate"), default="confirm")
    parser.add_argument("--project-id", default="")
    parser.add_argument(
        "--gate-code", choices=("", "G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7"), default=""
    )
    args = parser.parse_args()

    config = load_config(args.config)
    workspace = Path(config["workspace"])
    if args.scenario == "project-status":
        if not args.project_id:
            raise SystemExit("project-status 需要 --project-id")
        project_root = workspace / "projects" / args.project_id
        work = project_root / "work"
        files = work / "files"
        files.mkdir(parents=True, exist_ok=True)
        sample = files / "prototype.md"
        if not sample.exists():
            sample.write_text("# Project prototype\n\n模拟研发文件。\n", encoding="utf-8")
        payload = {
            "summary": args.summary or "已完成一次模拟项目进展更新。",
            "workflow_nodes": [
                {
                    "code": "C3",
                    "status": "in_progress",
                    "evidence": ["files/prototype.md"],
                }
            ],
            "todos": [
                {
                    "todo_id": "todo-demo-001",
                    "title": args.name or "核对模拟原型",
                    "owner": "项目研发负责人",
                    "status": "in_progress",
                    "due_date": None,
                }
            ],
            "file_notes": [
                {
                    "path": "files/prototype.md",
                    "status": "in_progress",
                    "summary": "模拟文件已生成",
                }
            ],
            "issues": [],
            "gate_claim": (
                {
                    "code": args.gate_code,
                    "claimed_passed": True,
                    "evidence": ["files/prototype.md"],
                    "note": "模拟 Gate 通过记录",
                }
                if args.gate_code
                else None
            ),
        }
        destination = work / "project_status.json"
        destination.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"已更新模拟项目状态：{destination}")
        return
    outbox = workspace / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    event = event_for(args, config["runner_id"])
    destination = outbox / f"{event['type']}-{event['event_id']}.json"
    destination.write_text(json.dumps(event, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已生成模拟事件：{destination}")


if __name__ == "__main__":
    main()
