"""Build the standalone Windows installer using the existing clean-source packager."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys
import sysconfig
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from windows_installer import runtime_public_file


def main():
    if sys.platform != 'win32':
        raise SystemExit('请在 Windows 构建安装器。')
    folder = ROOT / 'build' / 'installer-payload'
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / 'source.zip'
    subprocess.run([sys.executable, str(ROOT / 'tools/package_release.py'), '--root', str(ROOT),
                    '--include-launcher', '--app-only', '--output', str(source)], check=True)
    payload = folder / 'application.zip'
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(payload, 'w', zipfile.ZIP_DEFLATED) as output:
        for info in original.infolist():
            output.writestr(info.filename.removeprefix('Open-QQ-Dot/'), original.read(info))
    (folder / 'application.sha256').write_text(hashlib.sha256(payload.read_bytes()).hexdigest(), 'utf-8')
    runtime_archive = folder / 'runtime.zip'
    defaults = json.loads((ROOT / 'dotlink_vendor_defaults.json').read_text('utf-8'))
    runtime_source = ROOT / defaults.get('runtime_dir', 'qq-runtime')
    if not runtime_source.is_dir() and (ROOT / 'qq-runtime').is_dir():
        runtime_source = ROOT / 'qq-runtime'
    if '--with-runtime' in sys.argv and (runtime_source / 'node.exe').is_file():
        with zipfile.ZipFile(runtime_archive, 'w', zipfile.ZIP_DEFLATED) as package:
            for path in sorted(runtime_source.rglob('*')):
                if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(runtime_source.resolve()):
                    continue
                relative = path.relative_to(runtime_source)
                if not runtime_public_file(relative):
                    continue
                package.write(path, relative.as_posix())
        (folder / 'runtime.sha256').write_text(hashlib.sha256(runtime_archive.read_bytes()).hexdigest(), 'utf-8')
        print(f'QQ runtime: {runtime_archive} ({runtime_archive.stat().st_size / (1024 ** 2):.0f} MB)')
    else:
        runtime_archive.unlink(missing_ok=True)
        (folder / 'runtime.sha256').unlink(missing_ok=True)
        print('QQ runtime archive skipped; the installer will only connect if a runtime is provided separately.')
    uv = shutil.which('uv')
    if not uv:
        raise SystemExit('缺少构建依赖 uv，请先在构建环境安装 uv。')
    shutil.copy2(uv, folder / 'uv.exe')
    # uv's dual license must accompany its redistributed binary.
    licenses = Path(uv).parent.parent / 'Lib/site-packages'
    candidates = list(licenses.glob('uv-*.dist-info/licenses'))
    if not candidates:
        raise SystemExit('未找到 uv 授权文件，停止构建。')
    for license_file in candidates[0].glob('LICENSE*'):
        shutil.copy2(license_file, folder / ('uv-' + license_file.name))
    # This build environment is a conda layout: _tkinter.pyd exists but its
    # tcl86t/tk86t DLLs live in Library/bin, which PyInstaller does not search.
    # Bundle both DLLs and the Tcl/Tk script libraries explicitly so the
    # installer GUI does not fail with "DLL load failed while importing _tkinter".
    base = Path(sys.base_prefix)
    tk_args = []
    for name in ('tcl86t.dll', 'tk86t.dll', 'ffi-8.dll', 'libffi-8.dll'):
        for candidate in (base / 'Library' / 'bin' / name, base / 'DLLs' / name, base / name):
            if candidate.is_file():
                tk_args += ['--add-binary', f'{candidate};.']
                break
    for name in ('tcl8.6', 'tk8.6'):
        for candidate in (base / 'Library' / 'lib' / name, base / 'tcl' / name):
            if candidate.is_dir():
                tk_args += ['--add-data', f'{candidate};{name}']
                break
    args = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
            '--name', 'Open QQ Dot Setup', '--icon', str(ROOT / 'assets/app-icon.ico'),
            '--version-file', str(ROOT / 'tools/launcher.version'),
            '--paths', str(ROOT), '--specpath', str(ROOT / 'build'),
            '--hidden-import', 'installer_ui',
            '--hidden-import', 'psutil',
            '--add-data', str(ROOT / 'assets/app-icon.png') + ';assets',
            '--add-data', str(ROOT / 'assets/app-icon.ico') + ';assets', *tk_args]
    for name in ('application.zip', 'application.sha256', 'uv.exe', 'uv-LICENSE-APACHE', 'uv-LICENSE-MIT'):
        args += ['--add-data', str(folder / name) + ';.']
    if runtime_archive.is_file():
        args += ['--add-data', str(runtime_archive) + ';.']
        args += ['--add-data', str(folder / 'runtime.sha256') + ';.']
    args += [str(ROOT / 'windows_installer.py')]
    subprocess.run(args, cwd=ROOT, check=True)
    result = ROOT / 'dist/Open QQ Dot Setup.exe'
    result.with_suffix('.exe.sha256').write_text(hashlib.sha256(result.read_bytes()).hexdigest() + '  ' + result.name, 'utf-8')
    print(result)


if __name__ == '__main__':
    main()
