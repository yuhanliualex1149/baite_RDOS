"""Bounded phase3 suite plus the complete phase1 isolation regression."""
import sys
import unittest
from lab.check_phase1 import MODULES as PHASE1

MODULES = PHASE1 + ['tests.test_phase3_regressions', 'tests.test_phase3_core',
    'tests.test_phase3_api', 'tests.test_phase3_egress', 'tests.test_phase3_tools',
    'tests.test_phase3_presentations', 'tests.test_rdos_rules',
    'tests.test_phase3_business', 'tests.test_rdos_contract',
    'tests.test_rdos_skill', 'tests.test_business_api',
    'tests.test_phase3_model_context', 'tests.test_panel_projects',
    'tests.test_panel_project_integration', 'tests.test_panel_accounts',
    'tests.test_panel_password_cli', 'tests.test_panel_feishu_projects',
    'tests.test_panel_members', 'tests.test_panel_member_api',
    'tests.test_shared_knowledge', 'tests.test_knowledge_model_integration',
    'tests.test_lab_model_socket_guard', 'tests.test_skill_folder_knowledge',
    'tests.test_skill_folder_egress', 'tests.test_central_memory',
    'tests.test_central_requests', 'tests.test_central_inspection',
    'tests.test_central_interpretation', 'tests.test_central_capabilities',
    'tests.test_central_conversation_reference', 'tests.test_central_recall',
    'tests.test_central_settings', 'tests.test_central_maintenance',
    'tests.test_peer_tasks', 'tests.test_rodos_upgrade_integration',
    'tests.test_central_maintenance_requests', 'tests.test_peer_enrollment']

if __name__ == '__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(MODULES))
    sys.exit(not result.wasSuccessful())
