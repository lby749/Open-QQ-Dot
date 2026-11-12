"""Run the release regression set with temporary runtime state, without model accounts."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    'test_goals', 'test_goals_ui', 'test_round3_reliability', 'test_round3_integration', 'test_round3_packaging',
    'test_task_manager_p1', 'test_p1_task_lifecycle', 'test_p0_deadlock_concurrency',
    'test_p0_security_rbac', 'test_p2_orchestrator_verifier', 'test_multi_agent_p0',
    'test_p2_proactive_and_memory', 'test_p3_p4_ecosystem', 'test_release_modes',
    'test_claude_backend', 'test_antigravity_backend', 'test_topic_library',
    'test_custom_api', 'test_model_api', 'test_house_web', 'test_release_ui',
)


def main():
    sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
    previous = Path.cwd()
    previous_home = os.environ.get('QQ_BRIDGE_HOME')
    try:
        with tempfile.TemporaryDirectory(prefix='open-qq-dot-checks-') as temporary:
            os.environ['QQ_BRIDGE_HOME'] = temporary
            os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
            os.chdir(temporary)
            try:
                suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in MODULES)
                result = unittest.TextTestRunner(verbosity=2).run(suite)
            finally:
                os.chdir(previous)
        return 0 if result.wasSuccessful() else 1
    finally:
        if previous_home is None:
            os.environ.pop('QQ_BRIDGE_HOME', None)
        else:
            os.environ['QQ_BRIDGE_HOME'] = previous_home


if __name__ == '__main__':
    raise SystemExit(main())
