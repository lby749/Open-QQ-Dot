"""Self-signed TLS material for the local vNext bridge.

This is deliberately a local-development certificate, not a public CA.  It is
generated on first use and can be pinned by a phone client later.  The private key
is written with owner-only permissions.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import os
from pathlib import Path


def certificate_fingerprint(directory: Path) -> str:
    """Public SHA-256 of the DER certificate; never read or expose its private key."""
    import hashlib
    import ssl
    path = Path(directory) / 'bridge-cert.pem'
    if not path.is_file():
        return ''
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(path.read_text('ascii'))).hexdigest()

def ensure_certificate(directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    cert_path = directory / 'bridge-cert.pem'
    key_path = directory / 'bridge-key.pem'
    if cert_path.is_file() and key_path.is_file():
        return cert_path, key_path

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, 'Open QQ Dot Local Bridge'),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'Open QQ Dot'),
    ])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName('localhost'),
            x509.IPAddress(ipaddress.ip_address('127.0.0.1')),
            x509.IPAddress(ipaddress.ip_address('::1')),
            x509.IPAddress(ipaddress.ip_address('10.0.2.2')),
        ]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=True,
                                     data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                     crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return cert_path, key_path
