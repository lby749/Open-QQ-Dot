import json
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import dotlink as dn


def write_runtime(root):
    runtime = root / "napcat" / "runtime"
    (runtime / "napcat").mkdir(parents=True)
    (runtime / "node.exe").write_bytes(b"stub")
    (runtime / "index.js").write_text("const path = require('path');\nprocess.env.NAPCAT_DISABLE_PIPE = '1';\n",
                                      encoding="utf-8")
    (runtime / "napcat" / "napcat.mjs").write_text(
        "let e = this.context.wrapper.NodeQQNTWrapperUtil.getNTUserDataInfoConfig();\n", encoding="utf-8")
    return runtime


def test_prepare_creates_private_state_and_tokens(tmp_path):
    runtime = write_runtime(tmp_path)
    original = (runtime / "index.js").read_bytes()
    summary = dn.prepare(tmp_path)
    assert summary["ws_url"] == "ws://127.0.0.1:3001"
    settings = json.loads((tmp_path / "dotlink" / "local.json").read_text("utf-8"))
    assert settings["access_token"] and settings["webui_token"]
    assert settings["runtime_path"]
    webui = json.loads((tmp_path / "dotlink" / "state" / "config" / "webui.json").read_text("utf-8"))
    assert webui["token"] == settings["webui_token"] and webui["host"] == "127.0.0.1"
    onebot = json.loads((tmp_path / "dotlink" / "state" / "config" / "onebot11.json").read_text("utf-8"))
    assert onebot["network"]["websocketServers"][0]["token"] == settings["access_token"]
    assert summary["runtime_untouched"] is True
    assert (runtime / "index.js").read_bytes() == original


def test_prepare_never_reuses_another_install_token(tmp_path):
    write_runtime(tmp_path)
    old = tmp_path / "napcat" / "state" / "config"
    old.mkdir(parents=True, exist_ok=True)
    (old / "webui.json").write_text(json.dumps({"token": "old-management"}), "utf-8")
    (tmp_path / "napcat.local.json").write_text(json.dumps({"access_token": "old-access"}), "utf-8")
    summary = dn.prepare(tmp_path)
    settings = json.loads((tmp_path / "dotlink" / "local.json").read_text("utf-8"))
    assert settings["access_token"] != "old-access"
    assert settings["webui_token"] != "old-management"
    assert "999999" not in summary["accounts"]


class StubRuntime(BaseHTTPRequestHandler):
    calls = []
    quick = [{"uin": "123456", "nickName": "小明", "faceUrl": ""}]
    logged_in = False
    credential = 'cred-1'

    def log_message(self, *args):
        pass

    def _reply(self, data):
        body = json.dumps({"code": 0, "data": data, "message": "success"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}") if length else {}

    def do_POST(self):
        body = self._body()
        StubRuntime.calls.append((self.path, self.headers.get("Authorization"), body))
        if self.path == "/api/auth/login":
            assert len(body["hash"]) == 64
            return self._reply({"Credential": self.credential})
        if self.headers.get("Authorization") != "Bearer " + self.credential:
            self.send_response(401)
            self.end_headers()
            return
        if self.path == "/api/QQLogin/GetQuickLoginListNew":
            return self._reply(self.quick)
        if self.path == "/api/QQLogin/CheckLoginStatus":
            return self._reply({"isLogin": self.logged_in, "loginPhase": "ready" if self.logged_in else "waiting_qrcode",
                                "coreReady": self.logged_in, "qrcodeurl": "" if self.logged_in else "https://txz.qq.com/p?k=abc&f=1",
                                "loginError": ""})
        if self.path == "/api/QQLogin/GetQQLoginInfo":
            return self._reply({"uin": "123456", "nick": "小明", "online": True})
        if self.path == "/api/QQLogin/GetQQLoginQrcode":
            return self._reply({"qrcode": "https://txz.qq.com/p?k=abc&f=1"})
        if self.path == "/api/QQLogin/SetQuickLogin":
            assert body["uin"] == "123456"
            return self._reply(None)
        if self.path == "/api/QQLogin/RefreshQRcode":
            return self._reply({"qrcodeurl": "https://txz.qq.com/p?k=refreshed&f=1"})
        if self.path == "/api/QQLogin/PasswordLogin":
            assert body["uin"] == "123456"
            assert body["passwordMd5"] == hashlib.md5("hunter2".encode()).hexdigest()
            StubRuntime.logged_in = True
            return self._reply(None)
        self._reply(None)


@pytest.fixture
def stub():
    StubRuntime.calls = []
    StubRuntime.logged_in = False
    StubRuntime.credential = 'cred-1'
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubRuntime)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_webui_login_quick_accounts_and_qr(tmp_path, stub):
    (tmp_path / "dotlink" / "state" / "config").mkdir(parents=True)
    (tmp_path / "dotlink" / "local.json").write_text(json.dumps({"webui_token": "secret", "webui_port": stub}), "utf-8")
    client = dn.WebUIClient(stub, "secret")
    client.login()
    assert client.quick_accounts()[0]["uin"] == "123456"
    assert client.status()["loginPhase"] == "waiting_qrcode"
    client.quick_login("123456")
    assert StubRuntime.calls[-1][1] == "Bearer cred-1"
    image = dn.qr_image(dn.Paths(tmp_path), client)
    assert image["data_url"].startswith("data:image/png;base64,")
    assert (tmp_path / "dotlink" / "state" / "cache" / "qrcode.png").is_file()


def test_state_reports_login_without_reading_an_old_runtime(tmp_path, stub):
    write_runtime(tmp_path)
    config = tmp_path / "dotlink" / "state" / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "webui.json").write_text(json.dumps({"host": "127.0.0.1", "port": stub, "token": "secret"}), "utf-8")
    (tmp_path / "dotlink" / "local.json").write_text(json.dumps({"webui_token": "secret", "webui_port": stub}), "utf-8")
    (tmp_path / "dotlink" / "runtime.out.log").write_text(
        f"WebUi User Panel Url: http://127.0.0.1:{stub}/webui?token=secret\n", "utf-8")
    StubRuntime.logged_in = True
    value = dn.state(tmp_path)
    assert value["webui_port"] == stub
    assert value["login"]["is_login"] is True and value["login"]["uin"] == "123456"
    assert value["accounts"][0]["nickname"] == "小明"


def test_client_settings_live_under_dotlink_only(tmp_path, monkeypatch):
    monkeypatch.setenv("QQ_BRIDGE_HOME", str(tmp_path))
    (tmp_path / "dotlink").mkdir()
    (tmp_path / "dotlink" / "local.json").write_text(json.dumps({"ws_url": "ws://127.0.0.1:3999",
                                                                  "access_token": "fresh"}), "utf-8")
    import qq_link
    assert qq_link.local_settings(tmp_path)["access_token"] == "fresh"


def test_password_login_hashes_and_refreshes_qr(tmp_path, stub):
    (tmp_path / "dotlink" / "state" / "config").mkdir(parents=True)
    (tmp_path / "dotlink" / "local.json").write_text(json.dumps({"webui_token": "secret", "webui_port": stub}), "utf-8")
    client = dn.WebUIClient(stub, "secret")
    client.login()
    client.password_login("123456", "hunter2")
    assert StubRuntime.logged_in is True
    assert StubRuntime.calls[-1][2]["passwordMd5"] == hashlib.md5(b"hunter2").hexdigest()
    refreshed = client.refresh_qr()
    assert "refreshed" in refreshed


def test_status_polling_reuses_management_credential(stub):
    for _ in range(5):
        client = dn.WebUIClient(stub, "poll-secret")
        client.login()
        assert not client.status()['isLogin']
    assert sum(path == '/api/auth/login' for path, _, _ in StubRuntime.calls) == 1


def test_management_credential_is_scoped_to_auth_token(stub):
    for token in ['first-token', 'second-token', 'first-token']:
        client = dn.WebUIClient(stub, token)
        client.login()
        client.status()
    assert sum(path == '/api/auth/login' for path, _, _ in StubRuntime.calls) == 2


def test_runtime_session_rejection_invalidates_cache_without_replaying_action(stub):
    client = dn.WebUIClient(stub, 'restart-secret')
    client.login()
    StubRuntime.credential = 'cred-after-restart'
    with pytest.raises(dn.DotLinkError, match='HTTP 401'):
        client.quick_login('123456')
    assert not StubRuntime.logged_in
    assert sum(path == '/api/QQLogin/SetQuickLogin' for path, _, _ in StubRuntime.calls) == 1
    next_poll = dn.WebUIClient(stub, 'restart-secret')
    next_poll.login()
    assert not next_poll.status()['isLogin']
    assert sum(path == '/api/auth/login' for path, _, _ in StubRuntime.calls) == 2


def test_existing_runtime_can_be_adopted_without_download(tmp_path, monkeypatch):
    source = tmp_path / "existing-napcat"
    (source / "napcat").mkdir(parents=True)
    (source / "node.exe").write_bytes(b"node")
    (source / "index.js").write_text("console.log('napcat')\n", encoding="utf-8")
    monkeypatch.setenv("OPENQQDOT_NAPCAT_RUNTIME", str(source))
    home = tmp_path / "home"
    home.mkdir()
    found = dn.find_existing_runtimes(home)
    assert found and found[0] == source
    target = dn.adopt_runtime(home, source)
    assert dn.runtime_ready(dn.Paths(home))
    assert target.resolve() == source.resolve()
    meta = json.loads((home / "dotlink" / "runtime-source.json").read_text("utf-8"))
    assert meta["adopted"] is True and meta["copied"] is False
    assert not (target / "NAPCAT-LICENSE.txt").exists()


def test_adoption_uses_canonical_source_and_replaces_a_dangling_link(tmp_path):
    import os
    import subprocess
    source, alias, stale, home = (tmp_path/name for name in ('vendor', 'alias', 'stale', 'home'))
    source.mkdir()
    (source/'node.exe').write_bytes(b'synthetic runtime')
    (source/'index.js').write_text('// synthetic runtime', encoding='utf-8')
    stale.mkdir()
    home.mkdir()
    def link(target, destination):
        if os.name == 'nt':
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(destination), str(target)], check=True, capture_output=True)
        else:
            destination.symlink_to(target, target_is_directory=True)
    link(source, alias)
    link(stale, home/'qq-runtime')
    stale.rmdir()
    result = dn.adopt_runtime(home, alias)
    metadata = json.loads((home/'dotlink/runtime-source.json').read_text('utf-8'))
    assert metadata['source'] == str(source.resolve())
    assert result.resolve() == source.resolve()
    assert (source/'node.exe').read_bytes() == b'synthetic runtime'


def test_startup_reuses_saved_account_without_requesting_a_new_login(tmp_path, monkeypatch):
    monkeypatch.setattr(dn, 'prepare', lambda _: {})
    monkeypatch.setattr(dn, 'port_open', lambda _: False)
    monkeypatch.setattr(dn, '_settings', lambda _: {'last_account': '123456'})
    starts = []
    monkeypatch.setattr(dn, 'start', lambda paths, **kwargs: starts.append(kwargs))
    assert dn.ensure_started(tmp_path) == ''
    assert starts == [{'account': '123456', 'wait': True}]


def test_start_reuses_bundled_vc_runtime_only_in_child(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace
    runtime = write_runtime(tmp_path)
    qt = tmp_path / 'PySide6'
    qt.mkdir()
    (qt / 'vcruntime140.dll').write_bytes(b'fixture runtime')
    monkeypatch.setattr(dn.importlib.util, 'find_spec', lambda name: SimpleNamespace(origin=str(qt / '__init__.py')))
    monkeypatch.setattr(dn, 'ascii_root', lambda _: str(tmp_path))
    monkeypatch.setattr(dn, 'discover_port', lambda _: 0)
    original = os.environ.get('PATH', '')
    calls = []
    def spawn(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(pid=12345)
    monkeypatch.setattr(dn.subprocess, 'Popen', spawn)
    dn.start(tmp_path, wait=False)
    assert calls[-1]['env']['PATH'].split(os.pathsep)[0] == str(qt)
    assert os.environ.get('PATH', '') == original
