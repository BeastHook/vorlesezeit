"""U9/Mehrkalender U12: wer im Familienarchiv was sieht (R22, R23, R32, R35).

Eine Stelle fuer die Regel -- Liste und Audio-Route fragen beide hier.
Tagesgrenze nach KTD10: ein fremder Beitrag oeffnet sich um Mitternacht
(Europe/Berlin) nach seinem Kalendertag. `today` ist das Berliner Datum.

R32: fremde Beitraege nur aus Kalendern, in denen die Person aktuell einen
Auftrag mit Kalendertag hat; liegt eine Geschichte in mehreren davon, zaehlt
ihr fruehester Tag. Der Admin sieht alle Kalender, ebenfalls ab dem Tag danach.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.admin.state import ADVENT_YEAR, auftrag_slots
from app.models import Auftrag, Beitrag, Person, Slot
from app.recording.routing import AdminNames, admin_names

# R10: ohne Titel kein Rueckfall auf eine Tuerchennummer.
FALLBACK_TITLE = "Eigene Geschichte"


def last_open_day(today: date) -> int:
    """Hoechster Adventstag, dessen Geschichte heute schon offen ist (0 = keiner)."""
    return max(0, min(24, (today - date(ADVENT_YEAR, 12, 1)).days))


def _family_query(person: Person):
    """Die Regel fuer fremde Beitraege -- die einzige Definition, fuer Liste
    und Audio-Route: freigegeben, nicht abgelehnt, nicht geloest, an einem
    Auftrag mit Kalendertag in einem Kalender der Person (freie Nachrichten
    nie). Liefert die Abfrage (Beitrag, fruehester Tag) und die Tagesspalte."""
    days = select(Slot.auftrag_id, func.min(Slot.day).label("day")).where(
        Slot.auftrag_id.is_not(None)
    )
    if not person.is_admin:
        member_calendars = (
            select(Slot.campaign_id)
            .join(Auftrag, Slot.auftrag_id == Auftrag.id)
            .where(Auftrag.person_id == person.id)
        )
        days = days.where(Slot.campaign_id.in_(member_calendars))
    days = days.group_by(Slot.auftrag_id).subquery()
    query = (
        select(Beitrag, days.c.day)
        .join(days, Beitrag.auftrag_id == days.c.auftrag_id)
        .options(selectinload(Beitrag.person), selectinload(Beitrag.auftrag))
        .where(
            Beitrag.person_id != person.id,
            Beitrag.audio_object_key.is_not(None),
            Beitrag.approved_at.is_not(None),
            Beitrag.rejected_at.is_(None),
            Beitrag.detached_at.is_(None),
        )
    )
    return query, days.c.day


def can_hear(db: Session, person: Person, beitrag: Beitrag, today: date) -> bool:
    if beitrag.audio_object_key is None:
        return False
    if beitrag.person_id == person.id:
        return True
    query, day = _family_query(person)
    visible = query.where(Beitrag.id == beitrag.id, day <= last_open_day(today))
    return db.execute(visible).first() is not None


def web_title(beitrag: Beitrag) -> str:
    """Der Titel wie auf dem Tonie (R42: Kapitelname, Auftragstitel,
    Einreichungstitel), im Rueckfall ohne Tuerchennummer."""
    auftrag_title = beitrag.auftrag.title if beitrag.auftrag is not None else None
    return beitrag.chapter_title or auftrag_title or beitrag.title or FALLBACK_TITLE


@dataclass(frozen=True)
class ArchiveEntry:
    beitrag: Beitrag
    title: str
    sub: str
    day: int | None = None
    tag: tuple[str, str] | None = None  # (Text, CSS-Klassen)
    playable: bool = True
    rerecord_href: str | None = None


@dataclass(frozen=True)
class Archive:
    own: list[ArchiveEntry]
    family: list[ArchiveEntry]
    locked_day: int | None  # heutiger Tag, falls fuer ihn schon etwas bereitliegt
    first_opening: bool  # noch keine fremde Geschichte offen


def _own_entry(beitrag: Beitrag, person: Person, admin: AdminNames) -> ArchiveEntry:
    """R10: die eigenen Aufnahmen ohne Tuerchennummer."""
    auftrag = beitrag.auftrag
    only_us = f"Hören nur du und {admin.nom}."
    if beitrag.detached_at is not None:
        return ArchiveEntry(
            beitrag,
            beitrag.title or "Frühere Aufnahme",
            f"Deine Geschichte wurde neu vergeben. {only_us}",
        )
    if auftrag is None:
        return ArchiveEntry(
            beitrag,
            beitrag.title or "Freie Nachricht",
            only_us,
            tag=("Freie Nachricht", "tag tag-accent-2"),
        )
    sub = "Deine Geschichte"
    if beitrag.rejected_at is not None:
        own_open = auftrag.person_id == person.id and bool(auftrag.slots)
        return ArchiveEntry(
            beitrag,
            web_title(beitrag),
            sub,
            tag=("abgelehnt, bitte neu aufnehmen", "tag"),
            playable=False,
            rerecord_href=f"/record/auftrag/{auftrag.id}" if own_open else None,
        )
    if beitrag.approved_at is not None:
        return ArchiveEntry(
            beitrag, web_title(beitrag), sub, tag=("freigegeben", "tag tag-accent-2")
        )
    return ArchiveEntry(
        beitrag, web_title(beitrag), sub, tag=("wartet auf Freigabe", "tag tag-neutral")
    )


def _own_sort_key(beitrag: Beitrag) -> tuple:
    """Geschichten nach ihrem fruehesten Tag, dann Geloestes, freie Nachrichten zuletzt."""
    slots = auftrag_slots(beitrag.auftrag) if beitrag.auftrag is not None else []
    free = beitrag.auftrag is None and beitrag.detached_at is None
    return (free, beitrag.auftrag is None, slots[0].day if slots else 0, beitrag.id)


def build_archive(db: Session, person: Person, today: date) -> Archive:
    own_rows = (
        db.execute(
            select(Beitrag)
            .where(Beitrag.person_id == person.id, Beitrag.audio_object_key.is_not(None))
            .options(selectinload(Beitrag.auftrag).selectinload(Auftrag.slots))
        )
        .scalars()
        .all()
    )
    admin = admin_names(db)

    open_until = last_open_day(today)
    public, day = _family_query(person)
    family_rows = db.execute(public.where(day <= open_until).order_by(day.desc(), Beitrag.id)).all()
    today_day = today.day if today.year == ADVENT_YEAR and today.month == 12 else None
    locked_day = None
    if today_day is not None and today_day <= 24:
        pending_today = db.execute(public.where(day == today_day).limit(1)).first()
        locked_day = today_day if pending_today else None

    return Archive(
        own=[_own_entry(b, person, admin) for b in sorted(own_rows, key=_own_sort_key)],
        family=[
            ArchiveEntry(
                b,
                web_title(b),
                f"Gesprochen von {b.person.display_name or 'jemandem aus der Familie'}",
                b_day,
            )
            for b, b_day in family_rows
        ],
        locked_day=locked_day,
        first_opening=open_until == 0,
    )
