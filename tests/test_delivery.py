"""U5: Auslieferungsvorgang -- test-first, Fehlerpfade vor dem Erfolgsfall
(Execution note im Plan). Reine Logik-Bausteine zuerst, danach der
Auslieferungsvorgang selbst gegen httpx.MockTransport, wie in U4.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.delivery.audio import apply_cut, probe_duration_seconds
from app.delivery.calendar import advent_day_for
from app.delivery.job import DeliveryOutcome, run_delivery
from app.models import Auftrag, Beitrag, Campaign, CreativeTonie, DeliveryRun, Person, Slot
from app.toniecloud.client import API_BASE_URL, TOKEN_URL, TonieCloudClient
from tests.fixtures.audio import make_tone_mp3 as make_tone
from tests.mailutil import plain_text

TONIE_ID = "1234ABCDE00304E0"
HOUSEHOLD_ID = "11111111-1111-1111-1111-111111111111"
TONIE_URL = f"{API_BASE_URL}/households/{HOUSEHOLD_ID}/creativetonies/{TONIE_ID}"
# Mehrkalender U7: ein zweiter Creative Tonie (gespiegelt oder ohne Kalender).
TONIE_ID_2 = "5678FGHIJ00304E0"

# U16: die Platzpruefung misst die Dauer mit ffprobe -- Fake-Bytes reichen nicht mehr.
AUDIO = make_tone(1.0)

STOCK = [
    {"id": "familie-1", "title": "Familie Eins", "file": "familie-1"},
    {"id": "familie-2", "title": "Familie Zwei", "file": "familie-2"},
]


class FakeStorage:
    """Duck-typed Ersatz fuer ObjectStorage -- run_delivery braucht nur
    `.get(key) -> bytes`. Kein S3-Speicher noetig, um Auslieferungslogik zu
    testen (das deckt tests/test_storage.py bereits separat ab)."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files

    def get(self, key: str) -> bytes:
        return self._files[key]


def token_response(*, access_token: str = "access-token-1") -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": access_token, "expires_in": 300, "token_type": "Bearer"},
    )


def households_response() -> httpx.Response:
    return httpx.Response(
        200, json=[{"id": HOUSEHOLD_ID, "name": "Testhaushalt", "access": "owner"}]
    )


def creativetonies_list_response(tonie_id: str = TONIE_ID) -> httpx.Response:
    return httpx.Response(
        200, json=[{"id": tonie_id, "householdId": HOUSEHOLD_ID, "name": "Test-Tonie"}]
    )


def creative_tonie_response(
    *,
    chapters: list[dict] | None = None,
    transcoding: bool = False,
    transcoding_errors: list[dict] | None = None,
    seconds_present: float = 3.0,
) -> httpx.Response:
    chapters = chapters if chapters is not None else []
    return httpx.Response(
        200,
        json={
            "id": TONIE_ID,
            "householdId": HOUSEHOLD_ID,
            "name": "Test-Tonie",
            "chapters": chapters,
            "transcoding": transcoding,
            "chaptersPresent": len(chapters),
            "secondsPresent": seconds_present,
            "transcodingErrors": transcoding_errors or [],
            "lastUpdate": "2026-12-05T20:00:00+0100",
        },
    )


def config_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "maxChapters": 250,
            "maxSeconds": 5400,
            "maxBytes": 1073741824,
            "accepts": ["mp3", "wav"],
        },
    )


def config_response_with(max_seconds: float) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "maxChapters": 250,
            "maxSeconds": max_seconds,
            "maxBytes": 1073741824,
            "accepts": ["mp3", "wav"],
        },
    )


def file_response(file_id: str, s3_url: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "fileId": file_id,
            "request": {"url": s3_url, "fields": {"key": f"uploads/{file_id}.mp3"}},
        },
    )


def make_client(handler) -> TonieCloudClient:
    return TonieCloudClient(
        "family@example.test", "x", transport=httpx.MockTransport(handler), sleep=lambda s: None
    )


class FixedFactory:
    """Testersatz fuer `TonieCloudFactory` (Dependency `get_toniecloud_factory`):
    jeder Tonie bekommt denselben vorbereiteten Client, ausser `per_tonie`
    nennt fuer seine Toniecloud-ID einen eigenen."""

    def __init__(self, client: TonieCloudClient, per_tonie: dict | None = None) -> None:
        self.client = client
        self.per_tonie = per_tonie or {}

    def for_tonie(self, db, tonie) -> TonieCloudClient:
        return self.per_tonie.get(tonie.tonie_id, self.client)


def make_calendar_tonie(
    db_session: Session,
    *,
    tonie_id: str = TONIE_ID,
    campaign: Campaign | None = None,
    **fields,
) -> tuple[Campaign, CreativeTonie]:
    """Mehrkalender U7: ein Kalender (neu, falls nicht gegeben) mit einem
    Creative Tonie; `fields` landen am Tonie (app_chapters, verified_*)."""
    if campaign is None:
        campaign = Campaign()
        db_session.add(campaign)
        db_session.flush()
    tonie = CreativeTonie(tonie_id=tonie_id, campaign_id=campaign.id, **fields)
    db_session.add(tonie)
    db_session.commit()
    return campaign, tonie


def make_person_slot_beitrag(
    db_session: Session,
    campaign: Campaign,
    *,
    day: int,
    approved: bool = True,
    audio: bytes = b"tone",
) -> tuple[Slot, Beitrag]:
    """Ein Auftrag am Kalendertag `day` mit einer Aufnahme. Die Altspalten
    (Slot.assigned_person_id, Beitrag.slot_id) wie
    app/admin/setup.py::_sync_legacy_columns bis U12."""
    person = Person(email=f"person-{day}@example.test", display_name=f"Person {day}")
    db_session.add(person)
    db_session.flush()
    auftrag = Auftrag(person_id=person.id)
    db_session.add(auftrag)
    db_session.flush()
    slot = Slot(campaign_id=campaign.id, day=day, assigned_person_id=person.id, auftrag=auftrag)
    db_session.add(slot)
    db_session.flush()
    beitrag = Beitrag(
        person_id=person.id,
        slot_id=slot.id,
        auftrag=auftrag,
        title=f"Tuerchen {day} Titel",
        audio_object_key=f"audio/beitrag-{day}.mp3",
        approved_at=datetime(2026, 11, 20) if approved else None,
    )
    db_session.add(beitrag)
    db_session.commit()
    db_session.refresh(slot)
    db_session.refresh(beitrag)
    return slot, beitrag


def build_handler(
    *,
    tonie_gets: list[httpx.Response],
    tonie_patches: list[httpx.Response],
    file_ids: tuple[str, ...] = ("file-a",),
    s3_url: str = "https://s3.example.test/upload",
    config: httpx.Response | None = None,
    tonie_id: str = TONIE_ID,
):
    """Baut einen httpx.MockTransport-Handler fuer run_delivery-Tests.

    GET/PATCH auf den Tonie-Endpunkt ziehen aus je einer eigenen, exakt
    vorgegebenen Antwortfolge (aufgezeichnete Antworten). Ein unerwarteter
    Aufruf (z. B. ein PATCH, wenn keiner erwartet wird) wirft sofort --
    genau das beweist "Tonie bleibt unberuehrt" in den Fehlschlag-Tests.
    """
    import json

    tonie_url = f"{API_BASE_URL}/households/{HOUSEHOLD_ID}/creativetonies/{tonie_id}"
    gets_iter = iter(tonie_gets)
    patches_iter = iter(tonie_patches)
    file_ids_iter = iter(file_ids)
    captured_patch_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url == TOKEN_URL:
            return token_response()
        if request.method == "GET" and url == f"{API_BASE_URL}/households":
            return households_response()
        if (
            request.method == "GET"
            and url == f"{API_BASE_URL}/households/{HOUSEHOLD_ID}/creativetonies"
        ):
            return creativetonies_list_response(tonie_id)
        if request.method == "GET" and url == f"{API_BASE_URL}/config":
            return config or config_response()
        if request.method == "POST" and url == f"{API_BASE_URL}/file":
            return file_response(next(file_ids_iter), s3_url)
        if request.method == "POST" and url == s3_url:
            return httpx.Response(204)
        if request.method == "GET" and url == tonie_url:
            return next(gets_iter)
        if request.method == "PATCH" and url == tonie_url:
            captured_patch_bodies.append(json.loads(request.content))
            return next(patches_iter)
        raise AssertionError(f"Unerwarteter Aufruf: {request.method} {request.url}")

    return handler, captured_patch_bodies


# --- advent_day_for -------------------------------------------------------


def test_advent_day_for_first_of_december_is_day_one():
    assert advent_day_for(date(2026, 12, 1)) == 1


def test_advent_day_for_last_day_is_24():
    assert advent_day_for(date(2026, 12, 24)) == 24


def test_advent_day_for_november_is_none():
    assert advent_day_for(date(2026, 11, 30)) is None


def test_advent_day_for_after_christmas_eve_is_none():
    assert advent_day_for(date(2026, 12, 25)) is None


def test_advent_day_for_different_year_still_works():
    assert advent_day_for(date(2027, 12, 5)) == 5


# --- apply_cut --------------------------------------------------------------


def test_apply_cut_without_markers_returns_data_unchanged():
    data = make_tone(2.0)

    result = apply_cut(data, start_seconds=None, end_seconds=None)

    assert result == data


def test_apply_cut_trims_to_the_given_window():
    data = make_tone(4.0)

    result = apply_cut(data, start_seconds=1.0, end_seconds=3.0)

    duration = probe_duration_seconds(result)
    assert 1.5 <= duration <= 2.5


def test_apply_cut_with_only_start_marker_trims_the_beginning():
    data = make_tone(4.0)

    result = apply_cut(data, start_seconds=1.0, end_seconds=None)

    duration = probe_duration_seconds(result)
    assert 2.5 <= duration <= 3.5


# --- run_delivery ------------------------------------------------------------


def test_run_delivery_vorabend_uploads_and_verifies_success(db_session):
    campaign, tonie = make_calendar_tonie(db_session)
    slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)

    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=[]),  # Rueckfallstand
            creative_tonie_response(transcoding=True),  # Poll 1
            creative_tonie_response(
                transcoding=False,
                chapters=[
                    {"id": "server-id-a", "title": "Tuerchen 5 Titel", "file": "server-id-a"}
                ],
            ),  # Poll 2: fertig
        ],
        tonie_patches=[
            creative_tonie_response(
                transcoding=True,
                chapters=[{"id": "server-id-a", "title": "Tuerchen 5 Titel", "file": "file-a"}],
            ),
        ],
    )
    client = make_client(handler)
    storage = FakeStorage({beitrag.audio_object_key: AUDIO})

    outcome = run_delivery(db_session, client, storage, tonie, run_type="vorabend", target_day=5)

    assert outcome.success is True
    assert outcome.used_replacement is False
    assert outcome.used_beitrag_id == beitrag.id
    assert len(patch_bodies) == 1
    assert patch_bodies[0]["chapters"] == [{"title": "Tuerchen 5 Titel", "file": "file-a"}]

    db_session.refresh(tonie)
    assert tonie.verified_beitrag_id == beitrag.id
    assert tonie.verified_for_day == 5
    assert tonie.verified_chapter_id == "server-id-a"

    runs = db_session.execute(select(DeliveryRun)).scalars().all()
    assert len(runs) == 1
    assert runs[0].outcome == "erfolg"
    assert runs[0].target_day == 5
    # R44 (U8): der Verlauf nennt den betroffenen Inhalt auch bei
    # automatischen Laeufen.
    assert runs[0].beitrag_ids == str(beitrag.id)


def test_run_delivery_second_call_is_idempotent_no_reupload(db_session):
    campaign, tonie = make_calendar_tonie(
        db_session,
        verified_beitrag_id=None,
        verified_chapter_id="server-id-a",
        verified_for_day=5,
    )
    slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)
    tonie.verified_beitrag_id = beitrag.id
    db_session.commit()

    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(
                chapters=[{"id": "server-id-a", "title": "Tuerchen 5 Titel", "file": "server-id-a"}]
            ),
        ],
        tonie_patches=[],
    )
    client = make_client(handler)
    storage = FakeStorage({})  # leer -- ein Upload-Versuch wuerde KeyError werfen

    outcome = run_delivery(db_session, client, storage, tonie, run_type="vorabend", target_day=5)

    assert outcome.success is True
    assert len(patch_bodies) == 0


def test_run_delivery_kontrolllauf_repairs_missing_content(db_session):
    campaign, tonie = make_calendar_tonie(db_session)
    slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)
    tonie.verified_beitrag_id = beitrag.id
    tonie.verified_chapter_id = "server-id-a"
    tonie.verified_for_day = 5
    db_session.commit()

    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=[]),  # Live-Zustand passt nicht mehr -> widerlegt
            creative_tonie_response(transcoding=True),
            creative_tonie_response(
                transcoding=False,
                chapters=[
                    {"id": "server-id-b", "title": "Tuerchen 5 Titel", "file": "server-id-b"}
                ],
            ),
        ],
        tonie_patches=[
            creative_tonie_response(
                transcoding=True,
                chapters=[{"id": "server-id-b", "title": "Tuerchen 5 Titel", "file": "file-a"}],
            ),
        ],
    )
    client = make_client(handler)
    storage = FakeStorage({beitrag.audio_object_key: AUDIO})

    outcome = run_delivery(
        db_session, client, storage, tonie, run_type="kontrolllauf", target_day=5
    )

    assert outcome.success is True
    assert len(patch_bodies) == 1
    db_session.refresh(tonie)
    assert tonie.verified_chapter_id == "server-id-b"


def test_run_delivery_missing_beitrag_uses_replacement(db_session):
    campaign, tonie = make_calendar_tonie(db_session)

    person = Person(email="ersatz@example.test", display_name="Ersatz")
    db_session.add(person)
    db_session.flush()
    rep_beitrag = Beitrag(
        person_id=person.id,
        slot_id=None,
        title="Ersatzgeschichte",
        audio_object_key="audio/ersatz.mp3",
        approved_at=datetime(2026, 11, 1),
    )
    db_session.add(rep_beitrag)
    db_session.flush()
    campaign.replacement_beitrag_id = rep_beitrag.id
    db_session.add(Slot(campaign_id=campaign.id, day=5))  # Slot ohne freigegebenen Beitrag
    db_session.commit()

    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=[]),
            creative_tonie_response(transcoding=True),
            creative_tonie_response(
                transcoding=False,
                chapters=[
                    {"id": "server-id-e", "title": "Ersatzgeschichte", "file": "server-id-e"}
                ],
            ),
        ],
        tonie_patches=[
            creative_tonie_response(
                transcoding=True,
                chapters=[{"id": "server-id-e", "title": "Ersatzgeschichte", "file": "file-a"}],
            ),
        ],
    )
    client = make_client(handler)
    storage = FakeStorage({rep_beitrag.audio_object_key: AUDIO})

    outcome = run_delivery(db_session, client, storage, tonie, run_type="vorabend", target_day=5)

    assert outcome.success is True
    assert outcome.used_replacement is True
    assert outcome.used_beitrag_id == rep_beitrag.id
    assert "5" in (outcome.reason or "")


def test_run_delivery_upload_failure_leaves_tonie_untouched(db_session):
    campaign, tonie = make_calendar_tonie(db_session)
    slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url == TOKEN_URL:
            return token_response()
        if request.method == "GET" and url == f"{API_BASE_URL}/households":
            return households_response()
        if (
            request.method == "GET"
            and url == f"{API_BASE_URL}/households/{HOUSEHOLD_ID}/creativetonies"
        ):
            return creativetonies_list_response()
        if request.method == "GET" and url == TONIE_URL:
            return creative_tonie_response(chapters=[])
        if request.method == "GET" and url == f"{API_BASE_URL}/config":
            return config_response()
        if request.method == "POST" and url == f"{API_BASE_URL}/file":
            return httpx.Response(409)
        raise AssertionError(f"Unerwarteter Aufruf: {request.method} {request.url}")

    client = make_client(handler)
    storage = FakeStorage({beitrag.audio_object_key: AUDIO})

    outcome = run_delivery(db_session, client, storage, tonie, run_type="vorabend", target_day=5)

    assert outcome.success is False
    assert "Nutzungsbedingungen" in (outcome.reason or "")


def test_run_delivery_double_failure_restores_baseline(db_session):
    campaign, tonie = make_calendar_tonie(db_session)
    slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)
    person = Person(email="ersatz2@example.test", display_name="Ersatz2")
    db_session.add(person)
    db_session.flush()
    rep_beitrag = Beitrag(
        person_id=person.id,
        slot_id=None,
        title="Ersatz",
        audio_object_key="audio/ersatz2.mp3",
        approved_at=datetime(2026, 11, 1),
    )
    db_session.add(rep_beitrag)
    db_session.flush()
    campaign.replacement_beitrag_id = rep_beitrag.id
    db_session.commit()

    baseline_chapters = [{"id": "old-id", "title": "Alter Beitrag", "file": "old-blob"}]

    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=baseline_chapters),
            creative_tonie_response(
                transcoding=False, chapters=[], transcoding_errors=[{"reason": "wrongFormat"}]
            ),
            creative_tonie_response(
                transcoding=False, chapters=[], transcoding_errors=[{"reason": "wrongFormat"}]
            ),
        ],
        tonie_patches=[
            creative_tonie_response(transcoding=True, chapters=[]),
            creative_tonie_response(transcoding=True, chapters=[]),
            creative_tonie_response(chapters=baseline_chapters),
        ],
        file_ids=("file-a", "file-b"),
    )
    client = make_client(handler)
    storage = FakeStorage(
        {
            beitrag.audio_object_key: AUDIO,
            rep_beitrag.audio_object_key: AUDIO,
        }
    )

    outcome = run_delivery(db_session, client, storage, tonie, run_type="vorabend", target_day=5)

    assert outcome.success is False
    assert len(patch_bodies) == 3
    assert patch_bodies[-1]["chapters"] == [
        {"id": "old-id", "title": "Alter Beitrag", "file": "old-blob"}
    ]

    runs = db_session.execute(select(DeliveryRun)).scalars().all()
    assert len(runs) == 1
    assert runs[0].outcome == "fehlschlag"
    assert runs[0].reason


def test_run_delivery_trockenlauf_does_not_replace_chapters(db_session):
    campaign, tonie = make_calendar_tonie(db_session)
    slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url == TOKEN_URL:
            return token_response()
        if request.method == "GET" and url == f"{API_BASE_URL}/config":
            return config_response()
        if request.method == "POST" and url == f"{API_BASE_URL}/file":
            return file_response("file-a", "https://s3.example.test/upload")
        if request.method == "POST" and url == "https://s3.example.test/upload":
            return httpx.Response(204)
        raise AssertionError(f"Unerwarteter Aufruf: {request.method} {request.url}")

    client = make_client(handler)
    storage = FakeStorage({beitrag.audio_object_key: AUDIO})

    outcome = run_delivery(db_session, client, storage, tonie, run_type="trockenlauf", target_day=5)

    assert outcome.success is True
    db_session.refresh(tonie)
    assert tonie.verified_beitrag_id is None


def test_run_delivery_manual_multi_chapter_in_one_patch(db_session):
    campaign, tonie = make_calendar_tonie(db_session)
    _, beitrag1 = make_person_slot_beitrag(db_session, campaign, day=5)
    _, beitrag2 = make_person_slot_beitrag(db_session, campaign, day=6)

    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=[]),
            creative_tonie_response(
                transcoding=False,
                chapters=[
                    {"id": "id-1", "title": "Tuerchen 5 Titel", "file": "id-1"},
                    {"id": "id-2", "title": "Tuerchen 6 Titel", "file": "id-2"},
                ],
            ),
        ],
        tonie_patches=[
            creative_tonie_response(
                transcoding=True,
                chapters=[
                    {"id": "id-1", "title": "Tuerchen 5 Titel", "file": "file-1"},
                    {"id": "id-2", "title": "Tuerchen 6 Titel", "file": "file-2"},
                ],
            ),
        ],
        file_ids=("file-1", "file-2"),
    )
    client = make_client(handler)
    storage = FakeStorage(
        {
            beitrag1.audio_object_key: AUDIO,
            beitrag2.audio_object_key: AUDIO,
        }
    )

    outcome = run_delivery(
        db_session,
        client,
        storage,
        tonie,
        run_type="manuell",
        manual_beitrag_ids=[beitrag1.id, beitrag2.id],
    )

    assert outcome.success is True
    assert len(patch_bodies) == 1
    assert patch_bodies[0]["chapters"] == [
        {"title": "Tuerchen 5 Titel", "file": "file-1"},
        {"title": "Tuerchen 6 Titel", "file": "file-2"},
    ]
    db_session.refresh(tonie)
    assert tonie.verified_beitrag_id is None


def test_run_delivery_manual_failure_restores_baseline_without_replacement(db_session):
    campaign, tonie = make_calendar_tonie(db_session)
    _, beitrag1 = make_person_slot_beitrag(db_session, campaign, day=5)
    person = Person(email="ersatz3@example.test", display_name="Ersatz3")
    db_session.add(person)
    db_session.flush()
    rep_beitrag = Beitrag(
        person_id=person.id,
        slot_id=None,
        title="Ersatz",
        audio_object_key="audio/ersatz3.mp3",
        approved_at=datetime(2026, 11, 1),
    )
    db_session.add(rep_beitrag)
    db_session.flush()
    campaign.replacement_beitrag_id = rep_beitrag.id
    db_session.commit()

    baseline_chapters = [{"id": "old-id", "title": "Alter Beitrag", "file": "old-blob"}]
    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=baseline_chapters),
            creative_tonie_response(
                transcoding=False, chapters=[], transcoding_errors=[{"reason": "wrongFormat"}]
            ),
        ],
        tonie_patches=[
            creative_tonie_response(transcoding=True, chapters=[]),
            creative_tonie_response(chapters=baseline_chapters),
        ],
    )
    client = make_client(handler)
    storage = FakeStorage({beitrag1.audio_object_key: AUDIO})

    outcome = run_delivery(
        db_session, client, storage, tonie, run_type="manuell", manual_beitrag_ids=[beitrag1.id]
    )

    assert outcome.success is False
    assert len(patch_bodies) == 2
    assert patch_bodies[-1]["chapters"] == [
        {"id": "old-id", "title": "Alter Beitrag", "file": "old-blob"}
    ]


# --- U16: manueller Mehrkapitel-Lauf behaelt den Bestand ------------------


def test_manual_two_chapters_on_top_with_stock_behind_ae24(db_session):
    campaign, tonie = make_calendar_tonie(
        db_session, app_chapters='[{"id": "app-alt", "seconds": 1.0}]'
    )
    _s1, b1 = make_person_slot_beitrag(db_session, campaign, day=1)
    _s2, b2 = make_person_slot_beitrag(db_session, campaign, day=2)
    n1 = {"id": "app-n1", "title": "Tuerchen 1 Titel", "file": "app-n1"}
    n2 = {"id": "app-n2", "title": "Tuerchen 2 Titel", "file": "app-n2"}

    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(
                chapters=[{"id": "app-alt", "title": "Alt", "file": "app-alt"}, *STOCK]
            ),
            creative_tonie_response(chapters=[n1, n2, *STOCK]),
        ],
        tonie_patches=[creative_tonie_response(transcoding=True, chapters=[n1, n2, *STOCK])],
        file_ids=("file-1", "file-2"),
    )
    outcome = run_delivery(
        db_session,
        make_client(handler),
        FakeStorage({b1.audio_object_key: AUDIO, b2.audio_object_key: AUDIO}),
        tonie,
        run_type="manuell",
        manual_beitrag_ids=[b1.id, b2.id],
    )

    assert outcome.success is True
    assert len(patch_bodies) == 1
    assert [c.get("id") for c in patch_bodies[0]["chapters"]] == [
        None,
        None,
        "familie-1",
        "familie-2",
    ]
    db_session.refresh(tonie)
    assert [c["id"] for c in json.loads(tonie.app_chapters)] == ["app-n1", "app-n2"]
    assert all(c["seconds"] > 0 for c in json.loads(tonie.app_chapters))
    assert tonie.verified_beitrag_id is None


def test_manual_stock_loss_restores_baseline_without_replacement(db_session):
    campaign, tonie = make_calendar_tonie(db_session)
    _s1, b1 = make_person_slot_beitrag(db_session, campaign, day=1)
    n1 = {"id": "app-n1", "title": "Tuerchen 1 Titel", "file": "app-n1"}

    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=STOCK),
            creative_tonie_response(chapters=[n1]),
        ],
        tonie_patches=[
            creative_tonie_response(transcoding=True, chapters=[n1]),
            creative_tonie_response(chapters=STOCK),
        ],
    )
    outcome = run_delivery(
        db_session,
        make_client(handler),
        FakeStorage({b1.audio_object_key: AUDIO}),
        tonie,
        run_type="manuell",
        manual_beitrag_ids=[b1.id],
    )

    assert outcome.success is False
    assert len(patch_bodies) == 2
    assert [c["id"] for c in patch_bodies[1]["chapters"]] == ["familie-1", "familie-2"]
    db_session.refresh(tonie)
    assert tonie.app_chapters is None


def test_manual_without_space_uploads_nothing(db_session):
    campaign, tonie = make_calendar_tonie(db_session)
    _s1, b1 = make_person_slot_beitrag(db_session, campaign, day=1)

    handler, patch_bodies = build_handler(
        tonie_gets=[creative_tonie_response(chapters=STOCK, seconds_present=100.0)],
        tonie_patches=[],
        file_ids=(),
        config=config_response_with(max_seconds=100.5),
    )
    outcome = run_delivery(
        db_session,
        make_client(handler),
        FakeStorage({b1.audio_object_key: AUDIO}),
        tonie,
        run_type="manuell",
        manual_beitrag_ids=[b1.id],
    )

    assert outcome.success is False
    assert "Kein Platz" in outcome.reason
    assert patch_bodies == []


def test_manual_second_upload_failure_uploads_nothing_more_and_does_not_patch(db_session):
    campaign, tonie = make_calendar_tonie(db_session)
    _s1, b1 = make_person_slot_beitrag(db_session, campaign, day=1)
    _s2, b2 = make_person_slot_beitrag(db_session, campaign, day=2)
    _s3, b3 = make_person_slot_beitrag(db_session, campaign, day=3)

    inner, patch_bodies = build_handler(
        tonie_gets=[creative_tonie_response(chapters=STOCK)],
        tonie_patches=[],
        file_ids=("file-1", "file-3"),
    )
    file_posts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and str(request.url) == f"{API_BASE_URL}/file":
            file_posts.append(1)
            if len(file_posts) == 2:
                return httpx.Response(500)
        return inner(request)

    outcome = run_delivery(
        db_session,
        make_client(handler),
        FakeStorage(
            {b1.audio_object_key: AUDIO, b2.audio_object_key: AUDIO, b3.audio_object_key: AUDIO}
        ),
        tonie,
        run_type="manuell",
        manual_beitrag_ids=[b1.id, b2.id, b3.id],
    )

    assert outcome.success is False
    assert "Hochladen fehlgeschlagen" in outcome.reason
    assert len(file_posts) == 2
    assert patch_bodies == []


def test_manual_prepare_failure_uploads_nothing_and_does_not_patch(db_session):
    campaign, tonie = make_calendar_tonie(db_session)
    _s1, b1 = make_person_slot_beitrag(db_session, campaign, day=1)
    _s2, b2 = make_person_slot_beitrag(db_session, campaign, day=2)

    inner, patch_bodies = build_handler(
        tonie_gets=[creative_tonie_response(chapters=STOCK)],
        tonie_patches=[],
        file_ids=(),
    )
    file_posts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and str(request.url) == f"{API_BASE_URL}/file":
            file_posts.append(1)
        return inner(request)

    outcome = run_delivery(
        db_session,
        make_client(handler),
        FakeStorage({b1.audio_object_key: AUDIO}),
        tonie,
        run_type="manuell",
        manual_beitrag_ids=[b1.id, b2.id],
    )

    assert outcome.success is False
    assert "Hochladen fehlgeschlagen" in outcome.reason
    assert file_posts == []
    assert patch_bodies == []


def test_waiting_run_reads_app_chapters_committed_by_another_session(db_session):
    """Review #3: ein synchroner Aufrufer laedt die Kampagne, bevor er auf die
    Sperre wartet. Schreibt ein Hintergrundlauf (eigene Sitzung) in der
    Zwischenzeit neue App-Kapitel, darf der wartende Lauf sie nicht als
    Bestand behandeln."""
    campaign, tonie = make_calendar_tonie(db_session)
    _s1, b1 = make_person_slot_beitrag(db_session, campaign, day=1)
    # Starke Referenz auf den veralteten Stand (Known Pitfall Identity-Map).
    stale = tonie
    assert stale.app_chapters is None

    with Session(bind=db_session.get_bind()) as other:
        other.get(CreativeTonie, tonie.id).app_chapters = '[{"id": "app-neu", "seconds": 1.0}]'
        other.commit()

    n1 = {"id": "app-n1", "title": "Tuerchen 1 Titel", "file": "app-n1"}
    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(
                chapters=[{"id": "app-neu", "title": "Neu", "file": "app-neu"}, *STOCK]
            ),
            creative_tonie_response(chapters=[n1, *STOCK]),
        ],
        tonie_patches=[creative_tonie_response(transcoding=True, chapters=[n1, *STOCK])],
    )
    outcome = run_delivery(
        db_session,
        make_client(handler),
        FakeStorage({b1.audio_object_key: AUDIO}),
        stale,
        run_type="manuell",
        manual_beitrag_ids=[b1.id],
    )

    assert [c.get("id") for c in patch_bodies[0]["chapters"]] == [None, "familie-1", "familie-2"]
    assert outcome.success is True


def test_run_delivery_lock_serializes_concurrent_calls(db_session):
    import threading
    import time as time_module

    campaign, tonie = make_calendar_tonie(db_session)
    slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)

    started = threading.Event()
    release = threading.Event()

    class SlowStorage(FakeStorage):
        def get(self, key: str) -> bytes:
            started.set()
            release.wait(timeout=5)
            return super().get(key)

    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=[]),
            creative_tonie_response(transcoding=True),
            creative_tonie_response(
                transcoding=False,
                chapters=[
                    {"id": "server-id-a", "title": "Tuerchen 5 Titel", "file": "server-id-a"}
                ],
            ),
            creative_tonie_response(
                chapters=[{"id": "server-id-a", "title": "Tuerchen 5 Titel", "file": "server-id-a"}]
            ),
        ],
        tonie_patches=[
            creative_tonie_response(
                transcoding=True,
                chapters=[{"id": "server-id-a", "title": "Tuerchen 5 Titel", "file": "file-a"}],
            ),
        ],
    )
    client = make_client(handler)
    storage = SlowStorage({beitrag.audio_object_key: AUDIO})

    outcomes: list[DeliveryOutcome] = []

    def call():
        outcomes.append(
            run_delivery(db_session, client, storage, tonie, run_type="vorabend", target_day=5)
        )

    t1 = threading.Thread(target=call)
    t1.start()
    assert started.wait(timeout=5)

    t2_done = threading.Event()

    def call_second():
        outcomes.append(
            run_delivery(db_session, client, storage, tonie, run_type="vorabend", target_day=5)
        )
        t2_done.set()

    t2 = threading.Thread(target=call_second)
    t2.start()
    time_module.sleep(0.2)
    assert not t2_done.is_set(), "Zweiter Lauf haette am Lock warten muessen"

    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert len(outcomes) == 2
    assert all(o.success for o in outcomes)
    assert len(patch_bodies) == 1


# --- Abendmeldung --------------------------------------------------------


def test_should_send_report_always_on_failure():
    from app.mail.report import should_send_report

    outcome = DeliveryOutcome(
        success=False,
        run_type="manuell",
        target_day=None,
        used_beitrag_id=None,
        used_replacement=False,
    )
    assert should_send_report(outcome) is True


def test_should_send_report_not_on_eve_success_it_is_in_the_summary():
    """U8/KTD11: der erfolgreiche Vorabend-Lauf steht in der Sammelmeldung
    (tests/test_report.py), keine eigene Einzelmeldung mehr."""
    from app.mail.report import should_send_report

    outcome = DeliveryOutcome(
        success=True, run_type="vorabend", target_day=5, used_beitrag_id=1, used_replacement=False
    )
    assert should_send_report(outcome) is False


def test_should_send_report_not_on_manual_success():
    from app.mail.report import should_send_report

    outcome = DeliveryOutcome(
        success=True, run_type="manuell", target_day=None, used_beitrag_id=1, used_replacement=False
    )
    assert should_send_report(outcome) is False


def test_should_send_report_not_on_dry_run_success():
    from app.mail.report import should_send_report

    outcome = DeliveryOutcome(
        success=True,
        run_type="trockenlauf",
        target_day=5,
        used_beitrag_id=1,
        used_replacement=False,
    )
    assert should_send_report(outcome) is False


def test_build_report_message_masks_title_mismatches():
    from app.mail.report import build_report_message

    outcome = DeliveryOutcome(
        success=True,
        run_type="vorabend",
        target_day=5,
        used_beitrag_id=1,
        used_replacement=False,
        title_mismatches=(("Geheimer Titel", "Anderer Geheimer Titel"),),
    )

    message = build_report_message(
        to_address="admin@example.test", from_address="vorlesezeit@example.test", outcome=outcome
    )

    body = plain_text(message)
    assert "Geheimer Titel" not in body
    assert "Anderer Geheimer Titel" not in body
    assert "Ge" in body  # maskiert, nicht komplett entfernt


def test_build_report_message_names_failure_reason():
    from app.mail.report import build_report_message

    outcome = DeliveryOutcome(
        success=False,
        run_type="kontrolllauf",
        target_day=6,
        used_beitrag_id=None,
        used_replacement=False,
        reason="Hochladen fehlgeschlagen: Zeitueberschreitung",
    )

    message = build_report_message(
        to_address="admin@example.test", from_address="vorlesezeit@example.test", outcome=outcome
    )

    assert "Fehlschlag" in message["Subject"]
    assert "Zeitueberschreitung" in plain_text(message)
    # U9-Testzeile: Ursache *und* betroffener Tag.
    assert "6" in message["Subject"]
    assert "Zieltag: Tuerchen 6" in plain_text(message)


def test_build_report_message_on_replacement_success_names_missing_day_ae10():
    """AE10/R26: auch bei Erfolg eine Meldung; mit Ersatzbeitrag benennt sie
    den fehlenden Tag. U8: als Einzelmeldung nur noch beim Kontrolllauf mit
    Eingriff -- der Vorabend-Fall steht in der Sammelmeldung
    (tests/test_report.py::test_summary_row_names_replacement_and_missing_day_ae10)."""
    from app.mail.report import build_report_message, should_send_report

    outcome = DeliveryOutcome(
        success=True,
        run_type="kontrolllauf",
        target_day=5,
        used_beitrag_id=99,
        used_replacement=True,
        reason="Kein freigegebener Beitrag fuer Tag 5.",
        changed_tonie=True,
    )

    message = build_report_message(
        to_address="admin@example.test", from_address="vorlesezeit@example.test", outcome=outcome
    )

    assert should_send_report(outcome) is True
    assert "Erfolg" in message["Subject"]
    body = plain_text(message)
    assert "Ersatzbeitrag" in body
    assert "Kein freigegebener Beitrag fuer Tag 5." in body


# --- Ausloese-Endpunkte -----------------------------------------------------


def berlin(*args) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime(*args, tzinfo=ZoneInfo("Europe/Berlin"))


def test_determine_due_before_delivery_time_is_not_due():
    from datetime import time as time_of_day

    from app.delivery.trigger import determine_due

    assert determine_due(berlin(2026, 12, 5, 19, 59), time_of_day(20, 0)) is None


def test_determine_due_at_delivery_time_is_vorabend():
    from datetime import time as time_of_day

    from app.delivery.trigger import determine_due

    assert determine_due(berlin(2026, 12, 5, 20, 0), time_of_day(20, 0)).run_type == "vorabend"


def test_determine_due_just_before_control_offset_is_still_vorabend():
    from datetime import time as time_of_day

    from app.delivery.trigger import determine_due

    assert determine_due(berlin(2026, 12, 5, 21, 59), time_of_day(20, 0)).run_type == "vorabend"


def test_determine_due_at_control_offset_is_kontrolllauf():
    from datetime import time as time_of_day

    from app.delivery.trigger import determine_due

    assert determine_due(berlin(2026, 12, 5, 22, 0), time_of_day(20, 0)).run_type == "kontrolllauf"


def test_determine_due_after_window_end_is_not_due():
    from datetime import time as time_of_day

    from app.delivery.trigger import determine_due

    # U10/KTD13: das Fenster endet 2:45 nach der Lieferzeit, hier um 22:45.
    assert determine_due(berlin(2026, 12, 5, 23, 0), time_of_day(20, 0)) is None


def test_determine_due_late_delivery_time_control_window_crosses_midnight():
    from datetime import time as time_of_day

    from app.delivery.trigger import determine_due

    # U10/KTD13 ersetzt das frueher feste Fensterende 23:00: bei 22:30 laeuft
    # der Kontrolllauf ab 00:30 und gehoert zum Vorabend (Zieltag 6, nicht 7).
    assert determine_due(berlin(2026, 12, 5, 23, 0), time_of_day(22, 30)).run_type == "vorabend"
    control = determine_due(berlin(2026, 12, 6, 0, 30), time_of_day(22, 30))
    assert (control.run_type, control.target_day) == ("kontrolllauf", 6)
    assert determine_due(berlin(2026, 12, 6, 1, 15), time_of_day(22, 30)) is None


def test_tomorrows_advent_day_crosses_month_boundary():
    from app.delivery.trigger import tomorrows_advent_day

    assert tomorrows_advent_day(berlin(2026, 11, 30, 20, 0)) == 1


def test_tomorrows_advent_day_after_christmas_eve_is_none():
    from app.delivery.trigger import tomorrows_advent_day

    assert tomorrows_advent_day(berlin(2026, 12, 24, 20, 0)) is None


def test_trigger_route_without_secret_is_rejected(client):
    response = client.post("/delivery/trigger")
    assert response.status_code == 401


def test_trigger_route_with_wrong_secret_is_rejected(client):
    response = client.post("/delivery/trigger", headers={"X-Trigger-Secret": "falsch"})
    assert response.status_code == 401


def test_manual_route_requires_admin(person_client):
    response = person_client.post("/delivery/manual", json={"beitrag_ids": [1]})
    assert response.status_code == 403


def test_dry_run_route_requires_admin(person_client):
    response = person_client.post("/delivery/dry-run")
    assert response.status_code == 403


def test_run_now_route_requires_admin(person_client):
    response = person_client.post("/delivery/run-now")
    assert response.status_code == 403


def test_manual_route_runs_delivery_for_admin(admin_client, db_session, config):
    from app.delivery.trigger import get_storage, get_toniecloud_factory

    campaign, tonie = make_calendar_tonie(db_session)
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)

    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=[]),
            creative_tonie_response(
                transcoding=False,
                chapters=[{"id": "id-1", "title": "Tuerchen 5 Titel", "file": "id-1"}],
            ),
        ],
        tonie_patches=[
            creative_tonie_response(
                transcoding=True,
                chapters=[{"id": "id-1", "title": "Tuerchen 5 Titel", "file": "file-a"}],
            ),
        ],
    )
    fake_client = make_client(handler)
    fake_storage = FakeStorage({beitrag.audio_object_key: AUDIO})

    admin_client.app.dependency_overrides[get_toniecloud_factory] = lambda: FixedFactory(
        fake_client
    )
    admin_client.app.dependency_overrides[get_storage] = lambda: fake_storage
    try:
        response = admin_client.post("/delivery/manual", json={"beitrag_ids": [beitrag.id]})
    finally:
        admin_client.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["outcome"] == "erfolg"
    assert len(patch_bodies) == 1


# --- Review-Fixes U8 ------------------------------------------------------


@pytest.mark.parametrize(
    "unusable",
    [
        {"approved_at": None},
        {"rejected_at": datetime(2026, 11, 21)},
        {"detached_at": datetime(2026, 11, 21)},
    ],
)
def test_run_delivery_never_delivers_unusable_replacement_r11(db_session, unusable):
    """R11: auch der Ersatzbeitrag geht nur freigegeben, nicht abgelehnt und
    nicht geloest auf den Tonie -- sonst kein Upload, benannter Fehlschlag."""
    campaign, tonie = make_calendar_tonie(db_session)
    person = Person(email="ersatz@example.test", display_name="Ersatz")
    db_session.add(person)
    db_session.flush()
    fields = {"approved_at": datetime(2026, 11, 1)} | unusable
    rep_beitrag = Beitrag(
        person_id=person.id,
        slot_id=None,
        title="Ersatzgeschichte",
        audio_object_key="audio/ersatz.mp3",
        **fields,
    )
    db_session.add(rep_beitrag)
    db_session.flush()
    campaign.replacement_beitrag_id = rep_beitrag.id
    db_session.add(Slot(campaign_id=campaign.id, day=5))
    db_session.commit()

    handler, patch_bodies = build_handler(tonie_gets=[], tonie_patches=[])
    storage = FakeStorage({rep_beitrag.audio_object_key: AUDIO})

    outcome = run_delivery(
        db_session, make_client(handler), storage, tonie, run_type="vorabend", target_day=5
    )

    assert outcome.success is False
    assert patch_bodies == []
    assert "Ersatzbeitrag" in (outcome.reason or "")


def _run(db_session, campaign, run_type, target_day, outcome, minute, tonie_id=TONIE_ID):
    db_session.add(
        DeliveryRun(
            campaign_id=campaign.id,
            tonie_id=tonie_id,
            run_type=run_type,
            target_day=target_day,
            started_at=datetime(2026, 12, 4, 19, minute),
            outcome=outcome,
        )
    )
    db_session.commit()


@pytest.mark.parametrize("manual_type", ["manuell", "anstoss"])
def test_kontrolllauf_leaves_manual_run_after_vorabend_alone(db_session, manual_type):
    """Plan: nach einem manuellen Lauf raeumt erst der naechste Vorabend-Lauf
    auf genau ein Kapitel zurueck -- der Kontrolllauf greift nicht ein,
    hinterlaesst aber einen Verlaufseintrag (R44)."""
    campaign, tonie = make_calendar_tonie(db_session)
    make_person_slot_beitrag(db_session, campaign, day=5)
    _run(db_session, campaign, "vorabend", 5, "erfolg", 0)
    _run(db_session, campaign, manual_type, None, "erfolg", 30)

    def no_network(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unerwarteter Aufruf: {request.method} {request.url}")

    outcome = run_delivery(
        db_session,
        make_client(no_network),
        FakeStorage({}),
        tonie,
        run_type="kontrolllauf",
        target_day=5,
    )

    assert outcome.success is True
    assert outcome.reason
    last = db_session.execute(
        select(DeliveryRun).order_by(DeliveryRun.id.desc()).limit(1)
    ).scalar_one()
    assert (last.run_type, last.outcome) == ("kontrolllauf", "uebersprungen")


def test_kontrolllauf_still_repairs_when_manual_run_was_before_vorabend(db_session):
    """Ein manueller Lauf *vor* dem Vorabend zaehlt nicht -- der Kontrolllauf
    prueft wie bisher gegen den Live-Zustand."""
    campaign, tonie = make_calendar_tonie(db_session)
    _slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)
    _run(db_session, campaign, "manuell", None, "erfolg", 0)
    _run(db_session, campaign, "vorabend", 5, "erfolg", 30)

    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=[]),
            creative_tonie_response(transcoding=True),
            creative_tonie_response(
                transcoding=False,
                chapters=[
                    {"id": "server-id-b", "title": "Tuerchen 5 Titel", "file": "server-id-b"}
                ],
            ),
        ],
        tonie_patches=[
            creative_tonie_response(
                transcoding=True,
                chapters=[{"id": "server-id-b", "title": "Tuerchen 5 Titel", "file": "file-a"}],
            ),
        ],
    )
    storage = FakeStorage({beitrag.audio_object_key: AUDIO})

    outcome = run_delivery(
        db_session, make_client(handler), storage, tonie, run_type="kontrolllauf", target_day=5
    )

    assert outcome.success is True
    assert len(patch_bodies) == 1


# --- U10: Eintrag "gestartet" und keine Anmeldung ohne Zieltag ---------------


@pytest.mark.parametrize("target_day", [None, 9])
def test_run_delivery_without_target_or_content_does_not_log_in(db_session, target_day):
    """U10 Schritt 1: ohne Zieltag bzw. ohne auslieferbaren Inhalt entsteht
    keine Toniecloud-Verbindung -- vorher meldete sich der Lauf zuerst an."""
    campaign, tonie = make_calendar_tonie(db_session)

    def no_network(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unerwarteter Aufruf: {request.method} {request.url}")

    outcome = run_delivery(
        db_session,
        make_client(no_network),
        FakeStorage({}),
        tonie,
        run_type="vorabend",
        target_day=target_day,
    )

    assert outcome.success is False
    [run] = db_session.execute(select(DeliveryRun)).scalars().all()
    assert run.outcome == "fehlschlag"


def test_run_delivery_writes_started_entry_before_first_toniecloud_call(db_session):
    """U10 Schritt 4: der Verlaufseintrag "gestartet" steht, bevor die
    Toniecloud den ersten Aufruf sieht, und wird am Ende abgeschlossen
    (kein zweiter Eintrag)."""
    campaign, tonie = make_calendar_tonie(db_session)
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)
    inner, _ = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=[]),
            creative_tonie_response(
                transcoding=False,
                chapters=[{"id": "id-a", "title": "Tuerchen 5 Titel", "file": "id-a"}],
            ),
        ],
        tonie_patches=[
            creative_tonie_response(
                transcoding=True,
                chapters=[{"id": "id-a", "title": "Tuerchen 5 Titel", "file": "file-a"}],
            )
        ],
    )
    seen_at_first_call: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not seen_at_first_call:
            seen_at_first_call.extend(
                db_session.execute(select(DeliveryRun.outcome)).scalars().all()
            )
        return inner(request)

    outcome = run_delivery(
        db_session,
        make_client(handler),
        FakeStorage({beitrag.audio_object_key: AUDIO}),
        tonie,
        run_type="vorabend",
        target_day=5,
    )

    assert outcome.success is True
    assert seen_at_first_call == ["gestartet"]
    [run] = db_session.execute(select(DeliveryRun)).scalars().all()
    assert run.outcome == "erfolg"


def test_run_delivery_probelauf_uploads_replacement_without_replacing_chapters(db_session):
    """R48: Probelauf = Trockenlauf-Pfad mit dem Ersatzbeitrag, ohne Zieltag."""
    campaign, tonie = make_calendar_tonie(db_session)
    person = Person(email="probe@example.test", display_name="Probe")
    db_session.add(person)
    db_session.flush()
    rep_beitrag = Beitrag(
        person_id=person.id,
        title="Ersatz",
        audio_object_key="audio/probe.mp3",
        approved_at=datetime(2026, 10, 1),
    )
    db_session.add(rep_beitrag)
    db_session.flush()
    campaign.replacement_beitrag_id = rep_beitrag.id
    db_session.commit()
    handler, patch_bodies = build_handler(tonie_gets=[], tonie_patches=[])

    outcome = run_delivery(
        db_session,
        make_client(handler),
        FakeStorage({rep_beitrag.audio_object_key: AUDIO}),
        tonie,
        run_type="probelauf",
    )

    assert outcome.success is True
    assert outcome.used_beitrag_id == rep_beitrag.id
    assert "NICHT geprueft" in (outcome.reason or "")
    assert patch_bodies == []


# --- U11: Lueckenlose Meldungen (KTD18) -------------------------------------


def test_apply_cut_aborts_after_ffmpeg_timeout(monkeypatch):
    """Ein haengendes ffmpeg haelt den Lauf nicht fest (U11 Schritt 3)."""
    import subprocess

    monkeypatch.setattr("app.delivery.audio.FFMPEG_TIMEOUT_SECONDS", 0.001)
    with pytest.raises(subprocess.TimeoutExpired):
        apply_cut(make_tone(4.0), start_seconds=1.0, end_seconds=3.0)


@pytest.fixture
def sent_mails(monkeypatch) -> list:
    from tests.test_trigger import FakeSMTP

    FakeSMTP.sent = []
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    return FakeSMTP.sent


def test_manual_route_without_tonies_konto_fails_like_toniecloud_error(
    admin_client, db_session, sent_mails
):
    """U6 Rueckfallregel ueber die echte Fabrik: Tonie ohne tonies-Konto ->
    Fehlschlag mit Grund im Verlauf, Meldung, kein Anmeldeversuch (die echte
    Fabrik haette sonst ins Netz gegriffen)."""
    from app.delivery.trigger import get_storage

    campaign, _tonie = make_calendar_tonie(db_session, tonie_id="TONIE-OHNE-KONTO")
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)
    admin_client.app.dependency_overrides[get_storage] = lambda: FakeStorage(
        {beitrag.audio_object_key: AUDIO}
    )
    try:
        response = admin_client.post("/delivery/manual", json={"beitrag_ids": [beitrag.id]})
    finally:
        admin_client.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["outcome"] == "fehlschlag"
    assert "kein tonies-Konto hinterlegt" in response.json()["reason"]
    run = db_session.execute(select(DeliveryRun)).scalar_one()
    assert (run.run_type, run.outcome) == ("manuell", "fehlschlag")
    assert len(sent_mails) == 1


def test_manual_route_with_vanished_beitrag_fails_with_reason_instead_of_500(
    admin_client, db_session, config, sent_mails
):
    from app.delivery.trigger import get_storage, get_toniecloud_factory

    campaign, tonie = make_calendar_tonie(db_session)
    handler, patch_bodies = build_handler(tonie_gets=[], tonie_patches=[])
    fake_client = make_client(handler)
    admin_client.app.dependency_overrides[get_toniecloud_factory] = lambda: FixedFactory(
        fake_client
    )
    admin_client.app.dependency_overrides[get_storage] = lambda: FakeStorage({})
    try:
        response = admin_client.post("/delivery/manual", json={"beitrag_ids": [4711]})
    finally:
        admin_client.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["outcome"] == "fehlschlag"
    assert "4711" in response.json()["reason"]
    run = db_session.execute(select(DeliveryRun)).scalar_one()
    assert (run.run_type, run.outcome) == ("manuell", "fehlschlag")
    assert patch_bodies == []
    assert len(sent_mails) == 1  # R18: jeder Fehlschlag meldet sich


def test_run_now_route_runs_as_anstoss_and_reports_failure(
    admin_client, db_session, config, sent_mails
):
    """U11 Schritt 2: /delivery/run-now ist ein Anstoss, kein Vorabend-Lauf,
    und folgt derselben Melderegel wie jeder andere Ausloeseweg."""
    from app.delivery.trigger import get_storage, get_toniecloud_factory

    campaign, tonie = make_calendar_tonie(db_session)
    handler, _ = build_handler(tonie_gets=[], tonie_patches=[])
    fake_client = make_client(handler)
    admin_client.app.dependency_overrides[get_toniecloud_factory] = lambda: FixedFactory(
        fake_client
    )
    admin_client.app.dependency_overrides[get_storage] = lambda: FakeStorage({})
    try:
        # Ohne Beitrag scheitert der Lauf an jedem Datum: kein Zieltag
        # ausserhalb des Advents, sonst kein freigegebener Beitrag.
        response = admin_client.post("/delivery/run-now")
    finally:
        admin_client.app.dependency_overrides.clear()

    assert response.json()["outcome"] == "fehlschlag"
    run = db_session.execute(select(DeliveryRun)).scalar_one()
    assert run.run_type == "anstoss"
    assert len(sent_mails) == 1


# --- U16: Bestand auf dem Tonie bleibt hinter dem App-Kapitel (KTD20, R49) ---------


def _replacement(db_session: Session, campaign: Campaign) -> Beitrag:
    person = Person(email="ersatz@example.test", display_name="Ersatz")
    db_session.add(person)
    db_session.flush()
    rep = Beitrag(
        person_id=person.id,
        title="Ersatz",
        audio_object_key="audio/ersatz.mp3",
        approved_at=datetime(2026, 11, 1),
    )
    db_session.add(rep)
    db_session.flush()
    campaign.replacement_beitrag_id = rep.id
    db_session.commit()
    return rep


def test_vorabend_keeps_stock_behind_new_chapter_and_removes_old_app_chapter_ae7(db_session):
    campaign, tonie = make_calendar_tonie(
        db_session,
        app_chapters='[{"id": "app-alt", "seconds": 1.0}]',
    )
    _slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)
    new = {"id": "app-neu", "title": "Tuerchen 5 Titel", "file": "app-neu"}

    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(
                chapters=[{"id": "app-alt", "title": "Alt", "file": "app-alt"}, *STOCK]
            ),
            creative_tonie_response(transcoding=False, chapters=[new, *STOCK]),
        ],
        tonie_patches=[
            creative_tonie_response(transcoding=True, chapters=[{**new, "file": "file-a"}, *STOCK]),
        ],
    )
    outcome = run_delivery(
        db_session,
        make_client(handler),
        FakeStorage({beitrag.audio_object_key: AUDIO}),
        tonie,
        run_type="vorabend",
        target_day=5,
    )

    assert outcome.success is True
    assert patch_bodies[0]["chapters"] == [
        {"title": "Tuerchen 5 Titel", "file": "file-a"},
        {"id": "familie-1", "title": "Familie Eins", "file": "familie-1"},
        {"id": "familie-2", "title": "Familie Zwei", "file": "familie-2"},
    ]
    db_session.refresh(tonie)
    assert tonie.verified_chapter_id == "app-neu"
    stored = json.loads(tonie.app_chapters)
    assert [c["id"] for c in stored] == ["app-neu"]
    assert 0.5 <= stored[0]["seconds"] <= 1.5


def test_family_chapter_before_app_chapter_stays_right_behind_new_one_ae27(db_session):
    campaign, tonie = make_calendar_tonie(
        db_session, app_chapters='[{"id": "app-alt", "seconds": 1.0}]'
    )
    _slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=6)
    oben = {"id": "familie-oben", "title": "Von Hand oben", "file": "familie-oben"}
    new = {"id": "app-neu", "title": "Tuerchen 6 Titel", "file": "app-neu"}

    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(
                chapters=[oben, {"id": "app-alt", "title": "Alt", "file": "app-alt"}, *STOCK]
            ),
            creative_tonie_response(chapters=[new, oben, *STOCK]),
        ],
        tonie_patches=[
            creative_tonie_response(
                transcoding=True, chapters=[{**new, "file": "file-a"}, oben, *STOCK]
            )
        ],
    )
    outcome = run_delivery(
        db_session,
        make_client(handler),
        FakeStorage({beitrag.audio_object_key: AUDIO}),
        tonie,
        run_type="vorabend",
        target_day=6,
    )

    assert outcome.success is True
    assert [c.get("id") for c in patch_bodies[0]["chapters"]] == [
        None,
        "familie-oben",
        "familie-1",
        "familie-2",
    ]


def test_second_run_same_day_is_idempotent_with_stock_behind(db_session):
    campaign, tonie = make_calendar_tonie(
        db_session, verified_chapter_id="app-a", verified_for_day=5
    )
    _slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)
    tonie.verified_beitrag_id = beitrag.id
    tonie.app_chapters = '[{"id": "app-a", "seconds": 1.0}]'
    db_session.commit()

    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(
                chapters=[{"id": "app-a", "title": "T", "file": "app-a"}, *STOCK]
            )
        ],
        tonie_patches=[],
    )
    outcome = run_delivery(
        db_session,
        make_client(handler),
        FakeStorage({}),
        tonie,
        run_type="vorabend",
        target_day=5,
    )

    assert outcome.success is True
    assert patch_bodies == []


def test_kontrolllauf_with_stock_and_verified_chapter_present_does_nothing(db_session):
    """Kontrolllauf nach einem verifizierten Vorabend: das App-Kapitel liegt
    an Platz 1, Bestand dahinter -- kein Upload, kein PATCH."""
    campaign, tonie = make_calendar_tonie(
        db_session, verified_chapter_id="app-a", verified_for_day=5
    )
    _slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)
    tonie.verified_beitrag_id = beitrag.id
    tonie.app_chapters = '[{"id": "app-a", "seconds": 1.0}]'
    db_session.commit()

    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(
                chapters=[{"id": "app-a", "title": "T", "file": "app-a"}, *STOCK]
            )
        ],
        tonie_patches=[],
    )
    outcome = run_delivery(
        db_session,
        make_client(handler),
        FakeStorage({}),
        tonie,
        run_type="kontrolllauf",
        target_day=5,
    )

    assert outcome.success is True
    assert patch_bodies == []


def test_kontrolllauf_repairs_lost_app_chapter_and_keeps_stock(db_session):
    """Die Familie hat das App-Kapitel geloescht (Review Focus 2): der
    Kontrolllauf spielt es neu an Platz 1, der Bestand bleibt dahinter."""
    campaign, tonie = make_calendar_tonie(
        db_session, verified_chapter_id="app-a", verified_for_day=5
    )
    _slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)
    tonie.verified_beitrag_id = beitrag.id
    tonie.app_chapters = '[{"id": "app-a", "seconds": 1.0}]'
    db_session.commit()
    new = {"id": "app-b", "title": "Tuerchen 5 Titel", "file": "app-b"}

    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=STOCK),
            creative_tonie_response(chapters=[new, *STOCK]),
        ],
        tonie_patches=[
            creative_tonie_response(transcoding=True, chapters=[{**new, "file": "file-a"}, *STOCK])
        ],
    )
    outcome = run_delivery(
        db_session,
        make_client(handler),
        FakeStorage({beitrag.audio_object_key: AUDIO}),
        tonie,
        run_type="kontrolllauf",
        target_day=5,
    )

    assert outcome.success is True
    assert [c.get("id") for c in patch_bodies[0]["chapters"]] == [None, "familie-1", "familie-2"]
    db_session.refresh(tonie)
    assert [c["id"] for c in json.loads(tonie.app_chapters)] == ["app-b"]


def test_no_space_leaves_tonie_untouched_and_names_shortfall_ae28(db_session):
    campaign, tonie = make_calendar_tonie(db_session)
    _slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)

    handler, patch_bodies = build_handler(
        tonie_gets=[creative_tonie_response(chapters=STOCK, seconds_present=100.0)],
        tonie_patches=[],
        file_ids=(),  # kein Hochladen erwartet: ein POST /file wuerde StopIteration werfen
        config=config_response_with(max_seconds=100.5),
    )
    outcome = run_delivery(
        db_session,
        make_client(handler),
        FakeStorage({beitrag.audio_object_key: AUDIO}),
        tonie,
        run_type="vorabend",
        target_day=5,
    )

    assert outcome.success is False
    assert "Kein Platz" in outcome.reason
    assert patch_bodies == []


def test_too_long_replacement_also_not_uploaded_review_focus_4(db_session):
    campaign, tonie = make_calendar_tonie(db_session)
    rep = _replacement(db_session, campaign)
    _slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)

    handler, patch_bodies = build_handler(
        tonie_gets=[creative_tonie_response(chapters=STOCK, seconds_present=100.0)],
        tonie_patches=[],
        file_ids=(),
        config=config_response_with(max_seconds=100.5),
    )
    outcome = run_delivery(
        db_session,
        make_client(handler),
        FakeStorage({beitrag.audio_object_key: AUDIO, rep.audio_object_key: AUDIO}),
        tonie,
        run_type="vorabend",
        target_day=5,
    )

    assert outcome.success is False
    assert outcome.reason.count("Kein Platz") == 2
    assert "Tagesbeitrag" in outcome.reason and "Ersatzbeitrag" in outcome.reason
    assert patch_bodies == []


def test_stock_loss_on_day_story_falls_back_to_replacement_with_stock(db_session):
    campaign, tonie = make_calendar_tonie(db_session)
    rep = _replacement(db_session, campaign)
    _slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)
    tag = {"id": "app-tag", "title": "Tuerchen 5 Titel", "file": "app-tag"}
    ersatz = {"id": "app-ersatz", "title": "Ersatz", "file": "app-ersatz"}

    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=STOCK),
            creative_tonie_response(chapters=[tag, STOCK[1]]),  # familie-1 verloren
            creative_tonie_response(chapters=[ersatz, *STOCK]),
        ],
        tonie_patches=[
            creative_tonie_response(transcoding=True, chapters=[tag, STOCK[1]]),
            creative_tonie_response(transcoding=True, chapters=[ersatz, *STOCK]),
        ],
        file_ids=("file-a", "file-b"),
    )
    outcome = run_delivery(
        db_session,
        make_client(handler),
        FakeStorage({beitrag.audio_object_key: AUDIO, rep.audio_object_key: AUDIO}),
        tonie,
        run_type="vorabend",
        target_day=5,
    )

    assert outcome.success is True
    assert outcome.used_replacement is True
    assert [c.get("id") for c in patch_bodies[1]["chapters"]] == [None, "familie-1", "familie-2"]
    db_session.refresh(tonie)
    assert [c["id"] for c in json.loads(tonie.app_chapters)] == ["app-ersatz"]


def test_stock_loss_detected_even_when_id_sequence_matches(db_session):
    """Ein Bestandskapitel verschwindet zwischen PATCH und Abschluss nicht,
    aber der Dienst setzt es an eine andere Stelle: die id-Folge von PATCH
    und Abschluss stimmt ueberein, der Bestand steht trotzdem nicht in
    derselben Reihenfolge dahinter -- Fehlschlag, Rueckfallstand zurueck."""
    campaign, tonie = make_calendar_tonie(db_session)
    _slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)
    tag = {"id": "app-tag", "title": "Tuerchen 5 Titel", "file": "app-tag"}
    swapped = [tag, STOCK[1], STOCK[0]]

    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=STOCK),
            creative_tonie_response(chapters=swapped),
        ],
        tonie_patches=[
            creative_tonie_response(transcoding=True, chapters=swapped),
            creative_tonie_response(chapters=STOCK),  # Rueckspielen
        ],
    )
    outcome = run_delivery(
        db_session,
        make_client(handler),
        FakeStorage({beitrag.audio_object_key: AUDIO}),
        tonie,
        run_type="vorabend",
        target_day=5,
    )

    assert outcome.success is False
    assert "Bestand nicht vollständig erhalten" in outcome.reason
    assert [c["id"] for c in patch_bodies[-1]["chapters"]] == ["familie-1", "familie-2"]


def test_double_failure_restores_full_baseline_including_stock(db_session):
    campaign, tonie = make_calendar_tonie(
        db_session, app_chapters='[{"id": "app-alt", "seconds": 1.0}]'
    )
    _slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)
    alt = {"id": "app-alt", "title": "Alt", "file": "app-alt"}
    tag = {"id": "app-tag", "title": "Tuerchen 5 Titel", "file": "app-tag"}

    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=[alt, *STOCK]),
            creative_tonie_response(chapters=[tag]),  # Bestand verloren
        ],
        tonie_patches=[
            creative_tonie_response(transcoding=True, chapters=[tag]),
            creative_tonie_response(chapters=[alt, *STOCK]),  # Rueckspielen
        ],
    )
    outcome = run_delivery(
        db_session,
        make_client(handler),
        FakeStorage({beitrag.audio_object_key: AUDIO}),
        tonie,
        run_type="vorabend",
        target_day=5,
    )

    assert outcome.success is False
    assert [c["id"] for c in patch_bodies[-1]["chapters"]] == ["app-alt", "familie-1", "familie-2"]
    db_session.refresh(tonie)
    assert json.loads(tonie.app_chapters)[0]["id"] == "app-alt"


def test_replacement_from_yesterday_is_swapped_like_a_day_story(db_session):
    campaign, tonie = make_calendar_tonie(
        db_session, app_chapters='[{"id": "app-ersatz", "seconds": 1.0}]'
    )
    _slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=6)
    new = {"id": "app-6", "title": "Tuerchen 6 Titel", "file": "app-6"}

    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(
                chapters=[{"id": "app-ersatz", "title": "Ersatz", "file": "app-ersatz"}, *STOCK]
            ),
            creative_tonie_response(chapters=[new, *STOCK]),
        ],
        tonie_patches=[creative_tonie_response(transcoding=True, chapters=[new, *STOCK])],
    )
    outcome = run_delivery(
        db_session,
        make_client(handler),
        FakeStorage({beitrag.audio_object_key: AUDIO}),
        tonie,
        run_type="vorabend",
        target_day=6,
    )

    assert outcome.success is True
    assert [c.get("id") for c in patch_bodies[0]["chapters"]] == [None, "familie-1", "familie-2"]


# --- Aufraeumlauf am 25.12. (U16, R51) -------------------------------------

APP24 = {"id": "app-24", "title": "Tag 24", "file": "app-24"}


def test_cleanup_removes_app_chapter_and_keeps_stock_ae29(db_session):
    campaign, tonie = make_calendar_tonie(
        db_session,
        app_chapters='[{"id": "app-24", "seconds": 1.0}]',
        verified_chapter_id="app-24",
        verified_for_day=24,
    )

    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=[APP24, *STOCK]),
            creative_tonie_response(chapters=STOCK),
        ],
        tonie_patches=[creative_tonie_response(chapters=STOCK)],
        file_ids=(),
    )
    outcome = run_delivery(
        db_session, make_client(handler), FakeStorage({}), tonie, run_type="aufraeumen"
    )

    assert outcome.success is True
    assert [c["id"] for c in patch_bodies[0]["chapters"]] == ["familie-1", "familie-2"]
    db_session.refresh(tonie)
    assert tonie.app_chapters is None
    assert tonie.verified_chapter_id is None
    assert tonie.verified_for_day is None
    entry = db_session.execute(select(DeliveryRun)).scalar_one()
    assert (entry.run_type, entry.target_day, entry.outcome) == ("aufraeumen", None, "erfolg")


def test_cleanup_without_app_chapter_on_tonie_does_nothing_review_focus_5(db_session):
    campaign, tonie = make_calendar_tonie(
        db_session, app_chapters='[{"id": "app-24", "seconds": 1.0}]'
    )

    handler, patch_bodies = build_handler(
        tonie_gets=[creative_tonie_response(chapters=STOCK)], tonie_patches=[], file_ids=()
    )
    outcome = run_delivery(
        db_session, make_client(handler), FakeStorage({}), tonie, run_type="aufraeumen"
    )

    assert outcome.success is True
    assert patch_bodies == []
    db_session.refresh(tonie)
    assert tonie.app_chapters is None


def test_cleanup_stock_loss_restores_baseline(db_session):
    campaign, tonie = make_calendar_tonie(
        db_session, app_chapters='[{"id": "app-24", "seconds": 1.0}]'
    )

    handler, patch_bodies = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=[APP24, *STOCK]),
            creative_tonie_response(chapters=STOCK[:1]),
        ],
        tonie_patches=[
            creative_tonie_response(chapters=STOCK[:1]),
            creative_tonie_response(chapters=[APP24, *STOCK]),
        ],
        file_ids=(),
    )
    outcome = run_delivery(
        db_session, make_client(handler), FakeStorage({}), tonie, run_type="aufraeumen"
    )

    assert outcome.success is False
    assert [c["id"] for c in patch_bodies[1]["chapters"]] == ["app-24", "familie-1", "familie-2"]
    db_session.refresh(tonie)
    # Das App-Kapitel liegt wieder auf dem Tonie und bleibt gemerkt.
    assert tonie.app_chapters == '[{"id": "app-24", "seconds": 1.0}]'


def test_cleanup_patch_error_restores_baseline_and_keeps_app_chapters(db_session):
    campaign, tonie = make_calendar_tonie(
        db_session, app_chapters='[{"id": "app-24", "seconds": 1.0}]'
    )

    handler, patch_bodies = build_handler(
        tonie_gets=[creative_tonie_response(chapters=[APP24, *STOCK])],
        tonie_patches=[
            httpx.Response(500, json={"error": "kaputt"}),
            creative_tonie_response(chapters=[APP24, *STOCK]),
        ],
        file_ids=(),
    )
    outcome = run_delivery(
        db_session, make_client(handler), FakeStorage({}), tonie, run_type="aufraeumen"
    )

    assert outcome.success is False
    assert "Aufraeumen fehlgeschlagen" in outcome.reason
    assert [c["id"] for c in patch_bodies[1]["chapters"]] == ["app-24", "familie-1", "familie-2"]
    db_session.refresh(tonie)
    assert tonie.app_chapters == '[{"id": "app-24", "seconds": 1.0}]'


def test_cleanup_read_error_before_patch_fails_without_touching_tonie(db_session):
    campaign, tonie = make_calendar_tonie(
        db_session, app_chapters='[{"id": "app-24", "seconds": 1.0}]'
    )

    handler, patch_bodies = build_handler(
        tonie_gets=[httpx.Response(503, json={"error": "weg"})], tonie_patches=[], file_ids=()
    )
    outcome = run_delivery(
        db_session, make_client(handler), FakeStorage({}), tonie, run_type="aufraeumen"
    )

    assert outcome.success is False
    assert patch_bodies == []
    db_session.refresh(tonie)
    assert tonie.app_chapters == '[{"id": "app-24", "seconds": 1.0}]'
    entry = db_session.execute(select(DeliveryRun)).scalar_one()
    assert (entry.run_type, entry.outcome) == ("aufraeumen", "fehlschlag")


def test_should_send_report_not_on_cleanup_success_it_is_in_the_summary():
    """U8/KTD11: das Aufraeumen am 25.12. meldet sich ueber die Sammelmeldung
    (tests/test_trigger.py, Aufraeumlauf-Test pruefts am Betreff)."""
    from app.mail.report import should_send_report

    outcome = DeliveryOutcome(
        success=True,
        run_type="aufraeumen",
        target_day=None,
        used_beitrag_id=None,
        used_replacement=False,
    )
    assert should_send_report(outcome) is False


def test_cleanup_report_and_history_labels_have_umlauts():
    from app.admin.deliveries import RUN_LABELS
    from app.mail.report import RUN_TYPE_LABELS

    assert RUN_TYPE_LABELS["aufraeumen"] == "Aufräumen nach dem Advent"
    assert RUN_LABELS["aufraeumen"] == "Aufräumen"


# --- Mehrkalender U7: Auslieferung je Tonie (R2-R6, R37-R39, KTD3/KTD10) -------------


def _day_run_handler(stock: list[dict], new_id: str, *, title: str, tonie_id: str = TONIE_ID):
    """Erfolgreicher Tageslauf: neues Kapitel an Platz 1, `stock` dahinter."""
    new = {"id": new_id, "title": title, "file": new_id}
    return build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=stock),
            creative_tonie_response(chapters=[new, *stock]),
        ],
        tonie_patches=[creative_tonie_response(transcoding=True, chapters=[new, *stock])],
        tonie_id=tonie_id,
    )


def test_two_tonies_of_a_calendar_get_same_app_chapter_and_keep_own_stock_ae2(db_session):
    campaign, tonie1 = make_calendar_tonie(db_session)
    _, tonie2 = make_calendar_tonie(db_session, tonie_id=TONIE_ID_2, campaign=campaign)
    _slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)
    other_stock = [{"id": "oma-1", "title": "Omas Lied", "file": "oma-1"}]
    h1, patches1 = _day_run_handler(STOCK, "app-t1", title="Tuerchen 5 Titel")
    h2, patches2 = _day_run_handler(
        other_stock, "app-t2", title="Tuerchen 5 Titel", tonie_id=TONIE_ID_2
    )
    storage = FakeStorage({beitrag.audio_object_key: AUDIO})

    out1 = run_delivery(
        db_session, make_client(h1), storage, tonie1, run_type="vorabend", target_day=5
    )
    out2 = run_delivery(
        db_session, make_client(h2), storage, tonie2, run_type="vorabend", target_day=5
    )

    assert out1.success and out2.success
    assert out1.used_beitrag_id == out2.used_beitrag_id == beitrag.id
    assert patches1[0]["chapters"][0]["title"] == patches2[0]["chapters"][0]["title"]
    assert [c.get("id") for c in patches1[0]["chapters"]] == [None, "familie-1", "familie-2"]
    assert [c.get("id") for c in patches2[0]["chapters"]] == [None, "oma-1"]
    db_session.refresh(tonie1)
    db_session.refresh(tonie2)
    assert (tonie1.verified_chapter_id, tonie2.verified_chapter_id) == ("app-t1", "app-t2")
    assert '"app-t1"' in tonie1.app_chapters and '"app-t2"' in tonie2.app_chapters
    runs = db_session.execute(select(DeliveryRun).order_by(DeliveryRun.id)).scalars().all()
    assert [(r.tonie_id, r.outcome) for r in runs] == [(TONIE_ID, "erfolg"), (TONIE_ID_2, "erfolg")]


def test_auftrag_in_two_calendars_delivers_same_beitrag_on_each_day(db_session):
    """Auftrag in A am 5. und in B am 12.: der Lauf fuer B am Vorabend des 12.
    liefert dieselbe Aufnahme (R8), ohne neue Aufnahme."""
    calendar_a, _tonie_a = make_calendar_tonie(db_session)
    calendar_b, tonie_b = make_calendar_tonie(db_session, tonie_id=TONIE_ID_2)
    slot_a, beitrag = make_person_slot_beitrag(db_session, calendar_a, day=5)
    db_session.add(Slot(campaign_id=calendar_b.id, day=12, auftrag=slot_a.auftrag))
    db_session.commit()
    handler, patches = _day_run_handler(
        [], "app-b12", title="Tuerchen 5 Titel", tonie_id=TONIE_ID_2
    )

    outcome = run_delivery(
        db_session,
        make_client(handler),
        FakeStorage({beitrag.audio_object_key: AUDIO}),
        tonie_b,
        run_type="vorabend",
        target_day=12,
    )

    assert outcome.success is True
    assert outcome.used_beitrag_id == beitrag.id
    assert outcome.used_replacement is False
    run = db_session.execute(select(DeliveryRun)).scalar_one()
    assert (run.campaign_id, run.tonie_id, run.target_day) == (calendar_b.id, TONIE_ID_2, 12)


def _calendarless_tonie(db_session, **fields) -> CreativeTonie:
    tonie = CreativeTonie(tonie_id=TONIE_ID_2, **fields)
    db_session.add(tonie)
    db_session.commit()
    return tonie


def _free_beitrag(db_session, title: str) -> Beitrag:
    person = Person(email=f"{title}@example.test", display_name=title)
    db_session.add(person)
    db_session.flush()
    beitrag = Beitrag(
        person_id=person.id,
        title=title,
        audio_object_key=f"audio/{title}.mp3",
        approved_at=datetime(2026, 11, 20),
    )
    db_session.add(beitrag)
    db_session.commit()
    return beitrag


def _manual_handler(before: list[dict], new: dict, stock: list[dict], file_id: str):
    return build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=before),
            creative_tonie_response(chapters=[new, *stock]),
        ],
        tonie_patches=[creative_tonie_response(transcoding=True, chapters=[new, *stock])],
        file_ids=(file_id,),
        tonie_id=TONIE_ID_2,
    )


def test_manual_run_on_tonie_without_calendar_records_its_tonie_and_placeholder(db_session):
    """R6/KTD10: kein IntegrityError -- der Eintrag traegt den Tonie und den
    ersten Kalender der Instanz als Platzhalter."""
    first = Campaign(name="Erster")
    db_session.add_all([first, Campaign(name="Zweiter")])
    db_session.commit()
    tonie = _calendarless_tonie(db_session)
    free = _free_beitrag(db_session, "Gruss")
    new = {"id": "app-g", "title": "Gruss", "file": "app-g"}
    handler, patches = _manual_handler(STOCK, new, STOCK, "file-a")

    outcome = run_delivery(
        db_session,
        make_client(handler),
        FakeStorage({free.audio_object_key: AUDIO}),
        tonie,
        run_type="manuell",
        manual_beitrag_ids=[free.id],
    )

    assert outcome.success is True
    assert outcome.tonie_id == TONIE_ID_2
    run = db_session.execute(select(DeliveryRun)).scalar_one()
    assert (run.tonie_id, run.campaign_id, run.outcome) == (TONIE_ID_2, first.id, "erfolg")
    db_session.refresh(tonie)
    assert '"app-g"' in tonie.app_chapters
    assert tonie.campaign_id is None


def test_manual_run_without_any_calendar_is_refused(db_session):
    from app.delivery.job import NoCalendarError

    tonie = _calendarless_tonie(db_session)

    with pytest.raises(NoCalendarError, match="Kalender anlegen"):
        run_delivery(
            db_session,
            make_client(build_handler(tonie_gets=[], tonie_patches=[])[0]),
            FakeStorage({}),
            tonie,
            run_type="manuell",
            manual_beitrag_ids=[1],
        )
    assert db_session.execute(select(DeliveryRun)).scalars().all() == []


def test_second_manual_run_on_calendarless_tonie_replaces_only_app_chapters_ae13(db_session):
    db_session.add(Campaign())
    db_session.commit()
    first_app = {"id": "app-1", "title": "Erster Gruss", "file": "app-1"}
    tonie = _calendarless_tonie(db_session, app_chapters='[{"id": "app-1", "seconds": 1.0}]')
    free = _free_beitrag(db_session, "Zweiter")
    new = {"id": "app-2", "title": "Zweiter", "file": "app-2"}
    handler, patches = _manual_handler([first_app, *STOCK], new, STOCK, "file-b")

    outcome = run_delivery(
        db_session,
        make_client(handler),
        FakeStorage({free.audio_object_key: AUDIO}),
        tonie,
        run_type="manuell",
        manual_beitrag_ids=[free.id],
    )

    assert outcome.success is True
    assert [c.get("id") for c in patches[0]["chapters"]] == [None, "familie-1", "familie-2"]
    db_session.refresh(tonie)
    assert [c["id"] for c in json.loads(tonie.app_chapters)] == ["app-2"]


def test_day_run_on_tonie_without_calendar_fails_without_login(db_session):
    db_session.add(Campaign())
    db_session.commit()
    tonie = _calendarless_tonie(db_session)
    handler, _ = build_handler(tonie_gets=[], tonie_patches=[], tonie_id=TONIE_ID_2)

    outcome = run_delivery(
        db_session, make_client(handler), FakeStorage({}), tonie, run_type="vorabend", target_day=5
    )

    assert outcome.success is False
    assert "keinem Kalender" in outcome.reason


# --- Trennen (R37, AE11) ---------------------------------------------------------


def _cleanup_handler(before: list[dict], after: list[dict], *, patch=None):
    return build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=before),
            creative_tonie_response(chapters=after),
        ],
        tonie_patches=[patch or creative_tonie_response(chapters=after)],
        file_ids=(),
    )


def test_detach_removes_app_chapter_keeps_stock_and_empties_memory_ae11(db_session):
    from app.delivery.job import detach_tonie

    campaign, tonie = make_calendar_tonie(
        db_session,
        app_chapters='[{"id": "app-5", "seconds": 1.0}]',
        verified_chapter_id="app-5",
        verified_for_day=5,
    )
    app5 = {"id": "app-5", "title": "Tag 5", "file": "app-5"}
    handler, patches = _cleanup_handler([app5, *STOCK], STOCK)

    outcome = detach_tonie(db_session, make_client(handler), tonie)

    assert outcome.success is True
    assert [c["id"] for c in patches[0]["chapters"]] == ["familie-1", "familie-2"]
    db_session.refresh(tonie)
    assert tonie.campaign_id is None
    assert tonie.app_chapters is None
    assert tonie.abraeumen_offen is False
    assert tonie.verified_chapter_id is None
    run = db_session.execute(select(DeliveryRun)).scalar_one()
    assert (run.run_type, run.outcome, run.tonie_id, run.campaign_id) == (
        "abraeumen",
        "erfolg",
        TONIE_ID,
        campaign.id,
    )


def test_detach_with_failing_cleanup_keeps_memory_with_abraeumen_offen(db_session):
    from app.delivery.job import detach_tonie

    _campaign, tonie = make_calendar_tonie(
        db_session, app_chapters='[{"id": "app-5", "seconds": 1.0}]'
    )
    app5 = {"id": "app-5", "title": "Tag 5", "file": "app-5"}
    handler, patches = build_handler(
        tonie_gets=[creative_tonie_response(chapters=[app5, *STOCK])],
        tonie_patches=[httpx.Response(500), creative_tonie_response(chapters=[app5, *STOCK])],
        file_ids=(),
    )

    outcome = detach_tonie(db_session, make_client(handler), tonie)

    assert outcome.success is False
    assert len(patches) == 2  # Abraeumen, dann Rueckfallstand
    db_session.refresh(tonie)
    assert tonie.campaign_id is None
    assert tonie.app_chapters == '[{"id": "app-5", "seconds": 1.0}]'
    assert tonie.abraeumen_offen is True


def test_detach_without_app_chapter_only_unlinks_without_run(db_session):
    from app.delivery.job import detach_tonie

    _campaign, tonie = make_calendar_tonie(db_session)
    handler, _ = build_handler(tonie_gets=[], tonie_patches=[])

    assert detach_tonie(db_session, make_client(handler), tonie) is None

    db_session.refresh(tonie)
    assert tonie.campaign_id is None
    assert db_session.execute(select(DeliveryRun)).scalars().all() == []


def test_detach_is_refused_while_a_run_holds_the_lock(db_session):
    from app.delivery.job import TonieBusyError, _lock_for, detach_tonie, tonie_run_active

    campaign, tonie = make_calendar_tonie(
        db_session, app_chapters='[{"id": "app-5", "seconds": 1.0}]'
    )
    handler, _ = build_handler(tonie_gets=[], tonie_patches=[])
    lock = _lock_for(TONIE_ID)
    assert tonie_run_active(TONIE_ID) is False
    assert lock.acquire(blocking=False)
    try:
        assert tonie_run_active(TONIE_ID) is True
        assert tonie_run_active(TONIE_ID_2) is False
        with pytest.raises(TonieBusyError):
            detach_tonie(db_session, make_client(handler), tonie)
    finally:
        lock.release()

    db_session.refresh(tonie)
    assert tonie.campaign_id == campaign.id
    assert tonie.abraeumen_offen is False
    assert db_session.execute(select(DeliveryRun)).scalars().all() == []


# --- Konto je Tonie (U6-Rueckfallregel ueber client_for) ------------------------------


@pytest.mark.parametrize("konto", ["ohne", "neu_eingeben"])
def test_client_for_without_usable_konto_fails_without_http(db_session, config, konto):
    from app import settings
    from app.delivery.job import client_for
    from app.toniecloud.client import KontoUnavailable, TonieCloudFactory

    calls: list = []
    factory = TonieCloudFactory(
        config.credentials_key,
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(500)),
    )
    _campaign, tonie = make_calendar_tonie(db_session)
    if konto == "neu_eingeben":
        row = settings.create_konto(
            db_session,
            config.credentials_key,
            username="oma@example.test",
            password="pw-oma",
            now=datetime(2026, 10, 1),
        )
        row.needs_reentry = True
        tonie.konto_id = row.id
        db_session.commit()

    client = client_for(factory, db_session, tonie)

    with pytest.raises(KontoUnavailable) as excinfo:
        client.find_household_id(TONIE_ID)
    expected = "neu eingegeben" if konto == "neu_eingeben" else "kein tonies-Konto"
    assert expected in str(excinfo.value)
    assert calls == []
