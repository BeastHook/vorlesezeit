"""Wartung und Update (One-Klick-Setup Baustein 4, Mockup docs/design/wartung-mockup.html).

Status, Abfrage des neuesten Release bei GitHub und die drei Vorgänge im
Hintergrund: Aktualisieren (mit Rückfall), Neu starten und Deinstallieren.

Das Aktualisieren schreibt seinen Stand vor jedem Schritt nach
`aktualisierung.json` (ohne Geheimnis). Ein neu gestarteter Assistent setzt
dort fort, denn die Startdatei beendet einen laufenden Assistenten mit
`docker rm -f`. Die Frist von Schritt 3 rechnet deshalb mit der Wanduhr ab dem
gespeicherten Beginn.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from app.update_hinweis import RELEASES_URL, parse_version
from installer.docker import SICHERUNGSORDNER, Docker, Ergebnis
from installer.geraet import MIN_FREI_BYTES
from installer.installation import (
    APP_IMAGE,
    AUSSEN_WARTEN_SECONDS,
    GESUND_WARTEN_SECONDS,
    TAKT_SECONDS,
    TUNNEL_WARTEN_SECONDS,
    Fehler,
    Schrittfolge,
    _letzte_zeile,
    _schreibe_installation,
    aussen_erreichbar,
    lies_env,
    lies_installation,
    setze_image,
)

logger = logging.getLogger(__name__)

BERLIN = ZoneInfo("Europe/Berlin")
LAUFWERKE_ZIEL = "/laufwerke"
SICHERUNG_FRISCH = timedelta(hours=36)  # wie app/__init__.py::BACKUP_MAX_AGE
STAND_DATEI = "aktualisierung.json"
GEHEIME_VARIABLEN = (
    "STORAGE_SECRET_KEY",
    "SESSION_SECRET_KEY",
    "TRIGGER_SECRET",
    "CREDENTIALS_KEY",
    "CLOUDFLARE_TUNNEL_TOKEN",
    "SMTPPW",
)
GESCHWAERZT = "[geschwärzt]"
LINK_GESCHWAERZT = "[Anmeldelink geschwärzt]"
PROTOKOLL_HILFE = "Schick das Protokoll an die Person, die dich eingeladen hat."
_TOKEN = re.compile(r"(token=)[^\s&\"'<>]+")
_EINRICHTUNG = re.compile(r"(/einrichtung/)[^\s?&\"'<>]+")
_STAND = re.compile(r"vorlesezeit-(\d{8}T\d{6}Z)\.db")


# --- Zeit und Version ------------------------------------------------------------------


def berlin(moment: datetime) -> datetime:
    return moment.astimezone(BERLIN)


def im_advent(jetzt: datetime) -> bool:
    heute = berlin(jetzt).date()
    return heute.month == 12 and heute.day <= 25


def wann(moment: datetime, jetzt: datetime) -> str:
    """„heute, 03:00 Uhr“, „gestern, …“, „morgen, …“ oder „vor 9 Tagen“."""
    tage = (berlin(jetzt).date() - berlin(moment).date()).days
    uhrzeit = berlin(moment).strftime("%H:%M")
    namen = {0: "heute", 1: "gestern", -1: "morgen"}
    if tage in namen:
        return f"{namen[tage]}, {uhrzeit} Uhr"
    return f"vor {tage} Tagen"


def laufende_version(env: dict[str, str]) -> str:
    """Der Tag aus VORLESEZEIT_IMAGE, etwa 0.3.0."""
    return env["VORLESEZEIT_IMAGE"].rsplit(":", 1)[-1]


def _gb(n: int) -> str:
    return f"{n // 10**9} GB"


# --- Neuestes Release ---------------------------------------------------------------------


@dataclass(frozen=True)
class Release:
    version: str
    notizen: tuple[str, ...]
    dringend: bool


@dataclass(frozen=True)
class Abfrage:
    release: Release | None
    grund: str | None  # nur bei Fehlschlag, für „Technische Einzelheiten“
    zeit: datetime


def release_abfragen(http: httpx.Client, jetzt: datetime) -> Abfrage:
    """Liest das neueste Release ohne Zugangsdaten; jeder Fehlschlag ergibt kein Release."""

    def gescheitert(grund: str) -> Abfrage:
        logger.info("Update-Abfrage fehlgeschlagen: %s", grund)
        return Abfrage(None, grund, jetzt)

    try:
        r = http.get(RELEASES_URL, headers={"Accept": "application/vnd.github+json"})
    except httpx.TimeoutException:
        return gescheitert("Zeitüberschreitung")
    except httpx.HTTPError as exc:
        return gescheitert(type(exc).__name__)
    if r.status_code != 200:
        return gescheitert(f"Status {r.status_code}")
    try:
        daten = r.json()
        tag, body = daten["tag_name"], daten.get("body") or ""
    except (ValueError, KeyError, TypeError, AttributeError):
        return gescheitert("Antwort nicht lesbar")
    if not isinstance(tag, str) or parse_version(tag) is None or not isinstance(body, str):
        return gescheitert("Antwort nicht lesbar")
    zeilen = body.splitlines()
    erste = next((z.strip() for z in zeilen if z.strip()), "")
    notizen = tuple(z[2:].strip() for z in zeilen if z.startswith(("- ", "* ")))
    return Abfrage(Release(tag.removeprefix("v"), notizen, erste == "dringend: ja"), None, jetzt)


# --- Sicherungsziel -------------------------------------------------------------------------


@dataclass(frozen=True)
class Ziel:
    host_pfad: str  # BACKUP_TARGET_PATH, Pfad auf dem Gerät
    pfad: Path | None  # derselbe Ort im Assistenten, None ohne passenden Mount
    name: str  # „Samsung T7“ oder „diesem Rechner“
    lokal: bool

    @property
    def da(self) -> bool:
        return self.pfad is not None and self.pfad.is_dir()

    @property
    def laufwerk_host(self) -> str:
        return self.host_pfad.removesuffix(SICHERUNGSORDNER).rstrip("/\\")


def finde_ziel(
    install_dir: Path, laufwerke_dir: Path, host_install_dir: str, docker: Docker, env: dict
) -> Ziel:
    host = env["BACKUP_TARGET_PATH"]
    basis = host_install_dir.rstrip("/\\")
    teile = [t for t in re.split(r"[\\/]", host) if t]
    name = teile[-2] if len(teile) > 1 and teile[-1] == SICHERUNGSORDNER else host
    kein_ziel = Ziel(host, None, name, False)
    # Lokal liegt das Ziel im Installationsordner; so geht es ohne Mount-Abgleich,
    # auch mit `\` im Windows-Pfad.
    if host.startswith(basis) and host[len(basis) : len(basis) + 1] in ("/", "\\"):
        rest = host[len(basis) :].replace("\\", "/").strip("/")
        if not _sicherer_rest(rest):
            return kein_ziel
        return Ziel(host, install_dir / rest, "diesem Rechner", True)
    container = docker.container_pfad(host)
    if container and container.startswith(LAUFWERKE_ZIEL + "/"):
        rest = container[len(LAUFWERKE_ZIEL) + 1 :]
        if not _sicherer_rest(rest):
            return kein_ziel
        return Ziel(host, laufwerke_dir / rest, rest.split("/")[0], False)
    return kein_ziel


def _sicherer_rest(rest: str) -> bool:
    # Ein leerer Rest wäre der Installations- bzw. Laufwerksordner selbst, `..` führt
    # hinaus; beides darf das Deinstallieren nie löschen.
    return bool(rest) and ".." not in rest.split("/")


def neuester_stand(ziel: Ziel) -> Path | None:
    if not ziel.da:
        return None
    staende = sorted((ziel.pfad / "db").glob("vorlesezeit-*.db"))
    return staende[-1] if staende else None


def stand_zeit(name: str) -> datetime | None:
    treffer = _STAND.fullmatch(name)
    if treffer is None:
        return None
    return datetime.strptime(treffer.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


# --- Status ---------------------------------------------------------------------------------


@dataclass
class Zeile:
    zustand: str  # ok, fehler, laeuft, wartet
    text: str
    meta: str | None = None
    knopf: tuple[str, str] | None = None  # (Text, Adresse)
    zeichen: str | None = None  # bei fehler: „!“ oder „✗“


@dataclass
class Status:
    zeilen: list[Zeile]
    gesund: bool
    probleme: int
    technik: list[str] = field(default_factory=list)


def status(
    *,
    install_dir: Path,
    ziel: Ziel,
    docker: Docker,
    http: httpx.Client,
    adresse: str,
    version: str,
    die_neueste: bool,
    jetzt: datetime,
) -> Status:
    """Die fünf Zeilen der Übersicht: Version, läuft, von außen, Sicherung, Platz."""
    technik: list[str] = []
    zeilen = [Zeile("ok", f"Version {version}", "die neueste" if die_neueste else None)]

    gesund = docker.app_gesund()
    if gesund:
        zeilen.append(Zeile("ok", "Vorlesezeit läuft"))
        tunnel = docker.tunnel_verbunden()
        aussen, antwort = aussen_erreichbar(http, adresse)
        if tunnel and aussen:
            zeilen.append(Zeile("ok", "Von außen erreichbar", "Verbindung ins Internet steht"))
        else:
            zeilen.append(
                Zeile(
                    "fehler",
                    "Von außen nicht erreichbar",
                    "Die Familie kommt gerade nicht auf die Seite. Ein Neustart behebt das meist.",
                    knopf=("Neu starten", "/wartung/neustart"),
                )
            )
            technik.append(f"cloudflared: {'verbunden' if tunnel else 'nicht verbunden'}")
            technik.append(f"GET https://{adresse}/status: {antwort}")
    else:
        zeilen.append(Zeile("fehler", "Vorlesezeit läuft nicht", zeichen="✗"))
        zeilen.append(
            Zeile("wartet", "Von außen erreichbar", "erst prüfbar, wenn Vorlesezeit läuft")
        )
        technik.append("Dienst app: nicht gesund (docker compose ps)")

    zeilen.append(_sicherungszeile(ziel, jetzt, technik))

    frei = shutil.disk_usage(install_dir).free
    meta = f"{_gb(frei)} frei auf diesem Rechner"
    knapp = frei < MIN_FREI_BYTES
    if ziel.da and not ziel.lokal:
        frei_ziel = shutil.disk_usage(ziel.pfad).free
        meta += f", {_gb(frei_ziel)} auf {ziel.name}"
        knapp = knapp or frei_ziel < MIN_FREI_BYTES
    zeilen.append(
        Zeile("fehler" if knapp else "ok", "Zu wenig Platz" if knapp else "Genug Platz", meta)
    )

    probleme = sum(z.zustand == "fehler" for z in zeilen[2:])
    return Status(zeilen, gesund, probleme, technik)


def _sicherungszeile(ziel: Ziel, jetzt: datetime, technik: list[str]) -> Zeile:
    erneut = ("Erneut prüfen", "/wartung")
    if not ziel.da:
        technik.append(f"Sicherungsziel {ziel.host_pfad}: nicht gefunden")
        if ziel.lokal:
            meta = "Der Ordner Sicherung im Ordner Vorlesezeit fehlt."
        else:
            meta = (
                f"{ziel.name} ist nicht angesteckt. "
                "Steck sie wieder an, die nächste Sicherung läuft heute Nacht."
            )
        return Zeile("fehler", "Sicherung nicht erreichbar", meta, knopf=erneut)
    stand = neuester_stand(ziel)
    zeit = stand_zeit(stand.name) if stand else None
    if zeit is None:
        technik.append(f"{ziel.host_pfad}/db: kein Stand vorlesezeit-*.db")
        return Zeile(
            "fehler", "Noch keine Sicherung", "Die erste Sicherung läuft heute Nacht.", knopf=erneut
        )
    text = f"Letzte Sicherung {wann(zeit, jetzt)}"
    if jetzt - zeit > SICHERUNG_FRISCH:
        technik.append(f"Letzter Stand: {stand.name}")
        meta = "Lass den Rechner nachts an, dann läuft die Sicherung."
        return Zeile("fehler", text, meta, knopf=erneut)
    return Zeile("ok", text, f"auf {ziel.name}")


# --- Protokoll ------------------------------------------------------------------------------


def schwaerzen(text: str, env: dict[str, str]) -> str:
    for name in GEHEIME_VARIABLEN:
        if wert := env.get(name):
            text = text.replace(wert, GESCHWAERZT)
    text = _TOKEN.sub(rf"\g<1>{LINK_GESCHWAERZT}", text)
    return _EINRICHTUNG.sub(rf"\g<1>{LINK_GESCHWAERZT}", text)


def protokoll(env: dict[str, str], adresse: str, logs: Ergebnis, jetzt: datetime) -> str:
    """Adresse, Version, die Namen der .env-Einträge und die Logs der letzten 7 Tage."""
    zeilen = [
        "Vorlesezeit: Protokoll für die Fehlersuche",
        f"Erstellt: {berlin(jetzt):%Y-%m-%d %H:%M} (Europe/Berlin)",
        f"Adresse: {adresse}",
        f"Version: {laufende_version(env)}",
        "",
        "Einträge in der .env (nur die Namen):",
        *env,
        "",
        "docker compose logs --since 168h:",
        logs.ausgabe.rstrip("\n"),
    ]
    if logs.code != 0:
        zeilen.append(f"compose logs fehlgeschlagen: {_letzte_zeile(logs.fehler)}")
    return schwaerzen("\n".join(zeilen) + "\n", env)


# --- Vorgänge -------------------------------------------------------------------------------


class Neustart(Schrittfolge):
    art = "neustart"
    SCHRITTE = ("anhalten", "start", "tunnel", "aussen")

    def __init__(self, install_dir: Path, docker: Docker, http: httpx.Client, **uhr) -> None:
        super().__init__(**uhr)
        self.install_dir = install_dir
        self.docker = docker
        self.http = http

    def _ablauf(self) -> dict[str, Callable[[], Fehler | None]]:
        return {
            "anhalten": self._anhalten,
            "start": self._start,
            "tunnel": self._tunnel,
            "aussen": self._aussen,
        }

    def ausfuehren(self) -> None:
        super().ausfuehren()
        installation = lies_installation(self.install_dir)
        if self.fertig and installation.pop("deinstalliert", None) is not None:
            _schreibe_installation(self.install_dir, installation)

    def _fehler(self, satz: str, technik: str) -> Fehler:
        return Fehler(f"{satz} {PROTOKOLL_HILFE}", technik, "Protokoll exportieren")

    def _anhalten(self) -> Fehler | None:
        r = self.docker.stop()
        if r.code != 0:
            return self._fehler("Vorlesezeit ließ sich nicht anhalten.", _letzte_zeile(r.fehler))
        return None

    def _start(self) -> Fehler | None:
        r = self.docker.up()
        if r.code != 0:
            return self._fehler("Vorlesezeit startet nicht.", _letzte_zeile(r.fehler))
        if not self._warten(self.docker.app_gesund, GESUND_WARTEN_SECONDS):
            technik = "Healthcheck des Dienstes app nach 3 Minuten nicht gesund"
            return self._fehler("Vorlesezeit startet nicht.", technik)
        return None

    def _tunnel(self) -> Fehler | None:
        if self._warten(self.docker.tunnel_verbunden, TUNNEL_WARTEN_SECONDS):
            return None
        return self._fehler(
            "Die Verbindung ins Internet kommt nicht zustande.",
            "http://cloudflared:2000/ready antwortet nach 2 Minuten nicht",
        )

    def _aussen(self) -> Fehler | None:
        adresse = lies_installation(self.install_dir)["adresse"]
        letzte = ["noch keine Antwort"]

        def antwortet() -> bool:
            ok, letzte[0] = aussen_erreichbar(self.http, adresse)
            return ok

        if self._warten(antwortet, AUSSEN_WARTEN_SECONDS):
            return None
        return self._fehler(
            "Vorlesezeit läuft, ist von außen aber nicht erreichbar.",
            f"GET https://{adresse}/status: {letzte[0]}",
        )


class Deinstallation(Schrittfolge):
    art = "deinstallieren"

    def __init__(
        self, install_dir: Path, docker: Docker, *, ziel: Ziel, image: str, loeschen: bool, **uhr
    ) -> None:
        self.SCHRITTE = (
            ("anhalten", "entfernen", "sicherungen") if loeschen else ("anhalten", "entfernen")
        )
        super().__init__(**uhr)
        self.install_dir = install_dir
        self.docker = docker
        self.ziel = ziel
        self.image = image
        self.loeschen = loeschen
        self.adresse = lies_installation(install_dir)["adresse"]

    def _ablauf(self) -> dict[str, Callable[[], Fehler | None]]:
        ablauf = {"anhalten": self._anhalten, "entfernen": self._entfernen}
        if self.loeschen:
            ablauf["sicherungen"] = self._sicherungen
        return ablauf

    def _anhalten(self) -> Fehler | None:
        r = self.docker.stop()
        if r.code != 0:
            return Fehler(
                f"Vorlesezeit ließ sich nicht anhalten. {PROTOKOLL_HILFE}",
                _letzte_zeile(r.fehler),
                "Protokoll exportieren",
            )
        return None

    def _entfernen(self) -> Fehler | None:
        r = self.docker.down(volumes=self.loeschen)
        if r.code != 0:
            return Fehler(
                f"Vorlesezeit ließ sich nicht entfernen. {PROTOKOLL_HILFE}",
                _letzte_zeile(r.fehler),
                "Protokoll exportieren",
            )
        if not self.loeschen:
            # .env, compose.yml und Volumes bleiben: der nächste Start läuft weiter.
            installation = lies_installation(self.install_dir)
            installation["deinstalliert"] = True
            _schreibe_installation(self.install_dir, installation)
        return None

    def _sicherungen(self) -> Fehler | None:
        if self.ziel.lokal:
            if self.ziel.pfad.exists():
                shutil.rmtree(self.ziel.pfad)
        else:
            r = self.docker.ordner_loeschen(self.image, self.ziel.laufwerk_host)
            if r.code != 0:
                return Fehler(
                    f"Die Sicherungen auf {self.ziel.name} ließen sich nicht löschen. "
                    f"Steck {self.ziel.name} an und versuch es noch einmal.",
                    _letzte_zeile(r.fehler),
                    "Noch einmal versuchen",
                )
        # Danach beginnt ein erneuter Start eine frische Installation.
        for name in (".env", "compose.yml", "installation.json", STAND_DATEI):
            (self.install_dir / name).unlink(missing_ok=True)
        return None


class Aktualisierung(Schrittfolge):
    """Sichern, neue Version laden und starten, prüfen; sonst Rückfall auf die vorige."""

    art = "aktualisieren"
    SCHRITTE = ("sichern", "laden", "pruefen", "rueckfall")
    REIHENFOLGE = ("sichern", "laden", "pruefen")

    def __init__(
        self,
        install_dir: Path,
        docker: Docker,
        http: httpx.Client,
        *,
        stand: dict,
        finde: Callable[[dict], Ziel],
        jetzt: Callable[[], datetime],
        **uhr,
    ) -> None:
        super().__init__(**uhr)
        self.install_dir = install_dir
        self.docker = docker
        self.http = http
        self.stand = stand
        self.finde = finde
        self.jetzt = jetzt
        self.ergebnis: str | None = None  # ok, abbruch, rueckfall
        self.rueckfall_fehler: Fehler | None = None
        self.ziel_name: str | None = None
        self.zuletzt = {"gesund": False, "aussen": False, "technik": "noch nicht geprüft"}

    @staticmethod
    def neuer_stand(alt: str, neu: str) -> dict:
        return {
            "alt": alt,
            "neu": neu,
            "rueckfallstand": None,
            "schritt": "sichern",
            "pruefen_seit": None,
        }

    @staticmethod
    def lies_stand(install_dir: Path) -> dict | None:
        pfad = install_dir / STAND_DATEI
        return json.loads(pfad.read_text()) if pfad.exists() else None

    @property
    def _env(self) -> Path:
        return self.install_dir / ".env"

    def _merken(self, schritt: str) -> None:
        self.stand["schritt"] = schritt
        text = json.dumps(self.stand, indent=2, ensure_ascii=False) + "\n"
        (self.install_dir / STAND_DATEI).write_text(text)

    def _vergessen(self) -> None:
        (self.install_dir / STAND_DATEI).unlink(missing_ok=True)

    def ausfuehren(self) -> None:
        self.laeuft = True
        try:
            if self.stand["schritt"] == "rueckfall":
                self.schritte |= {"sichern": "ok", "laden": "ok", "pruefen": "fehler"}
                self.fehler = Fehler(
                    "", "Rückfall nach einem Neustart des Assistenten fortgesetzt", ""
                )
                self._rueckfall()
                return
            beginn = self.REIHENFOLGE.index(self.stand["schritt"])
            for name in self.REIHENFOLGE[:beginn]:
                self.schritte[name] = "ok"
            for name in self.REIHENFOLGE[beginn:]:
                self._merken(name)
                self.schritte[name] = "laeuft"
                logger.info("Aktualisierung: Schritt %s läuft", name)
                fehler = self._schritt(name)
                if fehler is not None:
                    self.schritte[name] = "fehler"
                    self.fehler = fehler
                    logger.warning("Aktualisierung: Schritt %s gescheitert", name)
                    if name == "sichern":
                        self._vergessen()
                        self.ergebnis = "abbruch"
                        return
                    self._rueckfall()
                    return
                self.schritte[name] = "ok"
            installation = lies_installation(self.install_dir)
            installation["version"] = self.stand["neu"]
            _schreibe_installation(self.install_dir, installation)
            self._vergessen()
            self.ergebnis = "ok"
            logger.info("Aktualisierung auf %s fertig", self.stand["neu"])
        finally:
            self.laeuft = False
            self.fertig = True

    def _schritt(self, name: str) -> Fehler | None:
        try:
            return {"sichern": self._sichern, "laden": self._laden, "pruefen": self._pruefen}[
                name
            ]()
        except Exception as exc:  # ein Lauf im Hintergrund darf nie hängen bleiben
            logger.exception("Aktualisierung: Schritt %s: unerwarteter Fehler", name)
            return Fehler("Etwas ist schiefgegangen.", type(exc).__name__, "")

    def _sichern(self) -> Fehler | None:
        r = self.docker.sichern()
        ziel = self.finde(lies_env(self._env))
        self.ziel_name = ziel.name
        satz = "Die Sicherung hat nicht geklappt, deshalb wurde nichts verändert."
        satz += (
            f" Steck {ziel.name} an und versuch es noch einmal."
            if not ziel.lokal and not ziel.da
            else " Versuch es noch einmal."
        )
        if r.code != 0:
            technik = f"backup.py --once: {_letzte_zeile(r.fehler or r.ausgabe)}"
            return Fehler(satz, technik, "Noch einmal versuchen")
        stand = neuester_stand(ziel)
        if stand is None:
            technik = f"{ziel.host_pfad}/db: kein Stand vorlesezeit-*.db"
            return Fehler(satz, technik, "Noch einmal versuchen")
        self.stand["rueckfallstand"] = stand.name
        return None

    def _laden(self) -> Fehler | None:
        image = f"{APP_IMAGE}:{self.stand['neu']}"
        setze_image(self._env, image)
        for befehl in (self.docker.pull, self.docker.up):
            r = befehl()
            if r.code != 0:
                return Fehler(
                    "Die neue Version ließ sich nicht laden oder starten.",
                    f"{image}: {_letzte_zeile(r.fehler)}",
                    "",
                )
        return None

    def _alles_da(self) -> bool:
        adresse = lies_installation(self.install_dir)["adresse"]
        gesund = self.docker.app_gesund()
        tunnel = gesund and self.docker.tunnel_verbunden()
        aussen, antwort = (
            aussen_erreichbar(self.http, adresse) if tunnel else (False, "nicht geprüft")
        )
        self.zuletzt = {
            "gesund": gesund,
            "aussen": tunnel and aussen,
            "technik": f"app {'gesund' if gesund else 'nicht gesund'}, "
            f"cloudflared {'verbunden' if tunnel else 'nicht verbunden'}, "
            f"GET https://{adresse}/status: {antwort}",
        }
        return gesund and tunnel and aussen

    def _pruefen(self) -> Fehler | None:
        if not self.stand.get("pruefen_seit"):
            self.stand["pruefen_seit"] = self.jetzt().isoformat()
            self._merken("pruefen")
        ende = datetime.fromisoformat(self.stand["pruefen_seit"]) + timedelta(
            seconds=GESUND_WARTEN_SECONDS
        )
        while not self._alles_da():
            if self.jetzt() >= ende:
                return Fehler("", f"{self.stand['neu']} nach 180 s: {self.zuletzt['technik']}", "")
            self.schlafen(TAKT_SECONDS)
        return None

    def seit_pruefen(self) -> timedelta | None:
        if not self.stand.get("pruefen_seit"):
            return None
        return self.jetzt() - datetime.fromisoformat(self.stand["pruefen_seit"])

    def _rueckfall(self) -> None:
        alt, datei = self.stand["alt"], self.stand["rueckfallstand"]
        self._merken("rueckfall")
        self.schritte["rueckfall"] = "laeuft"
        logger.warning("Aktualisierung: Rückfall auf %s", alt)
        self.ergebnis = "rueckfall"
        schritte = (
            lambda: self.docker.stop("app", "scheduler", "backup"),
            self.docker.db_beiseite,
            lambda: self.docker.rueckspielen(datei),
            self.docker.up,
        )
        try:
            setze_image(self._env, f"{APP_IMAGE}:{alt}")
            for befehl in schritte:
                r = befehl()
                if r.code != 0:
                    self._rueckfall_gescheitert(_letzte_zeile(r.fehler))
                    return
        except Exception as exc:  # ein Lauf im Hintergrund darf nie hängen bleiben
            logger.exception("Aktualisierung: Rückfall: unerwarteter Fehler")
            self._rueckfall_gescheitert(type(exc).__name__)
            return
        self._warten(self._alles_da, GESUND_WARTEN_SECONDS)
        self.schritte["rueckfall"] = "ok"
        self._vergessen()
        logger.info("Aktualisierung: Rückfall auf %s fertig", alt)

    def _rueckfall_gescheitert(self, technik: str) -> None:
        # Der Stand bleibt: ein neuer Start des Assistenten versucht den Rückfall erneut.
        self.schritte["rueckfall"] = "fehler"
        self.rueckfall_fehler = Fehler("", technik, "")
        logger.warning("Aktualisierung: Rückfall gescheitert")
