import subprocess
import sys
import time

import psutil
import pytest

from task_runtime import run_process


def test_timeout_stops_native_launcher_descendants(tmp_path):
    child_pid = tmp_path/'child.pid'
    script = (
        'import subprocess,sys,time; from pathlib import Path; '
        'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(10)"]); '
        f'Path({str(child_pid)!r}).write_text(str(p.pid)); '
        'print("child started",flush=True); time.sleep(10)'
    )
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_process([sys.executable, '-c', script], capture_output=True, text=True, timeout=1)
    elapsed = time.monotonic()-started
    assert child_pid.exists(), 'Child must have started before testing cleanup'
    pid = int(child_pid.read_text())
    alive = psutil.pid_exists(pid)
    if alive:
        process = psutil.Process(pid)
        alive = process.status() != psutil.STATUS_ZOMBIE
        if alive:
            for child in reversed(process.children(recursive=True)):
                child.kill()
            process.kill()
    assert not alive, 'Timed-out native child was left running'
    assert elapsed < 5, 'Inherited output pipes kept the timed-out call blocked'


def test_process_result_preserves_utf8_input_stderr_and_check():
    text = '第一行\n第二行：100% & 引号'
    result = run_process([sys.executable, '-X', 'utf8', '-c',
        'import sys; print(sys.stdin.read()); print("错误信息",file=sys.stderr)'],
        input=text, capture_output=True, text=True, encoding='utf-8', timeout=5)
    assert result.returncode == 0
    assert result.stdout.strip() == text
    assert result.stderr.strip() == '错误信息'
    with pytest.raises(subprocess.CalledProcessError) as error:
        run_process([sys.executable, '-c', 'raise SystemExit(7)'], check=True, timeout=5)
    assert error.value.returncode == 7
