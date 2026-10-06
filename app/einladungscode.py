"""Einladungscode für befreundete Instanzen (One-Klick-Setup, Baustein 2).

Das Einladungsskript (`scripts/einladen`) erzeugt ihn beim Organisator, der
Installer liest ihn beim Freund. Er trägt Geheimnisse (Tunnel-Token,
SMTP-Schlüssel): nie loggen, nie in Meldungen übernehmen.

Base32 statt Base64url: Messenger deuten `_text_` als Kursivschrift und
verlieren beim Kopieren die Unterstriche; Base32 kennt nur A-Z und 2-7 und
verträgt Kleinschreibung. zlib hält den Code trotzdem kurz.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import zlib
from dataclasses import asdict, dataclass

PREFIX = "VZ1-"
VERSION = 1

_HOST = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
_ANDERE_VERSION = re.compile(r"^VZ\d+-")


class EinladungscodeError(ValueError):
    """Ein Satz für den Freund, ohne Inhalt aus dem Code."""


@dataclass(frozen=True)
class SmtpZugang:
    host: str
    port: int
    user: str
    key: str
    absender: str


@dataclass(frozen=True)
class Einladung:
    host: str
    tunnel_token: str
    smtp: SmtpZugang | None


def encode(einladung: Einladung) -> str:
    data = {
        "v": VERSION,
        "host": einladung.host,
        "tunnel_token": einladung.tunnel_token,
        "smtp": asdict(einladung.smtp) if einladung.smtp else None,
    }
    raw = zlib.compress(json.dumps(data, separators=(",", ":")).encode(), 9)
    return PREFIX + base64.b32encode(raw).decode().rstrip("=")


def decode(code: str) -> Einladung:
    compact = re.sub(r"\s+", "", code).strip("\"'`“”„").upper()
    if not compact.startswith(PREFIX):
        if _ANDERE_VERSION.match(compact):
            raise EinladungscodeError(
                "Dieser Einladungscode gehört zu einer neueren Version. "
                "Bitte lade die Startdatei neu herunter."
            )
        raise EinladungscodeError("Das ist kein Einladungscode. Er beginnt mit VZ1-.")
    body = compact[len(PREFIX) :]
    try:
        raw = base64.b32decode(body + "=" * (-len(body) % 8))
        data = json.loads(zlib.decompress(raw))
    except (binascii.Error, ValueError, zlib.error):
        raise EinladungscodeError(
            "Der Einladungscode ist unvollständig. Bitte kopiere ihn noch einmal ganz."
        ) from None
    return _pruefe(data)


def _pruefe(data: object) -> Einladung:
    ungueltig = EinladungscodeError("Der Einladungscode passt nicht. Bitte frag nach einem neuen.")
    if not isinstance(data, dict) or data.get("v") != VERSION:
        raise ungueltig
    host, token, smtp = data.get("host"), data.get("tunnel_token"), data.get("smtp")
    if not isinstance(host, str) or not _HOST.match(host):
        raise ungueltig
    if not isinstance(token, str) or not token:
        raise ungueltig
    if smtp is None:
        return Einladung(host=host, tunnel_token=token, smtp=None)
    if not isinstance(smtp, dict):
        raise ungueltig
    try:
        zugang = SmtpZugang(**smtp)
    except TypeError:
        raise ungueltig from None
    if not isinstance(zugang.port, int) or not all(
        isinstance(v, str) and v for v in (zugang.host, zugang.user, zugang.key, zugang.absender)
    ):
        raise ungueltig
    return Einladung(host=host, tunnel_token=token, smtp=zugang)
