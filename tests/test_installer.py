"""One-Klick-Setup Baustein 3: der Assistent im Browser, Bildschirm 1 bis 7."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.einladungscode import Einladung, SmtpZugang, encode
from app.mail.smtp import ANMELDUNG_FEHLGESCHLAGEN, SmtpPruefung
from installer import Umgebung, Werkzeuge, create_installer
from installer.docker import Ergebnis
from installer.geraet import Messung
from installer.installation import lies_env
from tests.installer_fakes import FakeUhr, fake_docker, messung_mac, tls_ok, tls_proxy

TOKEN = "Zk3q8V1o2s9LwYxR4tNpBmA7cE5dF6gH0iJjKkLlMmN"
TUNNEL = "eyJhIjoiZmFrZS10dW5uZWwtdG9rZW4ifQ"
SMTP_KEY = "xsmtpsib-fake-schluessel-123"
EIGENES_PW = "mein-app-passwort-xyz"
SMTP = SmtpZugang(
    host="smtp-relay.brevo.com",
    port=587,
    user="9a1b2c@smtp-brevo.com",
    key=SMTP_KEY,
    absender="berger@vorlesezeit.app",
)
CODE = encode(Einladung(host="berger.vorlesezeit.app", tunnel_token=TUNNEL, smtp=SMTP))
CODE_OHNE_SMTP = encode(Einladung(host="berger.vorlesezeit.app", tunnel_token=TUNNEL, smtp=None))


class Aussen:
    def __init__(self) -> None:
        self.anfragen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.anfragen.append(request)
        return httpx.Response(200)


class Welt:
    """Alles, was der Assistent von außen braucht, als Fake."""

    def __init__(self, tmp_path: Path, host_os: str = "mac") -> None:
        self.install = tmp_path / "install"
        self.install.mkdir()
        self.laufwerke = tmp_path / "laufwerke"
        (self.laufwerke / "Samsung T7").mkdir(parents=True)
        (self.laufwerke / "USB-STICK").mkdir()
        self.host_os = host_os
        self.docker, self.fake = fake_docker(self.install)
        self.uhr = FakeUhr()
        self.aussen = Aussen()
        self.messung = messung_mac()
        self.tls = tls_ok
        self.smtp_aufrufe: list[dict] = []
        self.smtp_ergebnis = SmtpPruefung(True)
        self.beendet = 0
        self.starte = lambda f: f()

    def smtp(self, **kwargs) -> SmtpPruefung:
        self.smtp_aufrufe.append(kwargs)
        return self.smtp_ergebnis

    def beenden(self) -> None:
        self.beendet += 1

    def app(self):
        umgebung = Umgebung(
            token=TOKEN,
            host_os=self.host_os,
            install_dir=self.install,
            laufwerke_dir=self.laufwerke,
            host_install_dir="/Users/anja/Vorlesezeit",
            installer_image="ghcr.io/beasthook/vorlesezeit-installer:0.3.0",
        )
        werkzeuge = Werkzeuge(
            docker=self.docker,
            messung=lambda pfad: self.messung,
            tls=lambda: self.tls(),
            smtp_pruefung=self.smtp,
            http=httpx.Client(transport=httpx.MockTransport(self.aussen)),
            uhr=self.uhr,
            schlafen=self.uhr.schlafen,
            starte=lambda f: self.starte(f),
            beenden=self.beenden,
            wache=lambda pruefe: None,
        )
        return create_installer(umgebung, werkzeuge)

    def client(self) -> TestClient:
        client = TestClient(self.app(), follow_redirects=False)
        assert client.get(f"/?t={TOKEN}").status_code in (200, 303)
        return client


@pytest.fixture
def welt(tmp_path) -> Welt:
    return Welt(tmp_path)


def dateien(ordner: Path) -> dict[str, bytes]:
    return {str(p.relative_to(ordner)): p.read_bytes() for p in ordner.rglob("*") if p.is_file()}


def bis_zur_zusammenfassung(client: TestClient, code: str = CODE) -> None:
    assert client.post("/geraet", data={"erledigt": ["ruhezustand", "docker"]}).status_code == 303
    assert client.post("/adresse", data={"art": "code", "code": code}).status_code == 303
    r = client.post("/du", data={"admin_email": "anja@beispiel.de", "anzeigename": "Anja"})
    assert r.status_code == 303
    if code == CODE:
        assert client.post("/mail", data={"aktion": "weiter"}).status_code == 303
    else:
        eigene_mail(client)
    r = client.post("/sicherung", data={"aktion": "sichern", "laufwerk": "Samsung T7"})
    assert "Fertig. Ab heute Nacht" in r.text


def eigene_mail(client: TestClient) -> None:
    daten = {"anbieter": "gmail", "adresse": "familie.berger@gmail.com", "passwort": EIGENES_PW}
    r = client.post("/mail", data=daten | {"aktion": "test"})
    assert "Die Testmail ist raus" in r.text
    daten["passwort"] = ""
    assert client.post("/mail", data=daten | {"aktion": "weiter"}).status_code == 303


# --- Zugang --------------------------------------------------------------------


def test_ohne_token_404_mit_leerem_inhalt(welt):
    client = TestClient(welt.app(), follow_redirects=False)
    for pfad in ("/", "/geraet", "/static/styles.css", "/?t=falsch"):
        r = client.get(pfad)
        assert (r.status_code, r.content) == (404, b""), pfad
    r = client.post("/adresse", data={"art": "code", "code": CODE})
    assert (r.status_code, r.content) == (404, b"")


def test_gueltiges_token_setzt_httponly_strict_cookie(welt):
    client = TestClient(welt.app())
    r = client.get(f"/?t={TOKEN}")
    assert r.status_code == 200
    cookie = r.history[0].headers["set-cookie"] if r.history else r.headers["set-cookie"]
    assert "httponly" in cookie.lower()
    assert "samesite=strict" in cookie.lower()
    assert TOKEN not in str(r.url)
    assert client.get("/geraet").status_code == 200


def test_fremde_herkunft_wird_abgewiesen(welt):
    client = welt.client()
    r = client.post("/du", data={}, headers={"Origin": "https://boese.example"})
    assert r.status_code == 403


# --- Bildschirm 1 ----------------------------------------------------------------


def test_kopf_frage_und_balken(welt):
    r = welt.client().get("/geraet")
    assert "Schritt 1 von 7: Gerät" in r.text
    assert "Passt dieser Rechner für Vorlesezeit?" in r.text
    assert 'style="width:14.3%"' in r.text
    assert "Apple-Chip" in r.text
    assert "182 GB frei, nötig sind 5 GB" in r.text
    assert "16 GB" in r.text


def test_firmen_proxy_zeigt_hinweis_aussteller_und_trotzdem_weiter(welt):
    welt.tls = tls_proxy
    client = welt.client()
    r = client.get("/geraet")
    assert "Ein Firmen-Proxy liest mit" in r.text
    assert "Dieses Gerät eignet sich nicht, ein Firmen-Proxy liest mit." in r.text
    assert "Zscaler Intermediate Root CA" in r.text
    assert "Trotzdem weiter (nicht empfohlen)" in r.text
    assert "Erneut prüfen" in r.text
    tech = r.text.split("<details", 1)[1].split("</details>", 1)[0]
    assert "Trotzdem weiter" not in tech

    assert client.post("/geraet", data={"aktion": "weiter"}).status_code == 200
    r = client.post("/geraet", data={"aktion": "trotzdem"})
    assert (r.status_code, r.headers["location"]) == (303, "/adresse")


def test_zu_wenig_arbeitsspeicher_laesst_nicht_weiter(welt):
    welt.messung = Messung(maschine="arm64", frei_bytes=182 * 10**9, ram_bytes=1 * 10**9)
    client = welt.client()
    r = client.get("/geraet")
    assert "Zu wenig Arbeitsspeicher" in r.text
    assert "Erneut prüfen" in r.text
    assert client.post("/geraet", data={"aktion": "weiter"}).status_code == 200


@pytest.mark.parametrize(
    ("host_os", "anzahl", "filevault"),
    [("windows", 4, False), ("mac", 5, True), ("linux", 3, False)],
)
def test_checkliste_nach_system(tmp_path, host_os, anzahl, filevault):
    welt = Welt(tmp_path, host_os=host_os)
    r = welt.client().get("/geraet")
    assert r.text.count('name="erledigt"') == anzahl
    assert ("FileVault" in r.text) is filevault
    bilder = re.findall(r'src="(/assistent/bilder/[^"]+\.svg)"', r.text)
    assert len(bilder) == anzahl
    root = Path(__file__).resolve().parent.parent / "installer/static"
    for bild in bilder:
        assert (root / bild.removeprefix("/assistent/")).is_file(), bild
    assert "Umschalt" not in r.text


def test_offene_punkte_stehen_in_der_zusammenfassung(welt):
    client = welt.client()
    bis_zur_zusammenfassung(client)
    r = client.get("/zusammenfassung")
    assert "passt, 3 Einstellungen noch offen" in r.text


# --- Bildschirm 2 ----------------------------------------------------------------


def test_abgeschnittener_und_fremder_code(welt):
    client = welt.client()
    r = client.post("/adresse/pruefen", data={"code": CODE[:40]})
    assert "Dieser Code ist nicht vollständig." in r.text
    r = client.post("/adresse/pruefen", data={"code": "VZ2-KRUGS4ZANFZSAYJA"})
    assert "anderen Version" in r.text
    client.post("/geraet", data={})
    r = client.post("/adresse", data={"art": "code", "code": CODE[:40]})
    assert r.status_code == 200
    assert "Dieser Code ist nicht vollständig." in r.text


def test_gueltiger_code_passt_und_zeigt_die_adresse(welt):
    client = welt.client()
    r = client.post("/adresse/pruefen", data={"code": CODE})
    assert "Code passt" in r.text
    assert "berger.vorlesezeit.app" in r.text
    assert TUNNEL not in r.text


def test_eigene_domain_braucht_host_und_token(welt):
    client = welt.client()
    client.post("/geraet", data={})
    r = client.post("/adresse", data={"art": "domain", "host": "kein host", "tunnel_token": "x"})
    assert r.status_code == 200
    r = client.post(
        "/adresse", data={"art": "domain", "host": "advent.berger.de", "tunnel_token": ""}
    )
    assert r.status_code == 200
    r = client.post(
        "/adresse", data={"art": "domain", "host": "advent.berger.de", "tunnel_token": "tok"}
    )
    assert (r.status_code, r.headers["location"]) == (303, "/du")


# Ein `\` am Ende oder direkt vor `'` macht die .env für compose unlesbar
# (installation.env_tauglich).
UNLESBAR = "Ein \\ am Ende oder vor einem &#39; kann Vorlesezeit nicht speichern."


@pytest.mark.parametrize("token", ["tok\\", "to\\'k"])
def test_tunnel_token_mit_backslash(welt, token):
    client = welt.client()
    client.post("/geraet", data={})
    r = client.post(
        "/adresse", data={"art": "domain", "host": "advent.berger.de", "tunnel_token": token}
    )
    assert r.status_code == 200
    assert "Dieses Tunnel-Token kann Vorlesezeit nicht speichern." in r.text


@pytest.mark.parametrize("key", [SMTP_KEY + "\\", "ab\\'cd"])
def test_code_mit_backslash(welt, key):
    smtp = SmtpZugang(SMTP.host, SMTP.port, SMTP.user, key, SMTP.absender)
    code = encode(Einladung(host="berger.vorlesezeit.app", tunnel_token=TUNNEL, smtp=smtp))
    client = welt.client()
    r = client.post("/adresse/pruefen", data={"code": code})
    assert "Code passt" not in r.text
    assert "Bitte die Person, die dich eingeladen hat, um einen neuen Code." in r.text
    client.post("/geraet", data={})
    r = client.post("/adresse", data={"art": "code", "code": code})
    assert r.status_code == 200
    assert "Bitte die Person, die dich eingeladen hat, um einen neuen Code." in r.text


@pytest.mark.parametrize(
    ("feld", "wert"),
    [
        ("admin_email", "anja@beispiel.de\\"),
        ("anzeigename", "Anja\\"),
        ("anzeigename", "An\\'ja"),
    ],
)
def test_du_mit_backslash(welt, feld, wert):
    client = welt.client()
    client.post("/geraet", data={})
    client.post("/adresse", data={"art": "code", "code": CODE})
    daten = {"admin_email": "anja@beispiel.de", "anzeigename": "Anja"} | {feld: wert}
    r = client.post("/du", data=daten)
    assert r.status_code == 200
    assert UNLESBAR in r.text


@pytest.mark.parametrize(
    ("feld", "wert"),
    [("adresse", "ich@beispiel.de\\"), ("passwort", "pw\\"), ("passwort", "p\\'w")],
)
def test_mail_mit_backslash(welt, feld, wert):
    client = welt.client()
    _bis_mail(client, CODE_OHNE_SMTP)
    daten = {"anbieter": "gmail", "adresse": "ich@beispiel.de", "passwort": "pw"} | {feld: wert}
    r = client.post("/mail", data=daten | {"aktion": "test"})
    assert UNLESBAR in r.text
    assert welt.smtp_aufrufe == []


# --- Bildschirm 3 und 4 ------------------------------------------------------------


def test_ungueltige_mail_adresse(welt):
    client = welt.client()
    client.post("/geraet", data={})
    client.post("/adresse", data={"art": "code", "code": CODE})
    r = client.post("/du", data={"admin_email": "anja@beispiel", "anzeigename": "Anja"})
    assert r.status_code == 200
    assert "Hier fehlt noch etwas, zum Beispiel „.de“ am Ende." in r.text


def _bis_mail(client, code):
    client.post("/geraet", data={})
    client.post("/adresse", data={"art": "code", "code": code})
    client.post("/du", data={"admin_email": "anja@beispiel.de", "anzeigename": "Anja"})


def test_mail_aus_dem_code_zeigt_nur_den_absender(welt):
    client = welt.client()
    _bis_mail(client, CODE)
    r = client.get("/mail")
    assert "Antworten gehen an anja@beispiel.de" in r.text
    assert "berger@vorlesezeit.app" in r.text
    assert "Testmail" not in r.text
    assert SMTP_KEY not in r.text


def test_ohne_smtp_genau_vier_vorlagen(welt):
    client = welt.client()
    _bis_mail(client, CODE_OHNE_SMTP)
    r = client.get("/mail")
    assert re.findall(r'name="anbieter" value="([a-z]+)"', r.text) == [
        "gmail",
        "gmx",
        "webde",
        "brevo",
    ]


@pytest.mark.parametrize(
    ("anbieter", "host"),
    [
        ("gmail", "smtp.gmail.com"),
        ("gmx", "mail.gmx.net"),
        ("webde", "smtp.web.de"),
        ("brevo", "smtp-relay.brevo.com"),
    ],
)
def test_testmail_an_die_admin_mail(welt, anbieter, host):
    client = welt.client()
    _bis_mail(client, CODE_OHNE_SMTP)
    daten = {"anbieter": anbieter, "adresse": "ich@beispiel.de", "passwort": "pw"}
    if anbieter == "brevo":
        daten["absender"] = "advent@berger.de"
    client.post("/mail", data=daten | {"aktion": "test"})

    [aufruf] = welt.smtp_aufrufe
    assert (aufruf["host"], aufruf["port"]) == (host, 587)
    assert aufruf["to_address"] == "anja@beispiel.de"
    assert aufruf["user"] == "ich@beispiel.de"
    absender = "advent@berger.de" if anbieter == "brevo" else "ich@beispiel.de"
    assert aufruf["from_address"] == absender


def test_weiter_ohne_erfolgreiche_testmail_bleibt_auf_bildschirm_4(welt):
    client = welt.client()
    _bis_mail(client, CODE_OHNE_SMTP)
    daten = {"anbieter": "gmail", "adresse": "familie.berger@gmail.com", "passwort": "pw"}
    r = client.post("/mail", data=daten | {"aktion": "weiter"})
    assert r.status_code == 200
    assert "Über welches Postfach sollen die Mails gehen?" in r.text

    welt.smtp_ergebnis = SmtpPruefung(False, ANMELDUNG_FEHLGESCHLAGEN)
    r = client.post("/mail", data=daten | {"aktion": "test"})
    assert "Gmail hat die Anmeldung abgelehnt." in r.text
    r = client.post("/mail", data=daten | {"aktion": "weiter"})
    assert r.status_code == 200
    assert client.get("/sicherung").headers["location"] == "/mail"


# --- Bildschirm 5 ------------------------------------------------------------------


def test_laufwerke_und_systemlaufwerk_zuletzt(welt):
    client = welt.client()
    _bis_mail(client, CODE)
    client.post("/mail", data={"aktion": "weiter"})
    r = client.get("/sicherung")
    namen = re.findall(r'<span class="name">([^<]+)</span>', r.text)
    assert namen == ["Samsung T7", "USB-STICK", "Macintosh HD"]
    letzte = r.text.split("Macintosh HD", 1)[1]
    assert "nicht empfohlen" in letzte
    assert "dieser Rechner" in letzte
    assert "nicht empfohlen" not in r.text.split("Macintosh HD", 1)[0]


def test_neu_suchen_liest_die_laufwerke_neu(welt):
    client = welt.client()
    _bis_mail(client, CODE)
    client.post("/mail", data={"aktion": "weiter"})
    (welt.laufwerke / "NEU").mkdir()
    r = client.post("/sicherung", data={"aktion": "suchen"})
    assert "NEU" in r.text


def test_nicht_beschreibbares_laufwerk_wird_nicht_uebernommen(welt):
    client = welt.client()
    _bis_mail(client, CODE)
    client.post("/mail", data={"aktion": "weiter"})
    welt.fake.antworten["run"] = Ergebnis(1, "", "touch: Read-only file system")
    r = client.post("/sicherung", data={"aktion": "sichern", "laufwerk": "USB-STICK"})
    assert "kann Vorlesezeit nicht schreiben" in r.text
    assert "Read-only file system" in r.text
    assert client.get("/zusammenfassung").headers["location"] == "/sicherung"


def test_spaeter_ohne_rueckfrage_auf_diesen_rechner(welt):
    client = welt.client()
    _bis_mail(client, CODE)
    client.post("/mail", data={"aktion": "weiter"})
    r = client.post("/sicherung", data={"aktion": "spaeter"})
    assert (r.status_code, r.headers["location"]) == (303, "/zusammenfassung")
    assert "Bis dahin landet die Sicherung auf diesem Rechner." in client.get("/sicherung").text
    client.post("/installieren")
    env = lies_env(welt.install / ".env")
    assert env["BACKUP_TARGET_PATH"] == "/Users/anja/Vorlesezeit/Sicherung"


def test_nach_bildschirm_1_bis_5_keine_datei(welt):
    client = welt.client()
    bis_zur_zusammenfassung(client)
    assert client.get("/zusammenfassung").status_code == 200
    assert list(welt.install.iterdir()) == []
    run = [a for a in welt.fake.aufrufe if a[0] == "run"]
    assert run[0][3] == 'type=bind,"source=/Volumes/Samsung T7",target=/ziel'


# --- Bildschirm 6 ------------------------------------------------------------------


def test_zusammenfassung_mit_aendern_und_ansehen(welt):
    client = welt.client()
    bis_zur_zusammenfassung(client)
    r = client.get("/zusammenfassung")
    for text in (
        "berger.vorlesezeit.app",
        "Anja, anja@beispiel.de",
        "berger@vorlesezeit.app",
        "Samsung T7",
    ):
        assert text in r.text
    assert r.text.count(">Ändern</a>") == 4
    assert r.text.count(">Ansehen</a>") == 1


def test_installieren_bis_fertig(welt):
    client = welt.client()
    bis_zur_zusammenfassung(client)
    r = client.post("/installieren")
    assert (r.status_code, r.headers["location"]) == (303, "/installieren")
    assert client.get("/installieren").headers["location"] == "/fertig"
    env = lies_env(welt.install / ".env")
    assert env["BACKUP_TARGET_PATH"] == "/Volumes/Samsung T7/Vorlesezeit-Sicherung"
    assert not [k for k in env if k.startswith("SMTP")]
    assert welt.fake.verben[-4:] == ["compose pull", "compose up", "compose ps", "compose exec"]


def test_waehrend_der_installation_kein_zurueck(welt):
    welt.starte = lambda f: None  # der Lauf bleibt beim ersten Schritt stehen
    client = welt.client()
    bis_zur_zusammenfassung(client)
    client.post("/installieren")
    r = client.get("/installieren")
    assert r.status_code == 200
    assert "Vorlesezeit wird eingerichtet" in r.text
    assert "Einstellungen gespeichert" in r.text
    assert "Zurück" not in r.text


def test_scheiterndes_laden_bietet_wiederholen_und_zusammenfassung(welt):
    welt.fake.antworten["compose pull"] = Ergebnis(1, "", "timeout")
    client = welt.client()
    bis_zur_zusammenfassung(client)
    client.post("/installieren")
    r = client.get("/installieren")
    assert "Das Programm ließ sich nicht herunterladen." in r.text
    assert "Wiederholen" in r.text
    assert "Zurück zur Zusammenfassung" in r.text
    assert "linux/arm64" in r.text

    welt.fake.antworten["compose pull"] = Ergebnis(0, "", "")
    client.post("/installieren/wiederholen")
    assert client.get("/installieren").headers["location"] == "/fertig"


def test_nach_fehlschlag_wirkt_ein_neuer_einladungscode(welt):
    welt.fake.antworten["compose pull"] = Ergebnis(1, "", "timeout")
    client = welt.client()
    bis_zur_zusammenfassung(client)
    client.post("/installieren")
    assert "Zurück zur Zusammenfassung" in client.get("/installieren").text
    vorher = lies_env(welt.install / ".env")

    neuer_code = encode(
        Einladung(host="mueller.vorlesezeit.app", tunnel_token="eyJuZXUiOjF9", smtp=None)
    )
    r = client.post("/adresse", data={"art": "code", "code": neuer_code})
    assert r.headers["location"] == "/du"
    eigene_mail(client)  # der neue Code bringt keinen Mailzugang mit
    assert client.get("/zusammenfassung").status_code == 200
    welt.fake.antworten["compose pull"] = Ergebnis(0, "", "")
    r = client.post("/installieren")
    assert (r.status_code, r.headers["location"]) == (303, "/installieren")
    assert client.get("/installieren").headers["location"] == "/fertig"

    installation = json.loads((welt.install / "installation.json").read_text())
    assert installation["adresse"] == "mueller.vorlesezeit.app"
    assert installation["abgeschlossen"] is False
    env = lies_env(welt.install / ".env")
    assert env["CLOUDFLARE_TUNNEL_TOKEN"] == "eyJuZXUiOjF9"
    assert env["SESSION_SECRET_KEY"] == vorher["SESSION_SECRET_KEY"]
    assert "https://mueller.vorlesezeit.app" in client.get("/fertig").text
    assert "berger.vorlesezeit.app" not in client.get("/fertig").text


def test_installieren_waehrend_eines_laufs_schreibt_nichts(welt):
    welt.starte = lambda f: None  # der Lauf startet nicht von selbst
    client = welt.client()
    bis_zur_zusammenfassung(client)
    client.post("/installieren")
    client.app.state.lauf.laeuft = True
    vorher = dateien(welt.install)

    client.post("/du", data={"admin_email": "neu@beispiel.de", "anzeigename": "Neu"})
    r = client.post("/installieren")

    assert (r.status_code, r.headers["location"]) == (303, "/installieren")
    assert dateien(welt.install) == vorher


def test_neustart_mit_halber_installation_setzt_bei_schritt_6_fort(welt):
    client = welt.client()
    bis_zur_zusammenfassung(client)
    welt.fake.antworten["compose pull"] = Ergebnis(1, "", "timeout")
    client.post("/installieren")

    welt.fake.antworten["compose pull"] = Ergebnis(0, "", "")
    neu = welt.client()  # neuer Prozess: nichts mehr im Speicher
    r = neu.get("/")
    assert r.headers["location"] == "/installieren"
    assert neu.get("/installieren").headers["location"] == "/fertig"
    assert "berger.vorlesezeit.app" in neu.get("/fertig").text


# --- Bildschirm 7 ------------------------------------------------------------------


def _installiert(welt) -> TestClient:
    client = welt.client()
    bis_zur_zusammenfassung(client)
    client.post("/installieren")
    return client


def test_fertig_zeigt_adresse_und_anmeldelink(welt):
    r = _installiert(welt).get("/fertig")
    assert "https://berger.vorlesezeit.app" in r.text
    assert "Kopieren" in r.text
    assert "Dein Anmeldelink ist unterwegs an" in r.text
    assert "anja@beispiel.de" in r.text


def test_notfallblatt_herunterladen(welt):
    client = _installiert(welt)
    r = client.get("/notfallblatt")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert 'filename="Vorlesezeit-Notfallblatt-berger.pdf"' in r.headers["content-disposition"]
    key = lies_env(welt.install / ".env")["CREDENTIALS_KEY"]
    assert key.encode() in r.content
    assert "Heruntergeladen: Vorlesezeit-Notfallblatt-berger.pdf" in client.get("/fertig").text


def test_abschliessen_nur_mit_haken(welt):
    client = _installiert(welt)
    r = client.post("/abschliessen", data={})
    assert "Vorlesezeit läuft" in r.text
    assert welt.beendet == 0
    assert json.loads((welt.install / "installation.json").read_text())["abgeschlossen"] is False

    r = client.post("/abschliessen", data={"gespeichert": "on"})
    assert "Geschafft" in r.text
    assert json.loads((welt.install / "installation.json").read_text())["abgeschlossen"] is True
    assert welt.beendet == 1


def test_abgeschlossene_installation_fuehrt_zur_wartung(welt):
    _installiert(welt).post("/abschliessen", data={"gespeichert": "on"})
    vorher = dateien(welt.install)
    aufrufe = len(welt.fake.aufrufe)

    client = welt.client()
    for pfad in ("/", "/geraet", "/zusammenfassung", "/fertig"):
        assert client.get(pfad).headers["location"] == "/wartung", pfad
    assert client.post("/installieren").headers["location"] == "/wartung"
    assert dateien(welt.install) == vorher
    assert len(welt.fake.aufrufe) == aufrufe


# --- Log ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [CODE, CODE_OHNE_SMTP])
def test_log_ohne_token_code_und_passwort(welt, caplog, code):
    caplog.set_level(logging.DEBUG)
    client = welt.client()
    bis_zur_zusammenfassung(client, code)
    client.post("/installieren")
    client.get("/notfallblatt")
    client.post("/abschliessen", data={"gespeichert": "on"})

    # Ohne die Zeilen des Testclients: der spielt den Browser, nicht den Assistenten.
    log = "\n".join(
        r.getMessage() for r in caplog.records if "http://testserver" not in r.getMessage()
    )
    assert "Schritt" in log  # es wird überhaupt protokolliert
    key = lies_env(welt.install / ".env")["CREDENTIALS_KEY"]
    for geheim in (TOKEN, code, TUNNEL, SMTP_KEY, EIGENES_PW, key):
        assert geheim not in log
