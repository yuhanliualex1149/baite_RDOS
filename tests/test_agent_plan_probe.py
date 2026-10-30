"""Synthetic Agent Plan acceptance request never uses live credentials/network."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx


SCRIPT = Path(__file__).resolve().parents[1] / 'deploy/probe-agent-plan.py'


class AgentPlanProbeTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('rdos_agent_plan_probe', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.env = {'BAITE_LLM_API_KEY':'fixture-secret-never-print',
            'BAITE_LLM_BASE_URL':'https://ark.cn-beijing.volces.com/api/plan/v3',
            'BAITE_LLM_MODEL':'deepseek-v4-pro',
            'BAITE_LLM_THINKING':'enabled',
            'BAITE_LLM_REASONING_EFFORT':'high'}

    def run_probe(self, handler):
        client = httpx.Client
        transport = httpx.MockTransport(handler)
        with patch.dict(os.environ, self.env), \
             patch.object(self.module.httpx, 'Client', side_effect=lambda **kwargs: client(transport=transport, **kwargs)):
            return self.module.main()

    def test_exact_live_route_and_quota_contract(self):
        def handler(request):
            self.assertEqual(str(request.url), self.module.URL)
            self.assertEqual(request.headers['Authorization'], 'Bearer fixture-secret-never-print')
            self.assertEqual(__import__('json').loads(request.content), {
                'model':'deepseek-v4-pro',
                'messages':[{'role':'user','content':'请仅回复 OK。'}],
                'max_completion_tokens':393216,
                'thinking':{'type':'enabled'},
                'reasoning_effort':'high'})
            return httpx.Response(200, json={'choices':[{'finish_reason':'stop','message':{'content':'OK'}}]})
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.run_probe(handler), 0)
        self.assertIn('accepted', output.getvalue())

    def test_rejection_is_redacted_and_stops(self):
        def handler(_request):
            return httpx.Response(400, json={'error':{'code':'fixture-secret-never-print',
                                                        'message':'fixture-secret-never-print'}})
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(self.run_probe(handler), 1)
        self.assertIn('HTTP 400', output.getvalue())
        self.assertNotIn('fixture-secret-never-print', output.getvalue())


if __name__ == '__main__':
    unittest.main()
