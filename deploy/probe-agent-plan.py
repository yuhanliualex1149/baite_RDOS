"""Bounded, synthetic acceptance check for the actual RDOS Agent Plan route.

This program never reads a user prompt, memory or database, and never prints
the API key. A 200 response with a complete short answer is required before
the release procedure may install the selected 393216 output setting.
"""
from __future__ import annotations

import json
import hashlib
import os
import sys

import httpx


URL = 'https://ark.cn-beijing.volces.com/api/plan/v3/chat/completions'
MODEL = 'deepseek-v4-pro'
QUOTA = 393216


def payload() -> dict:
    return {'model': MODEL,
            'messages': [{'role': 'user', 'content': '请仅回复 OK。'}],
            'max_completion_tokens': QUOTA,
            'thinking': {'type': 'enabled'},
            'reasoning_effort': 'high'}


def main() -> int:
    if (os.environ.get('BAITE_LLM_BASE_URL', '').rstrip('/') != URL.rsplit('/chat/completions', 1)[0]
        or os.environ.get('BAITE_LLM_MODEL') != MODEL
        or os.environ.get('BAITE_LLM_THINKING') != 'enabled'
        or os.environ.get('BAITE_LLM_REASONING_EFFORT') != 'high'):
        print('Agent Plan endpoint/model/thinking configuration differs from reviewed RDOS settings', file=sys.stderr)
        return 1
    key = (os.environ.get('BAITE_LLM_API_KEY') or os.environ.get('ARK_API_KEY') or '').strip()
    if not key:
        print('Agent Plan credential unavailable to existing service account', file=sys.stderr)
        return 1
    try:
        with httpx.Client(trust_env=False, follow_redirects=False,
                          timeout=httpx.Timeout(35.0, connect=5.0)) as client:
            with client.stream('POST', URL, json=payload(),
                               headers={'Authorization': 'Bearer ' + key}) as response:
                raw = b''
                for chunk in response.iter_bytes():
                    raw += chunk
                    if len(raw) > 65536:
                        print('Agent Plan response exceeded synthetic probe size limit', file=sys.stderr)
                        return 1
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    print('Agent Plan returned non-JSON response; HTTP ' + str(response.status_code), file=sys.stderr)
                    return 1
                if not response.is_success:
                    error = value.get('error', {}) if isinstance(value, dict) else {}
                    if not isinstance(error, dict):
                        error = {}
                    # Only provider code/type are returned. Never print a
                    # reflected prompt, token, header or unbounded message.
                    code_digest = hashlib.sha256(str(error.get('code', 'unknown')).encode()).hexdigest()[:12]
                    print(f'Agent Plan rejected quota: HTTP {response.status_code}, error_code_sha256_prefix={code_digest}', file=sys.stderr)
                    return 1
                choices = value.get('choices') if isinstance(value, dict) else None
                if (not isinstance(choices, list) or not choices or
                    choices[0].get('finish_reason') != 'stop' or
                    not isinstance(choices[0].get('message'), dict) or
                    not isinstance(choices[0]['message'].get('content'), str) or
                    not choices[0]['message']['content'].strip()):
                    print('Agent Plan accepted HTTP request but did not complete the synthetic answer', file=sys.stderr)
                    return 1
    except httpx.HTTPError as exc:
        print('Agent Plan synthetic probe transport failed: ' + type(exc).__name__, file=sys.stderr)
        return 1
    print('Agent Plan accepted deepseek-v4-pro thinking/high max_completion_tokens=393216 on the live plan route')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
