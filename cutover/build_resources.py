"""Create a reviewed formal-resource candidate using the existing CLI login.

Run as the existing baite-rdos service user.  This does not contact the task
table or mutate Feishu; it only verifies the pinned CLI identity and writes
owner-only local preparation files.  The real event-table ID is filled by the
one-shot migration after the new table has been independently read back.
"""
from __future__ import annotations

import hashlib
import json
import os
import pwd
import subprocess
from pathlib import Path

from cutover import production_config as formal
from server.phase3_feishu import account_fingerprint


NODE_MEMBERS = {
    'node_yuhan': 'mem_p6BbcYJrwAcv_M5Y',
    'node_xiaofei': 'mem_x_rLn9VBAe_z8xol',
    'node_duoduo': 'mem_g3nKLUxRovywCSTk',
}


def _create_exact(path: Path, content: bytes):
    if path.exists() or path.is_symlink():
        if (path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077
                or path.read_bytes() != content):
            raise formal.ConfigurationError('现有正式准备文件不同或权限过宽，未覆盖')
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if path.exists():
            path.chmod(0o600)


def build(*, cli: Path = formal.LIVE_CLI, state_root: Path = formal.LIVE_DATABASE.parent,
          policy_source: Path | None = None, identity_response: dict | None = None):
    """Return the candidate path; test fixtures may supply an identity response."""
    if identity_response is None:
        if pwd.getpwuid(os.geteuid()).pw_name != 'baite-rdos' or os.environ.get('HOME') != str(state_root):
            raise formal.ConfigurationError('须以既有服务用户及其原 HOME 运行')
        if cli != formal.LIVE_CLI or cli.is_symlink() or not cli.is_file():
            raise formal.ConfigurationError('正式 CLI 路径不符合固定脚本')
    if cli.is_symlink() or not cli.is_file():
        raise formal.ConfigurationError('CLI 文件异常')
    cli_sha = hashlib.sha256(cli.read_bytes()).hexdigest()
    if identity_response is None:
        result = subprocess.run([str(cli), 'auth', 'status', '--json', '--verify'], capture_output=True,
                                text=True, timeout=40,
                                env={'HOME': str(state_root), 'PATH': str(formal.LIVE_NODE.parent)+':/usr/bin:/bin',
                                     'LANG': 'en_US.UTF-8', 'NO_COLOR': '1'})
        if result.returncode:
            raise formal.ConfigurationError('既有飞书登录未通过校验，未建立正式资源登记')
        try:
            identity_response = json.loads(result.stdout)
        except ValueError as exc:
            raise formal.ConfigurationError('CLI 身份响应不是 JSON') from exc
    fingerprint = account_fingerprint(identity_response)
    runtime, central = state_root / 'runtime', state_root / 'central'
    for directory in (runtime, central):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_mode & 0o077:
            raise formal.ConfigurationError('正式准备目录权限过宽')
    source = policy_source or Path(__file__).resolve().parents[1] / 'server/central_context/agent.md'
    policy = source.read_bytes()
    if not policy.strip():
        raise formal.ConfigurationError('中央职责文件为空')
    agent_md, memory_md = central / 'agent.md', central / 'memory.md'
    _create_exact(agent_md, policy)
    _create_exact(memory_md, b'')
    resources = {
        'schema_version': 1, 'mode': 'production',
        'base_token': formal.BASE_TOKEN, 'task_table': formal.TASK_TABLE,
        'task_view': formal.TASK_VIEW, 'event_table': 'tblPendingEvent000',
        'default_project_id': formal.DEFAULT_PROJECT,
        'finance_project_id': formal.FINANCE_PROJECT,
        'finance_folder': formal.FINANCE_FOLDER,
        'human_folder': formal.HUMAN_FOLDER, 'agent_folder': formal.AGENT_FOLDER,
        'selected_rag_folder': formal.SELECTED_RAG_FOLDER,
        'shared_skills_folder': formal.SHARED_SKILLS_FOLDER,
        'cli_path': str(cli), 'cli_sha256': cli_sha,
        'account_fingerprint': fingerprint, 'node_members': NODE_MEMBERS,
        'agent_md': str(agent_md), 'memory_md': str(memory_md),
    }
    formal.validate_resources(resources, cli_path=cli, state_root=state_root)
    candidate = runtime / 'rdos2-resources.candidate.json'
    _create_exact(candidate, (json.dumps(resources, ensure_ascii=False, sort_keys=True,
                                        separators=(',', ':')) + '\n').encode())
    return candidate


if __name__ == '__main__':
    # Print only the local candidate path; never print the raw Feishu identity.
    print(build())
