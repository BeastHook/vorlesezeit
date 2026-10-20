"""Bildschirm 6: Dateien schreiben und die Installationsschritte.

Erst „Installieren“ schreibt etwas: `.env` (nur für den Besitzer lesbar),
`compose.yml` (Kopie der Vorlage) und `installation.json` (ohne Geheimnis).
Eine vorhandene `.env` bekommt die neuen Antworten, ihre Geheimnisse gelten weiter.
Jeder Schritt ist wiederholbar; ein zweiter Lauf endet im selben Zustand.
Der Lauf liest alles aus den Dateien, damit er auch nach einem Neustart des
Assistenten weiterläuft.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from cryptography.fernet import Fernet

from app import __version__
from installer.antworten import Antworten
from installer.docker import Docker

logger = logging.getLogger(__name__)

VORLAGE = Path(__file__).resolve().parent.parent / "deploy/freunde/compose.yml"
APP_IMAGE = "ghcr.io/beasthook/vorlesezeit"
SMTP_VARIABLEN = ("SMTP_HOST", "SMTP_PORT", "SMTPUSER", "SMTPPW", "SMTP_FROM_ADDRESS")
ANTWORT_VARIABLEN = (
    "ADMIN_EMAIL",
    "ADMIN_DISPLAY_NAME",
    "CLOUDFLARE_TUNNEL_TOKEN",
    "BACKUP_TARGET_PATH",
    *SMTP_VARIABLEN,
)
LOKALER_SICHERUNGSORDNER = "Sicherung"

SCHRITTE = ("einstellungen", "laden", "start", "tunnel", "aussen")
GESUND_WARTEN_SECONDS = 180
TUNNEL_WARTEN_SECONDS = 120
AUSSEN_WARTEN_SECONDS = 300
TAKT_SECONDS = 5


# --- Dateien -------------------------------------------------------------------


def env_tauglich(wert: str) -> bool:
    # compose liest die Datei nicht mehr, wenn ein Wert auf `\` endet oder `\`
    # direkt vor `'` enthält (beides mit echtem compose beobachtet). Ein
    # Zeilenumbruch beendet die Zeile.
    return not (wert.endswith("\\") or "\\'" in wert or "\n" in wert or "\r" in wert)


def _zeile(name: str, wert: str) -> str:
    # Einfache Anführungszeichen: compose nimmt den Wert wörtlich, auch mit $, #
    # und \. Nur `\'` wird zu `'`.
    if not env_tauglich(wert):
        raise ValueError(f"{name}: Wert lässt sich nicht in die .env schreiben")
    return f"{name}='" + wert.replace("'", "\\'") + "'\n"


def lies_env(pfad: Path) -> dict[str, str]:
    werte = {}
    for nummer, zeile in enumerate(pfad.read_text().splitlines(), 1):
        if not zeile.strip() or zeile.lstrip().startswith("#") or "=" not in zeile:
            continue
        name, wert = zeile.split("=", 1)
        if wert.startswith("'"):
            # Wie compose: Ende ist das erste `'`, vor dem eine gerade Zahl `\` steht.
            ende = next(
                (
                    i
                    for i in range(1, len(wert))
                    if wert[i] == "'" and (i - 1 - len(wert[1:i].rstrip("\\"))) % 2 == 0
                ),
                None,
            )
            if ende != len(wert) - 1:
                raise ValueError(f"Zeile {nummer}: Anführungszeichen nicht geschlossen")
            wert = wert[1:ende].replace("\\'", "'")
        werte[name.strip()] = wert
    return werte


def _env_werte(a: Antworten) -> dict[str, str]:
    return {
        "VORLESEZEIT_IMAGE": f"{APP_IMAGE}:{__version__}",
        "STORAGE_ACCESS_KEY": "vorlesezeit",
        "STORAGE_SECRET_KEY": secrets.token_urlsafe(32),
        "SESSION_SECRET_KEY": secrets.token_urlsafe(32),
        "TRIGGER_SECRET": secrets.token_urlsafe(32),
        "CREDENTIALS_KEY": Fernet.generate_key().decode(),
        "ADMIN_EMAIL": a.admin_email,
        "ADMIN_DISPLAY_NAME": a.anzeigename,
        "CLOUDFLARE_TUNNEL_TOKEN": a.tunnel_token,
        "BACKUP_TARGET_PATH": a.sicherung.pfad,
        # Startwerte: nach dem ersten gesunden Start liegen sie verschlüsselt in
        # der Datenbank, dann streicht der Lauf diese Zeilen (smtp_entfernen).
        "SMTP_HOST": a.mail.host,
        "SMTP_PORT": str(a.mail.port),
        "SMTPUSER": a.mail.user,
        "SMTPPW": a.mail.passwort,
        "SMTP_FROM_ADDRESS": a.mail.absender,
    }


def _env_inhalt(werte: dict[str, str]) -> str:
    kopf = "# Von der Vorlesezeit-Einrichtung geschrieben. Enthält Geheimnisse, nicht teilen.\n"
    return kopf + "".join(_zeile(k, v) for k, v in werte.items())


def _schreibe_privat(pfad: Path, inhalt: str, *, neu: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if neu else os.O_TRUNC)
    with os.fdopen(os.open(pfad, flags, 0o600), "w") as datei:
        datei.write(inhalt)


def _ersetze_privat(env: Path, inhalt: str) -> None:
    neu = env.with_name(".env.neu")
    neu.unlink(missing_ok=True)
    _schreibe_privat(neu, inhalt, neu=True)
    os.replace(neu, env)


def schreibe_dateien(install_dir: Path, a: Antworten) -> None:
    (install_dir / "compose.yml").write_bytes(VORLAGE.read_bytes())
    env = install_dir / ".env"
    werte = _env_werte(a)
    if env.exists():
        # Nach einem Fehlschlag: neue Antworten, alles andere aus der vorhandenen
        # .env, sonst passen Datenbank und Speicher nicht mehr zu den Geheimnissen.
        alt = lies_env(env)
        if "SMTPPW" not in alt:  # schon in der Datenbank, siehe smtp_entfernen
            for name in SMTP_VARIABLEN:
                del werte[name]
        werte |= {k: v for k, v in alt.items() if k not in ANTWORT_VARIABLEN}
    _ersetze_privat(env, _env_inhalt(werte))
    if a.sicherung.lokal:
        (install_dir / LOKALER_SICHERUNGSORDNER).mkdir(exist_ok=True)
    daten = {
        "adresse": a.host,
        "admin_email": a.admin_email,
        "anzeigename": a.anzeigename,
        "version": __version__,
        "abgeschlossen": False,
    }
    _schreibe_installation(install_dir, daten)
    logger.info("Schritt einstellungen: .env, compose.yml und installation.json geschrieben")


def _schreibe_installation(install_dir: Path, daten: dict) -> None:
    text = json.dumps(daten, indent=2, ensure_ascii=False) + "\n"
    (install_dir / "installation.json").write_text(text)


def lies_installation(install_dir: Path) -> dict | None:
    pfad = install_dir / "installation.json"
    return json.loads(pfad.read_text()) if pfad.exists() else None


def setze_abgeschlossen(install_dir: Path) -> None:
    daten = lies_installation(install_dir)
    daten["abgeschlossen"] = True
    _schreibe_installation(install_dir, daten)


def setze_image(env: Path, image: str) -> None:
    """Nur die Zeile VORLESEZEIT_IMAGE ändern, alle anderen bleiben bytegleich."""
    zeilen = env.read_text().splitlines(keepends=True)
    neu = [
        _zeile("VORLESEZEIT_IMAGE", image) if z.startswith("VORLESEZEIT_IMAGE=") else z
        for z in zeilen
    ]
    _ersetze_privat(env, "".join(neu))


def smtp_entfernen(env: Path) -> None:
    zeilen = env.read_text().splitlines(keepends=True)
    rest = [z for z in zeilen if not z.startswith(tuple(f"{n}=" for n in SMTP_VARIABLEN))]
    if rest != zeilen:
        _ersetze_privat(env, "".join(rest))


# --- Installationsschritte -------------------------------------------------------


@dataclass(frozen=True)
class Fehler:
    satz: str
    technik: str
    knopf: str  # "Wiederholen" oder "Erneut prüfen"


def _letzte_zeile(text: str) -> str:
    zeilen = [z.strip() for z in text.splitlines() if z.strip()]
    return zeilen[-1] if zeilen else "ohne Meldung"


class Schrittfolge:
    """Schritte nacheinander im Hintergrund; der erste Fehler beendet die Folge."""

    SCHRITTE: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        uhr: Callable[[], float] = time.monotonic,
        schlafen: Callable[[float], None] = time.sleep,
    ) -> None:
        self.uhr = uhr
        self.schlafen = schlafen
        self.schritte = dict.fromkeys(self.SCHRITTE, "wartet")
        self.fehler: Fehler | None = None
        self.fertig = False
        self.laeuft = False

    def _ablauf(self) -> dict[str, Callable[[], Fehler | None]]:
        raise NotImplementedError

    def ausfuehren(self) -> None:
        self.schritte = dict.fromkeys(self.SCHRITTE, "wartet")
        self.fehler = None
        self.fertig = False
        self.laeuft = True
        try:
            for name, schritt in self._ablauf().items():
                self.schritte[name] = "laeuft"
                logger.info("Schritt %s läuft", name)
                try:
                    fehler = schritt()
                except Exception as exc:  # ein Lauf im Hintergrund darf nie hängen bleiben
                    logger.exception("Schritt %s: unerwarteter Fehler", name)
                    fehler = Fehler(
                        "Etwas ist schiefgegangen. Versuch es noch einmal.",
                        type(exc).__name__,
                        "Wiederholen",
                    )
                if fehler is not None:
                    self.schritte[name] = "fehler"
                    self.fehler = fehler
                    logger.warning("Schritt %s gescheitert", name)
                    return
                self.schritte[name] = "ok"
                logger.info("Schritt %s fertig", name)
            self.fertig = True
        finally:
            self.laeuft = False

    def _warten(self, pruefe: Callable[[], bool], sekunden: float) -> bool:
        ende = self.uhr() + sekunden
        while True:
            if pruefe():
                return True
            if self.uhr() >= ende:
                return False
            self.schlafen(TAKT_SECONDS)


def aussen_erreichbar(http: httpx.Client, host: str) -> tuple[bool, str]:
    """`GET https://<host>/status` von außen: (Status 200?, kurze Beschreibung)."""
    try:
        r = http.get(f"https://{host}/status")
    except httpx.TimeoutException:
        return False, "Zeitüberschreitung"
    except httpx.HTTPError as exc:
        return False, type(exc).__name__
    return r.status_code == 200, f"Status {r.status_code}"


class Lauf(Schrittfolge):
    SCHRITTE = SCHRITTE

    def __init__(
        self,
        install_dir: Path,
        docker: Docker,
        http: httpx.Client,
        *,
        uhr: Callable[[], float] = time.monotonic,
        schlafen: Callable[[float], None] = time.sleep,
        architektur: str | None,
    ) -> None:
        super().__init__(uhr=uhr, schlafen=schlafen)
        self.install_dir = install_dir
        self.docker = docker
        self.http = http
        self.architektur = architektur

    def _ablauf(self) -> dict[str, Callable[[], Fehler | None]]:
        return {
            "einstellungen": self._einstellungen,
            "laden": self._laden,
            "start": self._start,
            "tunnel": self._tunnel,
            "aussen": self._aussen,
        }

    def _einstellungen(self) -> Fehler | None:
        if lies_installation(self.install_dir) is None or not (self.install_dir / ".env").exists():
            return Fehler(
                "Die Einstellungen fehlen. Geh bitte zurück zur Zusammenfassung.",
                f"{self.install_dir}: .env oder installation.json fehlt",
                "Wiederholen",
            )
        return None

    def _laden(self) -> Fehler | None:
        image = lies_env(self.install_dir / ".env").get("VORLESEZEIT_IMAGE", "?")
        r = self.docker.pull()
        if r.code == 0:
            return None
        return Fehler(
            "Das Programm ließ sich nicht herunterladen. "
            "Prüf die Internetverbindung und versuch es noch einmal.",
            f"{image}\nGerät: linux/{self.architektur}\nFehler: {_letzte_zeile(r.fehler)}\n"
            '(Bei "no matching manifest": Architektur wird nicht unterstützt.)',
            "Wiederholen",
        )

    def _start(self) -> Fehler | None:
        r = self.docker.up()
        if r.code != 0:
            return Fehler(
                "Vorlesezeit ließ sich nicht starten. Versuch es noch einmal.",
                _letzte_zeile(r.fehler),
                "Wiederholen",
            )
        if not self._warten(self.docker.app_gesund, GESUND_WARTEN_SECONDS):
            return Fehler(
                "Vorlesezeit startet nicht richtig. Versuch es noch einmal.",
                "Healthcheck des Dienstes app nach 3 Minuten nicht gesund",
                "Wiederholen",
            )
        smtp_entfernen(self.install_dir / ".env")
        return None

    def _tunnel(self) -> Fehler | None:
        if self._warten(self.docker.tunnel_verbunden, TUNNEL_WARTEN_SECONDS):
            return None
        return Fehler(
            "Die Verbindung ins Internet kommt nicht zustande. "
            "Prüf den Einladungscode oder das Tunnel-Token.",
            "http://cloudflared:2000/ready antwortet nach 2 Minuten nicht",
            "Wiederholen",
        )

    def _aussen(self) -> Fehler | None:
        installation = lies_installation(self.install_dir)
        host = installation["adresse"]
        letzte = ["noch keine Antwort"]

        def antwortet() -> bool:
            ok, letzte[0] = aussen_erreichbar(self.http, host)
            return ok

        if not self._warten(antwortet, AUSSEN_WARTEN_SECONDS):
            return Fehler(
                "Die Adresse antwortet von außen noch nicht. "
                "Das dauert manchmal länger, prüf es gleich noch einmal.",
                f"Tunnel verbunden\nGET https://{host}/status: {letzte[0]}",
                "Erneut prüfen",
            )
        # Nur die öffentliche Adresse ergibt einen richtigen https-Anmeldelink.
        try:
            r = self.http.post(f"https://{host}/login", data={"email": installation["admin_email"]})
            grund = f"Status {r.status_code}" if r.status_code >= 400 else None
        except httpx.HTTPError as exc:
            grund = type(exc).__name__
        if grund is None:
            logger.info("Schritt aussen: Anmeldelink angefordert")
            return None
        return Fehler(
            "Der Anmeldelink ließ sich nicht verschicken. Prüf es gleich noch einmal.",
            f"POST https://{host}/login: {grund}",
            "Erneut prüfen",
        )
