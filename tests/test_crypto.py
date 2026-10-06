"""Mehrkalender U3/KTD4/KTD5: Fernet-Verschluesselung und HMAC-Fingerabdruck.

Alle Werte sind erfundene Platzhalter; kein Test gibt einen Klartext aus.
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from app.crypto import CredentialsUnavailable, decrypt, encrypt, fingerprint, key_usable

KEY_A = base64.urlsafe_b64encode(b"0" * 32).decode()
KEY_B = base64.urlsafe_b64encode(b"1" * 32).decode()
PLAIN = "platzhalter-passwort"


def test_roundtrip_with_same_key():
    token = encrypt(KEY_A, PLAIN)
    assert PLAIN not in token
    assert decrypt(KEY_A, token) == PLAIN


@pytest.mark.parametrize("key", [KEY_B, None, "", "kein-fernet-schluessel"])
def test_decrypt_with_wrong_missing_or_malformed_key_is_unavailable(key):
    token = encrypt(KEY_A, PLAIN)
    with pytest.raises(CredentialsUnavailable) as excinfo:
        decrypt(key, token)
    assert PLAIN not in str(excinfo.value)


@pytest.mark.parametrize("key", [None, "", "kein-fernet-schluessel"])
def test_encrypt_without_usable_key_is_unavailable(key):
    assert key_usable(key) is False
    with pytest.raises(CredentialsUnavailable):
        encrypt(key, PLAIN)


def test_garbage_token_is_unavailable():
    with pytest.raises(CredentialsUnavailable):
        decrypt(KEY_A, "kein-token")


def test_fingerprint_is_keyed_and_not_a_plain_hash():
    fp = fingerprint(KEY_A, PLAIN)
    assert fp == fingerprint(KEY_A, PLAIN)
    assert fp != fingerprint(KEY_B, PLAIN)
    assert fp != hashlib.sha256(PLAIN.encode()).hexdigest()
    assert PLAIN not in fp


def test_fingerprint_without_key_is_none():
    assert fingerprint(None, PLAIN) is None
    assert fingerprint("kein-fernet-schluessel", PLAIN) is None
