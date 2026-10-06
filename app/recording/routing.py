"""Einstiegslogik der Aufnahmeflaeche (U7, R3-R5, R25/AE9, R24; Mehrkalender U12).

Getrennt von den HTTP-Routen (views.py), weil dieselbe "naechster offener
Auftrag"-Logik an zwei Stellen gebraucht wird: beim Login (R3) und nach einer
erfolgreichen Einreichung (R25) -- und weil sie ohne FastAPI-Unterbau
testbar bleiben soll. Die Familie sieht Auftraege, keine Tuerchen (R10,
KTD14); die Reihenfolge folgt dem fruehesten Kalendertag.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app import settings
from app.admin.state import (
    auftraege_for,
    auftrag_active_beitrag,
    auftrag_is_open,
    open_auftraege_for,
)
from app.models import Auftrag, Person, Slot

MAX_TITLE_LENGTH = 100


def is_slot_open(db: Session, slot: Slot) -> bool:
    """Ein Kalendertag ist offen, solange sein Auftrag keine eingereichte,
    nicht abgelehnte Aufnahme hat (R32: 'eingereicht' erst nach
    erfolgreicher Uebertragung; R13: eine Ablehnung oeffnet wieder)."""
    return slot.auftrag is None or auftrag_active_beitrag(slot.auftrag) is None


def earliest_open_auftrag(db: Session, person: Person) -> Auftrag | None:
    """R3/R25: der offene Auftrag mit dem fruehesten Kalendertag; Entwuerfe
    zaehlen nicht (R36)."""
    return next(iter(open_auftraege_for(db, person)), None)


@dataclass
class AuftragOverviewRow:
    auftrag: Auftrag
    status: str  # "offen" | "eingereicht" | "freigegeben"


def _overview_status(auftrag: Auftrag) -> str:
    if auftrag_is_open(auftrag):
        return "offen"
    beitrag = auftrag_active_beitrag(auftrag)
    # R10: nach der Freigabe laesst sich nichts mehr ersetzen -- die Karte darf
    # dann kein "Neu aufnehmen" mehr anbieten.
    if beitrag is not None and beitrag.approved_at is not None:
        return "freigegeben"
    return "eingereicht"


def person_auftraege_overview(db: Session, person: Person) -> list[AuftragOverviewRow]:
    """R4-Zielseite "Meine Geschichten": jeder Auftrag mit Kalendertag einmal,
    auch bereits eingereichte -- der Weg zur Neuaufnahme vor der Freigabe (R10)."""
    return [AuftragOverviewRow(a, _overview_status(a)) for a in auftraege_for(db, person)]


@dataclass(frozen=True)
class AdminNames:
    """R26: der Anzeigename des Admins in Familientexten, je Fall gebeugt.
    Ohne Namen bleibt es bei "der Admin"."""

    nom: str
    Nom: str  # am Satzanfang
    dat: str
    acc: str


def admin_names(db: Session) -> AdminNames:
    name = settings.admin_display_name(db)
    if name:
        return AdminNames(name, name, name, name)
    return AdminNames("der Admin", "Der Admin", "dem Admin", "den Admin")


def sanitize_title(raw: str) -> str | None:
    """R24: von Zeilenumbruechen und Steuerzeichen befreit, auf eine feste
    Hoechstlaenge begrenzt. Ein leeres Ergebnis wird als None behandelt --
    konsistent mit Slot.title/Beitrag.title, wo None die Fallback-Kette in
    app/delivery/job.py::chapter_title_for ausloest (R42)."""
    cleaned = "".join(ch if ch.isprintable() else (" " if ch.isspace() else "") for ch in raw)
    cleaned = " ".join(cleaned.split())
    cleaned = cleaned[:MAX_TITLE_LENGTH]
    return cleaned or None
