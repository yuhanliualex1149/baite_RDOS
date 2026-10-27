"""RDOS 2.0 release regression suite.

The ten omitted modules exercise the removed pre-lab HTTP/Runner entry or its
old direct Feishu adapter.  Their replacement modules remain in discovery.
This explicit list is reviewable; a newly added test is included by default.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


RETIRED_MODULES = {
    'test_api', 'test_bootstrap', 'test_central_api', 'test_central_feishu',
    'test_central_projects', 'test_central_service_feishu', 'test_cloud_readiness',
    'test_projects', 'test_rag_sync', 'test_windows_onboarding',
}


def main():
    names = sorted(p.stem for p in Path('tests').glob('test_*.py'))
    missing = RETIRED_MODULES - set(names)
    if missing:
        raise RuntimeError('Retired-test inventory drift: ' + ', '.join(sorted(missing)))
    selected = [name for name in names if name not in RETIRED_MODULES]
    discovered = unittest.defaultTestLoader.discover('tests', pattern='test_*.py', top_level_dir='.')
    def flatten(suite):
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                yield from flatten(item)
            else:
                yield item
    suite = unittest.TestSuite(test for test in flatten(discovered)
                               if test.id().split('.')[1] in selected)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print(f'RDOS2 release modules={len(selected)} retired={len(RETIRED_MODULES)} '
          f'tests={result.testsRun} failures={len(result.failures)} '
          f'errors={len(result.errors)} skipped={len(result.skipped)}')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
