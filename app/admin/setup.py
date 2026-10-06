"""Kampagnen-Einrichtung (U2): Service-Funktionen + Formular-Ziele.

Die Routen sind hinter require_admin (U3) geschuetzt. Seit U8 rendern sie
keine eigenen Seiten mehr, sondern leiten auf die gestalteten Admin-Reiter
zurueck (app/admin/common.py).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.admin.common import (
    first_campaign,
    flash,
    get_campaign,
    redirect,
    remember_selection,
    render,
)
from app.admin.state import SELECTION_KEY, auftrag_lock_slot, is_slot_fixed, selections
from app.auth.dependencies import get_db, require_admin
from app.models import Auftrag, Beitrag, Campaign, Person, Slot
from app.settings import get_einstellungen

SLOT_COUNT = 24


def new_campaign(session: Session, **fields) -> Campaign:
    """Kalender mit 24 Tagen anlegen; nur flush, der Aufrufer committet."""
    campaign = Campaign(**fields)
    session.add(campaign)
    session.flush()
    session.add_all(Slot(campaign_id=campaign.id, day=day) for day in range(1, SLOT_COUNT + 1))
    return campaign


def create_campaign(session: Session) -> Campaign:
    existing = session.execute(select(Campaign.id).limit(1)).first()
    if existing is not None:
        raise ValueError("Es existiert bereits eine Kampagne.")

    campaign = new_campaign(session)
    session.commit()
    session.refresh(campaign)
    return campaign


def mark_invitation_ready(session: Session, slot_id: int) -> Slot:
    slot = session.get(Slot, slot_id)
    if slot is None:
        raise ValueError(f"Slot {slot_id} existiert nicht.")
    if slot.auftrag is None or not slot.auftrag.vorlesetext:
        raise ValueError(
            "Ein Slot ohne Vorlesetext kann nicht als einladungsbereit markiert werden."
        )
    slot.invitation_ready = True
    session.commit()
    session.refresh(slot)
    return slot


# --- Auftraege ueber mehrere Kalender (Mehrkalender U9) ---------------------
#
# Die Dienste schreiben nur in die Session (flush), der Aufrufer committet --
# so bleibt etwa der Admin-Upload (Auftrag anlegen + Beitrag) ein Commit.
# Zeitbezogene Sperren (R12, R13) brauchen `now` (zeitzonenbewusst,
# Europe/Berlin) und die Lieferzeit, siehe app/admin/state.py::is_slot_fixed.


class AuftragError(ValueError):
    """Benannter Fehler einer Auftragsaktion; die Meldung ist fuer den Admin."""


class SlotTakenError(AuftragError):
    def __init__(self, slot: Slot) -> None:
        super().__init__(f"Kalendertag schon belegt: Türchen {slot.day} in „{slot.campaign.name}“.")


class AlreadyInCalendarError(AuftragError):
    def __init__(self, slot: Slot) -> None:
        super().__init__(
            f"Auftrag liegt schon in diesem Kalender („{slot.campaign.name}“, Türchen {slot.day})."
        )


class SlotFixedError(AuftragError):
    def __init__(self, slot: Slot) -> None:
        super().__init__(
            f"Türchen {slot.day} in „{slot.campaign.name}“ ist schon fest, "
            "der Vorabend-Lauf ist gelaufen."
        )


class AuftragLockedError(AuftragError):
    def __init__(self, slot: Slot) -> None:
        self.slot = slot
        super().__init__(
            f"Türchen {slot.day} in „{slot.campaign.name}“ ist schon fest oder ausgeliefert. "
            "Ablehnen, Zurücknehmen, Neu vergeben und Löschen sind nicht mehr möglich."
        )


class ReplacementInUseError(ValueError):
    def __init__(self, other: Campaign) -> None:
        super().__init__(
            f"Diese Aufnahme ist schon Ersatzbeitrag im Kalender „{other.name}“. "
            "Jeder Kalender braucht einen eigenen."
        )


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _get(session: Session, model, object_id: int):
    obj = session.get(model, object_id)
    if obj is None:
        raise ValueError(f"{model.__name__} {object_id} existiert nicht.")
    return obj


def _ensure_unlocked(
    session: Session, auftrag: Auftrag, *, now: datetime, delivery_time: time
) -> None:
    slot = auftrag_lock_slot(session, auftrag, now=now, delivery_time=delivery_time)
    if slot is not None:
        raise AuftragLockedError(slot)


def create_auftrag(
    session: Session,
    *,
    person_id: int | None = None,
    title: str | None = None,
    vorlesetext: str | None = None,
) -> Auftrag:
    """Ohne Kalendertag ist der Auftrag ein Entwurf (R36); die Person darf fehlen."""
    if person_id is not None:
        _get(session, Person, person_id)
    auftrag = Auftrag(person_id=person_id, title=title or None, vorlesetext=vorlesetext or None)
    session.add(auftrag)
    session.flush()
    return auftrag


def update_auftrag(
    session: Session,
    auftrag_id: int,
    *,
    title: str | None = None,
    vorlesetext: str | None = None,
) -> Auftrag:
    """None laesst das Feld unveraendert, "" leert es."""
    auftrag = _get(session, Auftrag, auftrag_id)
    if title is not None:
        auftrag.title = title or None
    if vorlesetext is not None:
        auftrag.vorlesetext = vorlesetext or None
    session.flush()
    return auftrag


def add_calendar_day(
    session: Session, auftrag_id: int, slot_id: int, *, now: datetime, delivery_time: time
) -> Slot:
    """R7/R9/R13: legt den Auftrag an einen Kalendertag. Die vorhandene
    Aufnahme gilt dort ohne neue Aufnahme (R8)."""
    auftrag = _get(session, Auftrag, auftrag_id)
    slot = _get(session, Slot, slot_id)
    if slot.auftrag_id is not None:
        raise SlotTakenError(slot)
    if any(s.campaign_id == slot.campaign_id for s in auftrag.slots):
        raise AlreadyInCalendarError(slot)
    if is_slot_fixed(session, slot, now=now, delivery_time=delivery_time):
        raise SlotFixedError(slot)
    slot.auftrag = auftrag
    session.flush()
    return slot


def remove_calendar_day(
    session: Session, slot_id: int, *, now: datetime, delivery_time: time
) -> Auftrag:
    """R13: nur ein Tag, dessen Vorabend-Lauf noch nicht gelaufen ist. Die
    Aufnahme bleibt am Auftrag; ohne Tag ist er wieder Entwurf (R36)."""
    slot = _get(session, Slot, slot_id)
    auftrag = slot.auftrag
    if auftrag is None:
        raise AuftragError(f"Türchen {slot.day} in „{slot.campaign.name}“ ist frei.")
    if is_slot_fixed(session, slot, now=now, delivery_time=delivery_time):
        raise SlotFixedError(slot)
    slot.auftrag = None
    session.flush()
    return auftrag


def attach_beitrag(
    session: Session, auftrag: Auftrag, *, person_id: int, audio_object_key: str
) -> Beitrag:
    """Ein neuer Beitrag am Auftrag (Admin-Upload, R15); gilt an jedem
    seiner Kalendertage (R8)."""
    beitrag = Beitrag(person_id=person_id, auftrag=auftrag, audio_object_key=audio_object_key)
    session.add(beitrag)
    # Kein flush: der Aufrufer committet ueber storage.commit_or_discard, damit
    # auch ein Fehler beim Schreiben die schon abgelegte Datei entfernt (R40).
    return beitrag


def detach_beitraege(session: Session, auftrag: Auftrag) -> list[Beitrag]:
    """R35: alle Beitraege des Auftrags loesen sich (`detached_at`, damit sie
    nicht als freie Einreichung gelten) und bleiben bei ihrer Urheberin."""
    detached = list(auftrag.beitraege)
    for beitrag in detached:
        auftrag.beitraege.remove(beitrag)
        beitrag.detached_at = _utcnow()
    return detached


def reassign_auftrag(
    session: Session,
    auftrag_id: int,
    person_id: int | None,
    *,
    now: datetime,
    delivery_time: time,
) -> list[Beitrag]:
    """R12/R35: Neuvergabe (oder Entzug mit None) loest die Beitraege des
    Auftrags; gesperrt, sobald einer seiner Tage fest oder ausgeliefert ist.
    Gibt die geloesten Beitraege zurueck, damit der Aufrufer warnen kann."""
    auftrag = _get(session, Auftrag, auftrag_id)
    if person_id == auftrag.person_id:
        return []
    if person_id is not None:
        _get(session, Person, person_id)
    _ensure_unlocked(session, auftrag, now=now, delivery_time=delivery_time)
    auftrag.person_id = person_id
    detached = detach_beitraege(session, auftrag)
    session.flush()
    return detached


def reject_beitrag(
    session: Session, beitrag_id: int, *, now: datetime, delivery_time: time
) -> Beitrag:
    """R12/R13: der Auftrag gilt danach in allen seinen Kalendern wieder als offen."""
    beitrag = _get(session, Beitrag, beitrag_id)
    if beitrag.auftrag is not None:
        _ensure_unlocked(session, beitrag.auftrag, now=now, delivery_time=delivery_time)
    if beitrag.approved_at is not None:
        raise AuftragError(
            "Freigegebene Aufnahmen lassen sich nicht ablehnen. Erst die Freigabe zurücknehmen."
        )
    beitrag.rejected_at = _utcnow()
    session.flush()
    return beitrag


def withdraw_approval(
    session: Session, beitrag_id: int, *, now: datetime, delivery_time: time
) -> Beitrag:
    """R12/R30: Ruecknahme nur, solange kein Tag des Auftrags fest oder
    ausgeliefert ist; der Auftrag ist danach wieder offen. Eine freie
    Einreichung hat keinen Tag und laesst sich immer zuruecknehmen."""
    beitrag = _get(session, Beitrag, beitrag_id)
    if beitrag.approved_at is None:
        raise AuftragError("Diese Aufnahme ist nicht freigegeben.")
    if beitrag.auftrag is not None:
        _ensure_unlocked(session, beitrag.auftrag, now=now, delivery_time=delivery_time)
        beitrag.rejected_at = _utcnow()
    beitrag.approved_at = None
    session.flush()
    return beitrag


def delete_auftrag(
    session: Session, auftrag_id: int, *, now: datetime, delivery_time: time
) -> list[Beitrag]:
    """R12: der Auftrag verschwindet aus allen Kalendern; seine Aufnahmen
    bleiben geloest bei der Urheberin (endgueltig loeschen ist R40, Reiter
    Aufnahmen)."""
    auftrag = _get(session, Auftrag, auftrag_id)
    _ensure_unlocked(session, auftrag, now=now, delivery_time=delivery_time)
    released = list(auftrag.slots)
    for slot in released:
        slot.auftrag = None
    detached = detach_beitraege(session, auftrag)
    session.delete(auftrag)
    session.flush()
    return detached


def create_person(session: Session, email: str, display_name: str = "") -> Person:
    person = Person(email=email, display_name=display_name)
    # Nie die id einer geloeschten Person neu vergeben (Einstellungen.hoechste_person_id).
    deleted = get_einstellungen(session).hoechste_person_id or 0
    if deleted > (session.execute(select(func.max(Person.id))).scalar() or 0):
        person.id = deleted + 1
    session.add(person)
    session.commit()
    session.refresh(person)
    return person


def set_replacement_beitrag(
    session: Session, beitrag_id: int, campaign_id: int | None = None
) -> Campaign:
    """R38: jeder Kalender hat seinen eigenen Ersatzbeitrag. Ohne
    `campaign_id` der einzige Kalender (Altrouten, Generalprobe)."""
    if campaign_id is None:
        campaign = session.execute(select(Campaign)).scalar_one()
    else:
        campaign = _get(session, Campaign, campaign_id)
    beitrag = session.get(Beitrag, beitrag_id)
    if beitrag is None:
        raise ValueError(f"Beitrag {beitrag_id} existiert nicht.")
    if (
        beitrag.approved_at is None
        or beitrag.rejected_at is not None
        or beitrag.detached_at is not None
    ):
        raise ValueError("Nur ein freigegebener Beitrag kann Ersatzbeitrag sein.")
    other = session.execute(
        select(Campaign).where(
            Campaign.replacement_beitrag_id == beitrag.id, Campaign.id != campaign.id
        )
    ).scalar_one_or_none()
    if other is not None:
        raise ReplacementInUseError(other)
    campaign.replacement_beitrag_id = beitrag.id
    session.commit()
    session.refresh(campaign)
    return campaign


# --- Routen hinter require_admin. Die Seiten dazu sind die U8-Reiter
# (app/admin/overview.py, people.py, deliveries.py); diese Formular-Ziele
# leiten dorthin zurueck. ---

router = APIRouter(prefix="/admin")


@router.get("", response_class=HTMLResponse)
def admin_home(request: Request, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    """F6: ohne Kampagne die Einrichtung, sonst der Kalender."""
    if first_campaign(db) is None:
        return render(request, "admin/einrichtung.html", {}, tab=None)
    return redirect("/admin/kalender")


@router.post("/campaign", response_class=HTMLResponse)
def create_campaign_route(
    request: Request, db: Session = Depends(get_db), _admin=Depends(require_admin)
):
    try:
        create_campaign(db)
    except ValueError as exc:
        flash(request, str(exc), "err")
    else:
        flash(request, "Kampagne mit 24 Türchen angelegt.")
    return redirect("/admin/kalender")


@router.post("/persons", response_class=HTMLResponse)
def create_person_route(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(""),
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    email = email.strip()
    # Wie beim Login (auth/routes.py): ohne Gross-/Kleinschreibung vergleichen.
    existing = select(Person).where(func.lower(Person.email) == email.lower())
    if db.execute(existing).first() is not None:
        flash(request, f"{email} ist bereits angelegt.", "err")
    else:
        create_person(db, email=email, display_name=display_name.strip())
        flash(request, f"{display_name.strip() or email} angelegt.")
    return redirect("/admin/personen")


@router.post("/campaign/replacement", response_class=HTMLResponse)
def set_replacement_beitrag_route(
    request: Request,
    beitrag_id: int = Form(...),
    campaign_id: int | None = Form(None),
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    """R38: fuer den Kalender des Formulars, ohne Angabe den gewaehlten."""
    if campaign_id is None:
        campaign = get_campaign(db, request)
        if campaign is None:
            flash(request, "Dieser Tonie gehört zu keinem Kalender.", "err")
            return redirect("/admin/auslieferung")
        campaign_id = campaign.id
    try:
        set_replacement_beitrag(db, beitrag_id, campaign_id)
    except ValueError as exc:
        flash(request, str(exc), "err")
    else:
        flash(request, "Ersatzbeitrag gesetzt.")
    return redirect("/admin/auslieferung")


_BACK = re.compile(r"/admin/[a-z]+")


@router.post("/tonie", response_class=HTMLResponse)
def choose_tonie(
    request: Request,
    auswahl: str = Form(""),
    zurueck: str = Form(""),
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    """R14/R16/KTD12: die Wahl des Umschalters in die Sitzung; eine unbekannte
    Wahl faellt auf die Vorgabe zurueck. Zurueck auf denselben Reiter."""
    if auswahl in {row.value for row in selections(db)}:
        remember_selection(request, auswahl)
    else:
        request.session.pop(SELECTION_KEY, None)
    return redirect(zurueck if _BACK.fullmatch(zurueck) else "/admin/kalender")
