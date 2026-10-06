"""U7: Aufnahme-Endpunkte gegen echtes ffmpeg + echten S3-Speicher (kein Mock --
Plan-Execution-note: nur der Aufnahmeweg selbst ist nicht sinnvoll
unit-testbar; Route/Speicher/Normalisierung im Zusammenspiel schon).

- Covers AE1, AE2, AE9, AE4 (R10), AE17, AE25.
"""

from __future__ import annotations

import botocore.exceptions
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Auftrag, Beitrag, Campaign, Person, Slot
from tests.fixtures.audio import make_tone_webm, make_wrong_format_mp3


def _calendar(db_session: Session, name: str = "Familie") -> Campaign:
    campaign = db_session.execute(
        select(Campaign).where(Campaign.name == name)
    ).scalar_one_or_none()
    if campaign is None:
        campaign = Campaign(name=name)
        db_session.add(campaign)
        db_session.commit()
    return campaign


def _make_auftrag(
    db_session: Session,
    person: Person | None,
    day: int,
    *,
    vorlesetext: str | None = None,
    title: str | None = None,
    calendar: str = "Familie",
) -> Auftrag:
    """Ein Auftrag der Person an einem Kalendertag (Mehrkalender U12)."""
    campaign = _calendar(db_session, calendar)
    auftrag = Auftrag(person_id=person.id if person else None, title=title, vorlesetext=vorlesetext)
    db_session.add_all([auftrag, Slot(campaign_id=campaign.id, day=day, auftrag=auftrag)])
    db_session.commit()
    db_session.refresh(auftrag)
    return auftrag


def _current_person(db_session: Session) -> Person:
    return db_session.execute(
        select(Person).where(Person.email == "verwandte@example.test")
    ).scalar_one()


def test_home_redirects_to_earliest_open_slot_ae1(person_client: TestClient, db_session: Session):
    person = _current_person(db_session)
    _make_auftrag(db_session, person, day=9)
    earlier = _make_auftrag(db_session, person, day=3)

    response = person_client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/record/auftrag/{earlier.id}"


def test_home_shows_choice_without_open_slot_ae2(person_client: TestClient, db_session: Session):
    response = person_client.get("/")

    assert response.status_code == 200
    assert "Meine Geschichten" in response.text
    assert "Freie Nachricht" in response.text


def test_slot_view_denies_other_persons_slot(person_client: TestClient, db_session: Session):
    other = Person(email="andere@example.test", display_name="Andere")
    db_session.add(other)
    db_session.commit()
    auftrag = _make_auftrag(db_session, other, day=3)

    response = person_client.get(f"/record/auftrag/{auftrag.id}")

    assert response.status_code == 403


def test_slot_without_vorlesetext_hides_choice_ae17(person_client: TestClient, db_session: Session):
    person = _current_person(db_session)
    auftrag = _make_auftrag(db_session, person, day=3, vorlesetext=None)

    response = person_client.get(f"/record/auftrag/{auftrag.id}")

    assert response.status_code == 200
    assert "Vorschlag vorlesen" not in response.text


def test_slot_with_vorlesetext_shows_choice(person_client: TestClient, db_session: Session):
    person = _current_person(db_session)
    auftrag = _make_auftrag(db_session, person, day=3, vorlesetext="Es war einmal...")

    response = person_client.get(f"/record/auftrag/{auftrag.id}")

    assert response.status_code == 200
    assert "Vorschlag vorlesen" in response.text


def test_record_page_has_ornament_halo_and_timer_in_stop_button(
    person_client: TestClient, db_session: Session
):
    person = _current_person(db_session)
    auftrag = _make_auftrag(db_session, person, day=3)

    html = person_client.get(f"/record/auftrag/{auftrag.id}").text
    from app.advent import ornament_name

    assert f"/static/ornaments/{ornament_name(auftrag.id)}.svg" in html
    assert f"view-transition-name: orn-{auftrag.id}" in html
    stop = html.split('data-role="stop-btn"')[1].split("</button>")[0]
    assert 'data-role="timer"' in stop
    assert 'class="rec-halo"' in html


def test_free_recording_uses_kerze(person_client: TestClient):
    html = person_client.get("/record/free").text
    # Der Marken-Kopf verwendet ebenfalls kerze.svg -- eine Prüfung gegen den
    # vollen Seitentext würde also nie fehlschlagen. Ausschnitt auf den
    # page-head, der das Ornament tatsächlich trägt.
    page_head = html.split('class="page-head"')[1].split("</div>")[0]
    assert "/static/ornaments/kerze.svg" in page_head


def test_submit_recording_creates_beitrag_and_redirects_to_confirmation(
    person_client: TestClient, db_session: Session
):
    person = _current_person(db_session)
    auftrag = _make_auftrag(db_session, person, day=3)
    raw = make_tone_webm(seconds=1.5)

    response = person_client.post(
        f"/record/auftrag/{auftrag.id}",
        files={"audio": ("aufnahme.webm", raw, "audio/webm")},
        data={"title": "Der Mond zählt mit", "reported_type": "audio/webm"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["next_url"].endswith(f"/record/auftrag/{auftrag.id}/confirmed")

    beitrag = db_session.execute(
        select(Beitrag).where(Beitrag.auftrag_id == auftrag.id, Beitrag.person_id == person.id)
    ).scalar_one()
    assert beitrag.audio_object_key is not None
    assert beitrag.title == "Der Mond zählt mit"


def test_submit_empty_title_falls_back_at_confirmation_ae25(
    person_client: TestClient, db_session: Session
):
    person = _current_person(db_session)
    auftrag = _make_auftrag(db_session, person, day=3)
    raw = make_tone_webm(seconds=1.0)

    person_client.post(
        f"/record/auftrag/{auftrag.id}",
        files={"audio": ("aufnahme.webm", raw, "audio/webm")},
        data={"title": "", "reported_type": "audio/webm"},
    )
    confirmed = person_client.get(f"/record/auftrag/{auftrag.id}/confirmed")

    assert confirmed.status_code == 200
    # R10: ohne Titel kein Rueckfall auf eine Tuerchennummer.
    assert "„Eigene Geschichte“" in confirmed.text
    assert "Türchen" not in confirmed.text


def test_resubmit_replaces_previous_and_deletes_old_object_r10(
    person_client: TestClient, db_session: Session
):
    person = _current_person(db_session)
    auftrag = _make_auftrag(db_session, person, day=3)

    first = person_client.post(
        f"/record/auftrag/{auftrag.id}",
        files={"audio": ("aufnahme.webm", make_tone_webm(seconds=1.0), "audio/webm")},
        data={"title": "Erster Versuch", "reported_type": "audio/webm"},
    )
    assert first.status_code == 200

    beitraege_after_first = (
        db_session.execute(select(Beitrag).where(Beitrag.auftrag_id == auftrag.id)).scalars().all()
    )
    assert len(beitraege_after_first) == 1
    old_key = beitraege_after_first[0].audio_object_key
    storage = person_client.app.state.storage
    assert storage.get(old_key) is not None

    second = person_client.post(
        f"/record/auftrag/{auftrag.id}",
        files={"audio": ("aufnahme.webm", make_tone_webm(seconds=1.2), "audio/webm")},
        data={"title": "Zweiter Versuch", "reported_type": "audio/webm"},
    )
    assert second.status_code == 200

    db_session.expire_all()  # die App schreibt ueber eine eigene Session
    beitraege_after_second = (
        db_session.execute(select(Beitrag).where(Beitrag.auftrag_id == auftrag.id)).scalars().all()
    )
    # R10: ersetzt denselben Beitrag, legt keinen zweiten an.
    assert len(beitraege_after_second) == 1
    assert beitraege_after_second[0].id == beitraege_after_first[0].id
    assert beitraege_after_second[0].title == "Zweiter Versuch"

    # R40: die zuvor abgelegte Datei ist entfernt.
    try:
        storage.get(old_key)
        deleted = False
    except botocore.exceptions.ClientError:
        deleted = True
    assert deleted


def test_submit_rejects_unreadable_audio_r32(person_client: TestClient, db_session: Session):
    person = _current_person(db_session)
    auftrag = _make_auftrag(db_session, person, day=3)

    response = person_client.post(
        f"/record/auftrag/{auftrag.id}",
        files={"audio": ("aufnahme.mp3", make_wrong_format_mp3(), "audio/mpeg")},
        data={"title": "", "reported_type": "audio/mpeg"},
    )

    assert response.status_code == 422
    assert "error" in response.json()

    beitrag = db_session.execute(
        select(Beitrag).where(Beitrag.auftrag_id == auftrag.id, Beitrag.person_id == person.id)
    ).scalar_one_or_none()
    assert beitrag is None


def test_free_submission_creates_new_beitrag_without_slot(
    person_client: TestClient, db_session: Session
):
    response = person_client.post(
        "/record/free",
        files={"audio": ("aufnahme.webm", make_tone_webm(seconds=1.0), "audio/webm")},
        data={"title": "Nur so", "reported_type": "audio/webm"},
    )

    assert response.status_code == 200
    person = _current_person(db_session)
    beitrag = db_session.execute(
        select(Beitrag).where(Beitrag.person_id == person.id, Beitrag.auftrag_id.is_(None))
    ).scalar_one()
    assert beitrag.title == "Nur so"


def test_third_free_submission_does_not_crash(person_client: TestClient, db_session: Session):
    """Regression: _existing_beitrag() used to run unconditionally for
    free messages too (slot_id=None), so a third submission hit two
    existing slot_id=None rows for the same person and scalar_one_or_none()
    raised MultipleResultsFound. Free submissions must never look up an
    'existing' row to replace -- each one is always a new Beitrag."""
    person = _current_person(db_session)
    for i in range(3):
        response = person_client.post(
            "/record/free",
            files={"audio": ("aufnahme.webm", make_tone_webm(seconds=1.0), "audio/webm")},
            data={"title": f"Nachricht {i}", "reported_type": "audio/webm"},
        )
        assert response.status_code == 200, response.text

    beitraege = (
        db_session.execute(
            select(Beitrag).where(Beitrag.person_id == person.id, Beitrag.auftrag_id.is_(None))
        )
        .scalars()
        .all()
    )
    assert len(beitraege) == 3


def test_submit_storage_failure_returns_422(
    person_client: TestClient, db_session: Session, monkeypatch
):
    person = _current_person(db_session)
    auftrag = _make_auftrag(db_session, person, day=3)

    def failing_put(*args, **kwargs):
        raise RuntimeError("Speicher ist gerade nicht erreichbar")

    monkeypatch.setattr(person_client.app.state.storage, "put", failing_put)

    response = person_client.post(
        f"/record/auftrag/{auftrag.id}",
        files={"audio": ("aufnahme.webm", make_tone_webm(seconds=1.0), "audio/webm")},
        data={"title": "x", "reported_type": "audio/webm"},
    )

    assert response.status_code == 422
    beitrag = db_session.execute(
        select(Beitrag).where(Beitrag.auftrag_id == auftrag.id, Beitrag.person_id == person.id)
    ).scalar_one_or_none()
    assert beitrag is None


def test_submit_rejects_oversized_upload(person_client: TestClient, db_session: Session):
    from app.recording.views import MAX_UPLOAD_BYTES

    person = _current_person(db_session)
    auftrag = _make_auftrag(db_session, person, day=3)
    oversized = b"x" * (MAX_UPLOAD_BYTES + 1)

    response = person_client.post(
        f"/record/auftrag/{auftrag.id}",
        files={"audio": ("aufnahme.webm", oversized, "audio/webm")},
        data={"title": "x", "reported_type": "audio/webm"},
    )

    assert response.status_code == 413


def test_confirmation_points_to_next_open_slot_ae9(person_client: TestClient, db_session: Session):
    person = _current_person(db_session)
    auftrag = _make_auftrag(db_session, person, day=3)
    next_auftrag = _make_auftrag(db_session, person, day=9)

    person_client.post(
        f"/record/auftrag/{auftrag.id}",
        files={"audio": ("aufnahme.webm", make_tone_webm(seconds=1.0), "audio/webm")},
        data={"title": "x", "reported_type": "audio/webm"},
    )
    confirmed = person_client.get(f"/record/auftrag/{auftrag.id}/confirmed")

    assert confirmed.status_code == 200
    assert f"/record/auftrag/{next_auftrag.id}" in confirmed.text


def test_rejected_beitrag_reopens_slot_and_resubmit_clears_rejection_r13(
    person_client: TestClient, db_session: Session
):
    """R13 + R10: ein abgelehnter Beitrag blockiert den Slot nicht mehr; die
    neue Aufnahme ersetzt ihn und hebt die Ablehnung auf."""
    from datetime import datetime

    person = _current_person(db_session)
    auftrag = _make_auftrag(db_session, person, day=3)
    rejected = Beitrag(
        person_id=person.id,
        auftrag_id=auftrag.id,
        audio_object_key="beitraege/x/alt.mp3",
        rejected_at=datetime(2026, 10, 2),
    )
    db_session.add(rejected)
    db_session.commit()

    home = person_client.get("/", follow_redirects=False)
    assert home.headers["location"] == f"/record/auftrag/{auftrag.id}"

    response = person_client.post(
        f"/record/auftrag/{auftrag.id}",
        files={"audio": ("aufnahme.webm", make_tone_webm(seconds=1.0), "audio/webm")},
        data={"title": "Neu", "reported_type": "audio/webm"},
    )
    assert response.status_code == 200

    db_session.expire_all()
    rows = (
        db_session.execute(select(Beitrag).where(Beitrag.auftrag_id == auftrag.id)).scalars().all()
    )
    assert len(rows) == 1
    assert rows[0].id == rejected.id
    assert rows[0].rejected_at is None


def test_approved_beitrag_cannot_be_replaced_r10(person_client: TestClient, db_session: Session):
    """R10: ersetzen nur, solange der Admin nicht freigegeben hat."""
    from datetime import datetime

    person = _current_person(db_session)
    auftrag = _make_auftrag(db_session, person, day=3)
    approved = Beitrag(
        person_id=person.id,
        auftrag_id=auftrag.id,
        audio_object_key="beitraege/x/frei.mp3",
        approved_at=datetime(2026, 10, 2),
    )
    db_session.add(approved)
    db_session.commit()

    response = person_client.post(
        f"/record/auftrag/{auftrag.id}",
        files={"audio": ("aufnahme.webm", make_tone_webm(seconds=1.0), "audio/webm")},
        data={"title": "Neu", "reported_type": "audio/webm"},
    )
    assert response.status_code == 409

    db_session.expire_all()
    db_session.refresh(approved)
    assert approved.audio_object_key == "beitraege/x/frei.mp3"


def test_approval_during_normalization_still_blocks_replacement_r10(
    person_client: TestClient, db_session: Session, monkeypatch
):
    """R10, Review-Fund: gibt der Admin frei, waehrend die neue Aufnahme
    normalisiert wird, greift die zweite Pruefung unter dem Lock -- sie darf
    keinen veralteten Stand aus der Session der Anfrage lesen."""
    from datetime import datetime

    import app.recording.views as views

    person = _current_person(db_session)
    auftrag = _make_auftrag(db_session, person, day=3)
    pending = Beitrag(
        person_id=person.id, auftrag_id=auftrag.id, audio_object_key="beitraege/x/wartet.mp3"
    )
    db_session.add(pending)
    db_session.commit()

    # Die Identity-Map haelt Objekte nur schwach; ohne eine starke Referenz
    # laedt die zweite Abfrage zufaellig frisch. Hier haelt die Vorpruefung
    # ihren Fund fest -- wie es jeder Code taete, der z. B. auftrag.beitraege
    # laedt. Die Pruefung unter dem Lock darf davon nicht abhaengen.
    held: list[Beitrag] = []
    real_existing = views._existing_beitrag

    def existing_and_hold(db, **kw):
        found = real_existing(db, **kw)
        held.append(found)
        return found

    monkeypatch.setattr(views, "_existing_beitrag", existing_and_hold)
    real_normalize = views.normalize_recording

    def normalize_while_admin_approves(raw):
        pending.approved_at = datetime(2026, 10, 2)
        db_session.commit()
        return real_normalize(raw)

    monkeypatch.setattr(views, "normalize_recording", normalize_while_admin_approves)
    storage = person_client.app.state.storage
    put_keys: list[str] = []
    deleted_keys: list[str] = []
    real_put, real_delete = storage.put, storage.delete

    def spy_put(key, data, **kw):
        put_keys.append(key)
        return real_put(key, data, **kw)

    def spy_delete(key):
        deleted_keys.append(key)
        return real_delete(key)

    monkeypatch.setattr(storage, "put", spy_put)
    monkeypatch.setattr(storage, "delete", spy_delete)

    response = person_client.post(
        f"/record/auftrag/{auftrag.id}",
        files={"audio": ("aufnahme.webm", make_tone_webm(seconds=1.0), "audio/webm")},
        data={"title": "Neu", "reported_type": "audio/webm"},
    )

    assert response.status_code == 409
    db_session.expire_all()
    db_session.refresh(pending)
    assert pending.audio_object_key == "beitraege/x/wartet.mp3"
    assert deleted_keys == put_keys


def test_failed_commit_leaves_no_orphaned_file_r40(
    person_client: TestClient, db_session: Session, monkeypatch
):
    """R40: scheitert der Datenbank-Commit nach dem Ablegen, bleibt keine
    Datei im Speicher zurueck, auf die nichts zeigt."""
    import pytest
    from sqlalchemy.orm import Session as OrmSession

    person = _current_person(db_session)
    auftrag = _make_auftrag(db_session, person, day=4)
    storage = person_client.app.state.storage
    stored: list[str] = []
    original_put = storage.put

    def recording_put(key, data, **kw):
        stored.append(key)
        return original_put(key, data, **kw)

    def failing_commit(self):
        raise RuntimeError("DB weg")

    monkeypatch.setattr(storage, "put", recording_put)
    monkeypatch.setattr(OrmSession, "commit", failing_commit)

    with pytest.raises(RuntimeError):
        person_client.post(
            f"/record/auftrag/{auftrag.id}",
            files={"audio": ("a.webm", make_tone_webm(seconds=1.0), "audio/webm")},
            data={"title": "Probe", "reported_type": "audio/webm"},
        )

    assert len(stored) == 1
    try:
        storage.get(stored[0])
        raise AssertionError("die abgelegte Datei muss entfernt sein")
    except botocore.exceptions.ClientError:
        pass


# --- Mehrkalender U12: die Familie adressiert den Auftrag (KTD14) ----------


def _set_admin_name(db_session: Session, name: str) -> None:
    from datetime import datetime

    from app import settings

    settings.set_value(db_session, "admin_display_name", name, now=datetime(2026, 10, 3))
    db_session.commit()


def _two_calendar_auftrag(db_session: Session, person: Person, **kw) -> Auftrag:
    auftrag = _make_auftrag(db_session, person, day=12, **kw)
    patenkinder = _calendar(db_session, "Patenkinder")
    db_session.add(Slot(campaign_id=patenkinder.id, day=5, auftrag=auftrag))
    db_session.commit()
    return auftrag


def test_home_redirects_to_auftrag_with_earliest_open_calendar_day(
    person_client: TestClient, db_session: Session
):
    person = _current_person(db_session)
    _make_auftrag(db_session, person, day=9)
    across = _two_calendar_auftrag(db_session, person)  # Tag 12 in A, Tag 5 in B

    response = person_client.get("/", follow_redirects=False)

    assert response.headers["location"] == f"/record/auftrag/{across.id}"


def test_draft_auftrag_hidden_and_its_address_404_r36(
    person_client: TestClient, db_session: Session
):
    person = _current_person(db_session)
    draft = Auftrag(person_id=person.id, title="Noch ein Entwurf")
    db_session.add(draft)
    db_session.commit()

    assert person_client.get("/", follow_redirects=False).status_code == 200
    assert "Noch ein Entwurf" not in person_client.get("/record/campaign").text
    assert person_client.get(f"/record/auftrag/{draft.id}").status_code == 404
    submit = person_client.post(
        f"/record/auftrag/{draft.id}",
        files={"audio": ("a.webm", make_tone_webm(seconds=1.0), "audio/webm")},
        data={"title": "x", "reported_type": "audio/webm"},
    )
    assert submit.status_code == 404
    assert db_session.execute(select(Beitrag)).first() is None


def test_submission_to_foreign_auftrag_denied(person_client: TestClient, db_session: Session):
    other = Person(email="andere@example.test", display_name="Andere")
    db_session.add(other)
    db_session.commit()
    auftrag = _make_auftrag(db_session, other, day=3)

    submit = person_client.post(
        f"/record/auftrag/{auftrag.id}",
        files={"audio": ("a.webm", make_tone_webm(seconds=1.0), "audio/webm")},
        data={"title": "x", "reported_type": "audio/webm"},
    )

    assert submit.status_code == 403
    assert person_client.get(f"/record/auftrag/{auftrag.id}/confirmed").status_code == 403
    assert db_session.execute(select(Beitrag)).first() is None


def test_recording_page_shows_title_deadline_and_admin_name_without_day_or_calendar_r10(
    person_client: TestClient, db_session: Session
):
    person = _current_person(db_session)
    _set_admin_name(db_session, "Luca")
    auftrag = _two_calendar_auftrag(
        db_session, person, title="Sterne zählen", vorlesetext="Als der Schnee knisterte"
    )

    page = person_client.get(f"/record/auftrag/{auftrag.id}").text

    assert "Sterne zählen" in page
    assert "Aufnehmen bis 24. November" in page
    assert "Nur du und Luca hören die Aufnahme" in page
    assert "schick sie Luca. Luca spielt sie für dich ein." in page
    assert "Türchen" not in page
    assert "Dez." not in page and "Dezember" not in page
    assert "Familie" not in page and "Patenkinder" not in page
    assert "der Admin" not in page and "dem Admin" not in page


def test_overview_lists_auftrag_once_with_excerpt_and_deadline_r10(
    person_client: TestClient, db_session: Session
):
    person = _current_person(db_session)
    _set_admin_name(db_session, "Luca")
    across = _two_calendar_auftrag(
        db_session, person, title="Sterne zählen", vorlesetext="Als der Schnee knisterte"
    )
    own = _make_auftrag(db_session, person, day=20)

    page = person_client.get("/record/campaign").text

    assert page.count("Sterne zählen") == 1
    assert "Als der Schnee knisterte" in page
    assert "Eigene Geschichte" in page
    assert "Luca freut sich über eine Geschichte deiner Wahl." in page
    assert "Luca hat sie für dich ausgesucht." in page
    assert page.count("Aufnehmen bis 24. November") == 2
    assert f'href="/record/auftrag/{across.id}"' in page
    assert f'href="/record/auftrag/{own.id}"' in page
    assert "Türchen" not in page
    assert "Patenkinder" not in page
    assert 'aria-current="page">Meine Geschichten</a>' in page


def test_empty_overview_names_admin_r26(person_client: TestClient, db_session: Session):
    _set_admin_name(db_session, "Luca")

    page = person_client.get("/record/campaign").text

    assert "Luca hat dir noch keine Geschichte gegeben." in page


def test_free_recording_names_admin_r26(person_client: TestClient, db_session: Session):
    _set_admin_name(db_session, "Luca")

    page = person_client.get("/record/free").text

    assert "Die Nachricht geht nur an Luca" in page


def test_confirmation_names_admin_and_story_title(person_client: TestClient, db_session: Session):
    person = _current_person(db_session)
    _set_admin_name(db_session, "Luca")
    auftrag = _make_auftrag(db_session, person, day=3, title="Sterne zählen")

    person_client.post(
        f"/record/auftrag/{auftrag.id}",
        files={"audio": ("a.webm", make_tone_webm(seconds=1.0), "audio/webm")},
        data={"title": "", "reported_type": "audio/webm"},
    )
    page = person_client.get(f"/record/auftrag/{auftrag.id}/confirmed").text

    assert "„Sterne zählen“ liegt jetzt bei Luca zur Freigabe." in page
    assert 'href="/record/campaign">Zu deinen Geschichten</a>' in page


def test_confirmation_names_child(person_client: TestClient, db_session: Session):
    from datetime import datetime

    from app import settings

    person = _current_person(db_session)
    _set_admin_name(db_session, "Luca")
    settings.set_value(db_session, "kind_name", "Emma & Lukas", now=datetime(2026, 10, 4))
    auftrag = _make_auftrag(db_session, person, day=3, title="Sterne zählen")

    person_client.post(
        f"/record/auftrag/{auftrag.id}",
        files={"audio": ("a.webm", make_tone_webm(seconds=1.0), "audio/webm")},
        data={"title": "", "reported_type": "audio/webm"},
    )
    page = person_client.get(f"/record/auftrag/{auftrag.id}/confirmed").text

    assert "zur Freigabe. Danke dir, das wird Emma &amp; Lukas freuen." in page


def test_confirmed_page_shows_seal_with_initials(person_client: TestClient, db_session: Session):
    person = _current_person(db_session)
    auftrag = _make_auftrag(db_session, person, day=3, title="Sterne zählen")

    person_client.post(
        f"/record/auftrag/{auftrag.id}",
        files={"audio": ("a.webm", make_tone_webm(seconds=1.0), "audio/webm")},
        data={"title": "", "reported_type": "audio/webm"},
    )
    html = person_client.get(f"/record/auftrag/{auftrag.id}/confirmed").text

    assert 'class="siegel is-stamp"' in html
    assert "Versiegelt von" in html
    assert 'class="schnee"' in html


def test_submission_writes_auftrag_only(person_client: TestClient, db_session: Session):
    """Seit U10 liest kein Admin-Leser mehr `slot_id`; die Einreichung haengt
    nur am Auftrag."""
    person = _current_person(db_session)
    auftrag = _two_calendar_auftrag(db_session, person)
    earliest = min(auftrag.slots, key=lambda s: s.day)

    response = person_client.post(
        f"/record/auftrag/{auftrag.id}",
        files={"audio": ("a.webm", make_tone_webm(seconds=1.0), "audio/webm")},
        data={"title": "x", "reported_type": "audio/webm"},
    )

    assert response.json()["next_url"] == f"/record/auftrag/{auftrag.id}/confirmed"
    beitrag = db_session.execute(select(Beitrag)).scalar_one()
    assert beitrag.auftrag_id == auftrag.id
    # Mehrkalender U10: keine Doppelschreibung in die Altspalte `slot_id` mehr;
    # der Beitrag gilt ueber den Auftrag an jedem Tag, auch am fruehesten.
    assert beitrag.slot_id is None
    assert earliest.auftrag_id == beitrag.auftrag_id


def test_simultaneous_double_submission_keeps_one_beitrag(
    person_client: TestClient, db_session: Session, monkeypatch
):
    """Die Sperre je (Person, Auftrag) serialisiert Lesen und Schreiben: zwei
    gleichzeitige Einreichungen ergeben einen Beitrag, nicht zwei."""
    import threading
    import time

    import app.recording.views as views

    person = _current_person(db_session)
    auftrag = _make_auftrag(db_session, person, day=3)
    real_existing = views._existing_beitrag

    def slow_existing(db, **kw):
        found = real_existing(db, **kw)
        time.sleep(0.3)  # ohne Sperre lesen beide Anfragen "nichts vorhanden"
        return found

    monkeypatch.setattr(views, "_existing_beitrag", slow_existing)

    def submit(title: str) -> None:
        response = person_client.post(
            f"/record/auftrag/{auftrag.id}",
            files={"audio": ("a.webm", make_tone_webm(seconds=1.0), "audio/webm")},
            data={"title": title, "reported_type": "audio/webm"},
        )
        assert response.status_code == 200, response.text

    threads = [threading.Thread(target=submit, args=(t,)) for t in ("Eins", "Zwei")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    rows = db_session.execute(select(Beitrag)).scalars().all()
    assert len(rows) == 1


def test_campaign_shows_approved_story_as_done_without_rerecord(person_client, db_session: Session):
    """Nach der Freigabe zeigt "Deine Geschichten" den Stand "freigegeben" und
    bietet kein "Neu aufnehmen" mehr an -- eine neue Aufnahme waere nach R10
    erst beim Abschicken mit 409 abgelehnt worden."""
    from datetime import datetime

    person = _current_person(db_session)
    auftrag = _make_auftrag(db_session, person, day=3, title="Der kleine Tannenbaum")
    db_session.add(
        Beitrag(
            person_id=person.id,
            auftrag_id=auftrag.id,
            audio_object_key="beitraege/x/frei.mp3",
            approved_at=datetime(2026, 10, 2),
        )
    )
    db_session.commit()

    html = person_client.get("/record/campaign").text
    card = html.split("Der kleine Tannenbaum")[1].split("</li>")[0]

    assert "freigegeben, kommt auf den Tonie" in card
    assert "gibt sie frei" not in card
    assert "Neu aufnehmen" not in card
    assert f'href="/record/auftrag/{auftrag.id}"' not in card
    assert 'href="/archiv"' in card
    assert 'class="tuerchen is-offen' in html


def test_campaign_cards_show_door_with_stable_ornament(person_client, db_session: Session):
    person = _current_person(db_session)
    auftrag = _make_auftrag(db_session, person, day=3)

    html = person_client.get("/record/campaign").text
    from app.advent import ornament_name

    assert 'class="tuerchen is-fuge' in html
    assert f"/static/ornaments/{ornament_name(auftrag.id)}.svg" in html
    assert f"view-transition-name: orn-{auftrag.id}" in html
    assert "data-tuerchen-open" in html


def test_family_pages_carry_desktop_layout_classes(person_client, db_session: Session):
    """Desktop-Layout B (Nutzerentscheidung 2026-10-04,
    docs/design/desktop-layout-mockup.html): Geschichten und Archiv als
    Tuerchen-Wand, die Aufnahmeseite mit breiterem Lesebogen."""
    person = _current_person(db_session)
    auftrag = _make_auftrag(db_session, person, day=3)

    assert '<main class="page page-wand">' in person_client.get("/record/campaign").text
    assert '<main class="page page-wand">' in person_client.get("/archiv").text
    record = person_client.get(f"/record/auftrag/{auftrag.id}").text
    assert '<main class="page page-lesen">' in record
    assert '<main class="page page-lesen">' in person_client.get("/record/free").text
