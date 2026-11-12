"""Shell integration with fake external tools; real installation is a separate cloud check."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(os.name == 'nt', reason='requires a POSIX shell')
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def home(tmp_path):
    folder = tmp_path / 'application with spaces'
    folder.mkdir()
    shutil.copy2(ROOT / 'start.sh', folder / 'start.sh')
    tools = tmp_path / 'bin'
    tools.mkdir()
    for name in ('dirname', 'mkdir', 'rm', 'sh'):
        (tools / name).symlink_to(shutil.which(name))
    return folder, dict(os.environ, PATH=str(tools))


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\n' + text, 'utf-8')
    path.chmod(0o755)


def run(home, flag):
    folder, env = home
    return subprocess.run(['/bin/sh', 'start.sh', flag], cwd=folder, env=env,
                          capture_output=True, text=True, timeout=10)


def test_check_without_python_does_not_download_or_install(home):
    result = run(home, '--check')
    assert result.returncode == 1
    assert '--prepare-only' in result.stderr
    assert not (home[0] / '.bootstrap').exists()


def test_prepare_uses_cached_uv_and_keeps_windows_environment(home):
    folder, _ = home
    (folder / '.venv/Scripts').mkdir(parents=True)
    sentinel = folder / '.venv/Scripts/python.exe'
    sentinel.write_bytes(b'windows environment must remain unchanged')
    executable(folder / '.bootstrap/uv', '''printf '%s\\n' "$*" >> uv-calls.txt
if [ "$1" = venv ]; then
  mkdir -p .venv-posix/bin
  printf '#!/bin/sh\\nexit 0\\n' > .venv-posix/bin/python
  chmod +x .venv-posix/bin/python
fi
''')
    # The fake uv needs chmod, but there is still no Python or network tool in PATH.
    (Path(home[1]['PATH']) / 'chmod').symlink_to(shutil.which('chmod'))
    result = run(home, '--prepare-only')
    assert result.returncode == 0, result.stderr
    calls = (folder / 'uv-calls.txt').read_text('utf-8').splitlines()
    assert len(calls) == 3
    assert '>=3.11,<3.14' in calls[0] and '.venv-posix' in calls[0]
    assert 'requirements-ui-core.txt' in calls[1]
    assert '--no-deps -r requirements-ui-qfluent.txt' in calls[2]
    assert sentinel.read_bytes() == b'windows environment must remain unchanged'


def test_ready_environment_does_not_download_or_reinstall(home):
    folder, _ = home
    executable(folder / '.venv/bin/python', 'exit 0\n')
    result = run(home, '--prepare-only')
    assert result.returncode == 0, result.stderr
    assert not (folder / '.bootstrap').exists()
