"""Admin-Reiter Kalender und Geschichten (U8 Schritt 1, 11; R12, R24;
Mehrkalender U10: Tonie-Umschalter R14/R40, Auftragsvergabe R7/R9/R13/R36)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin.common import (
    berlin_now,
    campaign_slots,
    delivery_time_at,
    first_campaign,
    flash,
    redirect,
    render,
    scope_names,
    selection,
    tonie_name,
)
from app.admin.setup import (
    add_calendar_day,
    create_auftrag,
    reassign_auftrag,
    remove_calendar_day,
    update_auftrag,
)
from app.admin.state import (
    Selection,
    active_beitrag,
    auftrag_active_beitrag,
    auftrag_lock_slot,
    auftrag_slots,
    calendars,
    chapter_title_open,
    delivery_moment,
    is_slot_delivered,
    is_slot_fixed,
    slot_state,
)
from app.auth.dependencies import get_db, require_admin
from app.delivery.trigger import tomorrows_advent_day
from app.models import Auftrag, Campaign, Person, Slot
from app.settings import recording_deadline

router = APIRouter(prefix="/admin")

DETACH_WARNING = (
    "Der bisherige Beitrag hat sich vom Türchen gelöst; er bleibt im Archiv seiner "
    "Urheberin und wird nie ausgeliefert."
)

# Kraeftig markiert werden nur die naechsten Auslieferungen, damit die
# Markierung etwas bedeutet (Designabgleich 2026-09-29); "Was fehlt" nennt
# weiterhin alle Tage.
HIGHLIGHT_DAYS = 3


def person_name(person: Person | None) -> str | None:
    if person is None:
        return None
    return person.display_name or person.email


def _days(days: list[int]) -> str:
    """ "Tag 9" bzw. "Tage 16, 20 und 22"."""
    if len(days) == 1:
        return f"Tag {days[0]}"
    return "Tage " + ", ".join(str(d) for d in days[:-1]) + f" und {days[-1]}"


def slot_person(slot: Slot) -> Person | None:
    return slot.auftrag.person if slot.auftrag is not None else None


def slot_title(slot: Slot) -> str | None:
    return slot.auftrag.title if slot.auftrag is not None else None


def scope(campaign: Campaign) -> dict:
    """R40: Kalender und die Tonies, fuer die eine Aenderung hier gilt."""
    return {"scope_calendar": campaign.name, "scope_tonies": scope_names(campaign)}


def _calendar_or_hint(request: Request, db: Session, tab: str):
    """Der Kalender der Wahl; bei einem Tonie ohne Kalender die Hinweisseite
    (Mockup Abschnitt 1), ohne jeden Kalender die Einrichtung."""
    chosen: Selection | None = selection(db, request)
    if chosen is not None and chosen.campaign is not None:
        return chosen, None
    if chosen is None or first_campaign(db) is None:
        return None, redirect("/admin")
    page = render(
        request, "admin/ohne_kalender.html", {"tonie_name": tonie_name(chosen.tonie)}, tab=tab
    )
    return None, page


@router.get("/kalender", response_class=HTMLResponse)
def kalender(request: Request, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    chosen, other = _calendar_or_hint(request, db, "kalender")
    if chosen is None:
        return other
    campaign = chosen.campaign
    now = berlin_now(request)
    delivery_time = delivery_time_at(db, now)
    deadline = recording_deadline(db)

    slots = campaign_slots(db, campaign.id)
    # R14: die Kacheln zeigen die Laeufe des gewaehlten Tonies.
    tonie_id = chosen.tonie.tonie_id if chosen.tonie is not None else None
    naive_now = now.replace(tzinfo=None)
    first_upcoming = next(
        (s.day for s in slots if naive_now < delivery_moment(s.day, delivery_time)),
        None,
    )

    tiles = []
    overdue, unassigned, waiting = [], [], []
    missing_count = 0
    for slot in slots:
        state = slot_state(db, slot, now=now, delivery_time=delivery_time, tonie_id=tonie_id)
        person = person_name(slot_person(slot))
        label = state.label
        if state.detail and state.key in ("fehlschlag", "ersatz"):
            label = f"{label}: {state.detail}"
        beitrag = state.beitrag
        tiles.append(
            {
                "slot": slot,
                "title": slot_title(slot),
                "state": state,
                "soon": state.missing
                and first_upcoming is not None
                and slot.day < first_upcoming + HIGHLIGHT_DAYS,
                "line": f"{person} · {label}" if person else label,
                "chapter_open": beitrag is not None and chapter_title_open(beitrag),
                "href": (
                    f"/admin/aufnahmen?beitrag_id={beitrag.id}"
                    if beitrag is not None
                    else f"/admin/geschichten?slot_id={slot.id}"
                ),
            }
        )
        if state.missing:
            missing_count += 1
            if state.key == "frei":
                unassigned.append(slot.day)
            elif state.key == "eingereicht":
                waiting.append(slot.day)
            elif now.date() > deadline:
                overdue.append((slot.day, person))

    missing_notes = [
        f"Tag {day} ist seit dem {deadline:%d.%m.} überfällig ({person})."
        for day, person in overdue
    ]
    if unassigned:
        verb = "ist" if len(unassigned) == 1 else "sind"
        missing_notes.append(f"{_days(unassigned)} {verb} niemandem zugewiesen.")
    if waiting:
        verb = "wartet" if len(waiting) == 1 else "warten"
        missing_notes.append(f"{_days(waiting)} {verb} auf deine Freigabe.")

    next_day = tomorrows_advent_day(now)
    next_tile = next((t for t in tiles if t["slot"].day == next_day), None)

    return render(
        request,
        "admin/kalender.html",
        {
            **scope(campaign),
            "campaign": campaign,
            "tonie": chosen.tonie,
            "tonie_name": tonie_name(chosen.tonie) if chosen.tonie else None,
            "tiles": tiles,
            "next_tile": next_tile,
            "next_person": person_name(slot_person(next_tile["slot"])) if next_tile else None,
            "delivery_time": delivery_time,
            "missing_count": missing_count,
            "missing_notes": missing_notes,
        },
        tab="kalender",
    )


def _persons(db: Session) -> list[Person]:
    return list(
        db.execute(
            select(Person)
            .where(Person.is_admin.is_(False))
            .order_by(Person.display_name, Person.email)
        ).scalars()
    )


def _slot_row(db: Session, slot: Slot, campaign: Campaign) -> dict:
    auftrag = slot.auftrag
    if auftrag is None:
        return {"slot": slot, "auftrag": None, "title": "frei", "meta": "Auftrag anlegen"}
    others = [
        f"auch {s.campaign.name} · Tag {s.day}"
        for s in auftrag_slots(auftrag)
        if s.campaign_id != campaign.id
    ]
    tag = None
    if is_slot_delivered(db, slot, auftrag):
        tag = ("ausgeliefert", "tag tag-strong")
    elif auftrag.person is None:
        tag = ("ohne Person", "tag tag-outline")
    return {
        "slot": slot,
        "auftrag": auftrag,
        "title": auftrag.title or "(ohne Titel)",
        "meta": " · ".join([person_name(auftrag.person) or "noch nicht vergeben", *others]),
        "tag": tag,
    }


def _auftrag_view(db: Session, auftrag: Auftrag, *, now, delivery_time) -> dict:
    """Rechte Maske: Kalendertage mit Sperren, Auswahl fuer weitere Tage."""
    days = []
    for slot in auftrag_slots(auftrag):
        days.append(
            {
                "slot": slot,
                "fixed": is_slot_fixed(db, slot, now=now, delivery_time=delivery_time),
                "delivered": is_slot_delivered(db, slot, auftrag),
            }
        )
    lock = auftrag_lock_slot(db, auftrag, now=now, delivery_time=delivery_time)
    taken_calendars = {s.campaign_id for s in auftrag.slots}
    # Nur Kalender ohne diesen Auftrag und nur freie, noch nicht feste Tage
    # (Mockup Abschnitt 3); die Fehler bleiben fuer Nebenlaeufigkeit.
    add_groups = []
    for calendar in calendars(db):
        if calendar.id in taken_calendars:
            continue
        free = [
            s
            for s in campaign_slots(db, calendar.id)
            if s.auftrag_id is None
            and not is_slot_fixed(db, s, now=now, delivery_time=delivery_time)
        ]
        if free:
            add_groups.append((calendar.name, free))
    return {
        "auftrag": auftrag,
        "days": days,
        "lock": lock,
        "lock_delivered": lock is not None and is_slot_delivered(db, lock, auftrag),
        "add_groups": add_groups,
        "active_beitrag": auftrag_active_beitrag(auftrag),
    }


@router.get("/geschichten", response_class=HTMLResponse)
def geschichten(
    request: Request,
    slot_id: int | None = None,
    auftrag_id: int | None = None,
    neu: int = 0,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    chosen, other = _calendar_or_hint(request, db, "geschichten")
    if chosen is None:
        return other
    campaign = chosen.campaign
    slots = campaign_slots(db, campaign.id)
    now = berlin_now(request)
    delivery_time = delivery_time_at(db, now)

    selected_slot, auftrag = None, None
    if auftrag_id is not None:
        auftrag = db.get(Auftrag, auftrag_id)
        if auftrag is None:
            raise HTTPException(status_code=404, detail="Auftrag nicht gefunden.")
    elif slot_id is not None:
        selected_slot = db.get(Slot, slot_id)
        if selected_slot is None:
            raise HTTPException(status_code=404, detail="Türchen nicht gefunden.")
        auftrag = selected_slot.auftrag
    elif not neu and slots:
        selected_slot = slots[0]
        auftrag = selected_slot.auftrag

    drafts = list(
        db.execute(select(Auftrag).where(~Auftrag.slots.any()).order_by(Auftrag.id)).scalars()
    )
    return render(
        request,
        "admin/geschichten.html",
        {
            **scope(campaign),
            "campaign": campaign,
            "rows": [_slot_row(db, s, campaign) for s in slots],
            "drafts": drafts,
            "new": bool(neu) and auftrag is None and selected_slot is None,
            "selected_slot": selected_slot,
            "view": (
                _auftrag_view(db, auftrag, now=now, delivery_time=delivery_time)
                if auftrag is not None
                else None
            ),
            "persons": _persons(db),
            "person_name": person_name,
            "active_beitrag": active_beitrag(selected_slot) if selected_slot else None,
        },
        tab="geschichten",
    )


def _when(request: Request, db: Session) -> dict:
    now = berlin_now(request)
    return {"now": now, "delivery_time": delivery_time_at(db, now)}


def _person_id(raw: str) -> int | None:
    return int(raw) if raw.strip() else None


@router.post("/geschichten/{slot_id}", response_class=HTMLResponse)
def save_geschichte(
    slot_id: int,
    request: Request,
    person_id: str = Form(""),
    title: str = Form(""),
    vorlesetext: str = Form(""),
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    """Ein Kalendertag und sein Auftrag; ohne Auftrag legt die erste Angabe
    einen an."""
    slot = db.get(Slot, slot_id)
    if slot is None:
        raise HTTPException(status_code=404, detail=f"Slot {slot_id} existiert nicht.")
    when = _when(request, db)
    new_person_id = _person_id(person_id)
    title, vorlesetext = title.strip(), vorlesetext.strip()

    detached = []
    try:
        if slot.auftrag is None:
            if new_person_id is not None or title or vorlesetext:
                auftrag = create_auftrag(db, person_id=new_person_id)
                add_calendar_day(db, auftrag.id, slot.id, **when)
                update_auftrag(db, auftrag.id, title=title, vorlesetext=vorlesetext)
        else:
            auftrag = slot.auftrag
            detached = reassign_auftrag(db, auftrag.id, new_person_id, **when)
            update_auftrag(db, auftrag.id, title=title, vorlesetext=vorlesetext)
    except ValueError as exc:  # AuftragError und unbekannte Person
        db.rollback()
        flash(request, str(exc), "err")
        return redirect(f"/admin/geschichten?slot_id={slot_id}")
    db.commit()

    if detached:
        flash(request, f"Gespeichert. {DETACH_WARNING}")
    else:
        flash(request, "Geschichte gespeichert.")
    return redirect(f"/admin/geschichten?slot_id={slot_id}")


def _back_to(auftrag_id: int):
    return redirect(f"/admin/geschichten?auftrag_id={auftrag_id}")


def _auftrag_or_404(db: Session, auftrag_id: int) -> Auftrag:
    auftrag = db.get(Auftrag, auftrag_id)
    if auftrag is None:
        raise HTTPException(status_code=404, detail="Auftrag nicht gefunden.")
    return auftrag


@router.post("/auftraege", response_class=HTMLResponse)
def new_auftrag(
    request: Request,
    person_id: str = Form(""),
    title: str = Form(""),
    vorlesetext: str = Form(""),
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    """R36: ein neuer Auftrag ist ein Entwurf, bis er an einem Kalendertag liegt."""
    try:
        auftrag = create_auftrag(
            db,
            person_id=_person_id(person_id),
            title=title.strip(),
            vorlesetext=vorlesetext.strip(),
        )
    except ValueError as exc:
        db.rollback()
        flash(request, str(exc), "err")
        return redirect("/admin/geschichten?neu=1")
    db.commit()
    flash(request, "Entwurf angelegt. Lege ihn jetzt an einen Kalendertag.")
    return _back_to(auftrag.id)


@router.post("/auftraege/{auftrag_id}", response_class=HTMLResponse)
def save_auftrag(
    auftrag_id: int,
    request: Request,
    person_id: str = Form(""),
    title: str = Form(""),
    vorlesetext: str = Form(""),
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    """Person (R12/R35, gesperrt nach festem Tag), Titel und Vorlesetext --
    die beiden letzten bleiben nach der Auslieferung aenderbar (Abnahme)."""
    auftrag = _auftrag_or_404(db, auftrag_id)
    try:
        detached = reassign_auftrag(db, auftrag.id, _person_id(person_id), **_when(request, db))
        update_auftrag(db, auftrag.id, title=title.strip(), vorlesetext=vorlesetext.strip())
    except ValueError as exc:
        db.rollback()
        flash(request, str(exc), "err")
        return _back_to(auftrag_id)
    db.commit()
    flash(request, f"Gespeichert. {DETACH_WARNING}" if detached else "Auftrag gespeichert.")
    return _back_to(auftrag_id)


@router.post("/auftraege/{auftrag_id}/tage", response_class=HTMLResponse)
def add_day(
    auftrag_id: int,
    request: Request,
    slot_id: int | None = Form(None),
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    """R7/R9/R13: ein weiterer Kalendertag; die Aufnahme gilt dort mit (R8)."""
    _auftrag_or_404(db, auftrag_id)
    if slot_id is None:
        flash(request, "Bitte einen Kalendertag wählen.", "err")
        return _back_to(auftrag_id)
    try:
        slot = add_calendar_day(db, auftrag_id, slot_id, **_when(request, db))
    except ValueError as exc:  # belegt, schon im Kalender, fest, unbekannt
        db.rollback()
        flash(request, str(exc), "err")
        return _back_to(auftrag_id)
    db.commit()
    flash(request, f"Liegt jetzt auch in {slot.campaign.name} an Tag {slot.day}.")
    return _back_to(auftrag_id)


@router.post("/auftraege/{auftrag_id}/tage/{slot_id}/entfernen", response_class=HTMLResponse)
def remove_day(
    auftrag_id: int,
    slot_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    """R13: nur solange der Vorabend-Lauf des Tages nicht gelaufen ist."""
    _auftrag_or_404(db, auftrag_id)
    slot = db.get(Slot, slot_id)
    if slot is None or slot.auftrag_id != auftrag_id:
        flash(request, "Dieser Kalendertag gehört nicht zu diesem Auftrag.", "err")
        return _back_to(auftrag_id)
    try:
        auftrag = remove_calendar_day(db, slot_id, **_when(request, db))
    except ValueError as exc:
        db.rollback()
        flash(request, str(exc), "err")
        return _back_to(auftrag_id)
    db.commit()
    note = " Ohne Kalendertag ist er jetzt ein Entwurf." if not auftrag.slots else ""
    flash(request, f"Tag {slot.day} in {slot.campaign.name} entfernt.{note}")
    return _back_to(auftrag_id)
