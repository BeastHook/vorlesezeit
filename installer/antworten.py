"""Die Antworten aus Bildschirm 1 bis 5. Sie liegen nur im Speicher, bis
„Installieren“ sie schreibt (Fenster zu = nichts geschrieben)."""

from __future__ import annotations

from dataclasses import dataclass, field

from installer.geraet import Messung, TlsErgebnis


@dataclass(frozen=True)
class Mail:
    host: str
    port: int
    user: str
    passwort: str
    absender: str
    aus_code: bool = False
    anbieter: str | None = None


@dataclass(frozen=True)
class Sicherung:
    pfad: str  # Pfad auf dem Gerät, landet als BACKUP_TARGET_PATH in der .env
    name: str
    lokal: bool = False


@dataclass
class Antworten:
    messung: Messung | None = None
    tls: TlsErgebnis | None = None
    erledigt: set[str] = field(default_factory=set)
    geraet_ok: bool = False
    art: str = "code"
    code_smtp: Mail | None = None
    host: str | None = None
    tunnel_token: str | None = None
    admin_email: str | None = None
    anzeigename: str = ""
    mail: Mail | None = None
    sicherung: Sicherung | None = None


SCHRITTE = ("geraet", "adresse", "du", "mail", "sicherung")


def erster_offener(a: Antworten, vor: str | None = None) -> str | None:
    """Pfad des ersten unbeantworteten Bildschirms (vor `vor`, sonst überhaupt),
    None wenn alles beisammen ist."""
    erledigt = {
        "geraet": a.geraet_ok,
        "adresse": a.host is not None,
        "du": a.admin_email is not None,
        "mail": a.mail is not None,
        "sicherung": a.sicherung is not None,
    }
    for schritt in SCHRITTE[: SCHRITTE.index(vor)] if vor else SCHRITTE:
        if not erledigt[schritt]:
            return f"/{schritt}"
    return None
