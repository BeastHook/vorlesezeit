"""U3: zentrale Berechtigungspruefung (R28), deny-by-default.

Test scenarios aus dem Plan:
- Covers AE11. Eine angemeldete Person erhaelt beim Aufruf eines fremden
  Slots eine Ablehnung.
- Covers AE11. Die Audiodatei eines fremden Beitrags wird abgelehnt, auch
  bei gueltiger Sitzung.
- Der Adminbereich ist fuer eine gewoehnliche angemeldete Person nicht
  erreichbar.

authorize_auftrag_access/authorize_beitrag_access/authorize_audio_access werden
hier als eigenstaendige Funktionen geprueft (siehe Plan: keine Slot-/
Beitrag-Detailroute in dieser Session -- die kommt mit U7-U9 und haengt
diese Funktionen dann per Depends(...) ein). Das Ausliefern selbst (U13,
Bereichsanfragen) pruefen tests/test_archive.py und tests/test_admin_review.py
ueber die echten Routen; hier nur die Personenpruefung davor.
"""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.dependencies import (
    authorize_audio_access,
    authorize_auftrag_access,
    authorize_beitrag_access,
)
from app.models import Auftrag, Beitrag, Campaign, Person, Slot


def _make_auftrag(db_session: Session, *, assigned_to: Person | None) -> Auftrag:
    campaign = Campaign()
    db_session.add(campaign)
    db_session.flush()
    auftrag = Auftrag(person_id=assigned_to.id if assigned_to else None)
    db_session.add_all([auftrag, Slot(campaign_id=campaign.id, day=1, auftrag=auftrag)])
    db_session.commit()
    db_session.refresh(auftrag)
    return auftrag


def test_owner_may_access_own_auftrag(db_session: Session):
    owner = Person(email="owner@example.test")
    db_session.add(owner)
    db_session.commit()
    auftrag = _make_auftrag(db_session, assigned_to=owner)

    authorize_auftrag_access(owner, auftrag)  # wirft nicht


def test_admin_may_access_any_auftrag(db_session: Session):
    owner = Person(email="owner@example.test")
    admin = Person(email="admin@example.test", is_admin=True)
    db_session.add_all([owner, admin])
    db_session.commit()
    auftrag = _make_auftrag(db_session, assigned_to=owner)

    authorize_auftrag_access(admin, auftrag)  # wirft nicht


def test_foreign_person_denied_for_auftrag():
    """Covers AE11."""
    owner = Person(id=1, email="owner@example.test")
    foreign = Person(id=2, email="foreign@example.test")
    auftrag = Auftrag(id=1, person_id=owner.id)

    try:
        authorize_auftrag_access(foreign, auftrag)
        assert False, "sollte HTTPException(403) werfen"
    except HTTPException as exc:
        assert exc.status_code == 403


def test_foreign_person_denied_for_beitrag_audio():
    """Covers AE11: die Audiodatei eines fremden Beitrags wird
    abgelehnt, auch bei gueltiger Sitzung (die Sitzung ist hier implizit
    durch die uebergebene Person simuliert)."""
    owner = Person(id=1, email="owner@example.test")
    foreign = Person(id=2, email="foreign@example.test")
    beitrag = Beitrag(id=1, person_id=owner.id, audio_object_key="beitraege/1/audio.m4a")

    try:
        authorize_audio_access(foreign, beitrag)
        assert False, "sollte HTTPException(403) werfen"
    except HTTPException as exc:
        assert exc.status_code == 403


def test_owner_receives_audio_object_key():
    """U13: statt einer signierten Adresse gibt die Pruefung den
    Objektschluessel frei; ausgeliefert wird ueber die App."""
    owner = Person(id=1, email="owner@example.test")
    beitrag = Beitrag(id=1, person_id=owner.id, audio_object_key="beitraege/1/audio.m4a")

    assert authorize_audio_access(owner, beitrag) == "beitraege/1/audio.m4a"


def test_beitrag_owner_check_matches_person(db_session: Session):
    owner = Person(email="owner@example.test")
    foreign = Person(email="foreign@example.test")
    db_session.add_all([owner, foreign])
    db_session.commit()
    beitrag = Beitrag(person_id=owner.id, title="Meine Geschichte")
    db_session.add(beitrag)
    db_session.commit()
    db_session.refresh(beitrag)

    authorize_beitrag_access(owner, beitrag)  # wirft nicht
    try:
        authorize_beitrag_access(foreign, beitrag)
        assert False, "sollte HTTPException(403) werfen"
    except HTTPException as exc:
        assert exc.status_code == 403


def test_admin_area_unreachable_for_ordinary_person(person_client: TestClient):
    response = person_client.get("/admin")
    assert response.status_code == 403


def test_admin_area_reachable_for_admin(admin_client: TestClient):
    response = admin_client.get("/admin")
    assert response.status_code == 200
