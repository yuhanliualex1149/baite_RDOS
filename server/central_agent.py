from __future__ import annotations

import asyncio
import copy
import inspect
import json
import os
import time
from typing import Any, Awaitable, Callable

import httpx

from lab.guard import deny


# Actual runtime policy; no second embedded copy can drift from this file.
from pathlib import Path
SYSTEM_PROMPT = (Path(__file__).with_name('central_context') / 'agent.md').read_text(encoding='utf-8').strip()


class AgentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
StepCallback = Callable[[dict[str, Any]], Awaitable[None]]


class CentralAgent:
    """Bounded OpenAI-compatible tool loop; tool authorization stays in the caller."""

    max_seconds = 180.0
    max_model_calls = 6
    max_tool_calls = 12
    request_timeout = 90.0
    # Agent Plan's ZCode example gives 393216 output tokens for this model.
    # This includes reasoning and visible output; live API acceptance still
    # requires verification before switching the production environment.
    deepseek_v4_pro_completion_tokens = 393216

    def _local_mode(self) -> bool:
        transport = getattr(self, "transport", None)
        if transport is None or type(transport).__module__ != "server.lab_central_backend":
            return False
        from server.lab_central_backend import trusted_model
        return trusted_model(transport)

    def _live_mode(self) -> bool:
        if os.environ.get('RDOS_RUNTIME_MODE') == 'production':
            from server.production_runtime import active
            return active() and self.transport is None and self.base_url == 'https://ark.cn-beijing.volces.com/api/plan/v3' and self.model == 'deepseek-v4-pro'
        from lab.guard import trusted_model_agent
        return trusted_model_agent(self) and os.environ.get('RDOS_LAB_MODEL_MODE') == 'agent_plan'

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = (
            api_key if api_key is not None else
            os.environ.get("BAITE_LLM_API_KEY") or os.environ.get("ARK_API_KEY", "")
        ).strip()
        self.base_url = (
            base_url or os.environ.get("BAITE_LLM_BASE_URL")
            or "https://ark.cn-beijing.volces.com/api/plan/v3"
        ).rstrip("/")
        self.model = model or os.environ.get("BAITE_LLM_MODEL") or "deepseek-v4-pro"
        self.reasoning_effort = os.environ.get("BAITE_LLM_REASONING_EFFORT", "high")
        self.thinking = os.environ.get("BAITE_LLM_THINKING", "enabled")
        if self.reasoning_effort not in {"low", "medium", "high", "xhigh", "max"} or self.thinking not in {"enabled", "disabled"}:
            raise AgentError("model_config", "模型 thinking 或 reasoning_effort 配置无效")
        # Keep an explicit operator setting, but do not force the old 8192 cap
        # (or Ark's 4k omitted-parameter default) on this specific model.
        # Other providers retain their existing bounded request contract.
        ark_deepseek_v4_pro = (self.base_url == 'https://ark.cn-beijing.volces.com/api/plan/v3'
                               and self.model == 'deepseek-v4-pro')
        default_completion = self.deepseek_v4_pro_completion_tokens if ark_deepseek_v4_pro else 8192
        maximum_completion = self.deepseek_v4_pro_completion_tokens if ark_deepseek_v4_pro else 32768
        self.max_completion_tokens = self._setting(
            "BAITE_LLM_MAX_COMPLETION_TOKENS", default_completion, 1024, maximum_completion, int)
        self.max_seconds = self._setting("BAITE_AGENT_MAX_SECONDS", self.max_seconds, 1, 600, float)
        self.max_model_calls = self._setting("BAITE_AGENT_MAX_MODEL_CALLS", self.max_model_calls, 1, 12, int)
        self.max_tool_calls = self._setting("BAITE_AGENT_MAX_TOOL_CALLS", self.max_tool_calls, 1, 24, int)
        self.request_timeout = self._setting("BAITE_LLM_REQUEST_TIMEOUT", self.request_timeout, 1, 180, float)
        self.transport = transport
        self._policy_snapshot = None
        self._context_binding = None

    def bind_context(self, run: dict, actor: dict) -> None:
        """Bind the real run, not model-supplied identity or conversation IDs."""
        self._context_binding = (run, actor)

    def bind_policy(self, snapshot: dict[str, Any]) -> None:
        """Freeze the server-created run policy; edits affect the next run only."""
        from server.central_store import digest
        if (not isinstance(snapshot, dict) or type(snapshot.get('version')) is not int
                or snapshot['version'] < 1 or not isinstance(snapshot.get('content'), str)
                or not snapshot['content'].strip() or len(snapshot['content']) > 40000
                or snapshot.get('hash') != digest(snapshot['content'])):
            raise AgentError('policy_snapshot_invalid', '中央职责版本无法核对，请保留运行记录后重试')
        self._policy_snapshot = copy.deepcopy(snapshot)

    @staticmethod
    def _setting(name: str, default: float | int, minimum: float, maximum: float, convert: Callable) -> Any:
        value = os.environ.get(name)
        if value is None:
            return default
        try:
            parsed = convert(value)
        except (ValueError, TypeError) as exc:
            raise AgentError("model_config", f"{name} 须为有效数值") from exc
        if not minimum <= parsed <= maximum:
            raise AgentError("model_config", f"{name} 超出允许范围 {minimum:g} 至 {maximum:g}")
        return parsed

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AgentError("agent_timeout", "中央 Agent 已达到本次运行时间上限；请查看已完成步骤后再重试")
        return remaining

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return value.replace(self.api_key, "[凭据已隐藏]") if self.api_key else value
        if isinstance(value, dict):
            secrets = {"api_key", "authorization", "access_token", "refresh_token", "client_secret", "password"}
            return {
                key: "[凭据已隐藏]" if isinstance(key, str) and key.lower() in secrets else self._redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    async def interpret_request(self, messages: list[dict[str, Any]], on_step=None, should_pause=None,
                                stage='request_interpretation') -> dict:
        """One tool-free interpretation response, charged to this run's budget.

        Duties, effective memory and full current-thread history use the same
        input projection as the tool loop. They clarify intent but never grant
        execution authority; the caller still validates the current request.
        """
        if not (self._local_mode() or self._live_mode()):
            deny('real_central_request_interpretation')
        usage = getattr(self, '_active_usage', None)
        if usage is None:
            usage = copy.deepcopy(getattr(self, 'prior_usage', None) or {'model_calls': 0, 'tool_calls': 0})
        started = time.monotonic()

        async def emit(step):
            if on_step:
                await on_step(self._redact({**step, 'stage': stage}))

        async def pause():
            if should_pause:
                paused = should_pause()
                if inspect.isawaitable(paused):
                    paused = await paused
                if paused:
                    raise AgentError('paused', '中央 Agent 已暂停；本次尚未开始的动作没有执行。')

        payload = {'model': self.model, 'messages': self._redact(copy.deepcopy(messages)),
                   'max_completion_tokens': self.max_completion_tokens}
        if self.model.startswith(('deepseek-', 'glm-5.3-flash', 'glm-5-3-flash')):
            payload['reasoning_effort'] = self.reasoning_effort
        if self.model.startswith('deepseek-'):
            payload['thinking'] = {'type': self.thinking}
        async with httpx.AsyncClient(transport=self.transport, timeout=self.request_timeout,
                headers={} if self._local_mode() else {'Authorization': f'Bearer {self.api_key}'},
                trust_env=False, follow_redirects=False) as client:
            response = await self._request(client, payload, usage, emit, pause,
                                           started + self.max_seconds, started)
        message, reason = self._message(response)
        self._add_usage(usage, response.get('usage') or {})
        self.prior_usage = usage
        await emit({'type': 'model_response', 'model_call': usage['model_calls'],
                    'usage': response.get('usage') or {}})
        if reason == 'length':
            raise AgentError('model_incomplete', '模型输出额度耗尽，推理和回答共用该额度；请求解析未完成，本次未执行任务动作。')
        if reason == 'content_filter' or message.get('tool_calls'):
            raise AgentError('request_understanding_failed', '未完整理解当前请求，本次未执行任务动作。')
        content = message.get('content') or ''
        if content.startswith('```') and content.rstrip().endswith('```'):
            content = content.split('\n', 1)[-1].rsplit('```', 1)[0]
        try:
            data = json.loads(content)
        except (ValueError, TypeError) as exc:
            raise AgentError('request_understanding_failed', '未可靠理解当前请求，请补充具体目标或对象；本次未执行任务动作。') from exc
        if not isinstance(data, dict):
            raise AgentError('request_understanding_failed', '请求解析格式无效，本次未执行任务动作。')
        return {'data': self._redact(data), 'usage': usage, 'model': response.get('model') or self.model}

    async def run(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        execute_tool: ToolExecutor,
        on_step: StepCallback | None = None,
        should_pause: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        async def emit(step: dict[str, Any]) -> None:
            if on_step:
                await on_step(self._redact(step))

        async def check_pause() -> None:
            if should_pause:
                paused = should_pause()
                if inspect.isawaitable(paused):
                    paused = await paused
                if paused:
                    raise AgentError("paused", "中央 Agent 已暂停；已完成的工具操作保留在运行记录中")

        if not self.api_key and not self._local_mode():
            raise AgentError("model_missing", "尚未配置真实模型密钥，请在本机安全配置中设置 BAITE_LLM_API_KEY 或 ARK_API_KEY")
        try:
            return await asyncio.wait_for(
                self._run(messages, tools, execute_tool, emit, check_pause),
                timeout=self.max_seconds,
            )
        except asyncio.TimeoutError as exc:
            error = AgentError("agent_timeout", "中央 Agent 已达到本次运行时间上限；请查看已完成步骤后再重试")
            await emit({"type": "error", "code": error.code, "message": error.message})
            raise error from exc
        except AgentError as exc:
            await emit({"type": "error", "code": exc.code, "message": exc.message})
            raise
        finally:
            self._active_usage = None

    async def _run(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        execute_tool: ToolExecutor,
        emit: StepCallback,
        check_pause: Callable[[], Awaitable[None]],
    ) -> dict[str, Any]:
        if not (self._local_mode() or self._live_mode()):
            deny("real_central_model_run")
        started = time.monotonic()
        conversation = self._redact(copy.deepcopy(messages))
        allowed_tools = {
            tool["function"]["name"] for tool in tools
            if tool.get("type") == "function" and isinstance(tool.get("function"), dict)
            and isinstance(tool["function"].get("name"), str)
        }
        usage: dict[str, Any] = copy.deepcopy(getattr(self, 'prior_usage', None) or {"model_calls": 0, "tool_calls": 0})
        self._active_usage = usage
        deadline = started + self.max_seconds
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=self.request_timeout,
            headers={} if self._local_mode() else {"Authorization": f"Bearer {self.api_key}"},
            trust_env=False,
            follow_redirects=False,
        ) as client:
            while True:
                payload: dict[str, Any] = {"model": self.model, "messages": conversation,
                                           "max_completion_tokens": self.max_completion_tokens}
                if self.model.startswith(("deepseek-", "glm-5.3-flash", "glm-5-3-flash")):
                    payload["reasoning_effort"] = self.reasoning_effort
                if self.model.startswith("deepseek-"):
                    payload["thinking"] = {"type": self.thinking}
                if tools:
                    payload.update(tools=tools, tool_choice="auto")
                model_started = time.monotonic()
                response = await self._request(client, payload, usage, emit, check_pause, deadline, started)
                message, finish_reason = self._message(response)
                call_usage = response.get("usage") or {}
                if isinstance(call_usage, dict):
                    self._add_usage(usage, call_usage)
                await emit({"type": "model_response", "model_call": usage["model_calls"], "usage": call_usage,
                            "elapsed_seconds": round(time.monotonic() - started, 3),
                            "stage_elapsed_seconds": round(time.monotonic() - model_started, 3)})
                if finish_reason == "length":
                    raise AgentError("model_incomplete", "模型输出额度耗尽，推理和回答共用该额度；本次运行不能报告为完成")
                if finish_reason == "content_filter":
                    raise AgentError("model_incomplete", "模型回复未完整生成，本次运行不能报告为完成")
                message = self._redact(message)
                conversation.append(message)
                calls = message.get("tool_calls") or []
                if not calls:
                    content = message.get("content")
                    if not isinstance(content, str) or not content.strip():
                        raise AgentError("model_protocol", "模型没有返回可展示的回复或工具调用")
                    return {
                        "content": self._redact(content),
                        "model": response.get("model") or self.model,
                        "usage": usage,
                        "messages": conversation,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "agent_policy": {key: self._policy_snapshot[key] for key in ('version', 'hash')},
                    }
                for call in calls:
                    await check_pause()
                    if usage["tool_calls"] >= self.max_tool_calls:
                        raise AgentError("tool_limit", "中央 Agent 已达到本次工具调用上限；请查看已完成步骤后再继续")
                    usage["tool_calls"] += 1
                    tool_started = time.monotonic()
                    name = call["function"]["name"]
                    arguments, error = self._arguments(call, allowed_tools)
                    await emit({"type": "tool_call", "tool_call": usage["tool_calls"], "name": name, "arguments": arguments, "validation_error": error,
                                "elapsed_seconds": round(tool_started - started, 3)})
                    if error:
                        result = {"ok": False, "error": error}
                    else:
                        try:
                            result = await asyncio.wait_for(execute_tool(name, arguments), timeout=self._remaining(deadline))
                            if not isinstance(result, dict):
                                result = {"ok": False, "error": {"code": "tool_result_invalid", "message": "工具没有返回有效结果对象"}}
                        except AgentError as exc:
                            if exc.code == "agent_timeout":
                                raise
                            result = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
                        except asyncio.TimeoutError as exc:
                            raise AgentError("agent_timeout", "中央 Agent 在工具执行期间达到本次运行时间上限；写入结果须先核对后再重试") from exc
                        except Exception:
                            result = {"ok": False, "error": {"code": "tool_failed", "message": "工具执行失败，请查看工具层运行记录"}}
                    result = self._redact(result)
                    await emit({"type": "tool_result", "tool_call": usage["tool_calls"], "name": name, "result": result,
                                "elapsed_seconds": round(time.monotonic() - started, 3),
                                "stage_elapsed_seconds": round(time.monotonic() - tool_started, 3)})
                    conversation.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)})

    async def _request(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
        usage: dict[str, Any],
        emit: StepCallback,
        check_pause: Callable[[], Awaitable[None]],
        deadline: float,
        started: float,
    ) -> dict[str, Any]:
        if not (self._local_mode() or self._live_mode()):
            deny("real_central_model_http")
        provider = "local_mock 模拟模型" if self._local_mode() else "真实模型"
        for attempt in range(2):
            await check_pause()
            # Reproject immediately before every actual HTTP attempt, so a
            # correction/forget cannot survive in an old retry/tool payload.
            from server.central_model_context import assemble
            request_payload = {**payload, 'messages': self._redact(assemble(self, payload['messages']))}
            remaining = self._remaining(deadline)
            if usage["model_calls"] >= self.max_model_calls:
                raise AgentError("model_limit", "中央 Agent 已达到本次模型调用上限；请查看已完成步骤后再继续")
            usage["model_calls"] += 1
            request_started = time.monotonic()
            timeout = min(self.request_timeout, remaining)
            await emit({"type": "model_request", "model_call": usage["model_calls"], "model": self.model,
                        "elapsed_seconds": round(request_started - started, 3), "timeout_seconds": round(timeout, 3)})
            timeout = min(self.request_timeout, self._remaining(deadline))
            retry = False
            try:
                if self._live_mode() and os.environ.get('RDOS_RUNTIME_MODE') != 'production':
                    from lab.guard import model_permit
                    with model_permit(self):
                        response = await asyncio.wait_for(
                            client.post(f"{self.base_url}/chat/completions", json=request_payload, timeout=timeout),
                            timeout=timeout,
                        )
                else:
                    response = await asyncio.wait_for(
                        client.post(f"{self.base_url}/chat/completions", json=request_payload, timeout=timeout),
                        timeout=timeout,
                    )
                if response.status_code in {401, 403}:
                    raise AgentError("model_auth", f"{provider}授权失败（HTTP {response.status_code}），请核对本机模型配置")
                if not response.is_success:
                    if self._context_capacity_error(response):
                        raise AgentError('context_capacity', '完整对话与记忆超过模型接口容量；本次未裁剪历史，已有记录保留。请新建对话或调整模型容量后再试。')
                    retry = response.status_code in {429, 500, 502, 503, 504}
                    error = AgentError("model_http", f"{provider}请求失败（HTTP {response.status_code}）")
                else:
                    try:
                        data = response.json()
                    except ValueError as exc:
                        raise AgentError("model_protocol", "模型没有返回有效 JSON，不能报告为成功") from exc
                    if not isinstance(data, dict):
                        raise AgentError("model_protocol", "模型返回格式不符合 Chat Completions 协议")
                    return data
            except (httpx.TimeoutException, asyncio.TimeoutError):
                self._remaining(deadline)
                retry = True
                error = AgentError("model_timeout", f"{provider}请求超时，未取得完整结果")
            except httpx.RequestError:
                retry = True
                error = AgentError("model_network", f"无法连接{provider}服务，请核对本机网络及模型地址")
            if not retry or attempt == 1:
                raise error
            await emit({"type": "retry", "model_call": usage["model_calls"], "code": error.code, "message": error.message,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "stage_elapsed_seconds": round(time.monotonic() - request_started, 3)})
            await asyncio.sleep(min(0.25, self._remaining(deadline)))
        raise AgentError("model_network", "真实模型请求未完成")

    @staticmethod
    def _context_capacity_error(response) -> bool:
        if response.status_code not in {400, 413, 422}:
            return False
        try:
            data = response.json()
        except ValueError:
            return response.status_code == 413
        error = data.get('error', data) if isinstance(data, dict) else {}
        text = json.dumps(error, ensure_ascii=False).casefold()
        return response.status_code == 413 or any(term in text for term in (
            'context_length_exceeded', 'context_window_exceeded', 'maximum context length',
            'context length', 'context window', 'input tokens exceed', 'input length exceeds',
            'prompt is too long', '上下文长度', '上下文窗口'))

    @staticmethod
    def _message(payload: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise AgentError("model_protocol", "模型返回缺少有效的回复选项")
        choice = choices[0]
        raw = choice.get("message")
        if not isinstance(raw, dict) or raw.get("role", "assistant") != "assistant":
            raise AgentError("model_protocol", "模型返回缺少有效的助手消息")
        message = {"role": "assistant", "content": raw.get("content")}
        if "reasoning_content" in raw:
            message["reasoning_content"] = raw["reasoning_content"]
        calls = raw.get("tool_calls")
        if calls is not None and not isinstance(calls, list):
            raise AgentError("model_protocol", "模型工具调用格式无效")
        if calls:
            normalized = []
            call_ids = set()
            for call in calls:
                if (
                    not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"]
                    or call.get("type", "function") != "function" or not isinstance(call.get("function"), dict)
                    or not isinstance(call["function"].get("name"), str) or not call["function"]["name"]
                ):
                    raise AgentError("model_protocol", "模型工具调用缺少有效的名称或调用标识")
                if call["id"] in call_ids:
                    raise AgentError("model_protocol", "模型在同一回复中重复了工具调用标识，未执行该回复中的操作")
                call_ids.add(call["id"])
                function = dict(call["function"])
                if isinstance(function.get("arguments"), dict):
                    function["arguments"] = json.dumps(function["arguments"], ensure_ascii=False)
                normalized.append({"id": call["id"], "type": "function", "function": function})
            message["tool_calls"] = normalized
        return message, choice.get("finish_reason")

    @staticmethod
    def _arguments(call: dict[str, Any], allowed_tools: set[str]) -> tuple[dict[str, Any], dict[str, str] | None]:
        function = call["function"]
        if function["name"] not in allowed_tools:
            return {}, {"code": "tool_unknown", "message": "此工具未提供，不能调用或扩大授权范围"}
        try:
            arguments = json.loads(function.get("arguments", ""))
        except (ValueError, TypeError):
            arguments = None
        if not isinstance(arguments, dict):
            return {}, {"code": "tool_arguments_invalid", "message": "工具参数必须为有效的 JSON 对象，请修正后再调用"}
        return arguments, None

    @staticmethod
    def _add_usage(total: dict[str, Any], current: dict[str, Any]) -> None:
        for key, value in current.items():
            if key in {"model_calls", "tool_calls"}:
                continue
            if isinstance(value, dict):
                nested = total.setdefault(key, {})
                if isinstance(nested, dict):
                    CentralAgent._add_usage(nested, value)
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                total[key] = total.get(key, 0) + value
