"""Explicit bounded Phase 1 regression suite; never starts the main Panel."""
from __future__ import annotations
import sys
import unittest

MODULES = [
    'tests.test_lab_backend', 'tests.test_lab_config', 'tests.test_lab_guard',
    'tests.test_phase1_backend', 'tests.test_phase1_operations', 'tests.test_phase1_core',
    'tests.test_phase1_adversarial', 'tests.test_phase1_concurrency',
    'tests.test_phase1_publication', 'tests.test_phase1_api',
    'tests.test_central_agent', 'tests.test_central_runtime',
    'tests.test_central_task_diagnostics',
]

if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromNames(MODULES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
