"""One-Klick-Setup Baustein 3: Dateien schreiben und Installationsschritte."""

from __future__ import annotations

import json
import logging
import re
import stat
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.fernet import Fernet

from app import __version__
from installer.antworten import Antworten, Mail, Sicherung
from installer.docker import Ergebnis
from installer.installation import (
    Lauf,
    _zeile,
    env_tauglich,
    lies_env,
    schreibe_dateien,
    smtp_entfernen,
)
from tests.installer_fakes import FakeUhr, fake_docker

VORLAGE = Path(__file__).resolve().parent.parent / "deploy/freunde/compose.yml"
PASSWORT = "ge'heim $PW # mit Leer"


def antworten(**aenderungen) -> Antworten:
    a = Antworten()
    a.host = "berger.vorlesezeit.app"
    a.tunnel_token = "eyJhIjoiZmFrZSJ9"
    a.admin_email = "anja@beispiel.de"
    a.anzeigename = "Anja"
    a.mail = Mail(
        host="smtp-relay.brevo.com",
        port=587,
        user="9a1b2c@smtp-brevo.com",
        passwort=PASSWORT,
        absender="berger@vorlesezeit.app",
        aus_code=True,
    )
    a.sicherung = Sicherung(pfad="/Volumes/Samsung T7/Vorlesezeit-Sicherung", name="Samsung T7")
    for k, v in aenderungen.items():
        setattr(a, k, v)
    return a


def pflichtvariablen() -> set[str]:
    return set(re.findall(r"\$\{([A-Z_]+):\?", VORLAGE.read_text()))


# --- Dateien -----------------------------------------------------------------

# Beobachtet mit echtem `docker compose --env-file ... config` (2026-10-18):
# Zeile in der .env -> Wert, den compose liest. In einfachen Anführungszeichen
# wird nur `\'` zu `'`; jeder andere Backslash bleibt wörtlich.
COMPOSE_LIEST = [
    ("A='ab\\'cd'", "ab'cd"),
    ("A='a\\b'", "a\\b"),
    ("B='a\\\\b'", "a\\\\b"),
    ("C='$x#y'", "$x#y"),
]


@pytest.mark.parametrize(("zeile", "wert"), COMPOSE_LIEST)
def test_zeile_schreibt_was_compose_so_liest(zeile, wert):
    name = zeile.split("=", 1)[0]
    assert _zeile(name, wert) == zeile + "\n"


@pytest.mark.parametrize(("zeile", "wert"), COMPOSE_LIEST)
def test_lies_env_liest_wie_compose(tmp_path, zeile, wert):
    (tmp_path / ".env").write_text(zeile + "\n")
    assert lies_env(tmp_path / ".env") == {zeile.split("=", 1)[0]: wert}


def test_lies_env_lehnt_wie_compose_einen_wert_mit_backslash_am_ende_ab(tmp_path):
    # compose: line 3: unexpected character "'" in variable name "danach'"
    (tmp_path / ".env").write_text("A='x'\nB='endet\\'\nC='danach'\n")
    with pytest.raises(ValueError):
        lies_env(tmp_path / ".env")


# Ebenfalls mit echtem compose beobachtet: `\` direkt vor `'` bricht die Datei.
@pytest.mark.parametrize(
    "zeile",
    [
        "V4='a\\\\'b'",  # line 5: unexpected character "'" in variable name "b'"
        "V5='x\\\\''",
    ],
)
def test_lies_env_lehnt_wie_compose_backslash_vor_anfuehrungszeichen_ab(tmp_path, zeile):
    (tmp_path / ".env").write_text(zeile + "\nC='danach'\n")
    with pytest.raises(ValueError):
        lies_env(tmp_path / ".env")


@pytest.mark.parametrize(
    "wert", ["ab'cd", "'", "''", "a\\\\b", "\\a", "x'\\y", "$PW # mit Leer", '`dq` "dq"', ""]
)
def test_rundreise_zeile_lies_env(tmp_path, wert):
    (tmp_path / ".env").write_text(_zeile("A", wert) + _zeile("B", "danach"))
    assert lies_env(tmp_path / ".env") == {"A": wert, "B": "danach"}


@pytest.mark.parametrize(
    "wert", ["endet\\", "\\", "a\\\\", "a\\'b", "x\\'", "\\'", "zwei\nzeilen", "a\rb"]
)
def test_nicht_speicherbare_werte(wert):
    assert not env_tauglich(wert)
    with pytest.raises(ValueError):
        _zeile("A", wert)


@pytest.mark.parametrize("wert", ["ab'cd", "a\\b", "x'\\y", "$x#y", ""])
def test_speicherbare_werte(wert):
    assert env_tauglich(wert)


def test_env_nur_fuer_den_besitzer_und_mit_jeder_pflichtvariable(tmp_path):
    schreibe_dateien(tmp_path, antworten())

    env_datei = tmp_path / ".env"
    assert stat.S_IMODE(env_datei.stat().st_mode) == 0o600
    env = lies_env(env_datei)
    assert pflichtvariablen() <= set(env)
    for name in ("CREDENTIALS_KEY", "ADMIN_DISPLAY_NAME", "SMTP_HOST", "SMTP_PORT", "SMTPUSER"):
        assert env[name]
    assert env["SMTPPW"] == PASSWORT
    assert env["SMTP_FROM_ADDRESS"] == "berger@vorlesezeit.app"
    assert env["VORLESEZEIT_IMAGE"] == f"ghcr.io/beasthook/vorlesezeit:{__version__}"
    assert env["BACKUP_TARGET_PATH"] == "/Volumes/Samsung T7/Vorlesezeit-Sicherung"
    assert env["CLOUDFLARE_TUNNEL_TOKEN"] == "eyJhIjoiZmFrZSJ9"
    assert env["ADMIN_EMAIL"] == "anja@beispiel.de"
    assert (tmp_path / "compose.yml").read_bytes() == VORLAGE.read_bytes()


def test_zwei_installationen_haben_verschiedene_geheimnisse(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    schreibe_dateien(tmp_path / "a", antworten())
    schreibe_dateien(tmp_path / "b", antworten())
    a, b = lies_env(tmp_path / "a/.env"), lies_env(tmp_path / "b/.env")
    for name in ("SESSION_SECRET_KEY", "TRIGGER_SECRET", "STORAGE_SECRET_KEY", "CREDENTIALS_KEY"):
        assert a[name] != b[name], name
    for name in ("SESSION_SECRET_KEY", "TRIGGER_SECRET", "STORAGE_SECRET_KEY"):
        assert len(a[name]) == 43, name  # token_urlsafe(32)


def test_credentials_key_ist_ein_fernet_schluessel(tmp_path):
    schreibe_dateien(tmp_path, antworten())
    key = lies_env(tmp_path / ".env")["CREDENTIALS_KEY"]
    f = Fernet(key.encode())
    assert f.decrypt(f.encrypt(b"probe")) == b"probe"


def test_installation_json_ohne_geheimnis(tmp_path):
    schreibe_dateien(tmp_path, antworten())
    daten = json.loads((tmp_path / "installation.json").read_text())
    assert daten == {
        "adresse": "berger.vorlesezeit.app",
        "admin_email": "anja@beispiel.de",
        "anzeigename": "Anja",
        "version": __version__,
        "abgeschlossen": False,
    }
    text = (tmp_path / "installation.json").read_text()
    env = lies_env(tmp_path / ".env")
    for name in (
        "STORAGE_SECRET_KEY",
        "SESSION_SECRET_KEY",
        "TRIGGER_SECRET",
        "CREDENTIALS_KEY",
        "CLOUDFLARE_TUNNEL_TOKEN",
        "SMTPPW",
    ):
        assert env[name] not in text, name


ERZEUGT = (
    "STORAGE_SECRET_KEY",
    "SESSION_SECRET_KEY",
    "TRIGGER_SECRET",
    "CREDENTIALS_KEY",
    "VORLESEZEIT_IMAGE",
    "STORAGE_ACCESS_KEY",
)


def test_vorhandene_env_bekommt_neue_antworten_und_behaelt_die_geheimnisse(tmp_path):
    schreibe_dateien(tmp_path, antworten())
    env = tmp_path / ".env"
    # Ein älteres Image in der .env: erst das Update (Baustein 4) wechselt es.
    env.write_text(env.read_text().replace(__version__, "0.0.9"))
    vorher = lies_env(env)

    neu = antworten(
        host="neu.vorlesezeit.app", tunnel_token="eyJuZXUiOjF9", admin_email="neu@beispiel.de"
    )
    schreibe_dateien(tmp_path, neu)

    nachher = lies_env(env)
    assert nachher["CLOUDFLARE_TUNNEL_TOKEN"] == "eyJuZXUiOjF9"
    assert nachher["ADMIN_EMAIL"] == "neu@beispiel.de"
    for name in ERZEUGT:
        assert nachher[name] == vorher[name], name
    assert nachher["VORLESEZEIT_IMAGE"] == "ghcr.io/beasthook/vorlesezeit:0.0.9"
    daten = json.loads((tmp_path / "installation.json").read_text())
    assert daten["adresse"] == "neu.vorlesezeit.app"
    assert daten["admin_email"] == "neu@beispiel.de"
    assert daten["abgeschlossen"] is False
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert not (tmp_path / ".env.neu").exists()


def test_gleiche_antworten_ergeben_eine_bytegleiche_env(tmp_path):
    schreibe_dateien(tmp_path, antworten())
    vorher = (tmp_path / ".env").read_bytes()
    schreibe_dateien(tmp_path, antworten())
    assert (tmp_path / ".env").read_bytes() == vorher


def test_nach_smtp_entfernen_keine_smtp_zeile_zurueck(tmp_path):
    schreibe_dateien(tmp_path, antworten())
    smtp_entfernen(tmp_path / ".env")
    vorher = (tmp_path / ".env").read_bytes()
    neue_mail = Mail("smtp.gmail.com", 465, "neu@gmail.com", "pw-neu", "neu@gmail.com")

    schreibe_dateien(tmp_path, antworten(mail=neue_mail))

    zeilen = (tmp_path / ".env").read_text().splitlines()
    assert not [z for z in zeilen if z.startswith("SMTP")]
    assert (tmp_path / ".env").read_bytes() == vorher


def test_solange_smtppw_steht_gelten_die_neuen_smtp_werte(tmp_path):
    schreibe_dateien(tmp_path, antworten())
    neue_mail = Mail("smtp.gmail.com", 465, "neu@gmail.com", "pw-neu", "absender@gmail.com")

    schreibe_dateien(tmp_path, antworten(mail=neue_mail))

    env = lies_env(tmp_path / ".env")
    assert env["SMTP_HOST"] == "smtp.gmail.com"
    assert env["SMTP_PORT"] == "465"
    assert env["SMTPUSER"] == "neu@gmail.com"
    assert env["SMTPPW"] == "pw-neu"
    assert env["SMTP_FROM_ADDRESS"] == "absender@gmail.com"


def test_unbekannte_zeile_der_vorhandenen_env_bleibt(tmp_path):
    schreibe_dateien(tmp_path, antworten())
    with (tmp_path / ".env").open("a") as datei:
        datei.write("EIGENE='von Hand'\n")
    schreibe_dateien(tmp_path, antworten(admin_email="neu@beispiel.de"))
    env = lies_env(tmp_path / ".env")
    assert env["EIGENE"] == "von Hand"
    assert env["ADMIN_EMAIL"] == "neu@beispiel.de"


def test_log_des_schreibens_ohne_werte(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    schreibe_dateien(tmp_path, antworten())
    schreibe_dateien(tmp_path, antworten(tunnel_token="eyJuZXUiOjF9", admin_email="neu@x.de"))
    log = "\n".join(r.getMessage() for r in caplog.records)
    assert "Schritt einstellungen" in log
    env = lies_env(tmp_path / ".env")
    werte = [env[n] for n in ERZEUGT[:4]]
    for wert in [*werte, "eyJuZXUiOjF9", "neu@x.de", "eyJhIjoiZmFrZSJ9", "anja@beispiel.de"]:
        assert wert not in log
    assert PASSWORT not in log


def test_spaeter_sichert_in_den_installationsordner(tmp_path):
    lokal = Sicherung(pfad="/Users/anja/Vorlesezeit/Sicherung", name="dieser Rechner", lokal=True)
    schreibe_dateien(tmp_path, antworten(sicherung=lokal))
    assert lies_env(tmp_path / ".env")["BACKUP_TARGET_PATH"] == "/Users/anja/Vorlesezeit/Sicherung"
    assert (tmp_path / "Sicherung").is_dir()


def test_spaeter_legt_den_ordner_auch_bei_vorhandener_env_an(tmp_path):
    schreibe_dateien(tmp_path, antworten())
    lokal = Sicherung(pfad="/Users/anja/Vorlesezeit/Sicherung", name="dieser Rechner", lokal=True)
    schreibe_dateien(tmp_path, antworten(sicherung=lokal))
    assert lies_env(tmp_path / ".env")["BACKUP_TARGET_PATH"] == "/Users/anja/Vorlesezeit/Sicherung"
    assert (tmp_path / "Sicherung").is_dir()


# --- Installationsschritte -----------------------------------------------------


class Aussen:
    """Nachgebaute Instanz von außen: /status mit festem Status, /login zählt mit."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.anfragen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.anfragen.append(request)
        if request.url.path == "/status":
            return httpx.Response(self.status)
        return httpx.Response(200, text="Link ist unterwegs")


def lauf(tmp_path, aussen: Aussen | None = None, **docker_antworten):
    schreibe_dateien(tmp_path, antworten())
    docker, fake = fake_docker(tmp_path, **docker_antworten)
    uhr = FakeUhr()
    aussen = aussen or Aussen()
    http = httpx.Client(transport=httpx.MockTransport(aussen))
    return Lauf(tmp_path, docker, http, uhr=uhr, schlafen=uhr.schlafen, architektur="arm64"), fake


def test_reihenfolge_laden_starten_gesund_tunnel(tmp_path):
    aussen = Aussen()
    lf, fake = lauf(tmp_path, aussen)
    lf.ausfuehren()

    assert lf.fehler is None
    assert lf.fertig
    assert fake.verben == ["compose pull", "compose up", "compose ps", "compose exec"]
    assert [str(r.url) for r in aussen.anfragen] == [
        "https://berger.vorlesezeit.app/status",
        "https://berger.vorlesezeit.app/login",
    ]


def test_genau_ein_anmeldelink_an_die_admin_mail(tmp_path):
    aussen = Aussen()
    lf, _ = lauf(tmp_path, aussen)
    lf.ausfuehren()

    posts = [r for r in aussen.anfragen if r.method == "POST"]
    assert len(posts) == 1
    assert posts[0].url == "https://berger.vorlesezeit.app/login"
    assert parse_qs(posts[0].content.decode()) == {"email": ["anja@beispiel.de"]}


def test_nach_app_gesund_keine_smtp_zeile_mehr(tmp_path):
    lf, _ = lauf(tmp_path)
    vorher = lies_env(tmp_path / ".env")
    lf.ausfuehren()

    zeilen = (tmp_path / ".env").read_text().splitlines()
    assert not [z for z in zeilen if z.startswith("SMTP")]
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    nachher = lies_env(tmp_path / ".env")
    assert nachher == {k: v for k, v in vorher.items() if not k.startswith("SMTP")}


def test_smtp_zeilen_bleiben_solange_die_app_nicht_gesund_ist(tmp_path):
    lf, _ = lauf(tmp_path, compose_ps=Ergebnis(0, json.dumps({"Health": "unhealthy"})))
    lf.ausfuehren()
    assert lf.fehler is not None
    assert "SMTP_HOST" in lies_env(tmp_path / ".env")


def test_zweiter_lauf_endet_im_selben_zustand(tmp_path):
    lf, fake = lauf(tmp_path)
    lf.ausfuehren()
    env_nach_erstem = (tmp_path / ".env").read_bytes()
    zustand = dict(lf.schritte)

    lf.ausfuehren()

    assert lf.fehler is None
    assert lf.fertig
    assert lf.schritte == zustand
    assert (tmp_path / ".env").read_bytes() == env_nach_erstem
    assert fake.verben[4:] == ["compose pull", "compose up", "compose ps", "compose exec"]


def test_scheiterndes_laden_nennt_image_und_architektur(tmp_path):
    fehler = Ergebnis(1, "", "error: no matching manifest for linux/arm64")
    lf, fake = lauf(tmp_path, compose_pull=fehler)
    lf.ausfuehren()

    assert lf.schritte["laden"] == "fehler"
    assert lf.fehler.knopf == "Wiederholen"
    assert f"ghcr.io/beasthook/vorlesezeit:{__version__}" in lf.fehler.technik
    assert "linux/arm64" in lf.fehler.technik
    assert "no matching manifest" in lf.fehler.technik
    assert fake.verben == ["compose pull"]


def test_ohne_antwort_von_aussen_nach_fuenf_minuten_erneut_pruefen(tmp_path):
    aussen = Aussen(status=502)
    lf, _ = lauf(tmp_path, aussen)
    start = lf.uhr()
    lf.ausfuehren()

    assert lf.schritte["aussen"] == "fehler"
    assert lf.fehler.knopf == "Erneut prüfen"
    assert "antwortet von außen noch nicht" in lf.fehler.satz
    assert lf.uhr() - start >= 300
    assert not [r for r in aussen.anfragen if r.method == "POST"]


@pytest.mark.parametrize(
    ("antwort", "schritt"),
    [
        ({"compose_up": Ergebnis(1, "", "kaputt")}, "start"),
        ({"compose_exec": Ergebnis(1, "", "503")}, "tunnel"),
    ],
)
def test_fehler_spaeterer_schritte_bieten_wiederholen(tmp_path, antwort, schritt):
    lf, _ = lauf(tmp_path, **antwort)
    lf.ausfuehren()
    assert lf.schritte[schritt] == "fehler"
    assert lf.fehler.knopf == "Wiederholen"
    assert not lf.fertig
