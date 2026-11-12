import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

import pytest

import platform_runtime
import packaged_bridge

ROOT=Path(__file__).resolve().parents[1]


def test_shell_selection_matches_each_platform():
    assert platform_runtime.shell_command('echo ok','win32')[0]=='powershell.exe'
    for target in ('linux','darwin'):
        assert platform_runtime.shell_command('printf ok',target)==['/bin/sh','-c','printf ok']


def test_launcher_check_does_not_start_ui_models_or_services():
    result=subprocess.run([sys.executable,str(ROOT/'launcher.py'),'--check'],cwd=ROOT,
        capture_output=True,text=True,encoding='utf-8',timeout=25,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    assert result.returncode==0,result.stderr
    report=json.loads(result.stdout)
    assert report['model_requests']==0 and report['starts_services'] is False
    assert report['platform']==sys.platform


def test_startup_report_detects_native_library_import_failure(monkeypatch):
    original = platform_runtime.importlib.import_module
    def import_module(name):
        if name == 'PySide6.QtWidgets':
            raise ImportError('missing native GUI library')
        return original(name)
    monkeypatch.setattr(platform_runtime.importlib, 'import_module', import_module)
    report = platform_runtime.startup_report()
    assert report['dependencies']['PySide6'] is False
    assert report['ready'] is False
    assert report['model_requests'] == 0 and report['starts_services'] is False


def test_bridge_creation_does_not_require_windows_process_flags(tmp_path):
    process=SimpleNamespace(pid=12345,poll=lambda:None)
    with patch.object(packaged_bridge,'ROOT',tmp_path),patch.object(packaged_bridge,'PID_FILE',tmp_path/'bridge.pid'), \
         patch.object(packaged_bridge,'bridge_process',return_value=None), \
         patch.object(packaged_bridge.psutil,'process_iter',return_value=[]), \
         patch.object(packaged_bridge.time,'sleep'), \
         patch.object(packaged_bridge.subprocess,'CREATE_NO_WINDOW',0,create=True), \
         patch.object(packaged_bridge.subprocess,'Popen',return_value=process) as popen:
        assert '12345' in packaged_bridge.start()
    assert popen.call_args.kwargs['creationflags']==0


@pytest.mark.skipif(os.name=='nt',reason='fcntl exclusion requires a POSIX runtime')
def test_posix_lock_excludes_other_process_and_releases_after_exit(tmp_path,monkeypatch):
    monkeypatch.setenv('XDG_STATE_HOME',str(tmp_path))
    if sys.platform=='darwin':
        monkeypatch.setattr(platform_runtime,'state_directory',lambda:tmp_path/'locks')
    handle=platform_runtime.acquire_posix_lock('test-runtime')
    assert handle
    # Same helper in a child, with an explicit temporary state location for both platforms.
    script=('from pathlib import Path; import platform_runtime as p; '
            f'p.state_directory=lambda:Path({str(platform_runtime.state_directory())!r}); '
            'lock=p.acquire_posix_lock("test-runtime"); print(lock is not None)')
    try:
        first=subprocess.check_output([sys.executable,'-c',script],cwd=ROOT,text=True).strip()
        assert first=='False'
    finally:handle.close()
    assert subprocess.check_output([sys.executable,'-c',script],cwd=ROOT,text=True).strip()=='True'


def test_source_zip_preserves_executable_bits_for_posix_launchers(tmp_path):
    from tools.package_release import create_clean_package
    for name in ('start.sh','Open QQ Dot.command'):
        (tmp_path/name).write_bytes(b'#!/bin/sh\nexit 0\n')
    archive=tmp_path.parent/(tmp_path.name+'.zip')
    try:
        create_clean_package(tmp_path,archive)
        with zipfile.ZipFile(archive) as package:
            for name in ('start.sh','Open QQ Dot.command'):
                entry=package.getinfo('Open-QQ-Dot/'+name)
                assert entry.create_system==3 and (entry.external_attr>>16)&0o111==0o111
    finally:
        archive.unlink(missing_ok=True)
        archive.with_suffix('.zip.sha256').unlink(missing_ok=True)


@pytest.mark.parametrize('name', ('start.sh', 'Open QQ Dot.command'))
def test_source_launcher_line_endings_and_packaging_reject_crlf(tmp_path, name):
    from tools.package_release import create_clean_package
    assert b'\r' not in (ROOT/name).read_bytes()
    (tmp_path/name).write_bytes(b'#!/bin/sh\r\nexit 0\r\n')
    archive = tmp_path.parent/(tmp_path.name+'.zip')
    with pytest.raises(SystemExit):
        create_clean_package(tmp_path, archive)
    assert not archive.exists()
