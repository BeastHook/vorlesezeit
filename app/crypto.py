"""Mehrkalender U3 (KTD4/KTD5): Passwoerter als Fernet-Token, Schluessel aus
`CREDENTIALS_KEY`.

Fehlt der Schluessel, ist er unbrauchbar oder passt er nicht zum Token, gibt es
genau einen Zustand: `CredentialsUnavailable` -- der Einstellungsdienst macht
daraus "neu eingeben" (R21), nie einen Absturz. Keine Meldung traegt je den
Klartext oder den Schluessel (R20). Bewusst kein AAD und keine Rotation.
"""

from __future__ import annotations

import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken


class CredentialsUnavailable(Exception):
    """Schluessel fehlt/unbrauchbar oder Token passt nicht -> "neu eingeben"."""


def _fernet(key: str | None) -> Fernet:
    if not key:
        raise CredentialsUnavailable("CREDENTIALS_KEY fehlt")
    try:
        return Fernet(key)
    except (ValueError, TypeError):
        # Kein `from exc`: die Ursache koennte den Schluessel zitieren.
        raise CredentialsUnavailable("CREDENTIALS_KEY ist kein Fernet-Schluessel") from None


def key_usable(key: str | None) -> bool:
    try:
        _fernet(key)
    except CredentialsUnavailable:
        return False
    return True


def encrypt(key: str | None, plaintext: str) -> str:
    return _fernet(key).encrypt(plaintext.encode()).decode()


def decrypt(key: str | None, token: str) -> str:
    fernet = _fernet(key)
    try:
        return fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        raise CredentialsUnavailable("Token passt nicht zum CREDENTIALS_KEY") from None


def fingerprint(key: str | None, value: str) -> str | None:
    """KTD5: HMAC-SHA-256 mit dem Schluessel -- ohne ihn laesst sich ein
    geratener Klartext nicht gegen den Fingerabdruck pruefen. Ohne brauchbaren
    Schluessel gibt es keinen."""
    if not key_usable(key):
        return None
    return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()
