"""Magic-Link-Tokens: signiert, zustandslos (U3, KTD6).

Ein Token kodiert nur {person_id, access_version} -- keine eigene
Ablaufzeit. Gueltigkeit ist ein konfigurierbares Enddatum (seit
Mehrkalender U5 aus dem Einstellungsdienst, ohne Neustart wirksam), kein
rollendes Fenster: innerhalb der Frist ist
der Link beliebig oft nutzbar. Widerruf (R39) laeuft ueber denselben Zaehler
wie die Sitzung (app/auth/session.py): erhoeht der Admin
Person.access_version, weicht das im Token kodierte access_version vom
gespeicherten Stand ab, und der Link ist entwertet -- ohne eigene
Token-Tabelle.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy.orm import Session

from app import settings
from app.config import Config
from app.models import Person

# Name aus der Zeit vor der Umbenennung (2026-10-06); nie aendern: ein neues
# Salz entwertet alle ausgestellten Magic-Links und Sitzungen.
_SALT = "toniapply-magic-link"


class InvalidMagicLinkToken(Exception):
    """Token ist nicht lesbar (manipuliert, falsch signiert, unbekannte Person)."""


class ExpiredMagicLinkToken(Exception):
    """Token ist grundsaetzlich gueltig, aber die Frist ist abgelaufen (KTD6)."""


class RevokedMagicLinkToken(Exception):
    """Token ist grundsaetzlich gueltig, aber der Admin hat den Zugang widerrufen (R39)."""


def _serializer(config: Config) -> URLSafeSerializer:
    return URLSafeSerializer(config.session_secret_key, salt=_SALT)


def create_magic_link_token(config: Config, person: Person) -> str:
    return _serializer(config).dumps(
        {"person_id": person.id, "access_version": person.access_version}
    )


def verify_magic_link_token(config: Config, session: Session, token: str) -> Person:
    """Prueft Signatur, Frist (KTD6) und Widerruf (R39). Erzeugt noch keine
    Sitzung -- das macht erst das Absenden der Bestaetigungsseite."""
    try:
        payload = _serializer(config).loads(token)
    except BadSignature as exc:
        raise InvalidMagicLinkToken() from exc

    person = session.get(Person, payload.get("person_id"))
    if person is None:
        raise InvalidMagicLinkToken()

    # R29: die Frist endet um Mitternacht in Europe/Berlin, nicht nach der
    # Serveruhr (im Container UTC).
    today = datetime.now(ZoneInfo(config.timezone)).date()
    if today > settings.magic_link_valid_until(session):
        raise ExpiredMagicLinkToken()

    if payload.get("access_version") != person.access_version:
        raise RevokedMagicLinkToken()

    return person
