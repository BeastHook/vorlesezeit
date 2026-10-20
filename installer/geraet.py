"""Bildschirm 1: Gerät prüfen (Architektur, Platz, Arbeitsspeicher, TLS-Abfangen).

Ein Firmen-Proxy, der TLS aufbricht, legt für login.tonies.com ein eigenes
Zertifikat vor. Der Container vertraut ihm nicht, jede Toniecloud-Anmeldung
würde scheitern, und das Familienpasswort liefe über den Proxy (Known Pitfall
Zscaler). Erkannt wird das an einem Zertifikatsfehler mit dem
Standard-Kontext; der Aussteller kommt aus einer zweiten Verbindung ohne
Prüfung und dient nur der Anzeige.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cryptography import x509
from cryptography.x509.oid import NameOID

TONIES_HOST = "login.tonies.com"
MIN_FREI_BYTES = 5 * 10**9
MIN_RAM_BYTES = 2 * 10**9
_ARCHITEKTUREN = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


@dataclass(frozen=True)
class Messung:
    maschine: str
    frei_bytes: int
    ram_bytes: int


@dataclass(frozen=True)
class TlsErgebnis:
    art: Literal["ok", "proxy", "offline"]
    aussteller: str | None = None


def architektur(maschine: str) -> str | None:
    return _ARCHITEKTUREN.get(maschine.lower())


def messen(install_dir: Path) -> Messung:
    ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    return Messung(platform.machine(), shutil.disk_usage(install_dir).free, ram)


def tls_pruefen(host: str = TONIES_HOST, port: int = 443, timeout: float = 10) -> TlsErgebnis:
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ssl.create_default_context().wrap_socket(sock, server_hostname=host):
                return TlsErgebnis("ok")
    except ssl.SSLCertVerificationError:
        return TlsErgebnis("proxy", aussteller=_aussteller(host, port, timeout))
    except OSError:
        return TlsErgebnis("offline")


def _aussteller(host: str, port: int, timeout: float) -> str | None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except OSError:
        return None
    issuer = x509.load_der_x509_certificate(der).issuer
    namen = issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
    return str(namen[0].value) if namen else issuer.rfc4514_string()
