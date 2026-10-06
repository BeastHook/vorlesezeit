"""U8: Admin-Reiter Aufnahmen -- Freigabe, Ablehnung, Ruecknahme, Zuschnitt,
Kapitelname, Upload, Loeschen. Gegen echten S3-Speicher und echtes ffmpeg.

- Covers AE5, AE15, AE19 (Speicherseite), AE25, R11, R13-R15, R27, R28,
  R30, R40, R41, R42.
"""

from __future__ import annotations

import io
import re
import uuid
import zipfile
from datetime import UTC, datetime, time
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin.setup import (
    add_calendar_day,
    create_auftrag,
    create_campaign,
    create_person,
    new_campaign,
    update_auftrag,
)
from app.admin.state import slot_state
from app.models import Beitrag, Campaign, CreativeTonie, DeliveryRun, Person, Slot
from app.recording.routing import is_slot_open
from tests.fixtures.audio import make_tone_mp3, make_wrong_format_mp3
from tests.mailutil import plain_text


def _slot(db: Session, day: int) -> Slot:
    return db.execute(select(Slot).where(Slot.day == day)).scalar_one()


def _setup(db: Session):
    campaign = create_campaign(db)
    person = create_person(db, email="tante@example.test", display_name="Tante Ruth")
    return campaign, person


def _beitrag(client: TestClient, db: Session, person: Person, slot: Slot | None, **kw) -> Beitrag:
    storage = client.app.state.storage
    key = f"beitraege/{person.id}/{uuid.uuid4().hex}.mp3"
    storage.put(key, make_tone_mp3(1.0), content_type="audio/mpeg")
    auftrag = None
    if slot is not None:
        auftrag = slot.auftrag or _auftrag(db, slot, person)
    b = Beitrag(
        person_id=person.id,
        auftrag=auftrag,
        audio_object_key=key,
        **kw,
    )
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


def _auftrag(db: Session, slot: Slot, person: Person | None):
    """Mehrkalender U9: ein Auftrag fuer die Person an diesem Tag."""
    auftrag = create_auftrag(db, person_id=person.id if person else None)
    add_calendar_day(
        db,
        auftrag.id,
        slot.id,
        now=datetime(2026, 10, 1, 12, tzinfo=ZoneInfo("Europe/Berlin")),
        delivery_time=time(20),
    )
    db.commit()
    return auftrag


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _delivered(db: Session, campaign: Campaign, day: int, outcome: str = "erfolg") -> None:
    """Vorabend-Lauf eines Tonie des Kalenders (KTD10: das Festwerden liest
    `delivery_runs.tonie_id`)."""
    if not campaign.tonies:
        db.add(CreativeTonie(tonie_id="tonie-1", campaign=campaign))
    db.add(
        DeliveryRun(
            campaign_id=campaign.id,
            tonie_id="tonie-1",
            run_type="vorabend",
            target_day=day,
            started_at=_now(),
            outcome=outcome,
        )
    )
    db.commit()


# --- Liste und Detail -------------------------------------------------------


def test_list_redirects_without_campaign(admin_client: TestClient):
    response = admin_client.get("/admin/aufnahmen", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"


def test_free_submissions_only_in_inbox_detached_nowhere_r14(
    admin_client: TestClient, db_session: Session
):
    _, person = _setup(db_session)
    _beitrag(admin_client, db_session, person, _slot(db_session, 9), title="Ein Lied")
    _beitrag(admin_client, db_session, person, None, title="Hallo aus Leipzig")
    _beitrag(admin_client, db_session, person, None, title="Verwaist", detached_at=_now())

    text = admin_client.get("/admin/aufnahmen").text
    days, inbox = text.split("Eingang ohne Türchen", 1)

    assert "Ein Lied" in days
    assert "Hallo aus Leipzig" not in days
    assert "Hallo aus Leipzig" in inbox
    assert "Ein Lied" not in inbox.split("Audiodatei in ein Türchen legen")[0]
    # R35/R40: geloeste Beitraege weder bei den Tuerchen noch im Eingang,
    # nur im eigenen Abschnitt zum Loeschen.
    inbox_only, detached = inbox.split("Vom Türchen gelöst", 1)
    assert "Verwaist" not in days
    assert "Verwaist" not in inbox_only
    assert "Verwaist" in detached


def test_chapter_name_prefilled_from_auftrag_title_and_persists_r42(
    admin_client: TestClient, db_session: Session
):
    _, person = _setup(db_session)
    slot = _slot(db_session, 9)
    update_auftrag(
        db_session, _auftrag(db_session, slot, person).id, title="Ein Lied für die Nachbarn"
    )
    db_session.commit()
    b = _beitrag(admin_client, db_session, person, slot)

    page = admin_client.get(f"/admin/aufnahmen?beitrag_id={b.id}").text
    assert 'value="Ein Lied für die Nachbarn"' in page

    admin_client.post(
        f"/admin/aufnahmen/{b.id}/zuschnitt",
        data={
            "chapter_title": "Das Nachbarslied",
            "cut_start": "",
            "cut_end": "",
            "action": "save",
        },
    )
    db_session.expire_all()
    assert db_session.get(Beitrag, b.id).chapter_title == "Das Nachbarslied"
    page = admin_client.get(f"/admin/aufnahmen?beitrag_id={b.id}").text
    assert 'value="Das Nachbarslied"' in page


def test_chapter_name_open_marker_ae25(admin_client: TestClient, db_session: Session):
    _, person = _setup(db_session)
    _beitrag(admin_client, db_session, person, _slot(db_session, 11))

    page = admin_client.get("/admin/aufnahmen").text
    assert "Kapitelname offen" in page
    assert "Türchen 11" in page or "Tuerchen 11" in page


def test_detail_plays_audio_through_admin_route_r28(admin_client: TestClient, db_session: Session):
    """U13/KTD17: Player und Wellenform zeigen auf eine Route der App, nicht
    auf den Speicher."""
    _, person = _setup(db_session)
    b = _beitrag(admin_client, db_session, person, _slot(db_session, 9))

    page = admin_client.get(f"/admin/aufnahmen?beitrag_id={b.id}").text
    assert f'src="/admin/aufnahmen/{b.id}/audio" data-role="audio"' in page
    assert f'data-audio-src="/admin/aufnahmen/{b.id}/audio"' in page
    assert b.audio_object_key not in page
    assert "X-Amz-Signature" not in page


def test_admin_audio_route_serves_any_beitrag_r28(admin_client: TestClient, db_session: Session):
    _, person = _setup(db_session)
    b = _beitrag(admin_client, db_session, person, None, title="Frei")
    payload = admin_client.app.state.storage.get(b.audio_object_key)

    full = admin_client.get(f"/admin/aufnahmen/{b.id}/audio")
    part = admin_client.get(f"/admin/aufnahmen/{b.id}/audio", headers={"Range": "bytes=0-1"})

    assert full.status_code == 200
    assert full.content == payload
    assert full.headers["content-type"] == "audio/mpeg"
    assert full.headers["accept-ranges"] == "bytes"
    assert part.status_code == 206
    assert part.content == payload[:2]
    assert part.headers["content-range"] == f"bytes 0-1/{len(payload)}"


def test_admin_audio_route_forbidden_for_ordinary_person_ae11(
    person_client: TestClient, db_session: Session
):
    person = db_session.execute(
        select(Person).where(Person.email == "verwandte@example.test")
    ).scalar_one()
    _setup(db_session)
    b = _beitrag(person_client, db_session, person, None, title="Eigene")

    response = person_client.get(f"/admin/aufnahmen/{b.id}/audio")

    assert response.status_code == 403
    assert "accept-ranges" not in response.headers


def test_admin_audio_route_404_without_stored_file(admin_client: TestClient, db_session: Session):
    _, person = _setup(db_session)
    b = Beitrag(person_id=person.id, title="Ohne Datei")
    db_session.add(b)
    db_session.commit()

    assert admin_client.get(f"/admin/aufnahmen/{b.id}/audio").status_code == 404
    assert admin_client.get("/admin/aufnahmen/999999/audio").status_code == 404


def test_delivered_slot_beitrag_tagged_ausgeliefert(admin_client: TestClient, db_session: Session):
    campaign, person = _setup(db_session)
    _beitrag(admin_client, db_session, person, _slot(db_session, 6), approved_at=_now())
    _delivered(db_session, campaign, 6)

    page = admin_client.get("/admin/aufnahmen").text
    assert "ausgeliefert" in page


# --- Freigabe / Ablehnung / Ruecknahme --------------------------------------


def test_approve_sets_approved_r11(admin_client: TestClient, db_session: Session):
    _, person = _setup(db_session)
    b = _beitrag(admin_client, db_session, person, _slot(db_session, 9))

    response = admin_client.post(f"/admin/aufnahmen/{b.id}/freigeben", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/aufnahmen?beitrag_id={b.id}&stamp=1"
    db_session.expire_all()
    assert db_session.get(Beitrag, b.id).approved_at is not None


def test_manual_upload_form_only_for_approved(admin_client: TestClient, db_session: Session):
    _, person = _setup(db_session)
    b = _beitrag(admin_client, db_session, person, _slot(db_session, 9))

    page = admin_client.get(f"/admin/aufnahmen?beitrag_id={b.id}").text
    assert "/admin/auslieferung/manuell" not in page

    admin_client.post(f"/admin/aufnahmen/{b.id}/freigeben")
    page = admin_client.get(f"/admin/aufnahmen?beitrag_id={b.id}").text
    assert 'action="/admin/auslieferung/manuell"' in page
    assert f'name="order_{b.id}"' in page


def test_approved_detail_shows_seal(admin_client: TestClient, db_session: Session):
    """Advent-Fassung (Task 6): das Freigabe-Siegel steht neben dem Status,
    aber nur sobald der Beitrag freigegeben ist."""
    _, person = _setup(db_session)
    b = _beitrag(admin_client, db_session, person, _slot(db_session, 9))

    page = admin_client.get(f"/admin/aufnahmen?beitrag_id={b.id}").text
    assert 'class="siegel' not in page

    admin_client.post(f"/admin/aufnahmen/{b.id}/freigeben")
    page = admin_client.get(f"/admin/aufnahmen?beitrag_id={b.id}").text
    assert 'class="siegel' in page


def test_approval_seal_stamps_only_right_after_approving(
    admin_client: TestClient, db_session: Session
):
    """Final-Review-Fund (Ruling 13): die Praegeanimation lief bisher bei
    jedem Laden erneut; sie darf nur unmittelbar nach dem Freigeben-Klick
    erscheinen, bei jedem weiteren Aufruf steht das Siegel ruhig."""
    _, person = _setup(db_session)
    b = _beitrag(admin_client, db_session, person, _slot(db_session, 9))

    response = admin_client.post(f"/admin/aufnahmen/{b.id}/freigeben", follow_redirects=False)
    stamped_url = response.headers["location"]
    assert stamped_url.endswith("&stamp=1")

    page = admin_client.get(stamped_url).text
    assert 'class="siegel is-stamp"' in page

    page = admin_client.get(f"/admin/aufnahmen?beitrag_id={b.id}").text
    assert 'class="siegel is-stamp"' not in page
    assert 'class="siegel"' in page


def test_reject_reopens_slot_and_mails_submitter_ae5(
    admin_client: TestClient, db_session: Session, monkeypatch
):
    sent = []
    monkeypatch.setattr(
        "app.admin.review.send_rejection_mail", lambda config, **kw: sent.append(kw)
    )
    _, person = _setup(db_session)
    slot = _slot(db_session, 9)
    b = _beitrag(admin_client, db_session, person, slot)
    b.auftrag.title = "Der kleine Stern"
    db_session.commit()
    assert not is_slot_open(db_session, slot)

    admin_client.post(
        f"/admin/aufnahmen/{b.id}/ablehnen",
        data={"comment": "Am Ende fehlt ein Stück — magst du es noch mal aufnehmen?"},
    )

    db_session.expire_all()
    b = db_session.get(Beitrag, b.id)
    assert b.rejected_at is not None
    assert b.approved_at is None
    assert b.auftrag_id == db_session.get(Slot, slot.id).auftrag_id
    assert is_slot_open(db_session, db_session.get(Slot, slot.id))
    assert len(sent) == 1
    assert sent[0]["to_address"] == "tante@example.test"
    # R10: Titel statt Tuerchennummer.
    assert sent[0]["title"] == "Der kleine Stern"
    assert "day" not in sent[0]
    assert "Am Ende fehlt ein Stück" in sent[0]["comment"]
    assert "/login/confirm?token=" in sent[0]["login_url"]
    # R27/KTD14: direkt zur Aufnahmeansicht des abgelehnten Auftrags.
    query = parse_qs(urlsplit(sent[0]["login_url"]).query)
    assert query["next"] == [f"/record/auftrag/{b.auftrag_id}"]


def test_reject_free_submission_links_to_free_recording_r27(
    admin_client: TestClient, db_session: Session, monkeypatch
):
    sent = []
    monkeypatch.setattr(
        "app.admin.review.send_rejection_mail", lambda config, **kw: sent.append(kw)
    )
    _, person = _setup(db_session)
    b = _beitrag(admin_client, db_session, person, None)

    admin_client.post(f"/admin/aufnahmen/{b.id}/ablehnen", data={"comment": ""})

    assert parse_qs(urlsplit(sent[0]["login_url"]).query)["next"] == ["/record/free"]


def test_reject_refused_when_approved(admin_client: TestClient, db_session: Session, monkeypatch):
    monkeypatch.setattr("app.admin.review.send_rejection_mail", lambda config, **kw: None)
    _, person = _setup(db_session)
    b = _beitrag(admin_client, db_session, person, _slot(db_session, 9), approved_at=_now())

    page = admin_client.post(f"/admin/aufnahmen/{b.id}/ablehnen", data={"comment": ""}).text

    assert 'class="flash err"' in page
    db_session.expire_all()
    assert db_session.get(Beitrag, b.id).rejected_at is None


def test_reject_refused_once_a_day_of_the_auftrag_is_fixed_r12(
    admin_client: TestClient, db_session: Session, monkeypatch
):
    sent = []
    monkeypatch.setattr(
        "app.admin.review.send_rejection_mail", lambda config, **kw: sent.append(kw)
    )
    campaign, person = _setup(db_session)
    b = _beitrag(admin_client, db_session, person, _slot(db_session, 6))
    _delivered(db_session, campaign, 6)

    page = admin_client.post(f"/admin/aufnahmen/{b.id}/ablehnen", data={"comment": ""}).text

    assert 'class="flash err"' in page
    assert "Türchen 6" in page
    db_session.expire_all()
    assert db_session.get(Beitrag, b.id).rejected_at is None
    assert sent == []


def test_reject_keeps_rejection_when_mail_fails(
    admin_client: TestClient, db_session: Session, monkeypatch
):
    def boom(config, **kw):
        raise OSError("smtp down")

    monkeypatch.setattr("app.admin.review.send_rejection_mail", boom)
    _, person = _setup(db_session)
    b = _beitrag(admin_client, db_session, person, _slot(db_session, 9))

    page = admin_client.post(f"/admin/aufnahmen/{b.id}/ablehnen", data={"comment": ""}).text

    assert 'class="flash err"' in page
    db_session.expire_all()
    assert db_session.get(Beitrag, b.id).rejected_at is not None


def test_rejection_mail_is_plain_text_with_comment_and_link():
    """Textteil bleibt Kern, HTML ist Alternative (Advent-Design)."""
    from app.mail.rejection import build_rejection_message

    message = build_rejection_message(
        to_address="tante@example.test",
        from_address="vorlesezeit@example.test",
        display_name="Tante <b>Ruth</b>",
        title="Der kleine Stern",
        comment="Bitte <script>noch mal</script>",
        login_url="https://example.test/login/confirm?token=abc",
    )
    assert message["To"] == "tante@example.test"
    assert message.get_content_type() == "multipart/alternative"
    body = plain_text(message)
    assert "„Der kleine Stern“" in body
    assert "Türchen" not in body
    assert "Bitte <script>noch mal</script>" in body
    assert "https://example.test/login/confirm?token=abc" in body


def test_unapprove_before_delivery_reopens_slot_ae15(admin_client: TestClient, db_session: Session):
    _, person = _setup(db_session)
    slot = _slot(db_session, 9)
    b = _beitrag(admin_client, db_session, person, slot, approved_at=_now())

    admin_client.post(f"/admin/aufnahmen/{b.id}/zuruecknehmen")

    db_session.expire_all()
    b = db_session.get(Beitrag, b.id)
    assert b.approved_at is None
    assert is_slot_open(db_session, db_session.get(Slot, slot.id))


def test_unapprove_after_delivery_refused_ae15(admin_client: TestClient, db_session: Session):
    campaign, person = _setup(db_session)
    b = _beitrag(admin_client, db_session, person, _slot(db_session, 6), approved_at=_now())
    _delivered(db_session, campaign, 6)

    page = admin_client.post(f"/admin/aufnahmen/{b.id}/zuruecknehmen").text

    assert "Die Vorabend-Auslieferung für Türchen 6 ist bereits gelaufen" in page
    assert "Jetzt auf den Tonie aufspielen" in page
    db_session.expire_all()
    assert db_session.get(Beitrag, b.id).approved_at is not None


def test_unapprove_free_submission_always_allowed(admin_client: TestClient, db_session: Session):
    _, person = _setup(db_session)
    b = _beitrag(admin_client, db_session, person, None, approved_at=_now())

    admin_client.post(f"/admin/aufnahmen/{b.id}/zuruecknehmen")

    db_session.expire_all()
    assert db_session.get(Beitrag, b.id).approved_at is None


# --- Zuschnitt ---------------------------------------------------------------


def test_cut_saved_and_reset_r41(admin_client: TestClient, db_session: Session):
    _, person = _setup(db_session)
    b = _beitrag(admin_client, db_session, person, _slot(db_session, 9))

    admin_client.post(
        f"/admin/aufnahmen/{b.id}/zuschnitt",
        data={"chapter_title": "", "cut_start": "0:00,2", "cut_end": "0.8", "action": "save"},
    )
    db_session.expire_all()
    b = db_session.get(Beitrag, b.id)
    assert b.cut_start_seconds == pytest.approx(0.2)
    assert b.cut_end_seconds == pytest.approx(0.8)
    assert b.chapter_title is None

    admin_client.post(
        f"/admin/aufnahmen/{b.id}/zuschnitt",
        data={"chapter_title": "", "cut_start": "0:00,2", "cut_end": "0.8", "action": "reset"},
    )
    db_session.expire_all()
    b = db_session.get(Beitrag, b.id)
    assert b.cut_start_seconds is None
    assert b.cut_end_seconds is None


@pytest.mark.parametrize(
    ("start", "end"), [("0:05", "0:02"), ("-1", ""), ("abc", ""), ("1:75", "")]
)
def test_invalid_cut_saves_nothing(admin_client: TestClient, db_session: Session, start, end):
    _, person = _setup(db_session)
    b = _beitrag(admin_client, db_session, person, _slot(db_session, 9))

    page = admin_client.post(
        f"/admin/aufnahmen/{b.id}/zuschnitt",
        data={"chapter_title": "Neu", "cut_start": start, "cut_end": end, "action": "save"},
    ).text

    assert 'class="flash err"' in page
    db_session.expire_all()
    b = db_session.get(Beitrag, b.id)
    assert b.cut_start_seconds is None
    assert b.chapter_title is None


# --- Upload (R15) -------------------------------------------------------------


def test_upload_is_normalized_and_not_approved_r15(admin_client: TestClient, db_session: Session):
    _setup(db_session)
    slot = _slot(db_session, 15)
    admin = db_session.execute(select(Person).where(Person.is_admin.is_(True))).scalar_one()

    response = admin_client.post(
        "/admin/aufnahmen/upload",
        data={"slot_id": str(slot.id)},
        files={"file": ("geschichte.mp3", make_tone_mp3(1.5), "audio/mpeg")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.expire_all()
    auftrag_id = db_session.get(Slot, slot.id).auftrag_id
    b = db_session.execute(select(Beitrag).where(Beitrag.auftrag_id == auftrag_id)).scalar_one()
    assert b.approved_at is None
    assert b.person_id == admin.id
    assert b.audio_object_key.endswith(".mp3")
    stored = admin_client.app.state.storage.get(b.audio_object_key)
    assert stored[:3] == b"ID3" or stored[0] == 0xFF


def test_upload_refused_when_slot_has_active_beitrag(admin_client: TestClient, db_session: Session):
    _, person = _setup(db_session)
    slot = _slot(db_session, 9)
    _beitrag(admin_client, db_session, person, slot)

    page = admin_client.post(
        "/admin/aufnahmen/upload",
        data={"slot_id": str(slot.id)},
        files={"file": ("x.mp3", make_tone_mp3(1.0), "audio/mpeg")},
    ).text

    assert 'class="flash err"' in page
    db_session.expire_all()
    auftrag_id = db_session.get(Slot, slot.id).auftrag_id
    rows = db_session.execute(select(Beitrag).where(Beitrag.auftrag_id == auftrag_id)).all()
    assert len(rows) == 1


def test_upload_unreadable_file_refused(admin_client: TestClient, db_session: Session):
    _setup(db_session)
    slot = _slot(db_session, 15)

    page = admin_client.post(
        "/admin/aufnahmen/upload",
        data={"slot_id": str(slot.id)},
        files={"file": ("x.mp3", make_wrong_format_mp3(), "audio/mpeg")},
    ).text

    assert 'class="flash err"' in page
    db_session.expire_all()
    assert db_session.execute(select(Beitrag)).first() is None


# --- Loeschen (R40) -------------------------------------------------------------


def test_delete_confirmation_page(admin_client: TestClient, db_session: Session):
    _, person = _setup(db_session)
    b = _beitrag(admin_client, db_session, person, _slot(db_session, 9))

    page = admin_client.get(f"/admin/aufnahmen/{b.id}/loeschen").text
    assert f'action="/admin/aufnahmen/{b.id}/loeschen"' in page


def test_delete_removes_row_and_object_keeps_delivered_state_r40(
    admin_client: TestClient, db_session: Session, config
):
    campaign, person = _setup(db_session)
    slot = _slot(db_session, 6)
    b = _beitrag(admin_client, db_session, person, slot, approved_at=_now())
    _delivered(db_session, campaign, 6)
    campaign.verified_beitrag_id = b.id
    campaign.verified_chapter_id = "chap-1"
    campaign.verified_for_day = 6
    db_session.commit()
    key, beitrag_id = b.audio_object_key, b.id
    storage = admin_client.app.state.storage

    admin_client.post(f"/admin/aufnahmen/{beitrag_id}/loeschen")

    db_session.expire_all()
    assert db_session.get(Beitrag, beitrag_id) is None
    with pytest.raises(Exception):
        storage.get(key)
    campaign = db_session.get(Campaign, campaign.id)
    assert campaign.verified_beitrag_id is None
    assert campaign.verified_chapter_id is None
    assert campaign.verified_for_day is None
    now = datetime(2026, 10, 1, 12, 0).astimezone()
    state = slot_state(
        db_session,
        db_session.get(Slot, slot.id),
        now=now,
        delivery_time=config.tonie_delivery_time,
    )
    assert state.key == "ausgeliefert"


def test_delete_refused_for_replacement_beitrag(admin_client: TestClient, db_session: Session):
    campaign, person = _setup(db_session)
    b = _beitrag(admin_client, db_session, person, None)
    campaign.replacement_beitrag_id = b.id
    db_session.commit()

    page = admin_client.post(f"/admin/aufnahmen/{b.id}/loeschen").text

    assert 'class="flash err"' in page
    db_session.expire_all()
    assert db_session.get(Beitrag, b.id) is not None


# --- Autorisierung -----------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/admin/aufnahmen"),
        ("post", "/admin/aufnahmen/1/freigeben"),
        ("post", "/admin/aufnahmen/1/ablehnen"),
        ("post", "/admin/aufnahmen/1/zuruecknehmen"),
        ("post", "/admin/aufnahmen/1/zuschnitt"),
        ("get", "/admin/aufnahmen/1/loeschen"),
        ("post", "/admin/aufnahmen/1/loeschen"),
        ("post", "/admin/aufnahmen/upload"),
        ("get", "/admin/aufnahmen/download"),
    ],
)
def test_non_admin_gets_403(person_client: TestClient, method, path):
    response = getattr(person_client, method)(path, follow_redirects=False)
    assert response.status_code == 403


def test_failed_commit_after_admin_upload_leaves_no_file_r40(
    admin_client: TestClient, db_session: Session, monkeypatch
):
    import botocore.exceptions
    from sqlalchemy.orm import Session as OrmSession

    _setup(db_session)
    slot = _slot(db_session, 12)
    storage = admin_client.app.state.storage
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
        admin_client.post(
            "/admin/aufnahmen/upload",
            data={"slot_id": str(slot.id)},
            files={"file": ("a.mp3", make_tone_mp3(1.0), "audio/mpeg")},
        )

    assert len(stored) == 1
    with pytest.raises(botocore.exceptions.ClientError):
        storage.get(stored[0])


def test_detached_recordings_listed_separately_and_deletable_r40(
    admin_client: TestClient, db_session: Session
):
    """R40 nennt nach R35 verwaiste Beitraege ausdruecklich: sie brauchen
    einen Weg zum Loeschen, ohne Freigeben/Ablehnen anzubieten."""
    import botocore.exceptions

    _, person = _setup(db_session)
    detached = _beitrag(
        admin_client, db_session, person, None, title="Früher Versuch", detached_at=_now()
    )

    page = admin_client.get("/admin/aufnahmen").text
    assert "Vom Türchen gelöst" in page
    assert f'href="/admin/aufnahmen/{detached.id}/loeschen"' in page
    assert f'action="/admin/aufnahmen/{detached.id}/freigeben"' not in page

    confirm = admin_client.get(f"/admin/aufnahmen/{detached.id}/loeschen").text
    assert "Vom Türchen gelöst" in confirm

    detached_id, key = detached.id, detached.audio_object_key
    admin_client.post(f"/admin/aufnahmen/{detached_id}/loeschen")
    db_session.expire_all()
    assert db_session.get(Beitrag, detached_id) is None
    with pytest.raises(botocore.exceptions.ClientError):
        admin_client.app.state.storage.get(key)


# --- Mehrkalender U10: Kalender · Tag je Auftrag (R15) -----------------------


def test_rows_name_calendar_and_day_and_lock_after_delivery(
    admin_client: TestClient, db_session: Session
):
    campaign, person = _setup(db_session)
    b = _beitrag(admin_client, db_session, person, _slot(db_session, 6), approved_at=_now())

    page = admin_client.get(f"/admin/aufnahmen?beitrag_id={b.id}").text
    assert '<span class="cal-chip">Familie · Tag 6</span>' in page
    assert "lock-note" not in page

    _delivered(db_session, campaign, 6)
    page = admin_client.get(f"/admin/aufnahmen?beitrag_id={b.id}").text
    assert '<span class="cal-chip done">Familie · Tag 6 · ausgeliefert</span>' in page
    assert "lock-note" in page
    assert re.search(r"<button[^>]*disabled[^>]*>Freigabe zurücknehmen</button>", page)


# --- Herunterladen ------------------------------------------------------------


def _zip(response) -> zipfile.ZipFile:
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]
    return zipfile.ZipFile(io.BytesIO(response.content))


def test_download_all_contains_every_recording_named_by_day_title_person(
    admin_client: TestClient, db_session: Session
):
    _, person = _setup(db_session)
    assigned = create_person(db_session, email="oma@example.test", display_name="Oma Gisela")
    slot = _slot(db_session, 5)
    update_auftrag(db_session, _auftrag(db_session, slot, assigned).id, title="Sterne zählen")
    db_session.commit()
    # Admin-Upload: Urheber ist eine andere Person, im Namen steht die zugewiesene.
    day = _beitrag(admin_client, db_session, person, slot)
    _beitrag(admin_client, db_session, person, _slot(db_session, 12), rejected_at=_now())
    _beitrag(admin_client, db_session, person, None, title="Grüße/aus Lissabon")
    _beitrag(admin_client, db_session, person, None, title="Früher Versuch", detached_at=_now())

    archive = _zip(admin_client.get("/admin/aufnahmen/download"))

    assert sorted(archive.namelist()) == [
        "Eingang - Grüße aus Lissabon - Tante Ruth.mp3",
        "Gelöst - Früher Versuch - Tante Ruth.mp3",
        "Tag 05 - Sterne zählen - Oma Gisela.mp3",
        "Tag 12 - ohne Titel - Tante Ruth.mp3",
    ]
    stored = admin_client.app.state.storage.get(day.audio_object_key)
    assert archive.read("Tag 05 - Sterne zählen - Oma Gisela.mp3") == stored


def test_download_selection_only_selected(admin_client: TestClient, db_session: Session):
    _, person = _setup(db_session)
    a = _beitrag(admin_client, db_session, person, None, title="Eins")
    _beitrag(admin_client, db_session, person, None, title="Zwei")
    c = _beitrag(admin_client, db_session, person, None, title="Drei")

    archive = _zip(admin_client.get(f"/admin/aufnahmen/download?ids={a.id}&ids={c.id}"))

    assert sorted(archive.namelist()) == [
        "Eingang - Drei - Tante Ruth.mp3",
        "Eingang - Eins - Tante Ruth.mp3",
    ]


def test_download_same_names_get_id_suffix(admin_client: TestClient, db_session: Session):
    _, person = _setup(db_session)
    a = _beitrag(admin_client, db_session, person, None, title="Hallo")
    b = _beitrag(admin_client, db_session, person, None, title="Hallo")

    names = sorted(_zip(admin_client.get("/admin/aufnahmen/download")).namelist())

    assert names == [
        f"Eingang - Hallo - Tante Ruth - {a.id}.mp3",
        f"Eingang - Hallo - Tante Ruth - {b.id}.mp3",
    ]


def test_download_prefixes_calendar_with_several_calendars(
    admin_client: TestClient, db_session: Session
):
    _, person = _setup(db_session)
    second = new_campaign(db_session, name="Oma")
    db_session.commit()
    slot = db_session.execute(
        select(Slot).where(Slot.campaign_id != second.id, Slot.day == 3)
    ).scalar_one()
    _beitrag(admin_client, db_session, person, slot)

    names = _zip(admin_client.get("/admin/aufnahmen/download")).namelist()

    assert names == ["Familie Tag 03 - ohne Titel - Tante Ruth.mp3"]


def test_download_unknown_id_404(admin_client: TestClient, db_session: Session):
    _setup(db_session)
    assert admin_client.get("/admin/aufnahmen/download?ids=999").status_code == 404


def test_download_missing_file_listed_in_fehlt(admin_client: TestClient, db_session: Session):
    _, person = _setup(db_session)
    gone = _beitrag(admin_client, db_session, person, None, title="Weg")
    _beitrag(admin_client, db_session, person, None, title="Da")
    admin_client.app.state.storage.delete(gone.audio_object_key)

    archive = _zip(admin_client.get("/admin/aufnahmen/download"))

    assert sorted(archive.namelist()) == ["Eingang - Da - Tante Ruth.mp3", "FEHLT.txt"]
    assert "Eingang - Weg - Tante Ruth.mp3" in archive.read("FEHLT.txt").decode()


def test_list_offers_download_all_and_selection_checkboxes(
    admin_client: TestClient, db_session: Session
):
    _, person = _setup(db_session)
    a = _beitrag(admin_client, db_session, person, _slot(db_session, 4))
    b = _beitrag(admin_client, db_session, person, None, title="Frei")
    c = _beitrag(admin_client, db_session, person, None, title="Weg", detached_at=_now())

    text = admin_client.get("/admin/aufnahmen").text

    assert 'href="/admin/aufnahmen/download"' in text
    assert "3 Aufnahmen · ZIP" in text
    for beitrag in (a, b, c):
        assert f'form="dl-form" name="ids" value="{beitrag.id}"' in text


def test_download_names_drop_windows_reserved_characters(
    admin_client: TestClient, db_session: Session
):
    _, person = _setup(db_session)
    _beitrag(admin_client, db_session, person, None, title='Wer klopft da? "Der *Wolf*" <|>')

    names = _zip(admin_client.get("/admin/aufnahmen/download")).namelist()

    assert names == ["Eingang - Wer klopft da Der Wolf - Tante Ruth.mp3"]


def test_download_auftrag_without_day_is_ohne_tag(admin_client: TestClient, db_session: Session):
    _, person = _setup(db_session)
    auftrag = create_auftrag(db_session, person_id=person.id)
    update_auftrag(db_session, auftrag.id, title="Entwurf")
    db_session.commit()
    key = f"beitraege/{person.id}/{uuid.uuid4().hex}.mp3"
    admin_client.app.state.storage.put(key, make_tone_mp3(1.0), content_type="audio/mpeg")
    db_session.add(Beitrag(person_id=person.id, auftrag=auftrag, audio_object_key=key))
    db_session.commit()

    names = _zip(admin_client.get("/admin/aufnahmen/download")).namelist()

    assert names == ["ohne Tag - Entwurf - Tante Ruth.mp3"]
