"""Mehrkalender U9: Auftraege ueber mehrere Kalender (R7-R9, R11-R13, R36, R38).

Dienstebene (app/admin/setup.py, app/admin/state.py) plus die zwei
HTTP-Pfade, die der Plan ausdruecklich nennt (Erinnerung, Admin-Upload).
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin.setup import (
    AlreadyInCalendarError,
    AuftragLockedError,
    ReplacementInUseError,
    SlotFixedError,
    SlotTakenError,
    add_calendar_day,
    create_auftrag,
    delete_auftrag,
    reassign_auftrag,
    reject_beitrag,
    remove_calendar_day,
    set_replacement_beitrag,
    withdraw_approval,
)
from app.admin.state import (
    auftrag_is_open,
    deliverable_beitrag,
    is_slot_fixed,
    open_auftraege_for,
    slot_state,
)
from app.models import Auftrag, Beitrag, Campaign, CreativeTonie, DeliveryRun, Person, Slot
from tests.fixtures.audio import make_tone_mp3

BERLIN = ZoneInfo("Europe/Berlin")
DELIVERY = time(20, 0)
EARLY = datetime(2026, 10, 1, 12, 0, tzinfo=BERLIN)
T = {"now": EARLY, "delivery_time": DELIVERY}


def _calendar(db: Session, name: str, *tonie_ids: str) -> Campaign:
    calendar = Campaign(name=name)
    db.add(calendar)
    db.flush()
    for day in range(1, 25):
        db.add(Slot(campaign_id=calendar.id, day=day))
    for tonie_id in tonie_ids:
        db.add(CreativeTonie(tonie_id=tonie_id, campaign_id=calendar.id))
    db.commit()
    return calendar


def _slot(db: Session, calendar: Campaign, day: int) -> Slot:
    return db.execute(
        select(Slot).where(Slot.campaign_id == calendar.id, Slot.day == day)
    ).scalar_one()


def _person(db: Session, email: str, name: str = "") -> Person:
    person = Person(email=email, display_name=name)
    db.add(person)
    db.commit()
    return person


def _auftrag(db: Session, person: Person | None, *slots: Slot) -> Auftrag:
    auftrag = create_auftrag(db, person_id=person.id if person else None, title="Der Stern")
    for slot in slots:
        add_calendar_day(db, auftrag.id, slot.id, **T)
    db.commit()
    return auftrag


def _beitrag(db: Session, auftrag: Auftrag, person: Person, **kw) -> Beitrag:
    # Ueber die Beziehung, nicht die Spalte: die Session laeuft mit
    # expire_on_commit=False, eine schon geladene `auftrag.beitraege` bliebe sonst alt.
    beitrag = Beitrag(person_id=person.id, auftrag=auftrag, audio_object_key="k.mp3", **kw)
    db.add(beitrag)
    db.commit()
    return beitrag


def _run(db: Session, calendar: Campaign, tonie_id: str, day: int, **kw) -> None:
    db.add(
        DeliveryRun(
            campaign_id=calendar.id,
            tonie_id=tonie_id,
            run_type=kw.pop("run_type", "vorabend"),
            target_day=day,
            started_at=datetime(2026, 12, 1, 20, 0),
            outcome=kw.pop("outcome", "erfolg"),
            **kw,
        )
    )
    db.commit()


# --- Kalendertag fest (KTD10) ------------------------------------------------


def test_day_fixed_after_vorabend_run_of_any_tonie_of_the_calendar(db_session: Session):
    a = _calendar(db_session, "Familie", "tonie-a1", "tonie-a2")
    b = _calendar(db_session, "Oma", "tonie-b")
    slot = _slot(db_session, a, 5)

    assert is_slot_fixed(db_session, slot, **T) is False
    _run(db_session, b, "tonie-b", 5)  # Lauf eines fremden Kalenders zaehlt nicht
    _run(db_session, a, "tonie-a2", 5, run_type="trockenlauf")  # kein Vorabend-Lauf
    assert is_slot_fixed(db_session, slot, **T) is False

    _run(db_session, a, "tonie-a2", 5, outcome="fehlschlag")
    assert is_slot_fixed(db_session, slot, **T) is True


def test_day_of_calendar_without_tonie_fixed_once_delivery_time_passed(db_session: Session):
    calendar = _calendar(db_session, "Ohne Tonie")
    slot = _slot(db_session, calendar, 6)
    before = datetime(2026, 12, 5, 19, 59, tzinfo=BERLIN)
    after = datetime(2026, 12, 5, 20, 0, tzinfo=BERLIN)

    assert is_slot_fixed(db_session, slot, now=before, delivery_time=DELIVERY) is False
    assert is_slot_fixed(db_session, slot, now=after, delivery_time=DELIVERY) is True


# --- Kalendertage hinzufuegen und entfernen (R7, R9, R13) ---------------------


def test_auftrag_twice_in_same_calendar_refused(db_session: Session):
    a = _calendar(db_session, "Familie")
    auftrag = _auftrag(db_session, None, _slot(db_session, a, 3))

    with pytest.raises(AlreadyInCalendarError, match="Auftrag liegt schon in diesem Kalender"):
        add_calendar_day(db_session, auftrag.id, _slot(db_session, a, 9).id, **T)


def test_taken_calendar_day_refused(db_session: Session):
    a = _calendar(db_session, "Familie")
    slot = _slot(db_session, a, 3)
    _auftrag(db_session, None, slot)
    other = create_auftrag(db_session)

    with pytest.raises(SlotTakenError, match="Kalendertag schon belegt"):
        add_calendar_day(db_session, other.id, slot.id, **T)


def test_fixed_day_can_neither_be_added_nor_removed(db_session: Session):
    a = _calendar(db_session, "Familie", "tonie-a")
    fixed_empty, fixed_used = _slot(db_session, a, 4), _slot(db_session, a, 5)
    auftrag = _auftrag(db_session, None, fixed_used)
    _run(db_session, a, "tonie-a", 4)
    _run(db_session, a, "tonie-a", 5)

    with pytest.raises(SlotFixedError):
        add_calendar_day(db_session, create_auftrag(db_session).id, fixed_empty.id, **T)
    with pytest.raises(SlotFixedError):
        remove_calendar_day(db_session, fixed_used.id, **T)
    db_session.expire_all()
    assert db_session.get(Slot, fixed_used.id).auftrag_id == auftrag.id


def test_approved_auftrag_added_to_second_calendar_is_deliverable_there_ae8(
    db_session: Session,
):
    a, b = _calendar(db_session, "Familie"), _calendar(db_session, "Oma")
    tante = _person(db_session, "tante@example.test")
    auftrag = _auftrag(db_session, tante, _slot(db_session, a, 5))
    beitrag = _beitrag(db_session, auftrag, tante, approved_at=datetime(2026, 11, 1))

    add_calendar_day(db_session, auftrag.id, _slot(db_session, b, 12).id, **T)
    db_session.commit()

    in_b = _slot(db_session, b, 12)
    assert deliverable_beitrag(in_b) is beitrag
    assert deliverable_beitrag(_slot(db_session, a, 5)) is beitrag
    assert slot_state(db_session, in_b, **T).key == "freigegeben"
    assert db_session.execute(select(Beitrag)).scalars().all() == [beitrag]


def test_unapproved_or_rejected_beitrag_not_deliverable(db_session: Session):
    a = _calendar(db_session, "Familie")
    tante = _person(db_session, "tante@example.test")
    slot = _slot(db_session, a, 5)
    auftrag = _auftrag(db_session, tante, slot)
    beitrag = _beitrag(db_session, auftrag, tante)
    assert deliverable_beitrag(slot) is None
    beitrag.approved_at = datetime(2026, 11, 1)
    beitrag.rejected_at = datetime(2026, 11, 2)
    assert deliverable_beitrag(slot) is None
    assert deliverable_beitrag(_slot(db_session, a, 6)) is None


def test_fixed_day_in_a_locks_auftrag_but_day_in_b_removable_ae10(db_session: Session):
    a = _calendar(db_session, "Familie", "tonie-a")
    b = _calendar(db_session, "Oma")  # ohne Tonie: Tag 12 erst am 11.12. fest
    tante = _person(db_session, "tante@example.test")
    ruth = _person(db_session, "ruth@example.test")
    auftrag = _auftrag(db_session, tante, _slot(db_session, a, 5), _slot(db_session, b, 12))
    beitrag = _beitrag(db_session, auftrag, tante, approved_at=datetime(2026, 11, 1))
    _run(db_session, a, "tonie-a", 5)
    now = {"now": datetime(2026, 12, 4, 21, 0, tzinfo=BERLIN), "delivery_time": DELIVERY}

    with pytest.raises(AuftragLockedError, match="Türchen 5"):
        withdraw_approval(db_session, beitrag.id, **now)
    beitrag.approved_at = None
    db_session.commit()
    with pytest.raises(AuftragLockedError):
        reject_beitrag(db_session, beitrag.id, **now)
    with pytest.raises(AuftragLockedError):
        reassign_auftrag(db_session, auftrag.id, ruth.id, **now)
    with pytest.raises(AuftragLockedError):
        delete_auftrag(db_session, auftrag.id, **now)
    db_session.rollback()

    remove_calendar_day(db_session, _slot(db_session, b, 12).id, **now)
    db_session.commit()

    db_session.expire_all()
    assert _slot(db_session, b, 12).auftrag_id is None
    assert _slot(db_session, a, 5).auftrag_id == auftrag.id
    refreshed = db_session.get(Beitrag, beitrag.id)
    assert refreshed.auftrag_id == auftrag.id  # die Aufnahme bleibt erhalten
    assert refreshed.rejected_at is None
    assert db_session.get(Auftrag, auftrag.id).person_id == tante.id


def test_successful_anstoss_with_auftrag_beitrag_locks_auftrag(db_session: Session):
    """R12 "ausgeliefert": ein Anstoss vor dem Vorabend macht den Tag nicht
    fest, liefert den Beitrag aber aus."""
    a = _calendar(db_session, "Familie", "tonie-a")
    tante = _person(db_session, "tante@example.test")
    ruth = _person(db_session, "ruth@example.test")
    slot = _slot(db_session, a, 9)
    auftrag = _auftrag(db_session, tante, slot)
    beitrag = _beitrag(db_session, auftrag, tante, approved_at=datetime(2026, 11, 1))
    _run(db_session, a, "tonie-a", 9, run_type="anstoss", beitrag_ids=str(beitrag.id))

    assert is_slot_fixed(db_session, slot, **T) is False
    with pytest.raises(AuftragLockedError):
        reassign_auftrag(db_session, auftrag.id, ruth.id, **T)


def test_removing_last_day_makes_draft_without_reminder_r36(db_session: Session):
    a = _calendar(db_session, "Familie")
    tante = _person(db_session, "tante@example.test")
    slot = _slot(db_session, a, 5)
    auftrag = _auftrag(db_session, tante, slot)
    assert open_auftraege_for(db_session, tante) == [auftrag]

    remove_calendar_day(db_session, slot.id, **T)
    db_session.commit()

    assert auftrag.slots == []
    assert auftrag_is_open(auftrag) is False
    assert open_auftraege_for(db_session, tante) == []


def test_reassign_before_delivery_detaches_beitraege_r35(db_session: Session):
    a, b = _calendar(db_session, "Familie"), _calendar(db_session, "Oma")
    tante = _person(db_session, "tante@example.test")
    ruth = _person(db_session, "ruth@example.test")
    auftrag = _auftrag(db_session, tante, _slot(db_session, a, 5), _slot(db_session, b, 12))
    beitrag = _beitrag(db_session, auftrag, tante)

    detached = reassign_auftrag(db_session, auftrag.id, ruth.id, **T)
    db_session.commit()

    assert [d.id for d in detached] == [beitrag.id]
    db_session.expire_all()
    beitrag = db_session.get(Beitrag, beitrag.id)
    assert beitrag.auftrag_id is None
    assert beitrag.detached_at is not None
    assert beitrag.person_id == tante.id
    auftrag = db_session.get(Auftrag, auftrag.id)
    assert auftrag.person_id == ruth.id
    assert auftrag_is_open(auftrag)
    assert open_auftraege_for(db_session, ruth) == [auftrag]
    assert open_auftraege_for(db_session, tante) == []


def test_reassign_to_same_person_keeps_beitraege(db_session: Session):
    a = _calendar(db_session, "Familie")
    tante = _person(db_session, "tante@example.test")
    auftrag = _auftrag(db_session, tante, _slot(db_session, a, 5))
    beitrag = _beitrag(db_session, auftrag, tante)

    assert reassign_auftrag(db_session, auftrag.id, tante.id, **T) == []
    assert beitrag.auftrag_id == auftrag.id


def test_delete_auftrag_before_delivery_frees_days_and_keeps_recording(db_session: Session):
    a = _calendar(db_session, "Familie")
    tante = _person(db_session, "tante@example.test")
    slot = _slot(db_session, a, 5)
    auftrag = _auftrag(db_session, tante, slot)
    beitrag = _beitrag(db_session, auftrag, tante)

    delete_auftrag(db_session, auftrag.id, **T)
    db_session.commit()

    db_session.expire_all()
    assert db_session.get(Auftrag, auftrag.id) is None
    assert db_session.get(Slot, slot.id).auftrag_id is None
    kept = db_session.get(Beitrag, beitrag.id)
    assert kept.auftrag_id is None and kept.detached_at is not None


def test_replacement_in_other_calendar_refused_naming_it_r38(db_session: Session):
    a, b = _calendar(db_session, "Familie"), _calendar(db_session, "Oma Inge")
    tante = _person(db_session, "tante@example.test")
    beitrag = Beitrag(person_id=tante.id, audio_object_key="k", approved_at=datetime(2026, 11, 1))
    db_session.add(beitrag)
    db_session.commit()

    set_replacement_beitrag(db_session, beitrag.id, campaign_id=a.id)
    with pytest.raises(ReplacementInUseError, match="„Familie“"):
        set_replacement_beitrag(db_session, beitrag.id, campaign_id=b.id)

    db_session.expire_all()
    assert db_session.get(Campaign, b.id).replacement_beitrag_id is None
    # Im selben Kalender erneut setzen ist kein Konflikt.
    set_replacement_beitrag(db_session, beitrag.id, campaign_id=a.id)


# --- Mehrkalender U10: keine Doppelschreibung mehr in die Altspalten -------


def test_auftrag_services_no_longer_write_legacy_columns(db_session: Session):
    a, b = _calendar(db_session, "Familie"), _calendar(db_session, "Oma")
    tante = _person(db_session, "tante@example.test")
    a5, b3 = _slot(db_session, a, 5), _slot(db_session, b, 3)
    auftrag = _auftrag(db_session, tante, a5)
    beitrag = _beitrag(db_session, auftrag, tante)

    add_calendar_day(db_session, auftrag.id, b3.id, **T)
    db_session.commit()
    for slot in (a5, b3):
        assert (slot.assigned_person_id, slot.title, slot.vorlesetext) == (None, None, None)
        assert slot.auftrag.title == "Der Stern"
    assert beitrag.slot_id is None
    assert beitrag.auftrag_id == auftrag.id


# --- HTTP: Erinnerung (R11) und Admin-Upload ----------------------------------


def test_one_reminder_for_auftraege_in_two_calendars_r11(
    admin_client: TestClient, db_session: Session, monkeypatch
):
    sent = []
    monkeypatch.setattr("app.admin.people.send_reminder_mail", lambda config, **kw: sent.append(kw))
    a, b = _calendar(db_session, "Familie"), _calendar(db_session, "Oma")
    klaus = _person(db_session, "klaus@example.test", "Klaus")
    _auftrag(db_session, klaus, _slot(db_session, a, 3), _slot(db_session, b, 10))
    _auftrag(db_session, klaus, _slot(db_session, a, 7))
    create_auftrag(db_session, person_id=klaus.id)  # Entwurf, zaehlt nicht
    db_session.commit()

    admin_client.post(f"/admin/personen/{klaus.id}/erinnern")

    assert len(sent) == 1
    assert sent[0]["open_days"] == [3, 7]


def test_admin_upload_into_empty_day_creates_auftrag_without_person(
    admin_client: TestClient, db_session: Session
):
    a = _calendar(db_session, "Familie")
    slot = _slot(db_session, a, 15)

    response = admin_client.post(
        "/admin/aufnahmen/upload",
        data={"slot_id": str(slot.id)},
        files={"file": ("geschichte.mp3", make_tone_mp3(1.0), "audio/mpeg")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.expire_all()
    slot = db_session.get(Slot, slot.id)
    assert slot.auftrag is not None
    assert slot.auftrag.person_id is None
    beitrag = db_session.execute(select(Beitrag)).scalar_one()
    assert beitrag.auftrag_id == slot.auftrag_id
    assert beitrag.approved_at is None


def test_admin_upload_targets_existing_auftrag(admin_client: TestClient, db_session: Session):
    _calendar(db_session, "Familie")
    auftrag = _auftrag(db_session, None)  # Entwurf ohne Tag

    admin_client.post(
        "/admin/aufnahmen/upload",
        data={"auftrag_id": str(auftrag.id)},
        files={"file": ("geschichte.mp3", make_tone_mp3(1.0), "audio/mpeg")},
    )

    db_session.expire_all()
    beitrag = db_session.execute(select(Beitrag)).scalar_one()
    assert beitrag.auftrag_id == auftrag.id
    assert db_session.execute(select(Auftrag)).scalars().all() == [
        db_session.get(Auftrag, auftrag.id)
    ]
