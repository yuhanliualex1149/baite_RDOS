"""Rehearse local metadata upgrades on a consistent copy of the live SQLite.

Run as baite-rdos after staging. No Feishu/model calls or live-database writes
are made. This complements transport/migration tests; it does not prove remote
mutation or deployment. The restricted copy is retained as paired evidence.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import sqlite3

from cutover import production_config as formal
from cutover.build_resources import NODE_MEMBERS


def main():
    if pwd.getpwuid(os.geteuid()).pw_name != 'baite-rdos':
        raise RuntimeError('须以既有服务用户运行演练')
    root = formal.LIVE_DATABASE.parent / 'cutover-rehearsals' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    (root / 'runtime/tmp').mkdir(mode=0o700, parents=True)
    (root / 'central').mkdir(mode=0o700)
    database = root / 'baite.db'
    with sqlite3.connect(formal.LIVE_DATABASE.as_uri()+'?mode=ro', uri=True) as source:
        with sqlite3.connect(database) as target:
            source.backup(target)
            assert target.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    database.chmod(0o600)
    source_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    with sqlite3.connect(database) as connection:
        accounts = connection.execute('SELECT * FROM central_users ORDER BY id').fetchall()
        projects = {key: json.loads(body) for key, body in connection.execute(
            "SELECT id,payload FROM central_records WHERE kind='project'")}
        from cutover.formal_migration import archive_initial_memory
        archived_memory = archive_initial_memory(connection)
    policy = root / 'central/agent.md'
    policy.write_bytes((Path(__file__).resolve().parents[1] / 'server/central_context/agent.md').read_bytes())
    policy.chmod(0o600)
    memory = root / 'central/memory.md'
    memory.touch(mode=0o600)
    # Only this process points to the copy. No environment file is changed.
    os.environ.update(RDOS_RUNTIME_MODE='production', BAITE_DB_PATH=str(database),
                      BAITE_PUBLIC_URL=formal.PUBLIC_URL, BAITE_SESSION_SECRET='isolated-rehearsal-only-secret-000000')
    from server import production_runtime
    production_runtime._config = formal.FormalConfig(
        {'node_members': NODE_MEMBERS, 'base_token': formal.BASE_TOKEN, 'task_table': formal.TASK_TABLE},
        database, formal.LIVE_CLI, policy, memory)
    from server import db, central_store, phase3_store, phase1_runtime, rdos_projects, phase1_operations, central_memory, central_settings
    db.init_db(); central_store.init(); phase1_runtime.initialize(); phase3_store.init()
    rdos_projects.initialize({'mode':'feishu_production', 'zones':{'project':{
        'folder_id':formal.HUMAN_FOLDER, 'source_url':'https://scn33386s7ui.feishu.cn/drive/folder/'+formal.HUMAN_FOLDER}}})
    central_memory.initialize(); central_settings.initialize()
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT * FROM central_users ORDER BY id').fetchall() == accounts, '账号或密码哈希被修改'
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='phase3_mock'").fetchone(), '演练不应创建模拟后端'
    for key, original in projects.items():
        current = central_store.get('project', key)
        assert current['name'] == original['name'] and current.get('member_ids') == original.get('member_ids')
    finance = rdos_projects.get_project(formal.FINANCE_PROJECT)
    assert finance['resolved_folder_id'] == formal.FINANCE_FOLDER
    assert phase1_operations._environment()[0] == 'rdos_formal'
    assert phase3_store.get('state', 'business_workflow')
    result = {'status':'passed', 'scope':'SQLite metadata rehearsal only; no remote calls',
              'source_sqlite_sha256':source_hash, 'existing_accounts_preserved':len(accounts),
              'existing_projects_preserved':len(projects),
              'registered_projects':len(rdos_projects.list_projects(None, True)),
              'active_projects':len(rdos_projects.list_projects(None)),
              'node_count':len(phase3_store.node_registry()), 'evidence_directory':str(root)}
    result['archived_memory_records'] = archived_memory
    archived_ai = central_store.get('project', formal.LEGACY_AI_PROJECT)
    assert archived_ai is None or archived_ai.get('merged_into') == formal.DEFAULT_PROJECT and archived_ai['status'] == 'inactive'
    (root / 'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    (root / 'result.json').chmod(0o600)
    print(json.dumps(result,ensure_ascii=False))


if __name__ == '__main__':
    main()
