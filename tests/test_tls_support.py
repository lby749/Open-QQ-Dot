import datetime as dt
from pathlib import Path

import pytest

from cryptography import x509

import tls_support


def test_generates_idempotent_local_certificate(tmp_path):
    cert, key = tls_support.ensure_certificate(tmp_path)
    assert cert.is_file() and key.is_file()
    first = cert.read_bytes()
    assert tls_support.ensure_certificate(tmp_path) == (cert, key)
    assert cert.read_bytes() == first
    parsed = x509.load_pem_x509_certificate(first)
    assert parsed.not_valid_after_utc > dt.datetime.now(dt.timezone.utc)
    names = parsed.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert 'localhost' in names.get_values_for_type(x509.DNSName)
