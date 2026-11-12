import os
from pathlib import Path
import subprocess
import sys


def test_release_runner_applies_pytest_fixtures_and_collects_both_styles(tmp_path):
    tests = tmp_path/'tests'
    tests.mkdir()
    (tests/'test_runner_smoke.py').write_text('''
import os
import unittest
import pytest

@pytest.fixture(autouse=True)
def isolated_fixture(monkeypatch):
    monkeypatch.setenv('RELEASE_FIXTURE_ACTIVE', 'yes')

class UnitStyle(unittest.TestCase):
    def test_fixture_is_used(self):
        self.assertEqual(os.environ.get('RELEASE_FIXTURE_ACTIVE'), 'yes')

def test_function_style():
    assert os.environ['RELEASE_FIXTURE_ACTIVE'] == 'yes'
''', encoding='utf-8')
    runner = Path(__file__).resolve().parents[1]/'tools/run_release_checks.py'
    script = (
        'import runpy; from pathlib import Path; '
        f"m=runpy.run_path({str(runner)!r}); "
        f"m['main'].__globals__.update(ROOT=Path({str(tmp_path)!r}),MODULES=('test_runner_smoke',)); "
        "raise SystemExit(m['main']())"
    )
    env = dict(os.environ)
    env.pop('RELEASE_FIXTURE_ACTIVE', None)
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script], env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '2 passed' in result.stdout
