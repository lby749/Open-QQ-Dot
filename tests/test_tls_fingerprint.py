import hashlib
import ssl

from tls_support import certificate_fingerprint, ensure_certificate


def test_fingerprint_is_per_data_home_and_does_not_change_existing_keys(tmp_path):
    first, key = ensure_certificate(tmp_path/'home_a')
    key_hash = hashlib.sha256(key.read_bytes()).hexdigest()
    expected = hashlib.sha256(ssl.PEM_cert_to_DER_cert(first.read_text('ascii'))).hexdigest()
    assert certificate_fingerprint(tmp_path/'home_a') == expected
    assert len(expected) == 64
    ensure_certificate(tmp_path/'home_a')
    assert hashlib.sha256(key.read_bytes()).hexdigest() == key_hash
    ensure_certificate(tmp_path/'home_b')
    assert certificate_fingerprint(tmp_path/'home_b') != expected
    assert certificate_fingerprint(tmp_path/'not_started') == ''
