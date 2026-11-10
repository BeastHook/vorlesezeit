"""Job 035: hoechstens ein aktiver Beitrag je Auftrag.

Eine neue Aufnahme der Familie und ein Admin-Upload loesen einen aelteren,
nicht freigegebenen Beitrag desselben Auftrags ab (`detached_at`, Abschnitt
"Vom Türchen gelöst"). Ist der aeltere freigegeben, verweigern beide Wege
(R10). Bestehende Doppelungen bleiben und tragen im Reiter Aufnahmen die
Markierung "zwei aktive Beiträge". Gegen echten S3-Speicher und echtes ffmpeg.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin.setup import create_campaign, create_person
from app.models import Auftrag, Beitrag, Campaign, Person, Slot
from tests.fixtures.audio import make_tone_mp3, make_tone_webm

MARKER = "zwei aktive Beiträge"


def _admin(db: Session) -> Person:
    return db.execute(select(Person).where(Person.is_admin.is_(True))).scalar_one()


def _verwandte(db: Session) -> Person:
    return db.execute(select(Person).where(Person.email == "verwandte@example.test")).scalar_one()


def _family_auftrag(db: Session, person: Person, day: int = 3) -> Auftrag:
    campaign = Campaign(name="Familie")
    auftrag = Auftrag(person_id=person.id)
    db.add_all([campaign, auftrag])
    db.flush()
    db.add(Slot(campaign_id=campaign.id, day=day, auftrag=auftrag))
    db.commit()
    db.refresh(auftrag)
    return auftrag


def _stored_beitrag(
    client: TestClient, db: Session, person: Person, auftrag: Auftrag, **kw
) -> Beitrag:
    key = f"beitraege/{person.id}/{uuid.uuid4().hex}.mp3"
    client.app.state.storage.put(key, make_tone_mp3(1.0), content_type="audio/mpeg")
    beitrag = Beitrag(person_id=person.id, auftrag_id=auftrag.id, audio_object_key=key, **kw)
    db.add(beitrag)
    db.commit()
    db.refresh(beitrag)
    return beitrag


def _attached(db: Session, auftrag_id: int) -> list[Beitrag]:
    db.expire_all()
    return list(db.execute(select(Beitrag).where(Beitrag.auftrag_id == auftrag_id)).scalars().all())


def _record(client: TestClient, auftrag: Auftrag, title: str = "Neu"):
    return client.post(
        f"/record/auftrag/{auftrag.id}",
        files={"audio": ("aufnahme.webm", make_tone_webm(seconds=1.0), "audio/webm")},
        data={"title": title, "reported_type": "audio/webm"},
    )


# --- Familie: neue Aufnahme -------------------------------------------------


def test_recording_detaches_unapproved_admin_upload(person_client: TestClient, db_session: Session):
    person = _verwandte(db_session)
    auftrag = _family_auftrag(db_session, person)
    older = _stored_beitrag(person_client, db_session, _admin(db_session), auftrag)
    older_key = older.audio_object_key

    response = _record(person_client, auftrag)

    assert response.status_code == 200
    rows = _attached(db_session, auftrag.id)
    assert [b.person_id for b in rows] == [person.id]
    db_session.refresh(older)
    assert older.auftrag_id is None
    assert older.detached_at is not None
    # Abgeloest, nicht geloescht: die Datei bleibt fuer den Loeschabschnitt.
    assert older.audio_object_key == older_key
    assert person_client.app.state.storage.get(older_key)


def test_recording_detaches_rejected_beitrag_of_other_person(
    person_client: TestClient, db_session: Session
):
    person = _verwandte(db_session)
    auftrag = _family_auftrag(db_session, person)
    older = _stored_beitrag(
        person_client,
        db_session,
        _admin(db_session),
        auftrag,
        rejected_at=datetime(2026, 10, 2),
    )

    response = _record(person_client, auftrag)

    assert response.status_code == 200
    assert [b.person_id for b in _attached(db_session, auftrag.id)] == [person.id]
    db_session.refresh(older)
    assert older.detached_at is not None


def test_recording_refused_when_other_beitrag_is_approved(
    person_client: TestClient, db_session: Session
):
    person = _verwandte(db_session)
    auftrag = _family_auftrag(db_session, person)
    approved = _stored_beitrag(
        person_client,
        db_session,
        _admin(db_session),
        auftrag,
        approved_at=datetime(2026, 10, 2),
    )

    response = _record(person_client, auftrag)

    assert response.status_code == 409
    rows = _attached(db_session, auftrag.id)
    assert [b.id for b in rows] == [approved.id]
    assert rows[0].detached_at is None


def test_approval_of_other_beitrag_during_normalization_blocks(
    person_client: TestClient, db_session: Session, monkeypatch
):
    """Die zweite Pruefung unter dem Lock sieht eine Freigabe, die waehrend
    der Normalisierung erteilt wurde, auch fuer einen fremden Beitrag."""
    import app.recording.views as views

    person = _verwandte(db_session)
    auftrag = _family_auftrag(db_session, person)
    pending = _stored_beitrag(person_client, db_session, _admin(db_session), auftrag)
    real_normalize = views.normalize_recording

    def normalize_while_admin_approves(raw):
        pending.approved_at = datetime(2026, 10, 2)
        db_session.commit()
        return real_normalize(raw)

    monkeypatch.setattr(views, "normalize_recording", normalize_while_admin_approves)

    response = _record(person_client, auftrag)

    assert response.status_code == 409
    rows = _attached(db_session, auftrag.id)
    assert [b.id for b in rows] == [pending.id]
    assert rows[0].detached_at is None


# --- Admin-Upload --------------------------------------------------------------


def _admin_setup(db: Session) -> tuple[Person, Slot]:
    create_campaign(db)
    person = create_person(db, email="tante@example.test", display_name="Tante Ruth")
    slot = db.execute(select(Slot).where(Slot.day == 9)).scalar_one()
    auftrag = Auftrag(person_id=person.id)
    db.add(auftrag)
    slot.auftrag = auftrag
    db.commit()
    db.refresh(slot)
    return person, slot


def _upload(client: TestClient, slot: Slot):
    return client.post(
        "/admin/aufnahmen/upload",
        data={"slot_id": str(slot.id)},
        files={"file": ("geschichte.mp3", make_tone_mp3(1.0), "audio/mpeg")},
        follow_redirects=False,
    )


def test_admin_upload_detaches_unapproved_recording(admin_client: TestClient, db_session: Session):
    person, slot = _admin_setup(db_session)
    older = _stored_beitrag(admin_client, db_session, person, slot.auftrag, title="Erster Versuch")

    response = _upload(admin_client, slot)

    assert response.status_code == 303
    rows = _attached(db_session, slot.auftrag_id)
    assert [b.person_id for b in rows] == [_admin(db_session).id]
    db_session.refresh(older)
    assert older.auftrag_id is None
    assert older.detached_at is not None

    page = admin_client.get("/admin/aufnahmen").text
    detached_section = page.split("Vom Türchen gelöst", 1)[1].split("Einspringen", 1)[0]
    assert "Erster Versuch" in detached_section
    assert "oder einer neueren Aufnahme" in detached_section


def test_admin_upload_refused_when_recording_is_approved(
    admin_client: TestClient, db_session: Session
):
    person, slot = _admin_setup(db_session)
    approved = _stored_beitrag(
        admin_client, db_session, person, slot.auftrag, approved_at=datetime(2026, 10, 2)
    )

    page = admin_client.post(
        "/admin/aufnahmen/upload",
        data={"slot_id": str(slot.id)},
        files={"file": ("geschichte.mp3", make_tone_mp3(1.0), "audio/mpeg")},
    ).text

    assert 'class="flash err"' in page
    assert "hat schon eine freigegebene Aufnahme." in page
    rows = _attached(db_session, slot.auftrag_id)
    assert [b.id for b in rows] == [approved.id]
    assert rows[0].detached_at is None


def test_upload_choice_offers_slot_with_unapproved_recording_only(
    admin_client: TestClient, db_session: Session
):
    person, slot = _admin_setup(db_session)
    _stored_beitrag(admin_client, db_session, person, slot.auftrag)
    approved_slot = db_session.execute(select(Slot).where(Slot.day == 10)).scalar_one()
    approved_auftrag = Auftrag(person_id=person.id)
    db_session.add(approved_auftrag)
    approved_slot.auftrag = approved_auftrag
    db_session.commit()
    _stored_beitrag(
        admin_client, db_session, person, approved_auftrag, approved_at=datetime(2026, 10, 2)
    )

    page = admin_client.get("/admin/aufnahmen").text
    select_html = page.split('id="upload-slot"', 1)[1].split("</select>", 1)[0]

    assert f'value="{slot.id}"' in select_html
    assert "Tag 9 · Tante Ruth (wird abgelöst)" in select_html
    assert f'value="{approved_slot.id}"' not in select_html


# --- Bestehende Doppelungen -----------------------------------------------------


def test_existing_duplicate_is_marked_and_unchanged(admin_client: TestClient, db_session: Session):
    person, slot = _admin_setup(db_session)
    first = _stored_beitrag(admin_client, db_session, person, slot.auftrag, title="Erste")
    second = _stored_beitrag(
        admin_client, db_session, _admin(db_session), slot.auftrag, title="Zweite"
    )

    page = admin_client.get("/admin/aufnahmen").text
    days = page.split("Eingang ohne Türchen", 1)[0]

    assert days.count(MARKER) == 2
    rows = _attached(db_session, slot.auftrag_id)
    assert sorted(b.id for b in rows) == sorted([first.id, second.id])
    assert all(b.detached_at is None for b in rows)


def test_single_or_rejected_second_beitrag_is_not_marked(
    admin_client: TestClient, db_session: Session
):
    person, slot = _admin_setup(db_session)
    _stored_beitrag(admin_client, db_session, person, slot.auftrag, title="Erste")
    _stored_beitrag(
        admin_client,
        db_session,
        _admin(db_session),
        slot.auftrag,
        title="Abgelehnt",
        rejected_at=datetime(2026, 10, 2),
    )

    page = admin_client.get("/admin/aufnahmen").text

    assert MARKER not in page
