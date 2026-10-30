"""Current next-release regression; all writes are isolated fixtures."""
import sys
import unittest
from lab.check_phase3 import MODULES as EXISTING

MODULES = list(dict.fromkeys(EXISTING + [
    'tests.test_central_full_context', 'tests.test_next_task_management',
    'tests.test_next_release_migration', 'tests.test_peer_readback_recovery',
    'tests.test_peer_confirmation_recovery',
    'tests.test_peer_lifecycle_recovery', 'tests.test_peer_request_recovery',
    'tests.test_feishu_record_shapes',
    'tests.test_peer_auto_enrollment', 'tests.test_auto_enrollment_presentations',
    'tests.test_shared_skill_updates', 'tests.test_node_onboarding',
    'tests.test_periodic_tasks',
]))

if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromNames(MODULES))
    sys.exit(not result.wasSuccessful())
