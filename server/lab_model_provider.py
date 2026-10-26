"""Explicit live Agent Plan factory for the local experiment."""
from __future__ import annotations

import os

from lab.guard import register_model_token

_token = object()
_configured = False


def configure(cfg: dict) -> None:
    global _configured
    if cfg['backends']['model'] != 'agent_plan':
        return
    from lab import config
    data = config.read_json(config.ROOT / 'runtime/model.json')
    os.environ.update(BAITE_LLM_API_KEY=data['api_key'], BAITE_LLM_BASE_URL=data['base_url'],
                      BAITE_LLM_MODEL=data['model'], BAITE_LLM_THINKING=data['thinking'],
                      BAITE_LLM_REASONING_EFFORT=data['reasoning_effort'], RDOS_LAB_MODEL_MODE='agent_plan')
    register_model_token(_token)
    _configured = True


def make_agent():
    if os.environ.get('RDOS_RUNTIME_MODE') == 'production':
        from server.production_runtime import require
        require()
        from server.central_agent import CentralAgent
        agent = CentralAgent()
        if agent.base_url != 'https://ark.cn-beijing.volces.com/api/plan/v3' or agent.model != 'deepseek-v4-pro':
            raise RuntimeError('正式模型配置与已核对范围不一致')
        return agent
    if os.environ.get('RDOS_LAB_MODEL_MODE') != 'agent_plan' and not _configured:
        from server.lab_central_backend import make_agent as make_mock_agent
        return make_mock_agent()
    if not _configured or os.environ.get('RDOS_LAB_MODEL_MODE') != 'agent_plan':
        raise RuntimeError('真实模型未明确配置；不会回退模拟回复')
    from server.central_agent import CentralAgent
    agent = CentralAgent()
    if agent.base_url != 'https://ark.cn-beijing.volces.com/api/plan/v3' or agent.model != 'deepseek-v4-pro':
        raise RuntimeError('Agent Plan 地址或模型发生变化，拒绝调用')
    agent._lab_model_token = _token
    return agent
