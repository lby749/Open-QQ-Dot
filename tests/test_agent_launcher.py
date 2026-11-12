"""One-click preparation chooses the correct action per model source."""
import agent_launcher


def test_web_source_opens_login_page(monkeypatch):
    opened = []
    monkeypatch.setattr(agent_launcher.webbrowser, 'open', lambda url: opened.append(url))
    message = agent_launcher.prepare('webchat_chatgpt')
    assert opened == ['https://chatgpt.com']
    assert '登录' in message


def test_installed_cli_opens_terminal(monkeypatch):
    opened = []
    monkeypatch.setattr(agent_launcher, '_which', lambda names: 'C:/tools/claude.cmd')
    monkeypatch.setattr(agent_launcher, '_open_console', lambda command: opened.append(command))
    message = agent_launcher.prepare('claude')
    assert opened == ['C:/tools/claude.cmd']
    assert '登录' in message


def test_custom_sources_explain_configuration():
    for provider in ('custom', 'acp', 'mcp'):
        message = agent_launcher.prepare(provider)
        assert '不需要下载' in message


def test_missing_unknown_source_fails_clearly(monkeypatch):
    monkeypatch.setattr(agent_launcher, '_which', lambda names: '')
    monkeypatch.setattr(agent_launcher, '_desktop', lambda provider: '')
    try:
        agent_launcher.prepare('zcode')
    except RuntimeError as exc:
        assert '没有检测到' in str(exc)
    else:
        raise AssertionError('unknown source should not silently succeed')


def test_npm_metadata_requires_registry_integrity(monkeypatch):
    class Result:
        returncode = 0
        stdout = '{"version": "1.2.3", "dist.integrity": "sha512-abc"}'
        stderr = ''

    monkeypatch.setattr(agent_launcher.shutil, 'which', lambda names: 'npm')
    monkeypatch.setattr(agent_launcher.subprocess, 'run', lambda *a, **k: Result())
    assert agent_launcher._npm_metadata('@example/tool') == ('1.2.3', 'sha512-abc')

    class Bad(Result):
        stdout = '{"version": "1.2.3", "dist.integrity": ""}'

    monkeypatch.setattr(agent_launcher.subprocess, 'run', lambda *a, **k: Bad())
    try:
        agent_launcher._npm_metadata('@example/tool')
    except RuntimeError as exc:
        assert 'integrity' in str(exc)
    else:
        raise AssertionError('missing npm integrity was accepted')
