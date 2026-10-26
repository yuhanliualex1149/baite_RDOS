from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cutover import build_resources, production_config as formal


class CandidateResourceTests(unittest.TestCase):
    def test_candidate_is_fixed_owner_only_and_does_not_overwrite_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cli = root / 'run.js'
            cli.write_text('fixture', encoding='utf-8')
            policy = root / 'source.md'
            policy.write_text('current central policy\n', encoding='utf-8')
            identity = {'data': {'userOpenId': 'ou_fixture', 'appId': 'cli_fixture',
                                 'brand': 'feishu', 'identity': 'user'}}
            candidate = build_resources.build(cli=cli, state_root=root / 'state',
                                              policy_source=policy, identity_response=identity)
            value = json.loads(candidate.read_text())
            self.assertEqual(value['base_token'], formal.BASE_TOKEN)
            self.assertEqual(value['node_members'], build_resources.NODE_MEMBERS)
            self.assertEqual((root / 'state/central/agent.md').read_text(), policy.read_text())
            self.assertEqual((root / 'state/central/memory.md').read_bytes(), b'')
            self.assertFalse(candidate.stat().st_mode & 0o077)
            self.assertEqual(build_resources.build(cli=cli, state_root=root / 'state',
                                                   policy_source=policy, identity_response=identity), candidate)
            policy.write_text('changed', encoding='utf-8')
            with self.assertRaises(formal.ConfigurationError):
                build_resources.build(cli=cli, state_root=root / 'state',
                                      policy_source=policy, identity_response=identity)


if __name__ == '__main__':
    unittest.main()
