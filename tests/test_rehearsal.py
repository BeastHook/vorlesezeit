"""Mehrkalender U13: Auswahl- und Verdrahtungslogik der Generalprobe.

Kein echter Toniecloud-Aufruf, kein S3-Speicher: die Fabrik bekommt einen
MockTransport, `fill` einen Speicher-Ersatz.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from app import settings as st
from app.admin.setup import create_auftrag, create_campaign
from app.models import Beitrag, Campaign, CreativeTonie, Slot
from app.toniecloud.client import API_BASE_URL, TOKEN_URL
from scripts import rehearsal

TONIE_A = "1234ABCDE00304E0"
TONIE_B = "5678FEDCB00304E1"
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=ZoneInfo("Europe/Berlin"))


class MemoryStorage:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.files[key] = data


def _add_tonie(db, tonie_id: str, name: str, **kwargs) -> CreativeTonie:
    tonie = CreativeTonie(tonie_id=tonie_id, name=name, **kwargs)
    db.add(tonie)
    db.commit()
    return tonie


def test_select_tonie_by_index_returns_the_listed_tonie(db_session):
    _add_tonie(db_session, TONIE_A, "Oma")
    second = _add_tonie(db_session, TONIE_B, "Opa")

    assert rehearsal.select_tonie(db_session, 1).id == second.id


def test_unknown_index_aborts_with_readable_message(db_session):
    _add_tonie(db_session, TONIE_A, "Oma")

    with pytest.raises(SystemExit) as exc:
        rehearsal.select_tonie(db_session, 3)

    assert "Index 3" in str(exc.value)
    assert "list-tonies" in str(exc.value)


def test_list_tonies_masks_ids_and_names_calendar(db_session, capsys):
    campaign = create_campaign(db_session)
    campaign.name = "Familie Nord"
    db_session.commit()
    _add_tonie(db_session, TONIE_A, "Oma", campaign_id=campaign.id)
    _add_tonie(db_session, TONIE_B, "Opa")

    rehearsal.main(["list-tonies"])

    out = capsys.readouterr().out
    assert "[0]" in out and "[1]" in out
    assert "••••04E0" in out and "••••04E1" in out
    assert "Oma" in out and "Familie Nord" in out
    assert "kein Kalender" in out
    assert TONIE_A not in out and TONIE_B not in out
    assert TONIE_A[:-4] not in out


def test_client_comes_from_factory_with_konto_credentials_not_config(db_session, config):
    logins: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            logins.append(parse_qs(request.content.decode())["username"][0])
            return httpx.Response(
                200, json={"access_token": "t", "expires_in": 300, "token_type": "Bearer"}
            )
        if str(request.url) == f"{API_BASE_URL}/households":
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unerwarteter Aufruf: {request.url}")

    konto = st.create_konto(
        db_session,
        config.credentials_key,
        username="oma@example.test",
        password="pw-oma",
        now=NOW,
    )
    tonie = _add_tonie(db_session, TONIE_A, "Oma", konto_id=konto.id)

    _, db, _, factory = rehearsal._wiring(transport=httpx.MockTransport(handler))
    client = rehearsal.tonie_client(factory, db, db.get(CreativeTonie, tonie.id))
    client.list_creative_tonies()

    assert logins == ["oma@example.test"]
    assert config.tonie_username not in logins


def test_tonie_without_konto_aborts_before_any_request(db_session):
    tonie = _add_tonie(db_session, TONIE_A, "Oma")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("kein HTTP erwartet")

    _, db, _, factory = rehearsal._wiring(transport=httpx.MockTransport(handler))
    with pytest.raises(SystemExit) as exc:
        rehearsal.tonie_client(factory, db, db.get(CreativeTonie, tonie.id))

    assert "tonies-Konto" in str(exc.value)


def test_setup_refuses_tonie_without_calendar(db_session):
    tonie = _add_tonie(db_session, TONIE_A, "Oma")

    with pytest.raises(SystemExit) as exc:
        rehearsal.calendar_of(db_session, tonie)

    assert "Kalender" in str(exc.value)


def test_fill_creates_beitraege_on_auftraege_of_the_tonie_calendar(db_session):
    campaign = create_campaign(db_session)
    tonie = _add_tonie(db_session, TONIE_A, "Oma", campaign_id=campaign.id)
    storage = MemoryStorage()

    created, replacement_id = rehearsal.fill(db_session, storage, tonie, now=NOW)

    assert created == rehearsal.SLOT_COUNT - 1
    day_beitraege = db_session.scalars(
        select(Beitrag).where(Beitrag.id != replacement_id).order_by(Beitrag.id)
    ).all()
    assert len(day_beitraege) == created
    assert all(b.auftrag_id is not None for b in day_beitraege)
    for beitrag in day_beitraege:
        (slot,) = beitrag.auftrag.slots
        assert slot.campaign_id == campaign.id
        assert beitrag.chapter_title == f"Tag {slot.day}"
        assert beitrag.approved_at is not None
    missing = db_session.scalars(
        select(Slot).where(Slot.campaign_id == campaign.id, Slot.day == rehearsal.MISSING_DAY)
    ).one()
    assert missing.auftrag_id is None
    assert db_session.get(Campaign, campaign.id).replacement_beitrag_id == replacement_id
    assert db_session.get(Beitrag, replacement_id).auftrag_id is None
    assert set(storage.files) >= {b.audio_object_key for b in day_beitraege}


def test_fill_refuses_calendar_that_already_holds_auftraege(db_session):
    campaign = create_campaign(db_session)
    tonie = _add_tonie(db_session, TONIE_A, "Oma", campaign_id=campaign.id)
    slot = db_session.scalars(select(Slot).where(Slot.campaign_id == campaign.id)).first()
    slot.auftrag = create_auftrag(db_session, title="Echte Geschichte")
    db_session.commit()

    with pytest.raises(SystemExit) as exc:
        rehearsal.fill(db_session, MemoryStorage(), tonie, now=NOW)

    assert "belegt" in str(exc.value)


# --- nachholen (Job 040) ------------------------------------------------------


def test_fill_nachholen_leaves_the_missed_day_unapproved(db_session):
    campaign = create_campaign(db_session)
    tonie = _add_tonie(db_session, TONIE_A, "Oma", campaign_id=campaign.id)
    storage = MemoryStorage()

    missed_id = rehearsal.fill_nachholen(db_session, storage, tonie, now=NOW)

    day = rehearsal.NACHHOL_DAY
    by_day = {}
    for beitrag in db_session.scalars(select(Beitrag).where(Beitrag.auftrag_id.is_not(None))):
        (slot,) = beitrag.auftrag.slots
        by_day[slot.day] = beitrag
    assert sorted(by_day) == [day, day + 1, day + 2]
    assert by_day[day].id == missed_id
    assert by_day[day].approved_at is None
    assert all(by_day[d].approved_at is not None for d in (day + 1, day + 2))
    assert [by_day[d].chapter_title for d in sorted(by_day)] == [
        f"Tag {d}" for d in (day, day + 1, day + 2)
    ]
    replacement_id = db_session.get(Campaign, campaign.id).replacement_beitrag_id
    assert db_session.get(Beitrag, replacement_id).approved_at is not None
    assert set(storage.files) >= {b.audio_object_key for b in by_day.values()}


def test_fill_nachholen_stores_audio_that_takes_the_rahmen(db_session):
    """Roh erzeugte Probe-Toene liessen libmp3lame beim Rahmen abbrechen
    (Generalprobe 2026-11-10: jeder Lauf "Ohne Rahmen"); im Betrieb sind
    alle Dateien normalisiert, die Probe-Dateien muessen es also auch sein."""
    from app.delivery.rahmen import mit_rahmen
    from tests.test_delivery_rahmen import RahmenStorage

    campaign = create_campaign(db_session)
    tonie = _add_tonie(db_session, TONIE_A, "Oma", campaign_id=campaign.id)
    storage = RahmenStorage({})

    rehearsal.fill_nachholen(db_session, storage, tonie, now=NOW)

    keys = [k for k in storage.objects if not k.startswith("rahmen/")]
    assert keys
    for key in keys:
        assert mit_rahmen(db_session, storage, campaign, storage.objects[key])


def test_fill_nachholen_refuses_calendar_that_already_holds_auftraege(db_session):
    campaign = create_campaign(db_session)
    tonie = _add_tonie(db_session, TONIE_A, "Oma", campaign_id=campaign.id)
    slot = db_session.scalars(select(Slot).where(Slot.campaign_id == campaign.id)).first()
    slot.auftrag = create_auftrag(db_session, title="Echte Geschichte")
    db_session.commit()

    with pytest.raises(SystemExit) as exc:
        rehearsal.fill_nachholen(db_session, MemoryStorage(), tonie, now=NOW)

    assert "belegt" in str(exc.value)


def test_nachholen_sequence_passes_against_a_cloud_that_behaves(db_session):
    from tests.test_delivery import STOCK, TONIE_ID, make_client
    from tests.test_delivery_rahmen import FakeCloud, RahmenStorage

    st.get_einstellungen(db_session).rahmen_aus = True
    campaign = create_campaign(db_session)
    tonie = _add_tonie(db_session, TONIE_ID, "Oma", campaign_id=campaign.id)
    storage = RahmenStorage({})
    missed_id = rehearsal.fill_nachholen(db_session, storage, tonie, now=NOW)
    cloud = FakeCloud()
    client = make_client(cloud.handler)
    protocol = rehearsal.Protocol()

    rehearsal.nachholen_sequence(db_session, client, storage, tonie, protocol)

    assert protocol.unexplained_failures() == []
    assert [s.name for s in protocol.steps] == [
        "tag-5-ersatz",
        "tag-5-freigabe",
        "tag-6-nachgeholt",
        "tag-7-abgeraeumt",
    ]
    assert db_session.get(Beitrag, missed_id).approved_at is not None
    assert [c["title"] for c in cloud.chapters] == ["Tag 7", *(c["title"] for c in STOCK)]


def test_nachholen_sequence_reports_a_missing_second_chapter(db_session, monkeypatch):
    from tests.test_delivery import TONIE_ID, make_client
    from tests.test_delivery_rahmen import FakeCloud, RahmenStorage

    st.get_einstellungen(db_session).rahmen_aus = True
    campaign = create_campaign(db_session)
    tonie = _add_tonie(db_session, TONIE_ID, "Oma", campaign_id=campaign.id)
    storage = RahmenStorage({})
    rehearsal.fill_nachholen(db_session, storage, tonie, now=NOW)
    client = make_client(FakeCloud().handler)
    protocol = rehearsal.Protocol()
    # Ohne Freigabe nach dem Ersatz-Lauf holt Job 040 nicht nach.
    monkeypatch.setattr(rehearsal, "_approve_missed", lambda db, beitrag: None)

    rehearsal.nachholen_sequence(db_session, client, storage, tonie, protocol)

    failed = [s.name for s in protocol.unexplained_failures()]
    assert "tag-6-nachgeholt" in failed
