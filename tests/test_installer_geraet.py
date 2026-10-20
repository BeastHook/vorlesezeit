"""One-Klick-Setup Baustein 3: Geräteprüfung, TLS gegen einen lokalen Server."""

from __future__ import annotations

import datetime
import socket
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from installer.geraet import architektur, messen, tls_pruefen


@pytest.fixture
def proxy_server(tmp_path):
    """TLS-Server mit eigenem Zertifikat, wie ihn ein Firmen-Proxy vorlegt."""
    key = ec.generate_private_key(ec.SECP256R1())
    aussteller = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Zscaler Intermediate Root CA")]
    )
    jetzt = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(aussteller)
        .issuer_name(aussteller)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(jetzt - datetime.timedelta(days=1))
        .not_valid_after(jetzt + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    (tmp_path / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    server = socket.create_server(("127.0.0.1", 0))
    server.settimeout(5)

    def annehmen():
        for _ in range(2):
            try:
                conn, _ = server.accept()
            except OSError:
                return
            try:
                with ctx.wrap_socket(conn, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                pass

    thread = threading.Thread(target=annehmen, daemon=True)
    thread.start()
    yield server.getsockname()[1]
    server.close()


def test_fremdes_zertifikat_ist_ein_proxy_mit_aussteller(proxy_server):
    ergebnis = tls_pruefen("127.0.0.1", proxy_server, timeout=5)
    assert ergebnis.art == "proxy"
    assert ergebnis.aussteller == "Zscaler Intermediate Root CA"


def test_nicht_erreichbar_ist_offline():
    frei = socket.create_server(("127.0.0.1", 0))
    port = frei.getsockname()[1]
    frei.close()
    assert tls_pruefen("127.0.0.1", port, timeout=2).art == "offline"


@pytest.mark.parametrize(
    ("maschine", "arch"),
    [
        ("x86_64", "amd64"),
        ("AMD64", "amd64"),
        ("aarch64", "arm64"),
        ("arm64", "arm64"),
        ("armv7l", None),
    ],
)
def test_architektur(maschine, arch):
    assert architektur(maschine) == arch


def test_messen_liefert_platz_und_speicher(tmp_path):
    m = messen(tmp_path)
    assert m.frei_bytes > 0
    assert m.ram_bytes > 0
    assert m.maschine
