"""U2: reine Modell-/ORM-Ebene.

Test scenarios aus dem Plan (Service-Ebene -- assign_slot, mark_invitation_ready
-- liegt in test_admin_setup.py):
- Ein Beitrag ohne Slot ist gueltig und gilt als freie Einreichung.
- Ein Slot laesst sich nicht zwei Adventstagen zuordnen.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import init_db
from app.models import Beitrag, Campaign, Person, Slot


def test_beitrag_without_slot_is_valid_free_submission(db_session: Session):
    person = Person(email="person@example.test")
    db_session.add(person)
    db_session.commit()

    beitrag = Beitrag(person_id=person.id, slot_id=None, title="Eigene Geschichte")
    db_session.add(beitrag)
    db_session.commit()
    db_session.refresh(beitrag)

    assert beitrag.slot_id is None
    assert beitrag.person_id == person.id


def test_slot_cannot_be_assigned_to_two_advent_days(db_session: Session):
    campaign = Campaign()
    db_session.add(campaign)
    db_session.commit()

    db_session.add(Slot(campaign_id=campaign.id, day=1))
    db_session.commit()

    db_session.add(Slot(campaign_id=campaign.id, day=1))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_person_access_version_defaults_to_zero(db_session: Session):
    person = Person(email="neu@example.test")
    db_session.add(person)
    db_session.commit()
    db_session.refresh(person)

    assert person.access_version == 0


def test_init_db_adds_app_chapters_to_existing_database_and_keeps_rows(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'alt.db'}")
    init_db(engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE campaigns DROP COLUMN app_chapters"))
        conn.execute(
            text(
                "INSERT INTO campaigns (id, creative_tonie_id, verified_chapter_id) "
                "VALUES (1, 'T1', 'kapitel-alt'), (2, 'T2', NULL)"
            )
        )

    init_db(engine)

    columns = {c["name"] for c in inspect(engine).get_columns("campaigns")}
    assert "app_chapters" in columns
    with engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT id, app_chapters FROM campaigns")).all())
    assert json.loads(rows[1]) == [{"id": "kapitel-alt", "seconds": None}]
    assert rows[2] is None


def test_init_db_twice_is_harmless(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'neu.db'}")
    init_db(engine)
    init_db(engine)
    assert "app_chapters" in {c["name"] for c in inspect(engine).get_columns("campaigns")}


def test_repr_of_credentials_never_contains_password():
    from app.models import Einstellungen, TonieKonto

    konto = TonieKonto(id=1, label="Familie", username="a@example.test", password="geheim-123")
    einstellungen = Einstellungen(id=1, smtp_user="b@example.test", smtp_password="geheim-456")
    assert "geheim-123" not in repr(konto)
    assert "geheim-456" not in repr(einstellungen)
