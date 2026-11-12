from pathlib import Path
import sys
import shutil

ROOT = Path(SPECPATH).resolve()
# Conda's Python keeps the DLL that _ctypes.pyd depends on (ffi-8.dll) in
# Library/bin. PyInstaller does not discover it from DLLs, so without this the
# one-file launcher fails at startup with:
#   ImportError: DLL load failed while importing _ctypes
binaries = []
for directory in (Path(sys.base_prefix) / 'Library' / 'bin', Path(sys.base_prefix) / 'DLLs'):
    for name in ('ffi-8.dll', 'libffi-8.dll'):
        candidate = directory / name
        if candidate.is_file():
            binaries.append((str(candidate), '.'))
uv = shutil.which('uv')
if not uv:
    raise SystemExit('构建启动器需要 uv，首次启动将使用内置副本安装 Python。')
licenses = list((Path(uv).parent.parent / 'Lib/site-packages').glob('uv-*.dist-info/licenses'))
if not licenses:
    raise SystemExit('缺少 uv 授权文件，停止构建。')
datas = [(uv, '.')] + [(str(p), 'licenses/uv') for p in licenses[0].glob('LICENSE*')]

a = Analysis([str(ROOT / 'house_bootstrap.py')], pathex=[str(ROOT)], binaries=binaries, datas=datas,
             hiddenimports=['dotlink', 'qq_link'], hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=['tkinter'],
             noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='Open QQ Dot', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          version=str(ROOT / 'tools' / 'launcher.version'), icon=str(ROOT / 'friend-house.ico'))
