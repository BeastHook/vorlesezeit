"""One-Klick-Setup Baustein 2: Einladungscode (Spec 2026-10-04)."""

from __future__ import annotations

import base64
import json
import zlib

import pytest

from app.einladungscode import (
    PREFIX,
    Einladung,
    EinladungscodeError,
    SmtpZugang,
    decode,
    encode,
)

TOKEN = "eyJhIjoiZmFrZS1hY2NvdW50IiwidCI6ImZha2UtdHVubmVsIiwicyI6ImZha2UifQ"
SMTP = SmtpZugang(
    host="smtp-relay.brevo.com",
    port=587,
    user="9a1b2c@smtp-brevo.com",
    key="xsmtpsib-fake-schluessel",
    absender="mueller@vorlesezeit.app",
)
EINLADUNG = Einladung(host="mueller.vorlesezeit.app", tunnel_token=TOKEN, smtp=SMTP)


def _roh(data: dict) -> str:
    raw = zlib.compress(json.dumps(data).encode())
    return PREFIX + base64.b32encode(raw).decode().rstrip("=")


def test_roundtrip_mit_smtp():
    assert decode(encode(EINLADUNG)) == EINLADUNG


def test_roundtrip_ohne_smtp():
    ohne = Einladung(host="mueller.vorlesezeit.app", tunnel_token=TOKEN, smtp=None)
    assert decode(encode(ohne)) == ohne


def test_code_enthaelt_nur_messenger_feste_zeichen():
    code = encode(EINLADUNG)
    assert code.startswith("VZ1-")
    assert set(code[len(PREFIX) :]) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def test_umbrueche_leerzeichen_anfuehrungszeichen_und_kleinschreibung():
    code = encode(EINLADUNG)
    verbeult = '"' + code[:20] + "\n  " + code[20:60].lower() + " \r\n" + code[60:] + '"\n'
    assert decode(verbeult) == EINLADUNG


@pytest.mark.parametrize(
    ("code", "meldung"),
    [
        ("", "kein Einladungscode"),
        ("hallo", "kein Einladungscode"),
        ("VZ2-ABCD", "neueren Version"),
        ("VZ1-", "unvollständig"),
        ("VZ1-!!!!", "unvollständig"),
    ],
)
def test_kaputte_codes_melden_einen_satz(code, meldung):
    with pytest.raises(EinladungscodeError, match=meldung):
        decode(code)


def test_abgeschnittener_code():
    code = encode(EINLADUNG)
    with pytest.raises(EinladungscodeError, match="unvollständig"):
        decode(code[: len(code) // 2])


@pytest.mark.parametrize(
    "data",
    [
        {"v": 2, "host": "a.vorlesezeit.app", "tunnel_token": TOKEN, "smtp": None},
        {"v": 1, "host": "kein host", "tunnel_token": TOKEN, "smtp": None},
        {"v": 1, "host": "a.vorlesezeit.app", "tunnel_token": "", "smtp": None},
        {"v": 1, "host": "a.vorlesezeit.app", "tunnel_token": TOKEN, "smtp": {"host": "x"}},
        {
            "v": 1,
            "host": "a.vorlesezeit.app",
            "tunnel_token": TOKEN,
            "smtp": {"host": "x", "port": "587", "user": "u", "key": "k", "absender": "a@b.c"},
        },
        ["keine", "map"],
    ],
)
def test_falscher_inhalt(data):
    with pytest.raises(EinladungscodeError):
        decode(_roh(data))


def test_meldungen_enthalten_keine_geheimnisse():
    data = {"v": 1, "host": "kein host", "tunnel_token": TOKEN, "smtp": None}
    with pytest.raises(EinladungscodeError) as exc:
        decode(_roh(data))
    assert TOKEN not in str(exc.value)
    assert "kein host" not in str(exc.value)
