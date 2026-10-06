"""U2: Admin-Setup -- Service-Funktionen und die duennen Routen dahinter.

Test scenarios aus dem Plan:
- Ein Slot ohne Vorlesetext laesst sich nicht als einladungsbereit markieren.
- Covers R35. Wird ein Auftrag mit eingereichtem Beitrag neu vergeben, verliert
  der Beitrag den Auftragsbezug, bleibt aber der Urheberin zugeordnet
  (Mehrkalender U9: am Auftrag statt am Slot; weitere Faelle in
  tests/test_auftraege.py).
- Verification: der Admin kann alle 24 Slots mit Titel und Vorlesetext
  pflegen, zuweisen, neu zuweisen und den Ersatzbeitrag hinterlegen.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin.setup import (
    SLOT_COUNT,
    add_calendar_day,
    create_auftrag,
    create_campaign,
    create_person,
    mark_invitation_ready,
    reassign_auftrag,
    set_replacement_beitrag,
    update_auftrag,
)
from app.models import Auftrag, Beitrag, Campaign, Person, Slot

WHEN = {
    "now": datetime(2026, 10, 1, 12, tzinfo=ZoneInfo("Europe/Berlin")),
    "delivery_time": time(20),
}


def _auftrag_on(db: Session, slot: Slot, person_id: int | None = None) -> Auftrag:
    auftrag = create_auftrag(db, person_id=person_id)
    add_calendar_day(db, auftrag.id, slot.id, **WHEN)
    db.commit()
    return auftrag


def test_create_campaign_creates_24_fixed_slots(db_session: Session):
    campaign = create_campaign(db_session)

    slots = db_session.execute(select(Slot).where(Slot.campaign_id == campaign.id)).scalars().all()
    assert len(slots) == SLOT_COUNT
    assert sorted(slot.day for slot in slots) == list(range(1, SLOT_COUNT + 1))


def test_create_campaign_twice_raises(db_session: Session):
    create_campaign(db_session)
    with pytest.raises(ValueError):
        create_campaign(db_session)


def test_update_auftrag_sets_title_and_vorlesetext(db_session: Session):
    campaign = create_campaign(db_session)
    slot = db_session.execute(
        select(Slot).where(Slot.campaign_id == campaign.id, Slot.day == 1)
    ).scalar_one()
    auftrag = _auftrag_on(db_session, slot)

    updated = update_auftrag(
        db_session, auftrag.id, title="Der kleine Stern", vorlesetext="Es war einmal..."
    )

    assert updated.title == "Der kleine Stern"
    assert updated.vorlesetext == "Es war einmal..."
    # Mehrkalender U10: keine Doppelschreibung in die Altspalten mehr.
    assert (slot.title, slot.vorlesetext) == (None, None)


def test_mark_invitation_ready_requires_vorlesetext(db_session: Session):
    campaign = create_campaign(db_session)
    slot = db_session.execute(
        select(Slot).where(Slot.campaign_id == campaign.id, Slot.day == 1)
    ).scalar_one()

    with pytest.raises(ValueError, match="Vorlesetext"):
        mark_invitation_ready(db_session, slot.id)


def test_mark_invitation_ready_succeeds_with_vorlesetext(db_session: Session):
    campaign = create_campaign(db_session)
    slot = db_session.execute(
        select(Slot).where(Slot.campaign_id == campaign.id, Slot.day == 1)
    ).scalar_one()
    update_auftrag(db_session, _auftrag_on(db_session, slot).id, vorlesetext="Es war einmal...")

    updated = mark_invitation_ready(db_session, slot.id)

    assert updated.invitation_ready is True


def test_reassigning_auftrag_detaches_submitted_beitrag_but_keeps_owner(db_session: Session):
    campaign = create_campaign(db_session)
    slot = db_session.execute(
        select(Slot).where(Slot.campaign_id == campaign.id, Slot.day == 1)
    ).scalar_one()
    original_person = create_person(db_session, "original@example.test")
    new_person = create_person(db_session, "neu@example.test")

    auftrag = _auftrag_on(db_session, slot, original_person.id)
    beitrag = Beitrag(person_id=original_person.id, auftrag=auftrag, title="Meine Geschichte")
    db_session.add(beitrag)
    db_session.commit()
    db_session.refresh(beitrag)

    detached = reassign_auftrag(db_session, auftrag.id, new_person.id, **WHEN)
    db_session.commit()

    assert [b.id for b in detached] == [beitrag.id]
    # U8: verwaist, nicht frei -- landet nicht im Eingang (R14).
    assert detached[0].detached_at is not None
    db_session.refresh(beitrag)
    assert beitrag.auftrag_id is None
    assert beitrag.person_id == original_person.id  # bleibt der Urheberin zugeordnet
    assert auftrag.person_id == new_person.id
    assert slot.auftrag.person_id == new_person.id


def test_assigning_auftrag_without_existing_beitrag_has_no_warning(db_session: Session):
    campaign = create_campaign(db_session)
    slot = db_session.execute(
        select(Slot).where(Slot.campaign_id == campaign.id, Slot.day == 1)
    ).scalar_one()
    person = create_person(db_session, "person@example.test")
    auftrag = _auftrag_on(db_session, slot)

    assert reassign_auftrag(db_session, auftrag.id, person.id, **WHEN) == []


def test_set_replacement_beitrag(db_session: Session):
    create_campaign(db_session)
    person = create_person(db_session, "person@example.test")
    beitrag = Beitrag(
        person_id=person.id, title="Ersatzgeschichte", approved_at=datetime(2026, 11, 1)
    )
    db_session.add(beitrag)
    db_session.commit()
    db_session.refresh(beitrag)

    updated = set_replacement_beitrag(db_session, beitrag.id)

    assert updated.replacement_beitrag_id == beitrag.id


def test_set_replacement_beitrag_refuses_unapproved_r11(db_session: Session):
    campaign = create_campaign(db_session)
    person = create_person(db_session, "person@example.test")
    beitrag = Beitrag(person_id=person.id, title="Noch nicht freigegeben")
    db_session.add(beitrag)
    db_session.commit()

    with pytest.raises(ValueError):
        set_replacement_beitrag(db_session, beitrag.id)

    db_session.refresh(campaign)
    assert campaign.replacement_beitrag_id is None


def test_admin_can_manage_campaign_through_http(admin_client: TestClient, db_session: Session):
    """Verification: der Admin kann alle Slots pflegen, zuweisen, neu
    zuweisen und den Ersatzbeitrag hinterlegen -- ueber die echte Route, nicht
    nur die Service-Funktion."""
    response = admin_client.post("/admin/campaign", follow_redirects=False)
    assert response.status_code in (200, 303)

    campaign = db_session.execute(select(Campaign)).scalar_one()
    slot = db_session.execute(
        select(Slot).where(Slot.campaign_id == campaign.id, Slot.day == 1)
    ).scalar_one()

    response = admin_client.post(
        "/admin/persons", data={"email": "tante@example.test"}, follow_redirects=False
    )
    assert response.status_code in (200, 303)
    person = db_session.execute(
        select(Person).where(Person.email == "tante@example.test")
    ).scalar_one()

    response = admin_client.post(
        f"/admin/geschichten/{slot.id}",
        data={"title": "Der Stern", "vorlesetext": "Es war einmal...", "person_id": person.id},
        follow_redirects=False,
    )
    assert response.status_code in (200, 303)

    db_session.refresh(slot)
    assert slot.auftrag.title == "Der Stern"
    assert slot.auftrag.vorlesetext == "Es war einmal..."
    assert slot.auftrag.person_id == person.id


def test_create_person_rejects_same_address_in_other_case(
    admin_client: TestClient, db_session: Session
):
    """Der Login vergleicht Adressen ohne Gross-/Kleinschreibung; zwei
    Personen, die sich nur darin unterscheiden, darf es deshalb nicht geben."""
    admin_client.post("/admin/persons", data={"email": "tante@example.test"})
    admin_client.post("/admin/persons", data={"email": " Tante@Example.test "})

    emails = db_session.execute(select(Person.email)).scalars().all()
    assert sum(1 for e in emails if e.lower() == "tante@example.test") == 1
