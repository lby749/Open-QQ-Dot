"""Regression guard for the public-source vs installed-application package split."""
import zipfile
from pathlib import Path

import tools.package_release as package_release


def _fixture(root: Path) -> None:
    (root / 'README.md').write_text('# demo\n', 'utf-8')
    (root / 'BUILDING.md').write_text('# build\n', 'utf-8')
    (root / 'LICENSE').write_text('GNU GENERAL PUBLIC LICENSE Version 3\n', 'utf-8')
    (root / 'THIRD_PARTY_NOTICES.md').write_text('GPL-3.0-or-later\n', 'utf-8')
    (root / 'tests').mkdir()
    (root / 'tests' / 'test_demo.py').write_text('def test_ok(): pass\n', 'utf-8')
    (root / 'tools').mkdir()
    (root / 'tools' / 'build.py').write_text('print("build")\n', 'utf-8')
    (root / 'docs').mkdir()
    (root / 'docs' / 'ARCH.md').write_text('# architecture\n', 'utf-8')
    (root / 'Open QQ Dot.exe').write_bytes(b'launcher binary')
    (root / 'Desktop.lnk').write_bytes(b'local shortcut')
    (root / 'collect-final.txt').write_text('private collection\n', 'utf-8')


def test_source_archive_contains_tests_tools_docs_and_build_docs(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    _fixture(root)
    output = tmp_path / 'source.zip'
    count, _ = package_release.create_clean_package(root, output)
    with zipfile.ZipFile(output) as package:
        names = set(package.namelist())
    for expected in ('README.md', 'BUILDING.md', 'tests/test_demo.py', 'tools/build.py', 'docs/ARCH.md'):
        assert 'Open-QQ-Dot/' + expected in names
    assert 'Open-QQ-Dot/Desktop.lnk' not in names
    assert 'Open-QQ-Dot/collect-final.txt' not in names
    assert not any(name.lower().endswith('.exe') for name in names)
    assert count == len(names)


def test_windows_launcher_spec_bundles_ctypes_dll():
    spec = (Path(__file__).resolve().parents[1] / 'Open QQ Dot.spec').read_text('utf-8')
    assert 'ffi-8.dll' in spec
    assert 'libffi-8.dll' in spec
    assert 'binaries=binaries' in spec


def test_app_archive_excludes_development_material(tmp_path):
    root = tmp_path / 'app'
    root.mkdir()
    _fixture(root)
    output = tmp_path / 'app.zip'
    package_release.create_clean_package(root, output, app_only=True)
    with zipfile.ZipFile(output) as package:
        names = set(package.namelist())
    assert 'Open-QQ-Dot/README.md' in names
    assert 'Open-QQ-Dot/tests/test_demo.py' not in names
    assert 'Open-QQ-Dot/tools/build.py' not in names
    assert 'Open-QQ-Dot/docs/ARCH.md' not in names
    assert 'Open-QQ-Dot/Desktop.lnk' not in names
    assert 'Open-QQ-Dot/collect-final.txt' not in names


def test_blue_android_source_is_published_without_discarded_app_cache_or_signing_material(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    for name in ('android/app/src/main/MainActivity.kt', 'mobile/app/Discarded.kt',
                 'android/.gradle/cache.bin', 'android/local.properties', 'android/keystore.properties',
                 'android/private.jks', 'android/private.keystore'):
        path = root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('// synthetic fixture', 'utf-8')
    source, app = tmp_path/'source.zip', tmp_path/'app.zip'
    package_release.create_clean_package(root, source)
    package_release.create_clean_package(root, app, app_only=True)
    with zipfile.ZipFile(source) as archive:
        assert archive.namelist() == ['Open-QQ-Dot/android/app/src/main/MainActivity.kt']
    with zipfile.ZipFile(app) as archive:
        assert archive.namelist() == []


def test_only_exact_pinned_public_credential_corpus_is_allowed(tmp_path):
    relative = 'android/privacy-benchmark/src/main/resources/corpus_c.jsonl'
    original = Path(__file__).resolve().parents[1] / relative
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(original.read_bytes())
    assert package_release.scan_for_leaks(tmp_path, [target]) == []
    target.write_bytes(target.read_bytes() + b'\n')
    assert package_release.scan_for_leaks(tmp_path, [target])


def test_android_data_source_package_is_kept_while_root_private_data_is_excluded(tmp_path):
    public = tmp_path/'android/app/src/main/kotlin/example/data/model/Config.kt'
    private = tmp_path/'data/friend.json'
    for path in (public, private):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('// synthetic fixture', 'utf-8')
    (tmp_path/'.gitignore').write_text('/data/\n', 'utf-8')
    output = tmp_path.parent/'source-data.zip'
    package_release.create_clean_package(tmp_path, output)
    with zipfile.ZipFile(output) as archive:
        assert 'Open-QQ-Dot/'+public.relative_to(tmp_path).as_posix() in archive.namelist()
        assert 'Open-QQ-Dot/data/friend.json' not in archive.namelist()
    assert package_release.scan_for_leaks(tmp_path, [public]) == []
    assert package_release.scan_for_leaks(tmp_path, [private])
