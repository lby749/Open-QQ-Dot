import house_web


def test_funnel_header_detected_case_insensitive():
    assert house_web.funnel_request({'Tailscale-Funnel-Request': 'true'})
    assert house_web.funnel_request({'tailscale-funnel-request': ''})
    assert not house_web.funnel_request({'X-Forwarded-For': '1.2.3.4'})


def test_funnel_denied_by_default(monkeypatch):
    monkeypatch.delenv('QQ_ALLOW_FUNNEL', raising=False)
    assert house_web.funnel_allowed() is False
    monkeypatch.setenv('QQ_ALLOW_FUNNEL', '1')
    assert house_web.funnel_allowed() is True
