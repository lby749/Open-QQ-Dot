import pytest

from health_bridge import HealthBridge, HealthEvent


def bridge(tmp_path):
    return HealthBridge(tmp_path / 'health.json')


def event(**changes):
    base = {'adapter': 'health_connect', 'event_type': 'wake', 'occurred_at': '2026-01-01T07:00:00+08:00',
            'scope': 'sleep', 'value': {'source': 'test'}}
    base.update(changes)
    return HealthEvent(**base)


def test_nothing_supported_without_real_report(tmp_path):
    b = bridge(tmp_path)
    status = b.status()
    assert all(item['supported'] is False for item in status['adapters'])
    assert status['has_real_events'] is False
    with pytest.raises(ValueError):
        b.ingest(event())


def test_report_authorize_ingest_and_dedupe(tmp_path):
    b = bridge(tmp_path)
    b.report('health_connect', supported=True, note='reported by real device')
    b.authorize('health_connect', ('sleep',))
    result = b.ingest(event())
    assert result['accepted'] is True
    duplicate = b.ingest(event())
    assert duplicate['accepted'] is False
    assert b.status()['event_count'] == 1
    assert b.status()['has_real_events'] is True


def test_unauthorized_scope_and_unknown_adapter_rejected(tmp_path):
    b = bridge(tmp_path)
    b.report('health_connect', supported=True)
    b.authorize('health_connect', ('steps',))
    with pytest.raises(ValueError):
        b.ingest(event(scope='sleep'))
    with pytest.raises(ValueError):
        b.report('unknown-brand', supported=True)
    with pytest.raises(ValueError):
        b.authorize('xiaomi', ('sleep',))


def test_revoke_and_purge(tmp_path):
    b = bridge(tmp_path)
    b.report('health_connect', supported=True)
    b.authorize('health_connect', ('sleep',))
    b.ingest(event())
    assert b.purge('health_connect') == 1
    assert b.events() == []
    with pytest.raises(ValueError):
        b.ingest(event())
    assert b.status()['adapters'][0]['authorized_scopes'] == []


def test_persistence_contains_no_raw_phone_records(tmp_path):
    path = tmp_path / 'health.json'
    b = HealthBridge(path)
    b.report('health_connect', supported=True)
    b.authorize('health_connect', ('sleep',))
    b.ingest(event())
    raw = path.read_text('utf-8')
    assert 'source' in raw  # minimal normalized value only
    assert 'raw_payload' not in raw
