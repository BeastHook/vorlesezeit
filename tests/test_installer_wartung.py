"""One-Klick-Setup Baustein 4: Wartung und Update im Assistenten.

Nur mit FakeDocker, FakeUhr und httpx.MockTransport: kein echtes Docker, keine
echte GitHub-Abfrage.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from installer import Umgebung, Werkzeuge, create_installer
from installer.docker import Ergebnis
from installer.installation import _env_inhalt, lies_env
from installer.wartung import finde_ziel
from tests.installer_fakes import FakeUhr, fake_docker

TOKEN = "Wq7Lm2Zt9Rk4Hs1Nv8Bx3Cf6Jd0Gp5Ye"
GEHEIM = {
    "STORAGE_SECRET_KEY": "speicher-geheim-123",
    "SESSION_SECRET_KEY": "sitzung-geheim-456",
    "TRIGGER_SECRET": "anstoss-geheim-789",
    "CREDENTIALS_KEY": "c2NobHVlc3NlbC1mdWVyLWRpZS1kYXRlbmJhbms=",
    "CLOUDFLARE_TUNNEL_TOKEN": "eyJ0dW5uZWwiOiJnZWhlaW0ifQ",
}
HOST = "berger.vorlesezeit.app"
RELEASES = "https://api.github.com/repos/BeastHook/vorlesezeit/releases/latest"
# Montag, 19.10.2026, 14:02 Uhr in Berlin.
START = datetime(2026, 10, 19, 12, 2, tzinfo=UTC)
NOTIZ = (
    "dringend: nein\r\n\r\n- Wartung über die Startdatei\r\n"
    "* Antworten landen bei dir\r\nSonst nichts"
)


# --- Docker ------------------------------------------------------------------------


def test_neue_compose_aufrufe(tmp_path):
    docker, fake = fake_docker(tmp_path)
    docker.sichern()
    docker.stop("app", "scheduler", "backup")
    docker.db_beiseite()
    docker.rueckspielen("vorlesezeit-20261019T120200Z.db")
    docker.anmeldelink()
    docker.logs()
    docker.down(volumes=False)
    docker.down(volumes=True)
    befehle = [a[5:] for a in fake.aufrufe]
    assert befehle[0] == [
        "exec",
        "-T",
        "backup",
        "python",
        "deploy/backup/backup.py",
        "--once",
    ]
    assert befehle[1] == ["stop", "app", "scheduler", "backup"]
    assert befehle[2][:6] == ["run", "--rm", "--no-deps", "backup", "sh", "-c"]
    for endung in ('"$DATABASE_PATH"', '"$DATABASE_PATH-wal"', '"$DATABASE_PATH-shm"'):
        assert endung in befehle[2][6]
    assert ".vor-rueckfall" in befehle[2][6]
    assert befehle[3] == [
        "run",
        "--rm",
        "--no-deps",
        "backup",
        "python",
        "deploy/backup/backup.py",
        "restore",
        "vorlesezeit-20261019T120200Z.db",
    ]
    assert befehle[4] == ["exec", "-T", "app", "python", "-m", "app.auth.anmeldelink"]
    assert befehle[5] == ["logs", "--since", "168h", "--timestamps", "--no-color"]
    assert befehle[6] == ["down", "--rmi", "all"]
    assert befehle[7] == ["down", "--volumes", "--rmi", "all"]


def test_container_pfad_kehrt_host_pfad_um(tmp_path):
    docker, fake = fake_docker(tmp_path)
    assert docker.container_pfad("/Volumes/Samsung T7/Vorlesezeit-Sicherung") == (
        "/laufwerke/Samsung T7/Vorlesezeit-Sicherung"
    )
    assert fake.aufrufe[0] == ["inspect", "--format", "{{json .Mounts}}", "abc123"]
    assert docker.container_pfad("/Volumes") == "/laufwerke"
    assert docker.container_pfad("/VolumesX/a") is None


def test_ordner_loeschen_ueber_hilfscontainer_mit_mount(tmp_path):
    docker, fake = fake_docker(tmp_path)
    docker.ordner_loeschen("img", "/Volumes/Foto:2024")
    args = fake.aufrufe[0]
    assert args[:5] == [
        "run",
        "--rm",
        "--mount",
        'type=bind,"source=/Volumes/Foto:2024",target=/ziel',
        "img",
    ]
    assert args[-1] == "rm -rf /ziel/Vorlesezeit-Sicherung"


@pytest.mark.parametrize(
    "host",
    [
        "/Users/anja/Vorlesezeit/",
        "/Users/anja/Vorlesezeit/../anja",
        "/Users/anja/Vorlesezeit/Sicherung/../..",
        "/Volumes/Samsung T7/../..",
    ],
)
def test_finde_ziel_ohne_leeren_rest_und_ohne_punktpunkt(tmp_path, host):
    docker, _ = fake_docker(tmp_path)
    env = {"BACKUP_TARGET_PATH": host}
    ziel = finde_ziel(tmp_path, tmp_path / "laufwerke", "/Users/anja/Vorlesezeit", docker, env)
    assert ziel.pfad is None
    assert ziel.lokal is False


# --- Welt ---------------------------------------------------------------------------


class Aussen:
    """GitHub und die öffentliche Adresse als httpx.MockTransport."""

    def __init__(self) -> None:
        self.release: dict | None = {"tag_name": "v0.4.0", "body": NOTIZ}
        self.status = 200
        self.anfragen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.anfragen.append(request)
        if str(request.url) == RELEASES:
            if self.release is None:
                raise httpx.ConnectTimeout("timeout")
            return httpx.Response(200, json=self.release)
        if request.url.host == HOST and request.url.path == "/status":
            return httpx.Response(self.status)
        return httpx.Response(404)


class Welt:
    """Eine abgeschlossene Installation mit Sicherung auf „Samsung T7“."""

    def __init__(self, tmp_path: Path) -> None:
        self.install = tmp_path / "install"
        self.install.mkdir()
        self.laufwerke = tmp_path / "laufwerke"
        self.ziel = self.laufwerke / "Samsung T7" / "Vorlesezeit-Sicherung"
        (self.ziel / "db").mkdir(parents=True)
        self.docker, self.fake = fake_docker(self.install)
        mounts = [
            {"Source": "/Volumes", "Destination": "/laufwerke"},
            {"Source": "/Users/anja/Vorlesezeit", "Destination": "/install"},
        ]
        self.fake.antworten["inspect"] = Ergebnis(0, json.dumps(mounts), "")
        self.env = {
            "VORLESEZEIT_IMAGE": "ghcr.io/beasthook/vorlesezeit:0.3.0",
            "STORAGE_ACCESS_KEY": "vorlesezeit",
            **GEHEIM,
            "ADMIN_EMAIL": "anja@beispiel.de",
            "ADMIN_DISPLAY_NAME": "Anja",
            "BACKUP_TARGET_PATH": "/Volumes/Samsung T7/Vorlesezeit-Sicherung",
        }
        self.schreibe_env()
        (self.install / "compose.yml").write_text("services: {}\n")
        self.installation(abgeschlossen=True)
        self.sicherung(datetime(2026, 10, 19, 1, 0, tzinfo=UTC))
        self.uhr = FakeUhr()
        self.aussen = Aussen()
        self.beendet = 0
        self.starte = lambda f: f()
        self.wachen: list = []

    def schreibe_env(self) -> None:
        (self.install / ".env").write_text(_env_inhalt(self.env))

    def env_bytes(self) -> bytes:
        return (self.install / ".env").read_bytes()

    def jetzt(self) -> datetime:
        return START + timedelta(seconds=self.uhr.jetzt - 1000)

    def installation(self, **werte) -> None:
        daten = {
            "adresse": HOST,
            "admin_email": "anja@beispiel.de",
            "anzeigename": "Anja",
            "version": "0.3.0",
            "abgeschlossen": True,
        } | werte
        (self.install / "installation.json").write_text(json.dumps(daten))

    def gelesen(self) -> dict:
        return json.loads((self.install / "installation.json").read_text())

    def sicherung(self, wann: datetime) -> str:
        name = f"vorlesezeit-{wann.strftime('%Y%m%dT%H%M%SZ')}.db"
        (self.ziel / "db" / name).write_bytes(b"SQLite")
        return name

    def beenden(self) -> None:
        self.beendet += 1

    def app(self):
        umgebung = Umgebung(
            token=TOKEN,
            host_os="mac",
            install_dir=self.install,
            laufwerke_dir=self.laufwerke,
            host_install_dir="/Users/anja/Vorlesezeit",
            installer_image="ghcr.io/beasthook/vorlesezeit-installer:0.3.0",
        )
        werkzeuge = Werkzeuge(
            docker=self.docker,
            http=httpx.Client(transport=httpx.MockTransport(self.aussen)),
            uhr=self.uhr,
            schlafen=self.uhr.schlafen,
            starte=lambda f: self.starte(f),
            beenden=self.beenden,
            jetzt=self.jetzt,
            wache=self.wachen.append,
        )
        return create_installer(umgebung, werkzeuge)

    def client(self) -> TestClient:
        client = TestClient(self.app(), follow_redirects=False)
        assert client.get(f"/?t={TOKEN}").status_code == 303
        return client

    def krank(self) -> None:
        self.fake.antworten["compose ps"] = Ergebnis(
            0, json.dumps({"Service": "app", "Health": "unhealthy"}), ""
        )


@pytest.fixture
def welt(tmp_path) -> Welt:
    return Welt(tmp_path)


# --- Einstieg ----------------------------------------------------------------------


def test_start_nach_abschluss_fuehrt_zur_wartung(welt):
    client = welt.client()
    r = client.get("/")
    assert (r.status_code, r.headers["location"]) == (303, "/wartung")


def test_start_mit_unfertiger_installation_fuehrt_weiter_zu_installieren(welt):
    welt.installation(abgeschlossen=False)
    client = TestClient(welt.app(), follow_redirects=False)
    r = client.get(f"/?t={TOKEN}")
    assert (r.status_code, r.headers["location"]) == (303, "/installieren")


@pytest.mark.parametrize("pfad", ["/adresse", "/geraet", "/fertig", "/notfallblatt"])
def test_einrichtung_nach_abschluss_fuehrt_zur_wartung(welt, pfad):
    client = welt.client()
    r = client.get(pfad)
    assert (r.status_code, r.headers["location"]) == (303, "/wartung")
    assert client.post("/installieren").headers["location"] == "/wartung"


def test_wartung_ohne_abschluss_fuehrt_zum_start(welt):
    welt.installation(abgeschlossen=False)
    client = TestClient(welt.app(), follow_redirects=False)
    client.get(f"/?t={TOKEN}")
    for pfad in ("/wartung", "/wartung/neustart", "/wartung/protokoll"):
        r = client.get(pfad)
        assert (r.status_code, r.headers["location"]) == (303, "/"), pfad


def test_wartung_ohne_token_404_mit_leerem_inhalt(welt):
    client = TestClient(welt.app(), follow_redirects=False)
    for pfad in ("/wartung", "/wartung/protokoll/herunterladen"):
        r = client.get(pfad)
        assert (r.status_code, r.content) == (404, b"")


def test_fremde_herkunft_wird_abgewiesen(welt):
    client = welt.client()
    r = client.post("/wartung/neustart", headers={"Origin": "https://boese.example"})
    assert r.status_code == 403
    assert "compose stop" not in welt.fake.verben


def test_kopfzeile_wartung_und_zurueck_zur_uebersicht(welt):
    client = welt.client()
    assert '<span class="meta">Wartung</span>' in client.get("/wartung").text
    for pfad in (
        "/wartung/aktualisieren",
        "/wartung/neustart",
        "/wartung/anmeldelink",
        "/wartung/protokoll",
        "/wartung/notfallblatt",
        "/wartung/deinstallieren",
    ):
        text = client.get(pfad).text
        assert '<span class="meta">Wartung</span>' in text, pfad
        assert 'href="/wartung">‹ Übersicht</a>' in text, pfad


# --- Übersicht ----------------------------------------------------------------------


def test_alles_in_ordnung(welt):
    r = welt.client().get("/wartung")
    assert r.status_code == 200
    assert "Alles in Ordnung" in r.text
    assert f"<strong>{HOST}</strong>" in r.text
    for text in (
        "Version 0.3.0",
        "Vorlesezeit läuft",
        "Von außen erreichbar",
        "Letzte Sicherung heute, 03:00 Uhr",
        "auf Samsung T7",
        "Genug Platz",
    ):
        assert text in r.text, text
    liste = r.text[r.text.index('aria-label="Status"') :]
    reihenfolge = [
        liste.index(t)
        for t in ("Version 0.3.0", "Vorlesezeit läuft", "Von außen", "Letzte Sicherung", "Platz")
    ]
    assert reihenfolge == sorted(reihenfolge)
    assert "is-err" not in r.text
    assert "Fertig? Schließ einfach dieses Fenster. Vorlesezeit läuft weiter." in r.text
    for name, meta in (
        ("Aktualisieren", "Neue Version einspielen"),
        ("Neu starten", "Hilft oft, wenn etwas hängt"),
        ("Anmeldelink erzeugen", "Falls du nicht mehr in den Admin-Bereich kommst"),
        ("Protokoll exportieren", "Für die Person, die dir hilft"),
        ("Notfallblatt herunterladen", "Zum Wiederherstellen nach einem Defekt"),
        ("Deinstallieren", "Vorlesezeit von diesem Rechner entfernen"),
    ):
        assert f'<span class="name">{name}</span><span class="meta">{meta}</span>' in r.text


def test_tunnel_weg_und_sicherung_alt_zwei_dinge(welt):
    for datei in (welt.ziel / "db").iterdir():
        datei.unlink()
    welt.sicherung(datetime(2026, 10, 10, 1, 0, tzinfo=UTC))
    welt.fake.antworten["compose exec"] = Ergebnis(1, "", "HTTP Error 503")
    r = welt.client().get("/wartung")
    assert "Zwei Dinge brauchen dich" in r.text
    assert "Von außen nicht erreichbar" in r.text
    assert 'href="/wartung/neustart">Neu starten</a>' in r.text
    assert "Letzte Sicherung vor 9 Tagen" in r.text
    assert "Erneut prüfen" in r.text
    assert "Technische Einzelheiten" in r.text


def test_sicherungslaufwerk_fehlt(welt):
    shutil.rmtree(welt.laufwerke / "Samsung T7")
    r = welt.client().get("/wartung")
    assert "Samsung T7 ist nicht angesteckt." in r.text
    assert "Steck sie wieder an, die nächste Sicherung läuft heute Nacht." in r.text
    assert "Ein Ding braucht dich" in r.text


def test_keine_sicherung_ist_rot(welt):
    for datei in (welt.ziel / "db").iterdir():
        datei.unlink()
    r = welt.client().get("/wartung")
    assert "Ein Ding braucht dich" in r.text
    assert "Erneut prüfen" in r.text


def test_zu_wenig_platz(welt, monkeypatch):
    platz = namedtuple("platz", "total used free")
    monkeypatch.setattr(
        "installer.wartung.shutil.disk_usage", lambda p: platz(10**12, 0, 4 * 10**9)
    )
    r = welt.client().get("/wartung")
    assert "Zu wenig Platz" in r.text
    assert "4 GB frei auf diesem Rechner, 4 GB auf Samsung T7" in r.text
    assert "Ein Ding braucht dich" in r.text


def test_app_krank(welt):
    welt.krank()
    r = welt.client().get("/wartung")
    assert "Vorlesezeit läuft gerade nicht" in r.text
    assert "Vorlesezeit läuft nicht" in r.text
    assert "erst prüfbar, wenn Vorlesezeit läuft" in r.text
    assert 'class="btn btn-primary btn-block" href="/wartung/neustart">Neu starten</a>' in r.text
    assert not [a for a in welt.aussen.anfragen if a.url.host == HOST]


def test_update_karte(welt):
    r = welt.client().get("/wartung")
    assert "Vorlesezeit 0.4.0 ist da" in r.text
    assert 'href="/wartung/aktualisieren">Ansehen und aktualisieren</a>' in r.text
    assert "die neueste" not in r.text
    [anfrage] = [a for a in welt.aussen.anfragen if str(a.url) == RELEASES]
    assert "authorization" not in anfrage.headers


def test_ohne_neuere_version_die_neueste(welt):
    welt.aussen.release = {"tag_name": "v0.3.0", "body": ""}
    r = welt.client().get("/wartung")
    assert "ist da" not in r.text
    assert "die neueste" in r.text


def test_abfrage_scheitert_ohne_karte(welt):
    welt.aussen.release = None
    r = welt.client().get("/wartung")
    assert r.status_code == 200
    assert "ist da" not in r.text
    assert "die neueste" not in r.text


# --- Aktualisieren: Seite ------------------------------------------------------------


def test_aktualisieren_zeigt_stichpunkte(welt):
    r = welt.client().get("/wartung/aktualisieren")
    assert "Eine neue Version ist da" in r.text
    assert "0.3.0" in r.text and "0.4.0" in r.text
    assert "<li>Wartung über die Startdatei</li>" in r.text
    assert "<li>Antworten landen bei dir</li>" in r.text
    assert "dringend: nein" not in r.text
    assert "Sonst nichts" not in r.text
    assert "Jetzt aktualisieren" in r.text
    assert "Am besten tagsüber, abends ab 17 Uhr kommt die Geschichte auf den Tonie." in r.text


def test_aktualisieren_ohne_neuere_version(welt):
    welt.aussen.release = {"tag_name": "v0.3.0", "body": ""}
    r = welt.client().get("/wartung/aktualisieren")
    assert "Du hast die neueste Version" in r.text
    assert "Zuletzt nachgesehen: heute, 14:02 Uhr" in r.text
    assert "Jetzt aktualisieren" not in r.text


def test_aktualisieren_abfrage_scheitert(welt):
    welt.aussen.release = None
    r = welt.client().get("/wartung/aktualisieren")
    assert "Gibt es eine neue Version?" in r.text
    assert "Noch einmal nachsehen" in r.text


def advent(welt: Welt, tag: int) -> None:
    welt.uhr.jetzt += (datetime(2026, 12, tag, 10, 0, tzinfo=UTC) - START).total_seconds()


def test_adventssperre(welt):
    advent(welt, 10)
    client = welt.client()
    r = client.get("/wartung/aktualisieren")
    assert "Im Advent wird nicht aktualisiert" in r.text
    assert "Ab 26. Dezember möglich" in r.text
    assert "Jetzt aktualisieren" not in r.text
    r = client.post("/wartung/aktualisieren")
    assert r.headers["location"] == "/wartung/aktualisieren"
    assert "compose pull" not in welt.fake.verben
    assert "compose exec" not in welt.fake.verben


def test_advent_aber_dringend(welt):
    welt.aussen.release = {"tag_name": "v0.3.1", "body": "dringend: ja\n- Behebt einen Fehler"}
    advent(welt, 10)
    client = welt.client()
    r = client.get("/wartung/aktualisieren")
    assert "Ein wichtiges Update ist da" in r.text
    assert "dringend</span>" in r.text
    client.post("/wartung/aktualisieren")
    assert "compose pull" in welt.fake.verben


def test_am_26_dezember_wieder_frei(welt):
    advent(welt, 26)
    welt.client().post("/wartung/aktualisieren")
    assert "compose pull" in welt.fake.verben


def test_ohne_neuere_version_kein_vorgang(welt):
    welt.aussen.release = {"tag_name": "v0.3.0", "body": ""}
    welt.client().post("/wartung/aktualisieren")
    assert "compose exec" not in welt.fake.verben


# --- Aktualisieren: Vorgang ----------------------------------------------------------


def test_gelungenes_update(welt):
    vorher = welt.env_bytes()
    client = welt.client()
    r = client.post("/wartung/aktualisieren")
    assert r.headers["location"] == "/wartung/aktualisieren/stand"
    verben = [v for v in welt.fake.verben if v in ("compose exec", "compose pull", "compose up")]
    assert verben[:3] == ["compose exec", "compose pull", "compose up"]
    assert welt.fake.aufrufe[0][5:8] == ["exec", "-T", "backup"]

    neu = vorher.replace(
        b"VORLESEZEIT_IMAGE='ghcr.io/beasthook/vorlesezeit:0.3.0'",
        b"VORLESEZEIT_IMAGE='ghcr.io/beasthook/vorlesezeit:0.4.0'",
    )
    assert neu != vorher
    assert welt.env_bytes() == neu
    assert os.stat(welt.install / ".env").st_mode & 0o777 == 0o600
    assert welt.gelesen()["version"] == "0.4.0"
    assert not (welt.install / "aktualisierung.json").exists()
    r = client.get("/wartung/aktualisieren/stand")
    assert "Vorlesezeit 0.4.0 läuft" in r.text
    assert "Fertig. Die Familie merkt davon nichts, alle Links gelten weiter." in r.text


def test_fortschritt_laedt_neu(welt):
    welt.starte = lambda f: None
    client = welt.client()
    client.post("/wartung/aktualisieren")
    r = client.get("/wartung/aktualisieren/stand")
    assert '<meta http-equiv="refresh" content="3">' in r.text
    for text in ("Sichern", "Neue Version laden und starten", "Läuft und ist von außen erreichbar"):
        assert text in r.text
    assert "‹ Übersicht" not in r.text


def test_sicherung_scheitert_nichts_veraendert(welt):
    vorher = welt.env_bytes()
    welt.fake.antworten["compose exec"] = Ergebnis(1, "", "Ziel nicht gefunden")
    client = welt.client()
    client.post("/wartung/aktualisieren")
    assert welt.env_bytes() == vorher
    assert "compose pull" not in welt.fake.verben
    assert not (welt.install / "aktualisierung.json").exists()
    r = client.get("/wartung/aktualisieren/stand")
    assert "Die Sicherung hat nicht geklappt, deshalb wurde nichts verändert." in r.text
    assert "Noch einmal versuchen" in r.text


def test_rueckfall_nach_drei_minuten(welt):
    welt.sicherung(datetime(2026, 10, 19, 12, 2, tzinfo=UTC))
    welt.krank()
    vorher = welt.env_bytes()
    client = welt.client()
    client.post("/wartung/aktualisieren")

    assert welt.env_bytes() == vorher
    assert welt.uhr.jetzt - 1000 >= 180
    verben = welt.fake.verben
    stop = verben.index("compose stop")
    assert welt.fake.aufrufe[stop][5:] == ["stop", "app", "scheduler", "backup"]
    runs = [i for i, v in enumerate(verben) if v == "compose run"]
    assert len(runs) == 2 and runs[0] > stop
    assert "sh" in welt.fake.aufrufe[runs[0]]
    assert welt.fake.aufrufe[runs[1]][-2:] == ["restore", "vorlesezeit-20261019T120200Z.db"]
    assert "compose up" in verben[runs[1] :]
    assert not (welt.install / "aktualisierung.json").exists()
    assert welt.gelesen()["version"] == "0.3.0"

    r = client.get("/wartung/aktualisieren/stand")
    assert "Das Update hat nicht geklappt" in r.text
    assert 'href="/wartung/protokoll">Protokoll exportieren</a>' in r.text
    assert "Vorlesezeit 0.4.0 ist da" in client.get("/wartung").text


def test_unfertiges_update_geht_nach_neustart_weiter(welt):
    welt.krank()
    welt.env["VORLESEZEIT_IMAGE"] = "ghcr.io/beasthook/vorlesezeit:0.4.0"
    welt.schreibe_env()
    stand = {
        "alt": "0.3.0",
        "neu": "0.4.0",
        "rueckfallstand": "vorlesezeit-20261019T010000Z.db",
        "schritt": "pruefen",
        "pruefen_seit": (START - timedelta(seconds=100)).isoformat(),
    }
    (welt.install / "aktualisierung.json").write_text(json.dumps(stand))
    client = welt.client()
    r = client.get("/wartung")
    assert r.headers["location"] == "/wartung/aktualisieren/stand"
    # Die Frist läuft ab dem gespeicherten Beginn: 80 s Rest, dann der Rückfall (180 s).
    assert 80 <= welt.uhr.jetzt - 1000 < 80 + 180 + 10
    assert lies_env(welt.install / ".env")["VORLESEZEIT_IMAGE"].endswith(":0.3.0")
    assert "compose pull" not in welt.fake.verben
    [restore] = [a for a in welt.fake.aufrufe if "restore" in a]
    assert restore[-1] == "vorlesezeit-20261019T010000Z.db"
    assert not (welt.install / "aktualisierung.json").exists()


def test_stand_ohne_geheimnis_vor_jedem_schritt(welt):
    gesehen = []
    welt.starte = lambda f: None
    client = welt.client()
    client.post("/wartung/aktualisieren")
    vorgang = client.app.state.vorgang
    original = welt.docker.pull

    def pull():
        gesehen.append(json.loads((welt.install / "aktualisierung.json").read_text()))
        return original()

    welt.docker.pull = pull
    vorgang.ausfuehren()
    [stand] = gesehen
    assert stand["alt"] == "0.3.0" and stand["neu"] == "0.4.0"
    assert stand["schritt"] == "laden"
    assert stand["rueckfallstand"] == "vorlesezeit-20261019T010000Z.db"
    text = json.dumps(stand)
    for geheim in GEHEIM.values():
        assert geheim not in text


# --- Neu starten ---------------------------------------------------------------------


def test_neustart(welt):
    client = welt.client()
    assert "Vorlesezeit neu starten?" in client.get("/wartung/neustart").text
    r = client.post("/wartung/neustart")
    assert r.headers["location"] == "/wartung/neustart/stand"
    verben = welt.fake.verben
    assert verben.index("compose stop") < verben.index("compose up")
    r = client.get("/wartung/neustart/stand")
    assert "Vorlesezeit läuft wieder" in r.text
    for text in ("Angehalten", "Vorlesezeit läuft", "Verbindung ins Internet", "Von außen"):
        assert text in r.text


def test_neustart_app_bleibt_krank(welt):
    welt.krank()
    client = welt.client()
    client.post("/wartung/neustart")
    assert welt.uhr.jetzt - 1000 >= 180
    r = client.get("/wartung/neustart/stand")
    assert (
        "Vorlesezeit startet nicht. Schick das Protokoll an die Person, die dich eingeladen hat."
        in r.text
    )
    assert 'href="/wartung/protokoll">Protokoll exportieren</a>' in r.text


def test_waehrend_eines_vorgangs_keine_zweite_aufgabe(welt):
    welt.starte = lambda f: None
    client = welt.client()
    client.post("/wartung/neustart")
    r = client.post("/wartung/deinstallieren", data={})
    assert (r.status_code, r.headers["location"]) == (303, "/wartung/neustart/stand")
    assert "compose down" not in welt.fake.verben
    assert client.get("/wartung").headers["location"] == "/wartung/neustart/stand"


# --- Anmeldelink ---------------------------------------------------------------------


def test_anmeldelink(welt, caplog):
    caplog.set_level(logging.DEBUG)
    welt.fake.antworten["compose exec"] = Ergebnis(0, "/einrichtung/geheimerToken_123\n", "")
    client = welt.client()
    assert "Nicht mehr im Admin-Bereich?" in client.get("/wartung/anmeldelink").text
    r = client.post("/wartung/anmeldelink")
    link = f"https://{HOST}/einrichtung/geheimerToken_123"
    assert f'data-kopieren="{link}"' in r.text
    assert f'href="{link}" target="_blank" rel="noopener"' in r.text
    assert r.headers["cache-control"] == "no-store"
    assert "bis morgen, 14:02 Uhr" in r.text
    assert welt.fake.aufrufe[-1][5:] == [
        "exec",
        "-T",
        "app",
        "python",
        "-m",
        "app.auth.anmeldelink",
    ]
    log = [
        r.getMessage()
        for r in caplog.records
        if r.name.startswith("installer") and "http://testserver" not in r.getMessage()
    ]
    assert all("geheimerToken_123" not in zeile for zeile in log)


def test_anmeldelink_ohne_laufende_app(welt):
    welt.krank()
    r = welt.client().post("/wartung/anmeldelink")
    assert (
        "Vorlesezeit läuft gerade nicht, deshalb gibt es keinen Anmeldelink. Starte es zuerst neu."
        in r.text
    )
    assert 'href="/wartung/neustart">Neu starten</a>' in r.text
    assert "compose exec" not in welt.fake.verben


# --- Protokoll -----------------------------------------------------------------------


def test_protokoll(welt):
    logs = (
        f"app-1  | 2026-10-19T14:02:00Z start SESSION_SECRET_KEY={GEHEIM['SESSION_SECRET_KEY']}\n"
        "app-1  | 2026-10-19T14:02:01Z GET /login/confirm?token=abc&x=1\n"
        "app-1  | 2026-10-19T14:02:02Z Einrichtungslink: /einrichtung/xyz\n"
        f"cloudflared-1 | {GEHEIM['CLOUDFLARE_TUNNEL_TOKEN']}\n"
    )
    welt.fake.antworten["compose logs"] = Ergebnis(0, logs, "")
    client = welt.client()
    assert "Protokoll herunterladen" in client.get("/wartung/protokoll").text
    r = client.get("/wartung/protokoll/herunterladen")
    datei = "vorlesezeit-protokoll-berger-2026-10-19.txt"
    assert r.headers["content-disposition"] == f'attachment; filename="{datei}"'
    assert r.headers["cache-control"] == "no-store"
    text = r.text
    for geheim in (*GEHEIM.values(), "token=abc", "/einrichtung/xyz"):
        assert geheim not in text
    assert "SESSION_SECRET_KEY=[geschwärzt]" in text
    assert "token=[Anmeldelink geschwärzt]&x=1" in text
    assert "/einrichtung/[Anmeldelink geschwärzt]" in text
    assert HOST in text and "0.3.0" in text
    for name in ("VORLESEZEIT_IMAGE", "BACKUP_TARGET_PATH", "CREDENTIALS_KEY"):
        assert name in text
    assert welt.fake.aufrufe[-1][5:] == ["logs", "--since", "168h", "--timestamps", "--no-color"]

    seite = client.get("/wartung/protokoll").text
    assert datei in seite
    assert "So sieht es aus" in seite


# --- Notfallblatt --------------------------------------------------------------------


def test_notfallblatt(welt):
    client = welt.client()
    seite = client.get("/wartung/notfallblatt").text
    assert "data-bestaetigen" not in seite
    r = client.get("/wartung/notfallblatt/herunterladen")
    assert r.content.startswith(b"%PDF-")
    assert GEHEIM["CREDENTIALS_KEY"].encode() in r.content
    assert b"0.3.0" in r.content
    assert r.headers["cache-control"] == "no-store"
    seite = client.get("/wartung/notfallblatt").text
    assert "✓ Heruntergeladen: Vorlesezeit-Notfallblatt-berger.pdf" in seite


# --- Deinstallieren ------------------------------------------------------------------


def test_deinstallieren_ohne_haken(welt):
    client = welt.client()
    seite = client.get("/wartung/deinstallieren").text
    assert "Auch alle Aufnahmen, die Datenbank und die Sicherungen löschen" in seite
    r = client.post("/wartung/deinstallieren", data={})
    assert r.headers["location"] == "/wartung/deinstallieren/stand"
    [down] = [a for a in welt.fake.aufrufe if a[5:6] == ["down"]]
    assert down[5:] == ["down", "--rmi", "all"]
    for datei in (".env", "compose.yml", "installation.json"):
        assert (welt.install / datei).exists()
    assert welt.gelesen()["deinstalliert"] is True
    assert list((welt.ziel / "db").iterdir())
    r = client.get("/wartung/deinstallieren/stand")
    assert "Vorlesezeit ist entfernt" in r.text
    assert welt.beendet == 1


def test_deinstallieren_mit_falschem_namen(welt):
    client = welt.client()
    r = client.post("/wartung/deinstallieren", data={"loeschen": "on", "name": "Berger"})
    assert "Das passt noch nicht. Tippe genau „berger“, klein und ohne Leerzeichen." in r.text
    assert "compose down" not in welt.fake.verben
    r = client.post("/wartung/deinstallieren", data={"loeschen": "on", "name": "berger "})
    assert "Das passt noch nicht." in r.text
    assert "compose down" not in welt.fake.verben


def test_deinstallieren_mit_loeschen(welt):
    (welt.install / "aktualisierung.json").write_text("{}")
    client = welt.client()
    client.post("/wartung/deinstallieren", data={"loeschen": "on", "name": "berger"})
    [down] = [a for a in welt.fake.aufrufe if a[5:6] == ["down"]]
    assert "--volumes" in down
    [run] = [a for a in welt.fake.aufrufe if a[0] == "run"]
    assert run[3] == 'type=bind,"source=/Volumes/Samsung T7",target=/ziel'
    assert run[-1] == "rm -rf /ziel/Vorlesezeit-Sicherung"
    for datei in (".env", "compose.yml", "installation.json", "aktualisierung.json"):
        assert not (welt.install / datei).exists(), datei
    r = client.get("/wartung/deinstallieren/stand")
    assert "Vorlesezeit ist gelöscht" in r.text
    assert welt.beendet == 1


def test_deinstallieren_mit_loeschen_lokaler_sicherung(welt):
    (welt.install / "Sicherung" / "db").mkdir(parents=True)
    welt.env["BACKUP_TARGET_PATH"] = "/Users/anja/Vorlesezeit/Sicherung"
    welt.schreibe_env()
    welt.client().post("/wartung/deinstallieren", data={"loeschen": "on", "name": "berger"})
    assert not (welt.install / "Sicherung").exists()
    assert not [a for a in welt.fake.aufrufe if a[0] == "run"]


def test_deinstallieren_loescht_nie_den_installationsordner(welt):
    (welt.install / "Sicherung" / "db").mkdir(parents=True)
    welt.env["BACKUP_TARGET_PATH"] = "/Users/anja/Vorlesezeit/"
    welt.schreibe_env()
    welt.client().post("/wartung/deinstallieren", data={"loeschen": "on", "name": "berger"})
    assert (welt.install / "Sicherung" / "db").is_dir()


def test_nach_deinstallieren_startet_der_start_neu(welt):
    welt.installation(deinstalliert=True)
    client = TestClient(welt.app(), follow_redirects=False)
    r = client.get(f"/?t={TOKEN}")
    assert r.headers["location"] == "/wartung/neustart/stand"
    assert "compose up" in welt.fake.verben
    assert "deinstalliert" not in welt.gelesen()


# --- Leerlauf ------------------------------------------------------------------------


def test_nach_30_minuten_ohne_anfrage_beendet_sich_der_assistent(welt):
    client = welt.client()
    [pruefe] = welt.wachen
    welt.uhr.jetzt += 29 * 60
    assert pruefe() is False
    client.get("/wartung")
    welt.uhr.jetzt += 29 * 60
    assert pruefe() is False
    welt.uhr.jetzt += 60
    assert pruefe() is True
    assert welt.beendet == 1


def test_waehrend_eines_vorgangs_kein_beenden(welt):
    welt.starte = lambda f: None
    client = welt.client()
    client.post("/wartung/neustart")
    [pruefe] = welt.wachen
    welt.uhr.jetzt += 60 * 60
    assert pruefe() is False
    assert welt.beendet == 0
