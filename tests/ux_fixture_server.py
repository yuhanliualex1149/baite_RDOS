"""Explicit loopback-only UX fixture; never used by start_local.py or real APIs.
Replays a read-only baseline. Named scenarios and received writes are in-memory
schema fixtures, never Feishu/LLM calls. This harness is not real acceptance.
POST /__test__/scenario resets state; GET /__test__/writes shows redacted writes.
POST /__test__/session {"expired": true} expires fixture sessions without clearing
the page, replayed data, or writes. A successful fixture login restores access;
POST {"expired": false} also restores it. GET shows the current expiry state.
Both ports share scenario/session controls. clipboard-failure injects a rejecting
writeText stub into fixture HTML before application scripts, never source files.
"""
import copy
import hashlib
import json
import pathlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / 'data/ux-review-20261011'
SNAP = json.loads((DATA / 'snapshot.json').read_text())
MEMBER_NAMES = {
    user['id']: user.get('display_name') or user.get('username') or user['id']
    for snapshot in SNAP.values()
    for user in snapshot.get('/workspace?project_id=rdos-self', {}).get('users', [])
}


def add_member_names(project):
    # Only name members already on that project; both ports replay identical data.
    project['member_names'] = {member: MEMBER_NAMES[member] for member in project.get('member_ids', []) if member in MEMBER_NAMES}
    return project


for snapshot in SNAP.values():
    for project in snapshot.get('/projects', {}).get('items', []):
        add_member_names(project)
    for key, value in snapshot.items():
        if key.startswith('/workspace?'):
            if value.get('project'):
                add_member_names(value['project'])
            for project in value.get('projects', []):
                add_member_names(project)
SCENARIO = {'name': 'baseline'}
STATES, WRITES, ATTEMPTS = {}, [], {}
LOCK = threading.RLock()
UNCERTAIN = {'publish-uncertain', 'write-uncertain'}
TASK_SCENARIOS = {'task-draft', 'taskdraft-unpublished', 'task-update', 'task-withdraw', 'task-conflict', *UNCERTAIN}
METHOD_SCENARIOS = {'method-source-stale', 'source-stale', 'template-proposal', *UNCERTAIN}
PROJECT_SCENARIOS = {'project-failure', 'project-repair'}
CLIPBOARD_FAILURE_SCRIPT = b'''<script data-ux-fixture="clipboard-failure">
// Explicit UX fixture fault: not shipped by the real RDOS server.
(() => {
  const rejectWrite = () => Promise.reject(new DOMException('UX fixture clipboard write denied', 'NotAllowedError'));
  Object.defineProperty(navigator, 'clipboard', {configurable: true, value: Object.freeze({writeText: rejectWrite})});
  window.__UX_FIXTURE_CLIPBOARD_FAILURE__ = true;
})();
</script>'''


def session_expired():
    return bool(SCENARIO.get('session_expired', SCENARIO.get('name') == 'expired-session'))


def redact(value):
    if isinstance(value, dict):
        return {k: '[已隐藏]' if any(term in k.lower() for term in ('password', 'secret', 'api_key', 'api-key', 'token', 'authorization', 'credential')) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def workspace(snap):
    return snap['/workspace?project_id=rdos-self']


def state_for(who):
    with LOCK:
        if who in STATES:
            return STATES[who]
        snap = copy.deepcopy(SNAP[who])
        w = workspace(snap)
        # The real parser marks feedback read back from the source document.
        # The frozen snapshots predate that response field; replay the same
        # read-back evidence on both comparison ports.
        for key, value in snap.items():
            if key.startswith('/workspace?project_id='):
                for item in value.get('feedback', []):
                    item.setdefault('source_readback', bool(item.get('source_url')))
        user_id = snap['/auth/me']['user']['id']
        name = SCENARIO.get('name', 'baseline')
        if name in TASK_SCENARIOS:
            action = 'update' if name in ('task-update', 'task-conflict') else 'withdraw' if name == 'task-withdraw' else 'create'
            w['task_drafts'] = [{'id': 'ux-task-draft', 'project_id': 'rdos-self',
                                 'task_id': 'UX-DRAFT-01', 'instructions': '仅用于界面测试：核对一份已授权材料并报告具体问题。尚未发布，不能视为真实组织任务。',
                                 'member_ids': [user_id], 'task_kind': 'work', 'stage': '',
                                 'nature': '一次性', 'related_task': '', 'version': '2' if action != 'create' else '1',
                                 'draft_action': action, 'reason': '根据新反馈调整测试任务。' if action != 'create' else '',
                                 'base_task_version': '1' if action != 'create' else '',
                                 'status': 'draft', 'draft_hash': 'ux-task-hash',
                                 'publication_operation_id': None, 'created_at': '2026-10-11T01:00:00Z'}]
        if name in METHOD_SCENARIOS:
            w['proposals'] = [{'id': 'ux-method-draft', 'project_id': 'rdos-self', 'target_id': 'ux-method-source', 'target_kind': 'docx', 'target_name': '界面测试方法资料', 'source_url': 'http://127.0.0.1:8012/__test__/method-source', 'summary': '仅用于界面测试：补充实际工作反馈的来源。', 'old_text': '记录工作结果。', 'new_text': '记录实际工作结果，并保留可核对的来源。', 'base_hash': 'ux-source-before', 'evidence': [{'claim': '浏览器测试材料，不代表真实业务发现。', 'source_url': 'http://127.0.0.1:8012/__test__/method-source'}], 'status': 'draft', 'draft_hash': 'ux-method-hash', 'identity_mode': 'role_test', 'created_at': '2026-10-11T01:00:00Z'}]
            if name == 'template-proposal':
                w['proposals'][0].update(target_kind='template', target_id='project.feedback',
                    target_name='项目反馈说明', source_url='rdos-template:project.feedback',
                    summary='让成员更容易说明工作成果与需要的支持。',
                    old_text='记录工作结果。', new_text='记录工作成果和需要的支持。')
        if name == 'structured-feedback':
            w['feedback'] = [{'id': 'ux-feedback-structured', 'project_id': 'rdos-self',
                              'member_id': user_id, 'identity_mode': 'role_test',
                              'reported_at': '2026-10-12T09:00:00+08:00',
                              'observed_at': '2026-10-13T09:00:00+08:00',
                              'period': '2026-10-05 至 2026-10-11',
                              'source_readback': True,
                              'source_url': 'http://127.0.0.1:8012/__test__/method-source',
                              'content_hash': 'ux-structured-hash',
                              'version_status': 'unlinked',
                              'sections': {'progress': '已完成材料梳理。', 'outputs': '整理了可复用的工作清单。',
                                           'blockers': '需要负责人确认清单适用范围。',
                                           'human_gate': '评审决定尚未记录；仅为成员报告的线索。',
                                           'skill_rag': '参考了既有 RAG，效果尚未验证。',
                                           'local_skill': '暂无本地 Skill 更新。'},
                              'content': '仅用于隔离浏览器审核的六类项目反馈，不代表真实团队工作。'}]
        if name in ('chat-empty', 'empty-workspace'):
            for key in ('messages', 'runs'):
                w[key] = []
            if name == 'empty-workspace':
                for key in ('tasks', 'feedback', 'proposals', 'task_drafts'):
                    w[key] = []
            w['runtime'].update(model_configured=True, feishu_configured=True, paused=False, error='')
        if name in ('switch-race', 'chat-delay'):
            second = add_member_names({'id': 'ux-second', 'name': '交互测试第二项目', 'description': '仅用于浏览器交互测试，不会写入飞书。', 'status': 'active', 'member_ids': [user_id]})
            snap['/projects']['items'].append(second)
            w['projects'].append(second)
            second_workspace = copy.deepcopy(w)
            second_workspace['project'] = second
            for key in ('tasks', 'messages', 'feedback', 'proposals', 'runs', 'task_drafts'):
                second_workspace[key] = []
            snap['/workspace?project_id=ux-second'] = second_workspace
            snap['/onboarding/status?project_id=ux-second'] = copy.deepcopy(snap['/onboarding/status?project_id=rdos-self'])
        if name.startswith('onboard-'):
            o = snap.get('/onboarding/status?project_id=rdos-self', {})
            if name == 'onboard-new':
                o.update(choice=None, stage=0, history=[], summary='', missing=[], error=None, ready_for_work=False)
            elif name == 'onboard-error':
                o.update(choice='refresh', session_id='ux-onboard-check-error', stage=1, completed_steps=[0],
                         stage_status='pending', error='暂时无法解析检查结果，已有记录保留，可重新检查。',
                         checking=False)
            elif name == 'onboard-wait':
                o.update(choice='new', session_id='ux-onboard-wait', stage=1, completed_steps=[0],
                         stage_status='pending', summary='', missing=[], error=None)
            elif name == 'onboard-ready':
                o.update(choice='new', session_id='ux-onboard-ready', stage=3, completed_steps=[0, 1, 3],
                         stage_status='verified', ready_for_work=True, scheduled_verified=False, missing=[], error=None)
            elif name == 'onboard-passed':
                o.update(choice='new', session_id='ux-onboard-passed', stage=1, stage_status='pending',
                         completed_steps=[0], history=[{'stage': 0}], missing=[], error=None)
            elif name in ('onboard-user', 'onboard-agent'):
                actor = 'user' if name == 'onboard-user' else 'agent'
                missing = '请本人确认允许读取及共享的范围。' if actor == 'user' else '请 Agent 补充实际读取、工作结果和读回依据。'
                o.update(choice='new', session_id='ux-onboard-' + actor, stage=1 if actor == 'user' else 3,
                         completed_steps=[0] if actor == 'user' else [0, 1], stage_status='blocked',
                         required_actor=actor, next_actor=actor, ready_for_work=False, scheduled_verified=False,
                         missing=[missing], summary='仅用于界面测试：' + missing, error=None,
                         checking=False, history=[{'stage': 0}])
            w['onboarding'] = copy.deepcopy(o)
        STATES[who] = snap
        return snap


def legacy_state():
    with LOCK:
        if 'legacy' not in STATES:
            contract = {'contract': {'contract_version': '1', 'organization_guidance': '仅用于旧版浏览器交互测试的规则。', 'knowledge': {'selected_rag_required': False}, 'skills': {'shared_skill_policy': 'recommended'}}, 'contract_revision': 1, 'contract_hash': 'ux-contract-hash', 'shared_revision': 1, 'updated_at': '2026-10-11T01:00:00Z'}
            STATES['legacy'] = {
                '/api/admin/overview': {'runners': [], 'projects': [], 'shared_revision': 1, 'selected_rag': [], 'shared_skills': [], 'rag_sync': {'status': 'healthy', 'last_success_at': None}, 'work_queue': {'publish': [], 'judgments': [], 'incidents': []}, 'activities': []},
                '/api/admin/global-contract': contract,
                '/api/admin/global-contract/history': {'items': [copy.deepcopy(contract)]},
                '/api/admin/shared-skills': {'items': []}, '/api/admin/skill-proposals': {'items': []},
                '/api/admin/selected-rag': {'items': [], 'state': {'status': 'healthy', 'active_snapshot_id': None, 'last_success_at': None, 'failure_count': 0, 'ignored_items': []}},
                '/api/admin/projects': {'items': []},
                '/api/admin/workflow-reference': {'catalog': {'development_modes': [{'value': 'new_product', 'label': '新产品'}], 'product_types': [{'value': 'guide', 'label': '指南'}], 'delivery_scales': [{'value': 'small', 'label': '小型交付'}], 'core_nodes': []}},
            }
        return STATES['legacy']


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, value, status=200, mime='application/json', cookie=None):
        raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        if getattr(self, 'write_record', None) is not None:
            self.write_record.update(status=status, result=redact(value) if not isinstance(value, bytes) else 'binary')
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Cache-Control', 'no-store')
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self): self.handle_request()
    def do_POST(self): self.handle_request()
    def do_PATCH(self): self.handle_request()
    def do_DELETE(self): self.handle_request()
    def do_PUT(self): self.handle_request()

    def error(self, message, code='fixture_failure', status=503):
        return self.reply({'detail': {'message': message, 'code': code}}, status)

    def handle_request(self):
        parsed = urlsplit(self.path)
        path, body = parsed.path, {}
        if self.headers.get('Content-Length'):
            try:
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            except (ValueError, TypeError):
                return self.error('交互测试请求格式不正确。', 'fixture_input', 422)
        if path == '/__test__/scenario':
            with LOCK:
                if self.command != 'GET':
                    SCENARIO.clear(); SCENARIO.update(body or {'name': 'baseline'})
                    STATES.clear(); WRITES.clear(); ATTEMPTS.clear()
                return self.reply({'ok': True, 'scenario': copy.deepcopy(SCENARIO), 'fixture_only': True})
        if path == '/__test__/writes':
            with LOCK:
                return self.reply({'items': copy.deepcopy(WRITES), 'count': len(WRITES), 'fixture_only': True})
        if path == '/__test__/session':
            with LOCK:
                if self.command != 'GET':
                    SCENARIO['session_expired'] = bool(body.get('expired', True))
                return self.reply({'expired': session_expired(), 'fixture_only': True, 'recovery': '使用同一fixture账号重新登录即可恢复；也可POST {"expired":false}。此接口保留fixture数据与写入记录，两个端口共享状态。'})
        if path == '/__test__/method-source':
            return self.reply(b'Fixture method source; no real Feishu document.', mime='text/plain; charset=utf-8')
        if path in ('/', '/legacy') and parse_qs(parsed.query).get('scenario'):
            with LOCK:
                SCENARIO.clear(); SCENARIO['name'] = parse_qs(parsed.query)['scenario'][0]
                STATES.clear(); WRITES.clear(); ATTEMPTS.clear()
        if path in ('/', '/legacy') or path.startswith('/static/'):
            filename = ('central.html' if path == '/' else 'index.html') if path in ('/', '/legacy') else pathlib.Path(path).name
            folder = DATA / 'before-static' if self.server.server_port == 8011 else ROOT / 'server/static'
            target = folder / filename
            if not target.is_file():
                return self.reply({'detail': 'Not found'}, 404)
            raw = target.read_bytes()
            if target.suffix == '.html' and SCENARIO.get('name') == 'clipboard-failure':
                raw = raw.replace(b'<head>', b'<head>\n' + CLIPBOARD_FAILURE_SCRIPT, 1) if b'<head>' in raw else CLIPBOARD_FAILURE_SCRIPT + raw
            return self.reply(raw, mime={'html': 'text/html; charset=utf-8', 'js': 'application/javascript', 'css': 'text/css'}.get(target.suffix[1:], 'application/octet-stream'))
        api = path.removeprefix('/api/central')
        who = ''
        cookies = dict(v.strip().split('=', 1) for v in self.headers.get('Cookie', '').split(';') if '=' in v)
        who = cookies.get('ux_account', '')
        if self.command != 'GET':
            with LOCK:
                self.write_record = {'number': len(WRITES) + 1, 'method': self.command, 'path': api, 'account': who, 'body': redact(body), 'received_at': time.time(), 'port': self.server.server_port}
                WRITES.append(self.write_record)
        if path.startswith(('/api/auth/', '/api/admin/')):
            if getattr(self, 'write_record', None) is not None:
                self.write_record['account'] = 'ux-admin' if cookies.get('ux_legacy_admin') else body.get('username', '')
            if path == '/api/auth/login':
                if body.get('username') != 'ux-admin':
                    return self.reply({'detail': '仅限fixture账号 ux-admin，密码任意测试文本。'}, 401)
                SCENARIO['session_expired'] = False
                return self.reply({'admin': {'username': 'ux-admin'}}, cookie='ux_legacy_admin=1; Path=/; SameSite=Lax')
            if not cookies.get('ux_legacy_admin'):
                return self.reply({'detail': '需要旧版fixture管理员登录'}, 401)
            if session_expired():
                return self.reply({'detail': '旧版fixture登录已过期'}, 401)
            if path == '/api/auth/me':
                return self.reply({'admin': {'username': 'ux-admin'}})
            if path in ('/api/auth/logout', '/api/auth/change-password'):
                return self.reply({'ok': True}, cookie='ux_legacy_admin=; Path=/; Max-Age=0')
            old = legacy_state()
            if SCENARIO.get('name') in ('partial-failure', 'first-failure') and path == '/api/admin/overview':
                return self.error('旧版fixture读取失败，已有内容保留。')
            if path == '/api/admin/global-contract' and self.command == 'PUT':
                contract = old[path]
                if body.get('expected_contract_revision') != contract['contract_revision']:
                    return self.error('正式规则版本已变化，草稿保留。', 'stale_contract_revision', 409)
                contract['contract'].update(organization_guidance=body['organization_guidance'])
                contract['contract']['knowledge']['selected_rag_required'] = body['selected_rag_required']
                contract['contract']['skills']['shared_skill_policy'] = body['shared_skill_policy']
                contract.update(contract_revision=contract['contract_revision'] + 1, shared_revision=contract['shared_revision'] + 1)
                old['/api/admin/global-contract/history']['items'].insert(0, copy.deepcopy(contract))
                return self.reply(contract)
            if self.command == 'GET' and path in old:
                return self.reply(old[path])
            return self.error('旧版fixture未提供此写入；没有真实管理员或飞书变更。')
        if api == '/auth/login':
            who = body.get('username', '')
            if who not in SNAP:
                return self.reply({'detail': '测试账号不存在'}, 401)
            SCENARIO['session_expired'] = False
            return self.reply(SNAP[who]['/auth/me'], cookie='ux_account=' + who + '; Path=/; SameSite=Lax')
        if api == '/auth/logout':
            return self.reply({'ok': True}, cookie='ux_account=; Path=/; Max-Age=0')
        if who not in SNAP:
            return self.reply({'detail': '登录已过期，请重新登录。'}, 401)
        snap = state_for(who)
        name = SCENARIO.get('name', 'baseline')
        if session_expired():
            return self.reply({'detail': 'Session expired'}, 401)
        if name in ('partial-failure', 'first-failure') and (api == '/workspace' or name == 'first-failure'):
            return self.error('暂时无法连接服务，请稍后重试。', 'connection_failed')
        key = api + ('?' + parsed.query if parsed.query else '')
        if api == '/onboarding/status' and not parsed.query:
            # Organization onboarding no longer sends a project ID. Keep the
            # frozen fixture usable for responsive role checks.
            if name == 'slow-onboarding':
                time.sleep(1.2)
            return self.reply(snap['/onboarding/status?project_id=rdos-self'])
        if api == '/overview':
            if 'admin' not in snap['/auth/me']['user'].get('roles', []):
                return self.reply({'detail': '当前账号不能查看团队概览。'}, 403)
            workspaces = [(key.rsplit('=', 1)[-1], value) for key, value in snap.items()
                          if key.startswith('/workspace?project_id=')]
            def aggregate(field):
                result, seen = [], set()
                for project_id, value in workspaces:
                    for item in value.get(field, []):
                        identity = item.get('id') or item.get('task_id') or item.get('feedback_id')
                        if identity in seen:
                            continue
                        seen.add(identity)
                        result.append({**copy.deepcopy(item), 'project_id': item.get('project_id') or project_id})
                return result
            primary = workspace(snap)
            return self.reply({'project': None, 'projects': copy.deepcopy(snap['/projects']['items']),
                               'users': copy.deepcopy(primary.get('users', [])),
                               'sources': copy.deepcopy(snap.get('/sources', {'items': []}).get('items', [])),
                               'tasks': aggregate('tasks'), 'feedback': aggregate('feedback'),
                               'task_drafts': aggregate('task_drafts'), 'proposals': aggregate('proposals'),
                               'messages': aggregate('messages'), 'runs': aggregate('runs'),
                               'runtime': copy.deepcopy(primary.get('runtime', {}))})
        if api == '/onboarding/instruction':
            if name == 'switch-race':
                time.sleep(3 if body.get('project_id') == 'rdos-self' else .1)
            attempt_key = ('instruction', who, body.get('purpose', 'onboarding'), body.get('project_id', ''))
            with LOCK:
                ATTEMPTS[attempt_key] = ATTEMPTS.get(attempt_key, 0) + 1
                instruction_attempt = ATTEMPTS[attempt_key]
            data = copy.deepcopy(snap.get('/instruction/' + body.get('purpose', 'onboarding'), {'prompt': '仅用于界面测试：请读取当前步骤说明并完成接入。', 'url': 'http://127.0.0.1:8012/', 'expires_at': '2099-01-01T00:00:00Z'}))
            data['project_id'] = body.get('project_id', 'rdos-self')
            if data['project_id'] == 'ux-second':
                data['prompt'] = '这是第二项目的完整参与指令，仅用于界面测试。'
            if name == 'instruction-expired' and instruction_attempt == 1:
                data['expires_at'] = '2000-01-01T00:00:00Z'
            elif instruction_attempt > 1 or name == 'clipboard-failure':
                data['expires_at'] = '2099-01-01T00:00:00Z'
            return self.reply(data)
        if api == '/onboarding/start':
            o = snap['/onboarding/status?project_id=rdos-self']
            o.update(choice=body.get('path', 'new'), session_id='ux-started-' + body.get('path', 'new'),
                     stage=0, stage_status='pending', completed_steps=[], ready_for_work=False,
                     missing=[], summary='', error=None)
            return self.reply(o)
        if api == '/onboarding/check':
            if name != 'onboard-error':
                return self.reply(snap['/onboarding/status?project_id=rdos-self'])
            return self.reply({'detail': '暂时无法解析检查结果，已有记录保留，可重新检查。'}, 502)
        if api == '/chat' and name == 'chat-delay':
            time.sleep(3)
            return self.reply({'run_id': 'ux-running'})
        if api == '/diagnostics':
            if 'admin' not in snap['/auth/me']['user'].get('roles', []):
                return self.reply({'detail': '当前账号不能执行此操作。'}, 403)
            runtime = workspace(snap).get('runtime', {})
            last_scheduled = runtime.get('last_scheduled') or {}
            return self.reply({'observed_at': '2026-10-13T09:00:00+08:00',
                'scope': '隔离测试中的本机记录；未直接检查研究员电脑。',
                'online': {'onboarding': {'sessions': 3, 'ready_for_work': 1, 'needs_attention': 1},
                    'model': {'configured': True, 'last_success_at': '', 'last_failed_run_at': ''},
                    'tools': {'last_success_at': '', 'last_failure_at': '', 'recent_failures': []},
                    'feishu': {'last_full_read_at': '', 'last_full_read_failed': False,
                               'confirmed_operations': 0, 'unresolved_operations': 1},
                    'tasks': {'known': 2, 'published': 1, 'stopped': 0, 'pending_drafts': 1},
                    'central_schedule': {'last_actual_run_at': last_scheduled.get('at', ''),
                                         'last_result': last_scheduled.get('status', '未验证'),
                                         'next_due': runtime.get('next_due', ''),
                                         'missed_while_offline': bool(runtime.get('missed')),
                                         'paused': bool(runtime.get('paused'))}},
                'local_agents': {'feedback_readback_records': 1, 'last_member_reported_at': '2026-10-12T09:00:00+08:00',
                                 'last_feedback_read_at': '2026-10-13T09:00:00+08:00',
                                 'reported_actual_scheduled_trigger': 0,
                                 'evidence_boundary': '仅统计隔离测试中已提交并读回的反馈。'},
                'limits': ['没有反馈不代表没有工作。'], 'fixture_only': True})
        if api == '/users' and self.command == 'POST' and name == 'initial-password':
            user = {'id': 'ux-fixture-user-' + digest(body.get('username', ''))[:8], 'username': body.get('username', 'ux-fixture-researcher'), 'display_name': body.get('display_name', '隔离测试成员'), 'roles': body.get('roles', ['researcher']), 'identity_mode': 'role_test', 'enabled': True}
            users = workspace(snap)['users']
            previous = next((item for item in users if item['id'] == user['id']), None)
            if previous is None:
                users.append(user)
            else:
                previous.update(user)
            return self.reply({'user': user, 'password': 'UX-fixture-only-NOT-a-real-credential', 'fixture_only': True})
        if api.startswith('/runs/'):
            if api.endswith('ux-running'):
                return self.reply({'id': 'ux-running', 'status': 'running', 'kind': 'chat', 'project_id': 'rdos-self', 'steps': []})
            for run in workspace(snap)['runs']:
                if run['id'] == api.split('/')[-1]:
                    return self.reply(run)
        if api.startswith(('/task-drafts/', '/proposals/')) and self.command == 'POST':
            category, item_id, action = api.strip('/').split('/', 2)
            collection = workspace(snap)['task_drafts' if category == 'task-drafts' else 'proposals']
            item = next((i for i in collection if i['id'] == item_id), None)
            if item is None:
                return self.error('交互测试草稿不存在。', 'fixture_not_found', 404)
            if action == 'edit' and name in TASK_SCENARIOS | METHOD_SCENARIOS:
                item.update(body, status='draft', draft_hash=digest(body))
                return self.reply({'item': item})
            if action == 'reject' and name in TASK_SCENARIOS | METHOD_SCENARIOS:
                item.update(status='rejected', rejection_reason=body.get('reason', ''))
                return self.reply({'item': item})
            if action == 'publish':
                if category == 'proposals' and name in ('method-source-stale', 'source-stale'):
                    return self.error('来源已变化，请重新读取并形成草稿；当前草稿保留。', 'source_changed', 409)
                if name in TASK_SCENARIOS | METHOD_SCENARIOS:
                    if body.get('expected_hash') != item.get('draft_hash'):
                        return self.error('草稿已变化，请重新审阅当前内容。', 'draft_changed', 409)
                    if category == 'task-drafts' and name == 'task-conflict' and not item.get('_ux_conflict_emitted'):
                        item['_ux_conflict_emitted'] = True
                        return self.error('飞书任务已变化，请保留输入并重新审阅。', 'task_version_changed', 409)
                    op = body.get('operation_id', '')
                    attempt_key = (who, category, item_id, op)
                    with LOCK:
                        ATTEMPTS[attempt_key] = ATTEMPTS.get(attempt_key, 0) + 1
                        previous = item.get('publication_operation_id')
                        if previous and previous != op:
                            return self.error('原发布结果仍待核对，请继续原操作。', 'operation_mismatch', 409)
                        item['publication_operation_id'] = op
                        if name in UNCERTAIN and ATTEMPTS[attempt_key] == 1:
                            return self.error('操作结果尚未确认，请先检查已有结果，再继续原操作。', 'write_uncertain', 504)
                        item.update(status='published', published_at='2026-10-11T02:00:00Z', source_url='http://127.0.0.1:8012/__test__/method-source')
                        if category == 'proposals':
                            item.update(method_version=item_id + '@1', adoption_status='尚未验证，界面测试没有真实采用反馈。')
                        elif not any(t.get('task_id') == item.get('task_id') for t in workspace(snap)['tasks']):
                            workspace(snap)['tasks'].append({**copy.deepcopy(item), 'status': '已发布', 'version': '1', 'record_id': 'ux-record-' + item_id})
                        return self.reply({'item': item})
        if (api == '/projects' and self.command == 'POST' or api.startswith('/projects/') and self.command == 'PATCH') and name in PROJECT_SCENARIOS:
            attempt_key = (who, api, 'project-first-content')
            with LOCK:
                content = {key: value for key, value in body.items() if key != 'operation_id'}
                original_content = ATTEMPTS.setdefault(attempt_key, digest(content))
                if digest(content) == original_content:
                    return self.error('交互测试：项目设置尚未完成，请修改内容后再次保存。', 'fixture_failure')
                item_id = api.split('/')[-1] if self.command == 'PATCH' else 'ux-created-' + digest(body.get('operation_id', ''))[:8]
                project = add_member_names({**copy.deepcopy(body), 'id': item_id, 'status': 'active', 'folder_url': body.get('folder_url', ''), 'feedback_url': 'http://127.0.0.1:8012/__test__/method-source'})
                items = snap['/projects']['items']
                prior = next((p for p in items if p['id'] == item_id), None)
                if prior is None:
                    items.append(project)
                else:
                    prior.update(project)
                workspace(snap)['projects'] = copy.deepcopy(items)
                if '/workspace?project_id=' + item_id not in snap:
                    created_workspace = copy.deepcopy(workspace(snap))
                    created_workspace['project'] = copy.deepcopy(project)
                    for field in ('tasks', 'messages', 'feedback', 'proposals', 'runs', 'task_drafts'):
                        created_workspace[field] = []
                    snap['/workspace?project_id=' + item_id] = created_workspace
                    snap['/onboarding/status?project_id=' + item_id] = copy.deepcopy(snap['/onboarding/status?project_id=rdos-self'])
                else:
                    snap['/workspace?project_id=' + item_id]['project'] = copy.deepcopy(project)
                return self.reply({'item': project})
        if self.command != 'GET':
            return self.error('操作结果尚未确认，请先检查已有结果，再继续原操作。' if name == 'write-uncertain' else '交互测试：暂时无法保存，输入已保留，可重试。', 'write_uncertain' if name == 'write-uncertain' else 'fixture_failure', 504 if name == 'write-uncertain' else 503)
        if key in snap:
            return self.reply(snap[key])
        return self.reply({'detail': '交互测试未提供此数据'}, 404)


def main():
    for port in (8011, 8012):
        server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print('UX fixture only: before 8011 / after 8012; no outbound or real writes', flush=True)
    threading.Event().wait()


if __name__ == '__main__':
    main()
