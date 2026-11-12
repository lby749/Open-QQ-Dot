import zipfile

import diagnostics


def test_redact_hides_tokens_and_bearer_values():
    text = 'token="abcdef" Authorization: Bearer abc.def\npassword=hunter2'
    cleaned = diagnostics.redact(text)
    assert "abcdef" not in cleaned
    assert "abc.def" not in cleaned
    assert "hunter2" not in cleaned
    assert "<redacted>" in cleaned


def test_export_bundle_contains_report_and_redacted_logs(tmp_path):
    home = tmp_path / "app"
    home.mkdir()
    (home / "bridge.log").write_text("token=secret-value\nmessage ok", "utf-8")
    target = tmp_path / "diag.zip"
    result = diagnostics.export_bundle(home, target)
    assert result["logs"] == ["bridge.log"]
    with zipfile.ZipFile(target) as package:
        assert "report.json" in package.namelist()
        body = package.read("logs/bridge.log").decode("utf-8")
    assert "secret-value" not in body
    assert "message ok" in body
