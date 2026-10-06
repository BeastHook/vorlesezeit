"""U8/R41: Zuschnitt -- Zeitfeld-Parsing und die Garantie, dass die abgelegte
Datei unveraendert bleibt und erst beim Aufspielen geschnitten wird (AE19).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin.review import format_time, parse_time
from app.admin.setup import create_campaign, create_person
from app.delivery.audio import apply_cut, probe_duration_seconds
from app.models import Beitrag, Slot
from tests.fixtures.audio import make_tone_mp3


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0:02,4", 2.4),
        ("3:36.0", 216.0),
        ("1:05", 65.0),
        ("12", 12.0),
        ("12,5", 12.5),
        (" 0:00,2 ", 0.2),
        ("", None),
        ("   ", None),
    ],
)
def test_parse_time(raw, expected):
    result = parse_time(raw)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["abc", "1:75", "-1", "1:-3", "nan", "inf", "1:2:3"])
def test_parse_time_rejects_invalid(raw):
    with pytest.raises(ValueError):
        parse_time(raw)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(2.4, "0:02,4"), (216.0, "3:36,0"), (0.0, "0:00,0"), (59.96, "1:00,0")],
)
def test_format_time(seconds, expected):
    assert format_time(seconds) == expected


def test_cut_applies_only_at_delivery_stored_file_unchanged_ae19(
    admin_client: TestClient, db_session: Session
):
    create_campaign(db_session)
    person = create_person(db_session, email="tante@example.test", display_name="Tante")
    slot = db_session.execute(select(Slot).where(Slot.day == 9)).scalar_one()
    original = make_tone_mp3(4.0)
    storage = admin_client.app.state.storage
    key = f"beitraege/{person.id}/{uuid.uuid4().hex}.mp3"
    storage.put(key, original, content_type="audio/mpeg")
    b = Beitrag(person_id=person.id, slot_id=slot.id, audio_object_key=key)
    db_session.add(b)
    db_session.commit()

    admin_client.post(
        f"/admin/aufnahmen/{b.id}/zuschnitt",
        data={"chapter_title": "", "cut_start": "0:01,0", "cut_end": "0:03,0", "action": "save"},
    )
    db_session.expire_all()
    b = db_session.get(Beitrag, b.id)
    stored = storage.get(key)
    assert stored == original

    cut = apply_cut(stored, start_seconds=b.cut_start_seconds, end_seconds=b.cut_end_seconds)
    assert probe_duration_seconds(cut) == pytest.approx(2.0, abs=0.15)

    admin_client.post(
        f"/admin/aufnahmen/{b.id}/zuschnitt",
        data={"chapter_title": "", "cut_start": "", "cut_end": "", "action": "reset"},
    )
    db_session.expire_all()
    b = db_session.get(Beitrag, b.id)
    full = apply_cut(
        storage.get(key), start_seconds=b.cut_start_seconds, end_seconds=b.cut_end_seconds
    )
    assert full == original
    assert probe_duration_seconds(full) == pytest.approx(4.0, abs=0.15)
