"""Narrow phase-3 Feishu transport. No network is used during configuration.

Only this module constructs CLI commands. The audit hook grants exactly one
Popen for a command, while all Python network and other process access stays
closed. The native child is not an operating-system sandbox.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import tempfile
import time
from datetime import datetime, timezone
from server import rdos_contract

from lab import config

TENANT = "https://scn33386s7ui.feishu.cn"
_FORMAL = os.environ.get('RDOS_RUNTIME_MODE') == 'production'
if _FORMAL:
    from cutover import production_config
HUMAN_FOLDER = production_config.HUMAN_FOLDER if _FORMAL else "CHZvfeJH5l5yGVdZhXIceSg5n6e"
AGENT_FOLDER = production_config.AGENT_FOLDER if _FORMAL else "ZyWCfai9jlvVcMdo3iocwUSAnMe"
# Separate read-only roots; these never become document-write destinations.
KNOWLEDGE_ROOTS = {"selected_rag": "KQjyf6KrxlZ24MdkHirc9TP0n2d",
                   "shared_skills": "ImWqfzWcvl5LWUdFmxKcLIfdntp"}
PROJECT_ID = production_config.DEFAULT_PROJECT if _FORMAL else "lab_project_phase3_collab"
TASK_FIELDS = ("任务ID", "标题", "状态", "执行节点", "要求版本", "记录修订", "操作ID", "记录数据", "当前项目ID")
EVENT_FIELDS = ("事件ID", "类型", "自报节点", "任务引用", "操作ID", "记录数据")
FORMAL_UPGRADE_LEGACY_FIELDS = frozenset({"项目ID", "发布状态", "任务性质", "写回标识",
                                         "适用成员", "版本", "任务类别", "任务说明"})
_TOKEN = object()
_EXECUTORS = set()
_BACKEND = None
_CONFIGURED = False
_ENABLED_REQUESTED = False
_LOCK = threading.RLock()
_META = {"record_id", "source_url", "readback_verified", "source_environment", "remote_actor"}
_REVISION_CONFLICT = re.compile(
    r"(?:revision|version).{0,80}(?:mismatch|conflict|stale|not.match|out.of.date|expired|too.old|inconsistent)"
    r"|(?:mismatch|conflict|stale).{0,80}(?:revision|version)"
    r"|(?:版本|修订).{0,40}(?:冲突|不匹配|过期|不一致|已变更)", re.I)


class FeishuTestError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _fail(code, message):
    raise FeishuTestError(code, message) from None


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256((value if isinstance(value, str) else _json(value)).encode()).hexdigest()


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{4,200}", value):
        _fail("invalid_identifier", "无效的阶段3对象标识")
    return value


def _payload(value):
    return {k: copy.deepcopy(v) for k, v in value.items() if k not in _META and not k.startswith("_")}


def _body(content, title):
    """Remove only our known title, normalize line endings/boundary newlines."""
    text = content.replace("\r\n", "\n").strip("\n")
    wrappers = ("<title>" + html.escape(title, quote=False) + "</title>",
                "<title>" + title + "</title>", "# " + title)
    for prefix in wrappers:
        if text == prefix or text.startswith(prefix + "\n"):
            return text[len(prefix):].strip("\n")
    return text


def creation_matches(document, requested_content):
    """Verify complete actual body while preserving raw full-content evidence."""
    return document.get("body_content", document["content"]) == requested_content.replace("\r\n", "\n").strip("\n")


def _visible(read):
    """Bounded read-only reconciliation for native eventual visibility."""
    value = None
    for delay in (0, 0.4, 0.8, 1.6):
        if delay:
            time.sleep(delay)
        value = read()
        if value:
            return value
    return value


def _data(response):
    value = response.get("data", response)
    if not isinstance(value, dict):
        _fail("invalid_response", "飞书返回格式不符合本轮协议")
    return value


def _knowledge_cli_failure(stdout, stderr):
    """Classify connector failures without exposing connector output or paths."""
    structured = {}
    for text in (stdout, stderr):
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            structured = value
            break
    error = structured.get("error", {})
    if not isinstance(error, dict):
        error = {}
    kind = str(error.get("type", "")).lower()
    # Inspect only for classification; original strings never escape this method.
    message = str(error.get("message") or structured.get("msg") or structured.get("message") or "").lower()
    if kind == "validation":
        return "cli_validation_failed", "飞书CLI拒绝了本次读取参数"
    if kind in {"permission", "authorization", "forbidden"} or any(word in message for word in ("permission denied", "no permission", "not have permission", "missing necessary permission", "forbidden", "insufficient scope", "access denied")):
        return "source_permission_denied", "飞书来源读取权限不足"
    if kind in {"authentication", "unauthenticated"} or any(word in message for word in ("token expired", "unauthenticated", "login required")):
        return "source_authentication_failed", "飞书连接认证已失效"
    return "cli_read_failed", "飞书CLI未成功完成来源读取"


def account_fingerprint(response):
    """Hash only identity identifiers, never return raw account information."""
    value = _data(response)
    if not value.get('userOpenId'):
        identities = value.get('identities')
        user = identities.get('user') if isinstance(identities, dict) else None
        if (not isinstance(user, dict) or user.get('available') is not True
                or user.get('verified') is not True or not isinstance(user.get('openId'), str)
                or not user['openId'] or value.get('identity') != 'user'):
            _fail('account_unverified', '当前CLI未返回已在线核验的用户身份')
        value = {**value, 'userOpenId': user['openId']}
    identity = {k: value.get(k) for k in ("userOpenId", "appId", "brand")}
    if not all(isinstance(v, str) and v for v in identity.values()) or identity["brand"] != "feishu":
        _fail("account_unverified", "当前CLI不是已核实的飞书用户身份")
    # defaultAs=auto is normal for the installed CLI. Every data command below
    # explicitly uses --as user; the selected cached identity must be a user.
    if value.get("identity", "user") != "user":
        _fail("account_unverified", "阶段3必须使用现有飞书user连接")
    return _hash(identity)


def _folders(registry):
    return {HUMAN_FOLDER, AGENT_FOLDER} | {
        _identifier(p['folder_id']) for p in registry.get('projects', []) if p.get('folder_id')}


def _project_ids(registry):
    return {PROJECT_ID} | {p['id'] for p in registry.get('projects', [])}


def _project_folder(registry, project_id):
    if project_id == PROJECT_ID:
        return HUMAN_FOLDER
    item = next((p for p in registry.get('projects', []) if p['id'] == project_id), None)
    if not item:
        _fail('out_of_scope', '项目未登记')
    return _identifier(item['folder_id'])


def _validate_registry(value, cfg, *, ready=True):
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        _fail("invalid_registry", "缺少有效的阶段3资源登记")
    if value.get("env_id") != cfg["env_id"] or value.get("backend") != "feishu_test":
        _fail("invalid_registry", "阶段3资源登记不属于当前实验环境")
    if value.get("project_id") != PROJECT_ID:
        _fail("invalid_registry", "阶段3项目标识与批准设计不一致")
    if value.get("human_folder") != HUMAN_FOLDER or value.get("agent_folder") != AGENT_FOLDER:
        _fail("out_of_scope", "阶段3只能使用两个指定测试根")
    if not re.fullmatch(r"[0-9a-f]{32}", str(value.get("generation", ""))):
        _fail("invalid_registry", "阶段3资源缺少实验代次")
    if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("account_fingerprint", ""))):
        _fail("account_unverified", "阶段3资源没有固定调用账号")
    if ready and value.get("state") != "ready":
        _fail("setup_incomplete", "阶段3建置未完成，拒绝回退到模拟数据")
    for key in ("base_token", "tasks_table", "events_table"):
        if ready or value.get(key):
            _identifier(value.get(key))
    if value.get("base_token") and value["base_token"] in value.get("excluded_object_ids", []):
        _fail("out_of_scope", "旧Base不能登记为阶段3新资源")
    if value.get("tasks_table") and value.get("tasks_table") == value.get("events_table"):
        _fail("invalid_registry", "任务登记与协作记录不能共用同一张表")
    seen = set()
    for document in value.get("documents", []):
        identifier = _identifier(document.get("id"))
        if identifier in seen or document.get("folder_id") not in _folders(value):
            _fail("invalid_registry", "阶段3文档重复或越出指定根")
        if not document.get("operation_id"):
            _fail("invalid_registry", "阶段3文档缺少本轮创建依据")
        if identifier in value.get("excluded_object_ids", []):
            _fail("out_of_scope", "旧草案不能登记为阶段3新资源")
        seen.add(identifier)
    return value


def trusted_executor(executor):
    return type(executor) is _CLIExecutor and executor in _EXECUTORS


class _CLIExecutor:
    def __init__(self, token, cfg, registry, *, setup=False):
        if token is not _TOKEN:
            _fail("executor_denied", "未授权的阶段3执行器")
        self.binary = cfg["phase3"]["cli_path"]
        self.binary_hash = cfg["phase3"]["cli_sha256"]
        self.root = Path(cfg["root"])
        self.runtime = self.root / "runtime"
        self.registry = registry
        self.setup = setup
        user_home = os.environ.get("HOME")
        if not user_home or not Path(user_home).is_absolute():
            _fail("invalid_environment", "阶段3原生CLI需要启动器明确提供现有用户HOME；未读取凭据且未调用CLI")
        runtime_path = '/usr/bin:/bin'
        if _FORMAL:
            from cutover import production_config as formal_config
            runtime_path = str(formal_config.LIVE_NODE.parent) + ':/usr/bin:/bin'
        self.child_env = {"HOME": user_home, "PATH": runtime_path, "LANG": "en_US.UTF-8",
                          "TMPDIR": str(self.runtime / "tmp"), "NO_COLOR": "1"}
        self._pending = threading.local()
        _EXECUTORS.add(self)

    def validate_argv(self, argv):
        # A permit is only available while a typed method is dispatching this
        # exact immutable command; callers cannot use it as a generic CLI API.
        expected = getattr(self._pending, "argv", None)
        if not expected or tuple(argv) != expected or argv[0] != self.binary:
            _fail("command_denied", "CLI命令不是当前类型化操作")
        self._validate_args(argv[1:])

    def _validate_args(self, args):
        """Recheck complete syntax and object scope, including private calls."""
        if args == ["auth", "status"] or (_FORMAL and args == ['auth', 'status', '--json', '--verify']):
            return
        knowledge = getattr(self._pending, "knowledge", None)
        if args[:2] == ["drive", "+download"]:
            expected = getattr(self._pending, "knowledge_download", None)
            if not expected or tuple(args) != expected:
                _fail("command_denied", "知识文件下载不是已登记的一次读取")
            token = args[args.index("--file-token") + 1]
            item = (knowledge or {}).get("items", {}).get(token)
            if not item or item.get("type") != "file" or not item.get("name", "").lower().endswith(".md"):
                _fail("out_of_scope", "只允许下载指定知识目录中的Markdown正文")
            return
        command = tuple(args[:2])
        start = 3 if args[:3] == ["drive", "files", "list"] else 2
        tail = args[start:]
        if len(tail) % 2 or any(not isinstance(v, str) for v in args):
            _fail("command_denied", "CLI参数必须是明确键值对")
        flags = dict(zip(tail[::2], tail[1::2]))
        if len(flags) * 2 != len(tail) or flags.get("--as") != "user":
            _fail("command_denied", "CLI参数重复或身份无效")
        def exact(keys):
            if set(flags) != set(keys):
                _fail("command_denied", "CLI包含未允许的参数")
        if start == 3:
            exact(("--as", "--format", "--params"))
            params = json.loads(flags["--params"])
            if flags["--format"] != "json" or set(params) - {"folder_token", "page_size", "page_token"} or params.get("folder_token") not in (_folders(self.registry) | set((knowledge or {}).get("folders", ()))) or params.get("page_size") != 200:
                _fail("command_denied", "文件枚举越出两个测试根")
            return
        if command[0] == "docs":
            if flags.get("--api-version") != "v2" or flags.get("--doc-format") != "markdown":
                _fail("command_denied", "只允许明确版本的原生Markdown协议")
            if command[1] == "+fetch":
                exact(("--api-version", "--as", "--doc", "--format", "--doc-format", "--detail", "--scope"))
                if flags["--format"] != "json" or flags["--detail"] != "simple" or flags["--scope"] != "full" or flags["--doc"] not in ({d["id"] for d in self.registry["documents"]} | {token for token, item in (knowledge or {}).get("items", {}).items() if item.get("type") == "docx"}):
                    _fail("out_of_scope", "只允许完整读取本轮新文档")
                return
            if command[1] == "+create":
                exact(("--api-version", "--as", "--parent-token", "--doc-format", "--content"))
                if flags["--parent-token"] not in _folders(self.registry) or flags["--content"] != "-":
                    _fail("out_of_scope", "文档创建必须使用指定根与stdin正文")
                return
            if command[1] == "+update":
                exact(("--api-version", "--as", "--doc", "--doc-format", "--revision-id", "--command", "--pattern", "--content"))
                probe = self.registry.get("cas_probe_document")
                allowed_doc = flags["--doc"] in {d["id"] for d in self.registry["documents"]}
                if not allowed_doc or flags["--command"] != "str_replace" or flags["--content"] != "-" or not flags["--revision-id"].isdigit() or (not self.registry.get("cas_verified") and not (self.setup and flags["--doc"] == probe)):
                    _fail("cas_unverified", "文档修改未获并发保证或不属于专用验证文档")
                return
        if command[0] == "base":
            method = command[1]
            if method == "+base-create":
                exact(("--as", "--folder-token", "--name", "--time-zone"))
                if not self.setup or self.registry.get("base_token") or flags["--folder-token"] != AGENT_FOLDER or flags["--time-zone"] != "Asia/Shanghai" or not flags["--name"].startswith("RDOS阶段3协作登记 [P3-"):
                    _fail("setup_denied", "只能建置一个指定新Base")
                return
            if flags.get("--base-token") != self.registry.get("base_token") or not self.registry.get("base_token"):
                _fail("out_of_scope", "Base未登记为本轮新对象")
            if method in ("+table-list", "+table-create"):
                if not self.setup:
                    _fail("setup_denied", "表建置只允许独立setup阶段")
                if method == "+table-list":
                    exact(("--as", "--base-token", "--limit", "--offset"))
                    if flags["--limit"] != "100" or not flags["--offset"].isdigit():
                        _fail("command_denied", "表分页参数无效")
                else:
                    exact(("--as", "--base-token", "--name", "--fields"))
                    allowed_names = ("协作记录",) if self.registry.get('env_id') == 'rdos_formal' else ("阶段3任务登记", "阶段3协作记录")
                    if flags["--name"] not in allowed_names:
                        _fail("setup_denied", "只能创建两张指定表")
                    primary = '任务ID' if flags['--name'] == '阶段3任务登记' else '事件ID'
                    if json.loads(flags['--fields']) != [{'name': primary, 'type': 'text'}]:
                        _fail('setup_denied', '建表只能指定固定的文本主字段')
                return
            self._table(flags.get("--table-id"))
            common = ("--as", "--base-token", "--table-id")
            if method in ("+record-list", "+field-list"):
                exact((*common, "--limit", "--offset", *(("--format",) if method == "+record-list" else ())))
                if not flags["--offset"].isdigit() or flags["--limit"] != ("200" if method == "+record-list" else "100") or (method == "+record-list" and flags["--format"] != "json"):
                    _fail("command_denied", "记录分页参数无效")
                if method == "+field-list" and not self.setup:
                    _fail("setup_denied", "字段枚举仅用于setup")
                return
            if method == "+record-get":
                exact((*common, "--record-id", "--format"))
                _identifier(flags["--record-id"])
                if flags["--format"] != "json":
                    _fail("command_denied", "记录必须以JSON读回")
                return
            if method in ("+record-upsert", "+field-create"):
                exact((*common, "--json", *(("--record-id",) if "--record-id" in flags else ())))
                if "--record-id" in flags:
                    _identifier(flags["--record-id"])
                fields = json.loads(flags["--json"])
                allowed = TASK_FIELDS if flags["--table-id"] == self.registry["tasks_table"] else EVENT_FIELDS
                if method == "+field-create":
                    if not self.setup or set(fields) != {"name", "type"} or fields["name"] not in allowed or fields["type"] != "text":
                        _fail("setup_denied", "只能建置指定文本字段")
                elif (not isinstance(fields, dict)
                      or set(fields) - (set(allowed) | (FORMAL_UPGRADE_LEGACY_FIELDS
                                                        if _FORMAL and flags["--table-id"] == self.registry["tasks_table"] else set()))
                      or "记录数据" not in fields):
                    _fail("invalid_fields", "只允许指定任务与事件字段")
                elif _FORMAL and method == "+record-upsert" and flags["--table-id"] == self.registry["tasks_table"]:
                    old = set(fields) & FORMAL_UPGRADE_LEGACY_FIELDS
                    if old:
                        from cutover.upgrade_tasks import legacy_columns
                        try:
                            wire = json.loads(fields["记录数据"])
                            task = rdos_contract.decode(wire, self.registry)
                            projected = legacy_columns(task, wire.get('operation_id', ''))
                        except (ValueError, KeyError, TypeError):
                            _fail("invalid_fields", "旧读者字段缺少有效升级任务依据")
                        if not projected or old != set(projected) or any(fields[k] != v for k, v in projected.items()):
                            _fail("invalid_fields", "旧读者字段与授权升级任务不一致")
                return
        _fail("command_denied", "此CLI命令没有阶段3类型化出口")

    def _audit(self, category, target, state):
        path = self.runtime / "phase3_egress.jsonl"
        record = {"at": datetime.now(timezone.utc).isoformat(), "category": category,
                  "object_hash": _hash(target), "state": state}
        with path.open("a", encoding="utf-8") as output:
            output.write(_json(record) + "\n")

    def _run(self, args, *, content=None, mutation=False, accept_revision_rejection=False, download=False):
        self._validate_args(args)
        from server.maintenance_limits import cli_timeout
        bounded_timeout = cli_timeout(40)
        argv = [self.binary, *args]
        if Path(self.binary).is_symlink():
            _fail("binary_changed", "阶段3CLI文件无效")
        if hashlib.sha256(Path(self.binary).read_bytes()).hexdigest() != self.binary_hash:
            _fail("binary_changed", "阶段3CLI校验值已变化")
        self._pending.argv = tuple(argv)
        self._audit("/".join(args[:2]), args, "attempt")
        try:
            from lab import guard
            if _FORMAL:
                if self.setup:
                    # The one-shot migration must run before a ready manifest
                    # exists, so the service capability cannot be active yet.
                    # It is confined to the original service user, CLI and
                    # database parent; typed commands are rechecked above.
                    import pwd
                    if (self.registry.get('env_id') != 'rdos_formal'
                            or self.binary != str(production_config.LIVE_CLI)
                            or self.root != production_config.LIVE_DATABASE.parent
                            or os.geteuid() != pwd.getpwnam('baite-rdos').pw_uid
                            or os.environ.get('HOME') != str(self.root)):
                        _fail('setup_denied', '正式迁移未使用既有服务身份与固定资源')
                else:
                    from server.production_runtime import require
                    require()
                result = subprocess.run(argv, input=content, capture_output=True, text=True, encoding="utf-8",
                                        cwd=str(self.runtime), env=dict(self.child_env), timeout=bounded_timeout)
            elif self.setup:
                if guard._active:
                    _fail("setup_denied", "建置工具不能在已运行Panel中使用")
                result = subprocess.run(argv, input=content, capture_output=True, text=True, encoding="utf-8",
                                        cwd=str(self.runtime), env=dict(self.child_env), timeout=bounded_timeout)
            else:
                with guard.cli_permit(self, argv, str(self.runtime), dict(self.child_env)):
                    result = subprocess.run(argv, input=content, capture_output=True, text=True, encoding="utf-8",
                                            cwd=str(self.runtime), env=dict(self.child_env), timeout=bounded_timeout)
            if len(result.stdout.encode()) > 8_000_000 or len(result.stderr.encode()) > 256_000:
                _fail("write_uncertain" if mutation else "read_failed", "CLI输出超过本轮协议限制")
            if result.returncode and accept_revision_rejection:
                # Only a structured server/CLI version-conflict response counts
                # as a proven rejection. Rate limits, timeouts and plain errors
                # remain uncertain and cannot enable ordinary document patches.
                combined = result.stdout + "\n" + result.stderr
                if _REVISION_CONFLICT.search(combined):
                    return {"_revision_rejected": True, "_response_hash": _hash(combined)}
            if result.returncode:
                self._audit("/".join(args[:2]), args, "uncertain" if mutation else "failed")
                if not mutation and getattr(self._pending, "knowledge", None) is not None:
                    code, message = _knowledge_cli_failure(result.stdout, result.stderr)
                    _fail(code, message)
                _fail("write_uncertain" if mutation else "read_failed", "CLI调用未成功；原始输出不回显，请核对持久操作状态")
            if download:
                if args[:2] != ["drive", "+download"]:
                    _fail("command_denied", "下载响应只适用于知识文件读取")
                try:
                    download_response = json.loads(result.stdout)
                except (ValueError, TypeError):
                    download_response = None
                if isinstance(download_response, dict) and (download_response.get("ok") is False or download_response.get("code", 0) not in (0, None)):
                    code, message = _knowledge_cli_failure(result.stdout, result.stderr)
                    _fail(code, message)
                self._audit("knowledge/download", args, "response")
                return {"downloaded": True}
            try:
                value = json.loads(result.stdout)
            except (ValueError, TypeError):
                _fail("write_uncertain" if mutation else "read_failed", "CLI未返回可验证JSON")
            if isinstance(value, dict) and accept_revision_rejection and value.get("code", 0) not in (0, None):
                message = _json(value)
                if _REVISION_CONFLICT.search(message):
                    return {"_revision_rejected": True, "_response_hash": _hash(message)}
            if not isinstance(value, dict) or value.get("code", 0) not in (0, None) or value.get("ok") is False:
                if not mutation and getattr(self._pending, "knowledge", None) is not None:
                    code, message = _knowledge_cli_failure(result.stdout, result.stderr)
                    _fail(code, message)
                _fail("write_uncertain" if mutation else "read_failed", "飞书未确认本轮操作结果")
            self._audit("/".join(args[:2]), args, "response")
            return value
        except (subprocess.TimeoutExpired, OSError):
            self._audit("/".join(args[:2]), args, "uncertain" if mutation else "failed")
            _fail("write_uncertain" if mutation else "read_failed", "CLI调用超时或不可用；写操作只能核对，不能盲目重试")
        finally:
            self._pending.argv = None

    def identity(self):
        fingerprint = account_fingerprint(self._run(['auth', 'status', '--json', '--verify'] if _FORMAL else ["auth", "status"]))
        if fingerprint != self.registry["account_fingerprint"]:
            _fail("account_changed", "CLI调用账号与阶段3固定账号不同")
        return {"provider": "feishu_user", "account_fingerprint": fingerprint,
                "identity_mode": "actual" if _FORMAL else "role_test", "client_identity": "unverified_client"}

    def list_root(self, folder):
        if folder not in _folders(self.registry):
            _fail("out_of_scope", "只允许枚举指定测试根")
        output, page = [], ""
        for _ in range(100):
            params = {"folder_token": folder, "page_size": 200}
            if page:
                params["page_token"] = page
            data = _data(self._run(["drive", "files", "list", "--as", "user", "--format", "json", "--params", _json(params)]))
            for item in data.get("files", []):
                if item.get("parent_token", folder) != folder:
                    _fail("out_of_scope", "文件夹返回了根外对象")
                output.append(item)
            if not data.get("has_more"):
                return output
            next_page = data.get("next_page_token") or data.get("page_token")
            if not next_page or next_page == page:
                _fail("pagination_failed", "根目录分页不完整")
            page = next_page
        _fail("pagination_failed", "根目录分页超过本轮安全限制")

    def read_knowledge(self, category):
        return self._read_knowledge(category)

    def list_skill_folders(self):
        """Complete first-level discovery; files at the root are not Skills."""
        return self._read_knowledge('shared_skills', list_only=True)

    def read_skill_folder(self, folder_token):
        """Read exactly one complete Skill, after proving current root membership."""
        return self._read_knowledge('shared_skills', folder_token=_identifier(folder_token))

    def _knowledge_cached_source(self, category, token, marker):
        """Read an existing bounded cache; isolated adapters need no web imports."""
        if not marker or not os.environ.get('BAITE_DB_PATH'):
            return None
        import sqlite3
        path = Path(os.environ['BAITE_DB_PATH'])
        runtime = Path(os.environ.get('RDOS_LAB_RUNTIME', ''))
        if not path.is_absolute() or path.is_symlink() or path.resolve().parent != runtime.resolve() or not path.is_file():
            return None
        digest = lambda value: hashlib.sha256((value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
        key = 'knowledge_' + digest([category, token])[:24]
        kind = 'central_project_source' if category.startswith('project:') else 'knowledge_item'
        content_kind = 'central_project_content' if category.startswith('project:') else 'knowledge_content'
        try:
            connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
            try:
                row = connection.execute('SELECT payload FROM central_records WHERE kind=? AND id=?', (kind, key)).fetchone()
                item = json.loads(row[0]) if row else None
                if not item or not item.get('available') or item.get('source_marker') != marker or item.get('content_status') != 'full_text':
                    return None
                row = connection.execute('SELECT payload FROM central_records WHERE kind=? AND id=?', (content_kind, digest([key, item['version']]))).fetchone()
                record = json.loads(row[0]) if row else None
                if not record or not isinstance(record.get('content'), str):
                    return None
                content_digest = hashlib.sha256(record['content'].encode()).hexdigest()
                if not str(item.get('version', '')).endswith(content_digest) or item.get('content_hash', content_digest) != content_digest:
                    return None
                return {'content': record['content'], 'source_revision': item.get('source_revision', '')}
            finally:
                connection.close()
        except (sqlite3.Error, ValueError, KeyError):
            # A missing cache never weakens remote scope checks or proves no change.
            return None

    def read_project_files(self, project_id):
        return self._read_knowledge("project:" + project_id, project_id=project_id)

    def _read_knowledge(self, category, *, list_only=False, folder_token=None, project_id=None):
        """Enumerate only descendants proven by the fixed knowledge root listing.

        Discovery is transient and thread-local. It grants reads, never adds to
        the task document registry or changes the existing write allowlist.
        """
        if project_id is not None:
            if project_id not in _project_ids(self.registry):
                _fail("out_of_scope", "项目工作目录未登记")
            root_token = _project_folder(self.registry, project_id)
        elif category in KNOWLEDGE_ROOTS:
            root_token = KNOWLEDGE_ROOTS[category]
        else:
            _fail("out_of_scope", "知识来源未登记")
        if getattr(self._pending, "knowledge", None) is not None:
            _fail("command_denied", "知识读取不能嵌套")
        self.identity()
        scan = {"folders": {root_token}, "items": {}}
        self._pending.knowledge = scan
        output, seen, count = [], set(), 0
        try:
            def walk(folder, parent_segments, depth):
                nonlocal count
                if depth > 20 or len(seen) >= 500 or folder in seen:
                    _fail("incomplete_knowledge", "知识目录过深、循环或超过本轮读取范围")
                seen.add(folder)
                scan["reading"] = scan["items"].get(folder, {"token": folder, "name": category, "path": "", "type": "folder"})
                page, page_seen = "", set()
                children = []
                for _ in range(100):
                    params = {"folder_token": folder, "page_size": 200}
                    if page:
                        params["page_token"] = page
                    data = _data(self._run(["drive", "files", "list", "--as", "user", "--format", "json", "--params", _json(params)]))
                    files = data.get("files")
                    if not isinstance(files, list):
                        _fail("incomplete_source_listing", "知识目录没有返回完整文件清单")
                    for raw in files:
                        if not isinstance(raw, dict) or raw.get("parent_token", folder) != folder:
                            _fail("out_of_scope", "知识目录返回了范围外对象")
                        token = _identifier(raw.get("token"))
                        if token in scan["items"]:
                            _fail("duplicate_source_object", "知识目录出现重复或循环对象")
                        name, kind = raw.get("name"), raw.get("type")
                        if not isinstance(name, str) or not name or not isinstance(kind, str) or not kind:
                            _fail("invalid_source_metadata", "知识来源的文件名称或类型信息不完整")
                        count += 1
                        if count > 5000:
                            _fail("incomplete_knowledge", "知识目录超过本轮读取数量")
                        item = {"token": token, "name": name, "type": kind,
                                "parent_token": folder,
                                # Display names are not filesystem paths. Preserve
                                # exact components even when Feishu titles use /.
                                "path_segments": [*parent_segments, name],
                                "path": "/".join(part.replace("/", "／") for part in [*parent_segments, name]),
                                "modified_time": str(raw.get("modified_time") or raw.get("edited_time") or ""),
                                "source_marker": ("revision:" + str(raw["revision_id"]) if raw.get("revision_id") is not None
                                    else "modified:" + str(raw.get("modified_time") or raw.get("edited_time"))
                                    if raw.get("modified_time") or raw.get("edited_time") else "")}
                        scan["items"][token] = item
                        children.append(item)
                    if not data.get("has_more"):
                        break
                    next_page = data.get("next_page_token") or data.get("page_token")
                    if not isinstance(next_page, str) or not next_page or next_page in page_seen:
                        _fail("pagination_failed", "知识目录分页未完成")
                    page_seen.add(next_page)
                    page = next_page
                else:
                    _fail("pagination_failed", "知识目录分页超过限制")
                if depth == 0 and list_only:
                    # Do not traverse or fetch any body while discovering the
                    # complete list of top-level Skill units.
                    output.extend({**item, "content_status": "folder",
                                   "source_url": TENANT + "/drive/folder/" + item["token"]}
                                  for item in children if item["type"] == "folder")
                    return
                if depth == 0 and folder_token is not None:
                    selected = next((item for item in children
                                     if item["type"] == "folder" and item["token"] == folder_token), None)
                    if selected is None:
                        _fail("out_of_scope", "Skill必须是共享目录下当前登记的一级文件夹")
                    # Membership was proven by complete root pagination. Drop
                    # all sibling objects from this one read's transient scope.
                    children = [selected]
                    scan["items"] = {folder_token: selected}
                for item in children:
                    value = dict(item)
                    kind, token, name = item["type"], item["token"], item["name"]
                    scan["reading"] = item
                    value["source_url"] = TENANT + ("/drive/folder/" if kind == "folder" else "/docx/" if kind == "docx" else "/file/") + token
                    value["content_status"] = "folder" if kind == "folder" else "attachment_only"
                    if kind == "folder":
                        scan["folders"].add(token)
                        output.append(value)
                        walk(token, item["path_segments"], depth + 1)
                    elif kind == "docx":
                        cached = self._knowledge_cached_source(category, token, value.get("source_marker", ""))
                        if cached is not None:
                            value.update(content=cached["content"], source_revision=cached["source_revision"], content_status="full_text", cache_reused=True)
                            output.append(value)
                            continue
                        data = _data(self._run(["docs", "+fetch", "--api-version", "v2", "--as", "user", "--doc", token,
                            "--format", "json", "--doc-format", "markdown", "--detail", "simple", "--scope", "full"]))
                        document = data.get("document", data)
                        content = document.get("content", document.get("markdown"))
                        revision = document.get("revision_id", document.get("revision"))
                        if not isinstance(content, str) or revision is None or len(content.encode()) > 8_000_000:
                            _fail("incomplete_source_document", "知识文档未返回完整正文和版本")
                        value.update(content=content, source_revision=str(revision), content_status="full_text")
                        output.append(value)
                    elif kind == "file" and name.lower().endswith(".md"):
                        cached = self._knowledge_cached_source(category, token, value.get("source_marker", ""))
                        if cached is not None:
                            value.update(content=cached["content"], source_revision=cached["source_revision"], content_status="full_text", cache_reused=True)
                            output.append(value)
                            continue
                        # CPython rmtree first unlinks relative to a directory fd;
                        # this app's audit guard deliberately rejects relative
                        # mutations. Own one fixed file and clean absolute paths
                        # directly, including after a failed/partial download.
                        temporary_root = self.runtime / "tmp"
                        if temporary_root.is_symlink() or temporary_root.resolve().parent != self.runtime.resolve():
                            _fail("out_of_scope", "知识临时目录不属于当前运行目录")
                        temporary = Path(tempfile.mkdtemp(prefix="knowledge-", dir=str(temporary_root))).resolve()
                        if temporary.parent != temporary_root.resolve() or not temporary.name.startswith("knowledge-"):
                            _fail("out_of_scope", "知识临时目录越界")
                        destination = temporary / "content.md"
                        args = ["drive", "+download", "--as", "user", "--file-token", token,
                                "--output", str(destination.relative_to(self.runtime)), "--overwrite"]
                        self._pending.knowledge_download = tuple(args)
                        try:
                            self._run(args, download=True)
                            if destination.is_symlink() or not destination.is_file() or destination.stat().st_size > 8_000_000:
                                _fail("incomplete_source_download", "知识文件没有完整下载")
                            value.update(content=destination.read_text(encoding="utf-8-sig"), content_status="full_text")
                        except UnicodeError:
                            _fail("source_encoding_invalid", "知识Markdown文件不是UTF-8正文")
                        finally:
                            self._pending.knowledge_download = None
                            # Never follow/delete a source-named path or recurse.
                            if destination.is_symlink() or destination.resolve().parent != temporary:
                                _fail("out_of_scope", "知识临时下载路径异常，已停止清理")
                            destination.unlink(missing_ok=True)
                            temporary.rmdir()
                        output.append(value)
                    else:
                        if kind == "file" and name.lower().endswith((".py", ".yaml", ".yml", ".json", ".sh")):
                            value["content_status"] = "file_listing"
                        output.append(value)
            walk(root_token, [], 0)
            return output
        except FeishuTestError as exc:
            current = scan.get("reading", {})
            token, kind = current.get("token", root_token), current.get("type", "folder")
            exc.knowledge_source = {"category": category, "name": current.get("name", category),
                "path": current.get("path", ""),
                "source_url": TENANT + ("/drive/folder/" if kind == "folder" else "/docx/" if kind == "docx" else "/file/") + token}
            raise
        finally:
            self._pending.knowledge = None
            self._pending.knowledge_download = None

    def _table(self, table):
        if table not in {self.registry.get("tasks_table"), self.registry.get("events_table")} or not table:
            _fail("out_of_scope", "只允许本轮登记的两张表")
        return ["--as", "user", "--base-token", _identifier(self.registry["base_token"]), "--table-id", _identifier(table)]

    @staticmethod
    def _rows(data):
        if not isinstance(data, dict):
            _fail("invalid_response", "记录读取缺少有效结构，未视为空表")
        collections = [data[key] for key in ("items", "records") if key in data]
        if collections:
            if any(not isinstance(items, list) or any(not isinstance(row, dict) for row in items)
                   for items in collections):
                _fail("invalid_response", "记录列表格式无效")
            if len(collections) > 1 and collections[0] != collections[1]:
                _fail("invalid_response", "记录读取包含不一致的列表")
            return collections[0]
        if not all(key in data for key in ("fields", "record_id_list", "data")):
            _fail("invalid_response", "记录读取结构不完整，未视为空表")
        names, ids, rows = data["fields"], data["record_id_list"], data["data"]
        if any(not isinstance(value, list) for value in (names, ids, rows)):
            _fail("invalid_response", "记录字段、标识与正文须为列表")
        names = [f.get("name", f.get("field_name", f.get("id"))) if isinstance(f, dict) else f for f in names]
        if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
            _fail("invalid_response", "记录字段名称无效或重复")
        if any(not isinstance(identifier, str) or not identifier for identifier in ids):
            _fail("invalid_response", "记录标识格式无效")
        if len(ids) != len(rows):
            _fail("invalid_response", "记录ID与正文数量不一致")
        if any(not isinstance(cells, list) or len(cells) != len(names) for cells in rows):
            _fail("invalid_response", "记录正文与字段数量不一致")
        return [{"record_id": identifier, "fields": dict(zip(names, cells))} for identifier, cells in zip(ids, rows)]

    def records(self, table):
        output, offset = [], 0
        for _ in range(100):
            data = _data(self._run(["base", "+record-list", *self._table(table), "--format", "json", "--limit", "200", "--offset", str(offset)]))
            rows = self._rows(data)
            output.extend(rows)
            if not data.get("has_more", len(rows) == 200):
                return output
            if not rows:
                _fail("pagination_failed", "记录分页未前进")
            offset += len(rows)
        _fail("pagination_failed", "记录分页超过本轮安全限制")

    def get_record(self, table, identifier):
        data = _data(self._run(["base", "+record-get", *self._table(table), "--record-id", _identifier(identifier), "--format", "json"]))
        if "record" in data:
            record = data["record"]
        elif "record_id" in data and "fields" in data:
            record = data
        else:
            rows = self._rows(data)
            if len(rows) != 1:
                _fail("readback_mismatch", "无法独立读回指定记录")
            record = rows[0]
        if not isinstance(record, dict) or not isinstance(record.get("fields"), dict):
            _fail("invalid_response", "指定记录的正文结构无效")
        if record.get("record_id") != identifier:
            _fail("readback_mismatch", "无法独立读回指定记录")
        return record

    def upsert(self, table, fields, identifier=None):
        allowed = TASK_FIELDS if table == self.registry["tasks_table"] else EVENT_FIELDS
        if _FORMAL and table == self.registry["tasks_table"]:
            allowed = (*allowed, *FORMAL_UPGRADE_LEGACY_FIELDS)
        if set(fields) - set(allowed) or "记录数据" not in fields:
            _fail("invalid_fields", "只允许阶段3指定记录字段")
        args = ["base", "+record-upsert", *self._table(table)]
        if identifier:
            args.extend(["--record-id", _identifier(identifier)])
        return self._run([*args, "--json", _json(fields)], mutation=True)

    def fetch_document(self, identifier):
        if not any(d["id"] == identifier for d in self.registry["documents"]):
            _fail("out_of_scope", "文档未登记为本轮新资源")
        return self._run(["docs", "+fetch", "--api-version", "v2", "--as", "user", "--doc", _identifier(identifier),
                          "--format", "json", "--doc-format", "markdown", "--detail", "simple", "--scope", "full"])

    def create_document(self, folder, title, content):
        if folder not in _folders(self.registry):
            _fail("out_of_scope", "文档只能建在两个指定测试根")
        body = "<title>" + html.escape(title, quote=False) + "</title>\n\n" + content
        return self._run(["docs", "+create", "--api-version", "v2", "--as", "user", "--parent-token", folder,
                          "--doc-format", "markdown", "--content", "-"], content=body, mutation=True)

    def update_document(self, identifier, base_revision, old_text, new_text, *, probe=False):
        return self._run(["docs", "+update", "--api-version", "v2", "--as", "user", "--doc", identifier,
                          "--doc-format", "markdown", "--revision-id", str(base_revision), "--command", "str_replace",
                          "--pattern", re.escape(old_text), "--content", "-"],
                         content=new_text, mutation=True, accept_revision_rejection=probe)

    def create_base(self, name):
        if not self.setup or self.registry.get("base_token"):
            _fail("setup_denied", "仅建置阶段允许创建一个Base")
        return self._run(["base", "+base-create", "--as", "user", "--folder-token", AGENT_FOLDER,
                          "--name", name, "--time-zone", "Asia/Shanghai"], mutation=True)

    def tables(self):
        if not self.setup or not self.registry.get("base_token"):
            _fail("setup_denied", "表枚举只适用于本轮新Base建置")
        output, offset = [], 0
        for _ in range(100):
            data = _data(self._run(["base", "+table-list", "--as", "user", "--base-token", self.registry["base_token"],
                                   "--limit", "100", "--offset", str(offset)]))
            rows = data.get("items", data.get("tables", []))
            output.extend(rows)
            if not data.get("has_more", len(rows) == 100):
                return output
            if not rows:
                _fail("pagination_failed", "表分页未前进")
            offset += len(rows)
        _fail("pagination_failed", "表分页超过限制")

    def create_table(self, name):
        allowed_names = ("协作记录",) if self.registry.get('env_id') == 'rdos_formal' else ("阶段3任务登记", "阶段3协作记录")
        if not self.setup or name not in allowed_names:
            _fail("setup_denied", "仅允许本轮两张指定表")
        primary = '任务ID' if name == '阶段3任务登记' else '事件ID'
        return self._run(["base", "+table-create", "--as", "user", "--base-token", self.registry["base_token"],
                          "--name", name, '--fields', _json([{'name': primary, 'type': 'text'}])], mutation=True)

    def fields(self, table):
        return _data(self._run(["base", "+field-list", *self._table(table), "--limit", "100", "--offset", "0"]))

    def create_field(self, table, name):
        allowed = TASK_FIELDS if table == self.registry["tasks_table"] else EVENT_FIELDS
        if not self.setup or name not in allowed:
            _fail("setup_denied", "仅建置阶段允许指定文本字段")
        return self._run(["base", "+field-create", *self._table(table), "--json", _json({"name": name, "type": "text"})], mutation=True)


class FeishuBackend:
    def __init__(self, cfg, resources, executor):
        self.cfg, self.resources, self.executor = copy.deepcopy(cfg), resources, executor
        self.path = Path(cfg["phase3"]["registry_path"])
        self.operations_path = Path(cfg["root"]) / "runtime/phase3_operations.json"
        self._candidates = {}

    def registry(self):
        value = copy.deepcopy(self.resources)
        value.update(mode="feishu_production" if _FORMAL else "feishu_test",
                     identity_mode="actual" if _FORMAL else "role_test",
                     permission_boundary="节点仍共用飞书调用账号；节点自报不构成独立账号证明" if _FORMAL else "两个客户端共用现有user账号；未证明成员级隔离",
                     zones={"project": {"folder_id": HUMAN_FOLDER, "url": TENANT + "/drive/folder/" + HUMAN_FOLDER, "source_url": TENANT + "/drive/folder/" + HUMAN_FOLDER},
                            "agent": {"folder_id": AGENT_FOLDER, "url": TENANT + "/drive/folder/" + AGENT_FOLDER, "source_url": TENANT + "/drive/folder/" + AGENT_FOLDER}},
                     task_table_url=TENANT + "/base/" + value["base_token"] + "?table=" + value["tasks_table"],
                     event_table_url=TENANT + "/base/" + value["base_token"] + "?table=" + value["events_table"])
        value["documents"] = [{**d, "object_id": d["id"], "source_url": TENANT + "/docx/" + d["id"]} for d in value["documents"]]
        return value

    def register_projects(self, projects, *, persist=True):
        """Persist only administrator-registered project roots; shared Agent stays fixed."""
        values = []
        for project in projects:
            if project.get('merged_into'):
                continue
            values.append({'id': _identifier(project['id']),
                'public_id': rdos_contract.public_view(project['id']),
                'name': project['name'], 'status': project['status'],
                'node_ids': list(project.get('node_ids', [])),
                'folder_id': _identifier(project.get('resolved_folder_id') or project['folder_id'])})
        with _LOCK:
            self.resources['projects'] = values
            if persist:
                config.write_json(self.path, self.resources)

    def read_project_files(self, project_id):
        return self.executor.read_project_files(project_id)

    def read_knowledge(self, category):
        """Read-only knowledge snapshot, independent of task output registration."""
        return self.executor.read_knowledge(category)

    def list_skill_folders(self):
        return self.executor.list_skill_folders()

    def read_skill_folder(self, folder_token):
        return self.executor.read_skill_folder(folder_token)

    def external_document_title(self, task_id, version, node_id, title, opid):
        node_valid=(bool(re.fullmatch(r"[A-Za-z0-9_-]{4,200}",node_id))
                    if self.resources.get('public_protocol_version') == 2 else
                    node_id in ("node_lab_phase3_codex", "node_lab_phase3_workbuddy"))
        if not re.fullmatch(r"task_p3_[0-9a-f]{20}", task_id) or type(version) is not int or version < 1 or not node_valid:
            _fail("invalid_document", "客户端产物需要本轮任务、要求版本和节点")
        if self.resources.get('public_protocol_version') == 2:
            marker = '[RDOS-' + _hash([self.resources['workspace_id'], opid])[:20] + ']'
            return f'{title} · v{version} {marker}'
        marker = "[P3-" + _hash([self.resources["env_id"], opid])[:20] + "]"
        return f"RDOS3 role_test {PROJECT_ID} {task_id} v{version} {node_id} {marker} {title}"

    def refresh_registry(self):
        """Discover metadata only; new bodies require a valid task event."""
        with _LOCK:
            self.executor.identity()
            known = {d["id"] for d in self.resources["documents"]}
            excluded = set(self.resources.get("excluded_object_ids", []))
            candidates = {}
            for folder in sorted(_folders(self.resources)):
                for item in self.executor.list_root(folder):
                    identifier = item.get("token")
                    name = item.get("name", "")
                    if identifier in excluded or identifier in known or item.get("type") != "docx":
                        continue
                    if self.resources.get('public_protocol_version') == 2:
                        candidates[_identifier(identifier)] = {'id': identifier, 'title': name, 'folder_id': folder}
                        continue
                    if (not name.startswith("RDOS3 role_test " + PROJECT_ID + " ")
                            or not re.search(r"\[P3-[0-9a-f]{20}\]", name)
                            or not re.search(r"\btask_p3_[0-9a-f]{20}\b", name)
                            or not re.search(r"\bv[1-9][0-9]*\b", name)
                            or not re.search(r"\bnode_lab_phase3_(codex|workbuddy)\b", name)):
                        continue
                    candidates[_identifier(identifier)] = {"id": identifier, "title": name, "folder_id": folder}
            self._candidates = candidates
            return {"discovered": len(candidates), "registered": len(known), "bodies_read": 0}

    def register_event_document(self, event, task, reference):
        """Register an output only after a valid, assigned current task event."""
        with _LOCK:
            self._check_scope(event)
            self._check_scope(task)
            if event['project_id'] != task['project_id']:
                _fail('out_of_scope', '事件与任务不属于同一项目')
            node = event.get("claimed_node_id")
            version = task.get("requirements_version")
            if (node not in task.get("executor_node_ids", []) or task.get("business_status") not in ("active", "pending_review")
                    or not any(r.get("task_id") == task.get("task_id") and r.get("requirements_version") == version for r in event.get("task_refs", []))
                    or event.get("event_type") not in {"read", "started", "progress", "submission", "review", "blocker", "clarification", "correction", "check_result"}):
                _fail("authorization_failed", "新产物没有当前任务与分配节点的有效事件依据")
            identifier = reference.get("document_id", reference.get("id"))
            existing = next((d for d in self.resources['documents'] if d['id'] == identifier), None)
            if existing and existing.get('project_id', PROJECT_ID) != task['project_id']:
                _fail('out_of_scope', '成果属于其他项目')
            if existing:
                return self.read_document(identifier)
            candidate = self._candidates.get(identifier)
            folder = _project_folder(self.resources, task['project_id']) if task.get("output_zone") == "project" else AGENT_FOLDER if task.get("output_zone") == "agent" else None
            if not candidate or candidate["folder_id"] != folder:
                _fail("out_of_scope", "新产物未在任务允许根的本轮新对象中发现")
            prefix = f"RDOS3 role_test {PROJECT_ID} {task['task_id']} v{version} {node} "
            if self.resources.get('public_protocol_version') != 2 and (not candidate["title"].startswith(prefix) or not re.match(r"\[P3-[0-9a-f]{20}\](?: |$)", candidate["title"][len(prefix):])):
                _fail("out_of_scope", "新产物标题与任务、要求版本或自报节点不一致")
            item = {**candidate, "operation_id": "external-event:" + str(event.get("id", "")),
                    'project_id': task['project_id'],
                    "task_id": task["task_id"], "requirements_version": version, "claimed_node_id": node,
                    "zone": "agent" if folder == AGENT_FOLDER else "human", "registration_evidence": "valid_shared_account_task_event"}
            self.resources["documents"].append(item)
            try:
                document = self.read_document(identifier)
                if str(document["revision"]) != str(reference.get("revision")) or document["content_hash"] != reference.get("content_hash"):
                    _fail("version_conflict", "新产物的完整读回版本或正文指纹与事件引用不同")
            except BaseException:
                self.resources["documents"].remove(item)
                raise
            config.write_json(self.path, self.resources)
            self._candidates.pop(identifier, None)
            return document

    def _operations(self):
        return config.read_json(self.operations_path) if self.operations_path.exists() else {}

    def _operation(self, opid, action, inputs):
        if not isinstance(opid, str) or not 1 <= len(opid) <= 160 or "\x00" in opid:
            _fail("invalid_operation", "操作标识无效")
        operations = self._operations()
        digest = _hash({"action": action, "inputs": inputs})
        old = operations.get(opid)
        if old and old["hash"] != digest:
            _fail("operation_conflict", "相同操作标识对应不同正文")
        return operations, old or {"hash": digest, "action": action, "state": "new"}

    def _save_operation(self, operations, opid, entry, **changes):
        entry.update(changes)
        operations[opid] = entry
        config.write_json(self.operations_path, operations)

    def _check_scope(self, value):
        if value.get("project_id") not in _project_ids(self.resources) or value.get("env_id") != self.resources["env_id"]:
            _fail("out_of_scope", "记录不属于当前阶段3项目与环境")
        if value.get("generation") != self.resources["generation"]:
            _fail("out_of_scope", "记录不属于当前实验代次")

    @staticmethod
    def _cell(value):
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(v.get("text", "") if isinstance(v, dict) else str(v) for v in value)
        return "" if value is None else str(value)

    def _decode(self, row, table):
        try:
            raw = self._cell(row.get("fields", {}).get("记录数据"))
            wire = json.loads(raw)
            if not isinstance(wire, dict):
                raise ValueError()
            if wire.get('protocol_version') == 2:
                value = rdos_contract.decode(wire, self.resources)
            elif self.resources.get('public_protocol_version') != 2 or self.resources.get('allow_legacy_reads') is True:
                value = wire
            else:
                raise ValueError('旧协议记录只允许归档读取')
            if _FORMAL and table == self.resources['events_table']:
                # Account labels from the old system are private provenance,
                # omitted from the wire contract. Restore that one label only
                # from the authorized original operation when its *entire*
                # public event matches. Never turn a remote claim into proof.
                from server import central_store
                operation_id = str(wire.get('operation_id', ''))
                operation = central_store.get('operation', operation_id[:-6]) if operation_id.endswith(':event') else None
                prepared = (operation or {}).get('prepared_event')
                binding = (operation or {}).get('authorization')
                authorized = isinstance(binding, dict) and central_store.digest(binding) == operation.get('authorization_hash')
                # Automatic result settlement is authorized by the original
                # task grant, not by a new human confirmation. Its actor is
                # still only a shared-account node claim, never a proven user.
                if not authorized and (operation or {}).get('authority_kind') == 'task_execution_grant':
                    from server import phase3_store
                    task_id, version = operation.get('task_id'), operation.get('requirements_version')
                    grant = phase3_store.get('grant', str(task_id) + '@' + str(version)) or {}
                    history = next((v for v in phase3_store.history(task_id)
                                    if v.get('requirements_version') == version), {}) if task_id else {}
                    requirements = {k: v for k, v in history.items() if k != 'requirements_version'}
                    actor = (prepared or {}).get('verified_actor') or {}
                    authorized = (bool(operation.get('dispatched_at')) and bool(history)
                        and grant.get('operation_id') == operation.get('grant_operation_id')
                        and grant.get('requirements_hash') == operation.get('requirements_hash')
                        and central_store.digest(requirements) == grant.get('requirements_hash')
                        and actor.get('id') in requirements.get('executor_node_ids', []))
                if authorized and isinstance(prepared, dict) and rdos_contract.encode(prepared, self.resources) == wire:
                    mode = (prepared.get('verified_actor') or {}).get('identity_mode')
                    if mode in ('actual', 'role_test', 'shared_account_claim') and isinstance(value.get('verified_actor'), dict):
                        value['verified_actor']['identity_mode'] = mode
            self._check_scope(value)
            fields = row.get("fields", {})
            expected = {"操作ID": wire.get("operation_id", "")}
            if table == self.resources["tasks_table"]:
                expected.update({"任务ID": wire.get("task_id", ""), "标题": wire.get("title", ""),
                                 "状态": wire.get("business_status", ""), "执行节点": ", ".join(wire.get("executor_node_ids", [])),
                                 "要求版本": str(wire.get("requirements_version", "")), "记录修订": str(wire.get("record_revision", ""))})
                if _FORMAL:
                    expected['当前项目ID'] = wire.get('project_id', '')
                    from cutover.upgrade_tasks import legacy_columns
                    expected.update(legacy_columns(value, wire.get('operation_id', '')))
            else:
                expected.update({"事件ID": wire.get("id", ""), "类型": wire.get("event_type", wire.get("type", "")),
                                 "自报节点": wire.get("claimed_node_id", ""), "任务引用": _json(wire.get("task_refs", []))})
            if any(self._cell(fields.get(key)) != expected_value for key, expected_value in expected.items()):
                raise ValueError()
            return {**value, "record_id": row["record_id"], "source_url": TENANT + "/base/" + self.resources["base_token"] + "?table=" + table,
                    "remote_actor": {"provider": "feishu_user", "account_fingerprint": self.resources["account_fingerprint"],
                                     "identity_mode": "actual" if _FORMAL else "role_test", "client_identity": "unverified_client"},
                    "source_environment": self.registry().get('mode'), "readback_verified": True}
        except (ValueError, KeyError, TypeError):
            # Preserve a separately quarantinable entry without aborting all
            # valid records. Never turn readable-column edits into authority.
            return {"id": "invalid_" + _hash(row)[:16], "record_id": row.get("record_id", ""),
                    "_invalid_remote_record": True, "_raw_hash": _hash(row), "_raw_record": copy.deepcopy(row)}

    def _list(self, table):
        self.executor.identity()
        self._assert_base_scope()
        return [self._decode(row, table) for row in self.executor.records(table)]

    def _assert_base_scope(self):
        if not any(r.get("token") == self.resources["base_token"] and r.get("type") == "bitable"
                   for r in self.executor.list_root(AGENT_FOLDER)):
            _fail("out_of_scope", "登记Base不在指定Agent测试根，已停止读取")

    def list_tasks(self):
        with _LOCK:
            return self._list(self.resources["tasks_table"])

    def list_events(self):
        with _LOCK:
            return self._list(self.resources["events_table"])

    def read_task(self, taskid):
        values = [v for v in self.list_tasks() if v.get("task_id") == taskid]
        from server.peer_records import select_task_rows
        values = select_task_rows(values)
        if not values:
            return None
        if any(_payload(v) != _payload(values[0]) for v in values[1:]):
            _fail("remote_conflict", "相同任务ID有不同远端正文，须人工核对")
        return values[0]

    def _record_payload(self, kind, value):
        if kind == "event":
            from server.peer_records import event_readback_payload
            return event_readback_payload(value, self.resources)
        return _payload(value)

    def _write_record(self, kind, value, opid):
        self._check_scope(value)
        table = self.resources["tasks_table" if kind == "task" else "events_table"]
        id_key = "task_id" if kind == "task" else "id"
        if not value.get(id_key):
            _fail("invalid_record", "缺少任务或事件ID")
        inputs = _payload(value)
        inputs["operation_id"] = opid
        with _LOCK:
            operations, entry = self._operation(opid, kind, inputs)
            self.executor.identity()
            self._assert_base_scope()
            desired = inputs
            rows = [self._decode(row, table) for row in self.executor.records(table)]
            exact = [r for r in rows if r.get("operation_id") == opid]
            if exact:
                if any(self._record_payload(kind, r) != self._record_payload(kind, desired) for r in exact):
                    _fail("operation_conflict", "远端同一操作标识的正文不一致")
                self._save_operation(operations, opid, entry, state="verified", record_id=exact[0]["record_id"])
                return exact[0]
            if entry["state"] != "new":
                _fail("write_uncertain", "旧写操作尚未读回，只允许继续核对，不再次写入")
            existing = [r for r in rows if r.get(id_key) == value[id_key]]
            if kind == 'task':
                from server.peer_records import select_task_rows
                existing = select_task_rows(existing)
            if any(r.get('project_id') != value['project_id'] for r in existing):
                _fail('out_of_scope', '已有记录不能改派到另一个项目')
            identifier = None
            if existing:
                if kind == "event":
                    _fail("operation_conflict", "事件已存在，只能新增更正记录")
                if any(_payload(r) != _payload(existing[0]) for r in existing[1:]):
                    _fail("remote_conflict", "任务存在冲突的物理重复行")
                if value.get("record_revision") != existing[0].get("record_revision", 0) + 1:
                    _fail("version_conflict", "远端任务修订已变化")
                identifier = existing[0]["record_id"]
            elif kind == "task" and value.get("record_revision") != 1:
                _fail("version_conflict", "新任务记录必须从修订1开始")
            wire = rdos_contract.encode(desired, self.resources) if self.resources.get('public_protocol_version') == 2 else desired
            fields = {"操作ID": opid, "记录数据": _json(wire)}
            if kind == "task":
                fields.update({"任务ID": wire["task_id"], "标题": wire.get("title", ""), "状态": wire.get("business_status", ""),
                               "执行节点": ", ".join(wire.get("executor_node_ids", [])), "要求版本": str(wire.get("requirements_version", "")),
                               "记录修订": str(wire.get("record_revision", ""))})
                if _FORMAL:
                    fields['当前项目ID'] = wire.get('project_id', '')
                    from cutover.upgrade_tasks import legacy_columns
                    fields.update(legacy_columns(value, opid))
            else:
                fields.update({"事件ID": wire["id"], "类型": wire.get("event_type", wire.get("type", "")),
                               "自报节点": wire.get("claimed_node_id", ""), "任务引用": _json(wire.get("task_refs", []))})
            self._save_operation(operations, opid, entry, state="sent")
            try:
                self.executor.upsert(table, fields, identifier)
            except FeishuTestError as exc:
                if exc.code != "write_uncertain":
                    raise
            # Independent list + record GET; the mutation response never proves
            # success. Lost responses are reconciled but never resent.
            matches = _visible(lambda: [r for r in self.executor.records(table) if self._cell(r.get("fields", {}).get("操作ID")) == opid])
            if not matches:
                _fail("write_uncertain", "写入可能已提交，尚未独立读回；不会重复发送")
            def read_complete():
                result = [self._decode(self.executor.get_record(table, r["record_id"]), table) for r in matches]
                return result if all(self._record_payload(kind, r) == self._record_payload(kind, desired) for r in result) else []
            verified = _visible(read_complete)
            if not verified:
                _fail("readback_mismatch", "记录完整正文独立读回不一致")
            self._save_operation(operations, opid, entry, state="verified", record_id=verified[0]["record_id"])
            return verified[0]

    def write_task(self, task, opid):
        return self._write_record("task", task, opid)

    def append_event(self, event, opid):
        return self._write_record("event", event, opid)

    def read_document(self, identifier):
        with _LOCK:
            item = next((d for d in self.resources["documents"] if d["id"] == identifier), None)
            if item is None:
                _fail("out_of_scope", "文档未登记为本轮新资源")
            self.executor.identity()
            if not any(r.get("token") == identifier and r.get("type") == "docx"
                       for r in self.executor.list_root(item["folder_id"])):
                _fail("out_of_scope", "登记文档不在指定测试根，已停止读取")
            data = _data(self.executor.fetch_document(identifier))
            document = data.get("document", data)
            content = document.get("content", document.get("markdown"))
            revision = document.get("revision_id", document.get("revision"))
            if not isinstance(content, str) or revision is None:
                _fail("incomplete_document", "未取得完整原生正文与版本，拒绝以摘要替代")
            return {"id": identifier, "object_id": identifier, "title": document.get("title") or item["title"],
                    "content": content, "revision": revision, "content_hash": _hash(content),
                    "body_content": _body(content, item["title"]), "body_content_hash": _hash(_body(content, item["title"])),
                    "folder_id": item["folder_id"], "zone": "agent" if item["folder_id"] == AGENT_FOLDER else "project",
                    "operation_id": item["operation_id"], "project_id": item.get('project_id', PROJECT_ID),
                    "source_url": TENANT + "/docx/" + identifier,
                    "source_environment": "feishu_production" if _FORMAL else "feishu_test", "readback_verified": True}

    def create_document(self, zone, title, content, opid, *, project_id=PROJECT_ID):
        if zone not in ("human", "project", "agent"):
            _fail("out_of_scope", "新文档必须位于两个指定测试根")
        if not isinstance(title, str) or not title.strip() or len(title) > 180 or not isinstance(content, str) or len(content.encode()) > 500_000:
            _fail("invalid_document", "新文档标题或正文无效")
        project_folder = _project_folder(self.resources, project_id)
        folder = AGENT_FOLDER if zone == "agent" else project_folder
        marker = ("[RDOS-" + _hash([self.resources.get('workspace_id'), opid])[:20] + "]"
                  if self.resources.get('public_protocol_version') == 2 else
                  "[P3-" + _hash([self.resources["env_id"], opid])[:20] + "]")
        remote_title = title + " " + marker
        with _LOCK:
            inputs = [folder, title, content] if project_id == PROJECT_ID else [project_id, folder, title, content]
            operations, entry = self._operation(opid, "document_create", inputs)
            self.executor.identity()
            registered = next((d for d in self.resources["documents"] if d["operation_id"] == opid), None)
            if registered and entry["state"] == "verified":
                return self.read_document(registered["id"])
            matches = [r for r in self.executor.list_root(folder) if r.get("name") == remote_title and r.get("type") == "docx"]
            if len(matches) > 1:
                _fail("remote_conflict", "同一创建标识对应多份文档，须人工核对")
            if not matches and entry["state"] == "new":
                self._save_operation(operations, opid, entry, state="sent", folder_id=folder, remote_title=remote_title, project_id=project_id,
                                     requested_body_hash=_hash(content.replace("\r\n", "\n").strip("\n")), requested_content_hash=_hash(content))
                try:
                    self.executor.create_document(folder, remote_title, content)
                except FeishuTestError as exc:
                    if exc.code != "write_uncertain":
                        raise
                matches = _visible(lambda: [r for r in self.executor.list_root(folder) if r.get("name") == remote_title and r.get("type") == "docx"])
            if len(matches) != 1:
                _fail("write_uncertain", "新文档创建尚未独立找到，只允许继续核对")
            identifier = _identifier(matches[0]["token"])
            if identifier in self.resources.get("excluded_object_ids", []):
                _fail("out_of_scope", "旧对象不能作为新建结果")
            if registered and identifier != registered["id"]:
                _fail("remote_conflict", "创建标识与已登记文档不同")
            if not registered:
                item = {"id": identifier, "folder_id": folder, 'project_id': project_id, "zone": "agent" if folder == AGENT_FOLDER else "human",
                        "title": remote_title, "operation_id": opid, "creation_marker": marker}
                self.resources["documents"].append(item)
                config.write_json(self.path, self.resources)
            document = self.read_document(identifier)
            if not creation_matches(document, content):
                _fail("readback_mismatch", "新文档完整正文独立读回与请求不一致")
            self._save_operation(operations, opid, entry, state="verified", document_id=identifier,
                                 content_hash=document["content_hash"], revision=document["revision"],
                                 requested_body_hash=_hash(content.replace("\r\n", "\n").strip("\n")), requested_content_hash=_hash(content))
            return document

    def find_created_document(self, opid):
        """Read-only recovery; never dispatch a create, even if not found."""
        with _LOCK:
            operations = self._operations()
            entry = operations.get(opid)
            if not entry or entry.get("action") != "document_create":
                return None
            registered = next((d for d in self.resources["documents"] if d["operation_id"] == opid), None)
            if registered:
                document = self.read_document(registered["id"])
            else:
                folder, title = entry.get("folder_id"), entry.get("remote_title")
                if folder not in _folders(self.resources) or not title:
                    return None
                self.executor.identity()
                matches = [r for r in self.executor.list_root(folder) if r.get("name") == title and r.get("type") == "docx"]
                if not matches:
                    return None
                if len(matches) != 1 or matches[0]["token"] in self.resources.get("excluded_object_ids", []):
                    _fail("remote_conflict", "文档创建恢复出现冲突对象")
                item = {"id": _identifier(matches[0]["token"]), "folder_id": folder, 'project_id': entry.get('project_id', PROJECT_ID), "zone": "agent" if folder == AGENT_FOLDER else "human",
                        "title": title, "operation_id": opid}
                self.resources["documents"].append(item)
                config.write_json(self.path, self.resources)
                document = self.read_document(item["id"])
            if entry.get("requested_body_hash") and document["body_content_hash"] != entry["requested_body_hash"]:
                _fail("readback_mismatch", "恢复文档完整正文与原请求指纹不同")
            self._save_operation(operations, opid, entry, state="verified", document_id=document["id"],
                                 content_hash=document["content_hash"], revision=document["revision"])
            return document

    def patch_document(self, identifier, base_revision, base_hash, old_text, new_text, opid):
        with _LOCK:
            operations, entry = self._operation(opid, "document_patch", [identifier, base_revision, base_hash, old_text, new_text])
            current = self.read_document(identifier)
            if entry.get("expected_hash") and current["content_hash"] == entry["expected_hash"]:
                self._save_operation(operations, opid, entry, state="verified")
                return current
            if entry["state"] != "new":
                _fail("write_uncertain", "文档修改已发出但尚未读回，只允许核对")
            if str(current["revision"]) != str(base_revision) or current["content_hash"] != base_hash:
                _fail("version_conflict", "文档基准版本已变化，保留当前原文")
            if not old_text or current["content"].count(old_text) != 1:
                _fail("patch_conflict", "局部修改必须唯一匹配原文")
            if self.resources.get("cas_verified") is not True:
                _fail("cas_unverified", "尚未实证飞书原生覆盖的并发安全；请另建新版本，原文已保留")
            expected = current["content"].replace(old_text, new_text, 1)
            self._save_operation(operations, opid, entry, state="sent", expected_hash=_hash(expected))
            try:
                self.executor.update_document(identifier, base_revision, old_text, new_text)
            except FeishuTestError as exc:
                if exc.code != "write_uncertain":
                    raise
            result = self.read_document(identifier)
            if result["content"] != expected or str(result["revision"]) == str(base_revision):
                _fail("write_uncertain", "文档修改未完整读回，保留当前正文并停止重试")
            self._save_operation(operations, opid, entry, state="verified")
            return result


def configure(cfg):
    """Validate enabled configuration before guard.install; never call CLI."""
    global _BACKEND, _CONFIGURED, _ENABLED_REQUESTED
    phase3 = cfg.get("phase3", {"enabled": False})
    _BACKEND, _CONFIGURED = None, False
    _ENABLED_REQUESTED = phase3 != {"enabled": False}
    if phase3 == {"enabled": False}:
        _BACKEND, _CONFIGURED = None, True
        os.environ["RDOS_LAB_PHASE3_ENABLED"] = "0"
        return
    if phase3.get("enabled") is not True or set(phase3) != {"enabled", "cli_path", "cli_sha256", "registry_path"}:
        _fail("invalid_configuration", "阶段3启用配置不完整")
    binary = Path(phase3["cli_path"])
    if str(binary) != config.PHASE3_CLI or binary.is_symlink() or not binary.is_file():
        _fail("invalid_configuration", "阶段3只能使用固定原生CLI")
    if hashlib.sha256(binary.read_bytes()).hexdigest() != phase3["cli_sha256"]:
        _fail("binary_changed", "阶段3CLI与已固定校验值不一致")
    root = Path(cfg["root"])
    path = config.checked_path(Path(phase3["registry_path"]), root)
    if path != root / "runtime/phase3_resources.json":
        _fail("invalid_configuration", "阶段3资源登记路径越界")
    resources = _validate_registry(config.read_json(path), cfg)
    instance_path = root / "runtime/instance.json"
    if instance_path.exists() and config.read_json(instance_path).get("generation") != resources["generation"]:
        _fail("invalid_registry", "资源登记与当前实验代次不一致")
    executor = _CLIExecutor(_TOKEN, cfg, resources)
    from lab.guard import register_cli_executor
    register_cli_executor(executor)
    _BACKEND, _CONFIGURED = FeishuBackend(cfg, resources, executor), True
    os.environ["RDOS_LAB_PHASE3_ENABLED"] = "1"


def configure_formal(formal):
    """Pin the original Base and formal folders before enabling business I/O."""
    global _BACKEND, _CONFIGURED, _ENABLED_REQUESTED
    if not _FORMAL:
        _fail('invalid_configuration', '正式适配器只能在正式运行模式配置')
    from server.production_runtime import require
    if require() is not formal:
        _fail('invalid_configuration', '正式配置未通过启动器验证')
    import hashlib as _hashlib
    state_root = formal.database.parent
    runtime = state_root / 'runtime'
    runtime.mkdir(mode=0o700, exist_ok=True)
    if runtime.is_symlink() or runtime.stat().st_mode & 0o077:
        _fail('invalid_configuration', '正式操作记录目录权限无效')
    (runtime / 'tmp').mkdir(mode=0o700, exist_ok=True)
    resources_path = runtime / 'phase3_resources.json'
    fixed = formal.resources
    seed = {
        'schema_version': 1, 'state': 'ready', 'backend': 'feishu_test',
        'env_id': 'rdos_formal',
        'generation': _hashlib.sha256((fixed['base_token'] + ':' + fixed['task_table']).encode()).hexdigest()[:32],
        'project_id': PROJECT_ID, 'default_project_id': PROJECT_ID,
        'workspace_id': 'ws_rdos', 'public_protocol_version': 2,
        'identity_mode': 'actual',
        'human_folder': HUMAN_FOLDER, 'agent_folder': AGENT_FOLDER,
        'base_token': fixed['base_token'], 'tasks_table': fixed['task_table'],
        'events_table': fixed['event_table'],
        'account_fingerprint': fixed['account_fingerprint'],
        'documents': [], 'projects': [{
            'id': fixed['finance_project_id'], 'public_id': fixed['finance_project_id'],
            'name': '财赋', 'status': 'active',
            'node_ids': list(fixed['node_members']), 'folder_id': fixed['finance_folder'],
        }], 'excluded_object_ids': [],
        'nodes': [{'node_id': node, 'public_id': node, 'member_id': member,
                   'enabled': True, 'ready': False}
                  for node, member in fixed['node_members'].items()],
    }
    if resources_path.exists():
        resources = config.read_json(resources_path)
        for key in ('schema_version','backend','env_id','generation','project_id',
                    'default_project_id','workspace_id','public_protocol_version','identity_mode',
                    'human_folder','agent_folder','base_token','tasks_table',
                    'events_table','account_fingerprint'):
            if resources.get(key) != seed[key]:
                _fail('invalid_configuration', '正式运行登记与固定资源不一致')
        if resources_path.is_symlink() or resources_path.stat().st_mode & 0o077:
            _fail('invalid_configuration', '正式运行登记权限过宽')
        finance = next((p for p in resources.get('projects', [])
                        if p.get('id') == fixed['finance_project_id']), None)
        if not finance or finance.get('folder_id') != fixed['finance_folder']:
            _fail('invalid_configuration', '财赋项目仍须使用原工作文件夹')
    else:
        resources = seed
        config.write_json(resources_path, resources)
        resources_path.chmod(0o600)
    cfg = {'root': str(state_root), 'env_id': seed['env_id'],
           'phase3': {'cli_path': str(formal.cli),
                      'cli_sha256': fixed['cli_sha256'],
                      'registry_path': str(resources_path)}}
    _validate_registry(resources, cfg)
    executor = _CLIExecutor(_TOKEN, cfg, resources)
    if executor.identity()['account_fingerprint'] != fixed['account_fingerprint']:
        _fail('account_changed', '正式飞书调用账号与固定登记不同')
    _BACKEND, _CONFIGURED, _ENABLED_REQUESTED = FeishuBackend(cfg, resources, executor), True, True


def get_backend():
    if _FORMAL and not _CONFIGURED:
        _fail('not_configured', '正式飞书资源未验证配置')
    if not _CONFIGURED and (_ENABLED_REQUESTED or os.environ.get("RDOS_LAB_PHASE3_ENABLED") == "1"):
        _fail("not_configured", "阶段3被要求启用但适配层未配置")
    return _BACKEND
