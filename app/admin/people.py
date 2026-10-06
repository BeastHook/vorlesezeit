"""Admin-Reiter Personen: Uebersicht, Erinnerung, Einladung, Widerruf (U8 Schritte 6, 10),
Loeschen ohne Aufnahmen (2026-10-04)."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.admin.common import berlin_now, delivery_time_at, flash, login_url, redirect, render
from app.admin.state import auftraege_for, auftrag_is_open, auftrag_slots, slot_state
from app.auth.dependencies import get_db, require_admin
from app.mail.magic_link import send_invitation_mail
from app.mail.reminder import send_reminder_mail
from app.models import Auftrag, Beitrag, Person, Slot
from app.settings import get_einstellungen, invitation_date, recording_deadline

router = APIRouter(prefix="/admin")


@dataclass
class PersonRow:
    person: Person
    days: list[int]
    open_days: list[int]
    # R15: je Kalendertag "Kalender · Tag" (ausgeliefert hervorgehoben),
    # Entwuerfe mit ihrem Titel.
    chips: list[tuple[str, bool]]
    drafts: list[str]
    # "Eingeladen am 4. Okt.", "Erinnert am 12. Nov." -- je Teil ein Span, damit
    # nur am Trenner umgebrochen wird.
    status: list[str]
    # R23: ab dem Einladungstermin noch ohne Einladungsmail.
    uninvited: bool = False

    @property
    def name(self) -> str:
        return self.person.display_name or self.person.email


_MONTHS = "Jan. Feb. März Apr. Mai Juni Juli Aug. Sept. Okt. Nov. Dez.".split()


def _date(value) -> str:
    return f"{value.day}. {_MONTHS[value.month - 1]}"


def _status(person: Person) -> list[str]:
    """Einladung: erster Versand (R23). Erinnerung: letzter Versand."""
    invited = person.invited_at
    status = [f"Eingeladen am {_date(invited)}" if invited else "Noch nicht eingeladen"]
    if person.reminded_at:
        status.append(f"Erinnert am {_date(person.reminded_at)}")
    elif invited:
        status.append("Noch nicht erinnert")
    return status


def _chips(db: Session, person: Person, *, now, delivery_time) -> list[tuple[str, bool]]:
    chips = []
    for auftrag in auftraege_for(db, person):
        for slot in auftrag_slots(auftrag):
            state = slot_state(db, slot, now=now, delivery_time=delivery_time)
            chips.append((f"{slot.campaign.name} · Tag {slot.day}", state.key == "ausgeliefert"))
    return chips


def _drafts(db: Session, person: Person) -> list[str]:
    drafts = db.execute(
        select(Auftrag)
        .where(Auftrag.person_id == person.id, ~Auftrag.slots.any())
        .order_by(Auftrag.id)
    ).scalars()
    return [a.title or "ohne Titel" for a in drafts]


def _names(names: list[str]) -> str:
    """ "A", "A und B", "A, B und C"."""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" und {names[-1]}"


def _open_days(db: Session, person: Person) -> tuple[list[int], list[int]]:
    """R11/R36: je Auftrag mit Kalendertag sein fruehester Tag -- ein Auftrag
    in zwei Kalendern ist eine Aufnahme und zaehlt einmal; Entwuerfe nicht."""
    auftraege = auftraege_for(db, person)
    first_day = {a.id: auftrag_slots(a)[0].day for a in auftraege}
    return (
        [first_day[a.id] for a in auftraege],
        [first_day[a.id] for a in auftraege if auftrag_is_open(a)],
    )


def _get_relative(db: Session, person_id: int) -> Person:
    """Nur gewoehnliche Personen -- der Admin selbst ist hier nicht verwaltbar."""
    person = db.get(Person, person_id)
    if person is None or person.is_admin:
        raise HTTPException(status_code=404)
    return person


@router.get("/personen", response_class=HTMLResponse)
def personen(request: Request, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    persons = (
        db.execute(select(Person).where(Person.is_admin.is_(False)).order_by(Person.id))
        .scalars()
        .all()
    )
    now = berlin_now(request)
    delivery_time = delivery_time_at(db, now)
    invite_from = invitation_date(db)
    invitations_due = invite_from is not None and now.date() >= invite_from
    rows = []
    for p in persons:
        days, open_days = _open_days(db, p)
        rows.append(
            PersonRow(
                p,
                days,
                open_days,
                _chips(db, p, now=now, delivery_time=delivery_time),
                _drafts(db, p),
                _status(p),
                # R23/R36: nicht widerrufen, mindestens ein Auftrag mit
                # Kalendertag, noch keine Einladungsmail.
                uninvited=invitations_due
                and bool(days)
                and p.invited_at is None
                and p.access_version == 0,
            )
        )
    uninvited = sorted((r.name for r in rows if r.uninvited), key=lambda name: name.casefold())
    free_count = db.execute(
        select(func.count(Beitrag.id)).where(
            Beitrag.auftrag_id.is_(None),
            Beitrag.detached_at.is_(None),
            Beitrag.audio_object_key.is_not(None),
        )
    ).scalar_one()
    overdue = now.date() > recording_deadline(db)
    return render(
        request,
        "admin/personen.html",
        {
            "rows": rows,
            "free_count": free_count,
            "overdue": overdue,
            "invite_from": invite_from if invitations_due else None,
            "uninvited": _names(uninvited) if uninvited else None,
        },
        tab="personen",
    )


@router.post("/personen/{person_id}/erinnern", response_class=HTMLResponse)
def erinnern(
    person_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    person = _get_relative(db, person_id)
    name = person.display_name or person.email
    _, open_days = _open_days(db, person)
    if not open_days:
        # AE20: ohne offene Tuerchen nicht ausloesbar, auch serverseitig.
        flash(request, f"{name} hat keine offenen Türchen, keine Erinnerung verschickt.", "err")
        return redirect("/admin/personen")
    try:
        send_reminder_mail(
            request.app.state.config,
            session=db,
            to_address=person.email,
            display_name=person.display_name,
            open_days=open_days,
            login_url=login_url(request, person),
        )
    except Exception:
        flash(request, f"Erinnerung an {name} konnte nicht verschickt werden.", "err")
    else:
        person.reminded_at = berlin_now(request)
        db.commit()
        flash(request, f"Erinnerung an {name} verschickt.")
    return redirect("/admin/personen")


@router.post("/personen/{person_id}/einladen", response_class=HTMLResponse)
def einladen(
    person_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    # F6: die Einladung traegt den Magic-Link, mit eigenem, langem Text. R23: eingeladen ist, wer
    # eine Einladungsmail bekam -- der erste erfolgreiche Versand zaehlt.
    person = _get_relative(db, person_id)
    name = person.display_name or person.email
    try:
        send_invitation_mail(
            request.app.state.config,
            session=db,
            to_address=person.email,
            login_url=login_url(request, person),
        )
    except Exception:
        flash(request, f"Einladung an {name} konnte nicht verschickt werden.", "err")
    else:
        if person.invited_at is None:
            person.invited_at = berlin_now(request)
            db.commit()
        flash(request, f"Einladung an {name} verschickt.")
    return redirect("/admin/personen")


@router.get("/personen/{person_id}/widerrufen", response_class=HTMLResponse)
def widerrufen_bestaetigen(
    person_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    person = _get_relative(db, person_id)
    return render(request, "admin/personen_widerrufen.html", {"person": person}, tab="personen")


@router.post("/personen/{person_id}/widerrufen", response_class=HTMLResponse)
def widerrufen(
    person_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    # R39: ein Zaehler entwertet ausstehende Links und laufende Sitzungen
    # zugleich (app/auth/tokens.py, app/auth/dependencies.py).
    person = _get_relative(db, person_id)
    person.access_version += 1
    db.commit()
    flash(request, f"Zugang von {person.display_name or person.email} widerrufen.")
    return redirect("/admin/personen")


def _has_beitraege(db: Session, person: Person) -> bool:
    """Jede Aufnahme zaehlt, auch abgelehnte oder geloeste (R35: sie gehoert
    dauerhaft der Person). Dann bleibt nur der Widerruf."""
    return db.execute(select(Beitrag.id).where(Beitrag.person_id == person.id)).first() is not None


def _freed_stories(db: Session, person: Person) -> list[str]:
    """'„Titel“ (Kalender · Tag N)' je Auftrag der Person, Entwuerfe ohne Tag."""
    auftraege = db.execute(
        select(Auftrag).where(Auftrag.person_id == person.id).order_by(Auftrag.id)
    ).scalars()
    labels = []
    for auftrag in auftraege:
        label = f"„{auftrag.title or 'ohne Titel'}“"
        days = [f"{s.campaign.name} · Tag {s.day}" for s in auftrag_slots(auftrag)]
        labels.append(f"{label} ({', '.join(days)})" if days else label)
    return labels


@router.get("/personen/{person_id}/loeschen", response_class=HTMLResponse)
def loeschen_bestaetigen(
    person_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    person = _get_relative(db, person_id)
    return render(
        request,
        "admin/personen_loeschen.html",
        {
            "person": person,
            "blocked": _has_beitraege(db, person),
            "stories": _freed_stories(db, person),
        },
        tab="personen",
    )


@router.post("/personen/{person_id}/loeschen", response_class=HTMLResponse)
def loeschen(
    person_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    person = _get_relative(db, person_id)
    name = person.display_name or person.email
    if _has_beitraege(db, person):
        flash(request, f"{name} hat schon Aufnahmen eingereicht und bleibt angelegt.", "err")
        return redirect("/admin/personen")
    # Die Geschichten bleiben und sind wieder "noch nicht vergeben".
    db.execute(update(Auftrag).where(Auftrag.person_id == person.id).values(person_id=None))
    db.execute(
        update(Slot).where(Slot.assigned_person_id == person.id).values(assigned_person_id=None)
    )
    einstellungen = get_einstellungen(db)
    einstellungen.hoechste_person_id = max(einstellungen.hoechste_person_id or 0, person.id)
    db.delete(person)
    db.commit()
    flash(request, f"{name} gelöscht.")
    return redirect("/admin/personen")
