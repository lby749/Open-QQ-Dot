import zipfile

from tools.verify_release_archive import check


def test_source_docs_allowed_but_runtime_and_internal_notes_rejected(tmp_path):
    archive = tmp_path/'source.zip'
    with zipfile.ZipFile(archive, 'w') as package:
        for name in ('LICENSE', 'licenses/NapCat-LICENSE', 'docs/ARCHITECTURE.md'):
            package.writestr('Open-QQ-Dot/'+name, 'public')
    assert check(archive, source=True) == []
    assert any('docs/ARCHITECTURE' in error for error in check(archive))
    with zipfile.ZipFile(archive, 'a') as package:
        package.writestr('Open-QQ-Dot/docs/AGENT_HANDOFF.md', 'internal')
        package.writestr('Open-QQ-Dot/friends/12345/friend.json', '{}')
    errors = check(archive, source=True)
    assert any('internal handoff' in error for error in errors)
    assert any('friends/' in error for error in errors)


def test_source_android_data_package_is_not_private_runtime_data(tmp_path):
    archive = tmp_path/'android-source.zip'
    with zipfile.ZipFile(archive, 'w') as package:
        for name in ('LICENSE', 'licenses/NapCat-LICENSE',
                     'android/app/src/main/kotlin/example/data/model/Config.kt'):
            package.writestr('Open-QQ-Dot/'+name, 'public source')
    assert check(archive, source=True) == []
    with zipfile.ZipFile(archive, 'a') as package:
        package.writestr('Open-QQ-Dot/data/private.json', '{}')
    assert any('data/private.json' in error for error in check(archive, source=True))
