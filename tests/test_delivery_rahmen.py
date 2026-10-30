"""Advent-Anreicherung U5: Rahmen in der Auslieferung (KTD1, KTD4, KTD5).

Echtes ffmpeg; Audio nur aus tests/fixtures/audio.py. Die Toniecloud ist ein
kleiner Zustandsersatz im Speicher: er merkt sich die Kapitelliste, misst
jede hochgeladene Datei und verwirft auf Wunsch einzelne Uploads mit
`wrongFormat`.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.delivery.audio import probe_duration_seconds
from app.delivery.chapters import load_app_chapters
from app.delivery.job import run_delivery
from app.delivery.rahmen import MELODIE_KEY, PAUSE_SEKUNDEN, melodie, rahmen_sekunden
from app.delivery.trigger import get_run_starter, get_storage, get_toniecloud_factory
from app.mail.report import SummaryRow, build_summary_message
from app.models import Beitrag, Campaign, CreativeTonie, DeliveryRun, Person, Slot
from app.recording.normalize import normalize_recording
from app.settings import get_einstellungen
from app.storage import ObjectMissing, StoredObject
from app.toniecloud.client import API_BASE_URL, TOKEN_URL
from tests.fixtures.audio import make_tone_mp3, make_wrong_format_mp3
from tests.mailutil import plain_text
from tests.test_delivery import (
    HOUSEHOLD_ID,
    STOCK,
    TONIE_URL,
    FixedFactory,
    config_response_with,
    creative_tonie_response,
    creativetonies_list_response,
    file_response,
    households_response,
    make_calendar_tonie,
    make_client,
    make_person_slot_beitrag,
    token_response,
)

TOLERANZ = 0.3
S3_URL = "https://s3.example.test/upload"
ABSPANN_KEY = "rahmen/abspann.mp3"
VORSPANN_KEY = "rahmen/vorspann.mp3"
STOCK_SECONDS = 3.0

HINWEIS_BAU = "Ohne Rahmen ausgeliefert: Der Rahmen ließ sich nicht bauen."
HINWEIS_PLATZ = "Ohne Rahmen ausgeliefert: Für den Rahmen fehlt Platz auf dem Tonie."
HINWEIS_ABGELEHNT = (
    "Ohne Rahmen ausgeliefert: Die Toniecloud hat das Kapitel mit Rahmen abgelehnt ("
)
HINWEIS_PROBE = "Rahmen ließ sich nicht bauen, Datei ohne Rahmen hochgeladen."
ERSATZ_REASON = (
    "Tagesbeitrag fehlgeschlagen (Verarbeitungsfehler: wrongFormat); Ersatzbeitrag ausgeliefert."
)


class RahmenStorage:
    """Speicher-Ersatz mit `get`, `read`, `put` und `ObjectMissing`; merkt
    sich jeden Schreibzugriff."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = dict(objects)
        self.puts: list[str] = []

    def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise ObjectMissing(key)
        return self.objects[key]

    def read(self, key: str) -> StoredObject:
        data = self.get(key)
        return StoredObject(
            data=data, total=len(data), content_type="audio/mpeg", content_range=None
        )

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.puts.append(key)
        self.objects[key] = data


class FakeCloud:
    """Toniecloud im Speicher. `reject` nennt die Nummern der Uploads (ab 1),
    deren Kapitel die Verarbeitung mit `wrongFormat` verwirft; "alle"
    verwirft jedes."""

    def __init__(self, *, max_seconds: float = 5400, reject=()) -> None:
        self.chapters = [dict(c) for c in STOCK]
        self.seconds = {c["id"]: STOCK_SECONDS / len(STOCK) for c in STOCK}
        self.max_seconds = max_seconds
        self.reject = reject
        self.created: list[str] = []
        self.uploads: list[bytes] = []
        self.rejected_files: set[str] = set()
        self.patches: list[dict] = []
        self.pending_errors: list[dict] = []

    def upload_seconds(self) -> list[float]:
        return [probe_duration_seconds(data) for data in self.uploads]

    def _state(self, *, transcoding: bool = False, chapters=None, errors=None):
        chapters = self.chapters if chapters is None else chapters
        return creative_tonie_response(
            chapters=chapters,
            transcoding=transcoding,
            transcoding_errors=errors,
            seconds_present=sum(self.seconds.get(c["id"], 0.0) for c in chapters),
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
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
        if request.method == "GET" and url == f"{API_BASE_URL}/config":
            return config_response_with(self.max_seconds)
        if request.method == "POST" and url == f"{API_BASE_URL}/file":
            self.created.append(f"file-{len(self.created) + 1}")
            return file_response(self.created[-1], S3_URL)
        if request.method == "POST" and url == S3_URL:
            return self._upload(request)
        if request.method == "GET" and url == TONIE_URL:
            errors, self.pending_errors = self.pending_errors, []
            return self._state(errors=errors)
        if request.method == "PATCH" and url == TONIE_URL:
            return self._patch(json.loads(request.content))
        raise AssertionError(f"Unerwarteter Aufruf: {request.method} {request.url}")

    def _upload(self, request: httpx.Request) -> httpx.Response:
        boundary = request.headers["content-type"].split("boundary=")[1].encode()
        part = next(p for p in request.content.split(b"--" + boundary) if b'filename="' in p)
        data = part.split(b"\r\n\r\n", 1)[1].removesuffix(b"\r\n")
        self.uploads.append(data)
        file_id = self.created[-1]
        self.seconds[f"ch-{file_id}"] = probe_duration_seconds(data)
        if self.reject == "alle" or len(self.uploads) in self.reject:
            self.rejected_files.add(file_id)
        return httpx.Response(204)

    def _patch(self, body: dict) -> httpx.Response:
        self.patches.append(body)
        patched = [{**c, "id": c.get("id") or f"ch-{c['file']}"} for c in body["chapters"]]
        kept = [c for c in patched if c["file"] not in self.rejected_files]
        if len(kept) != len(patched):
            self.pending_errors = [{"reason": "wrongFormat"}]
        self.chapters = [{**c, "file": c["id"]} for c in kept]
        return self._state(transcoding=True, chapters=patched)


@pytest.fixture(scope="module")
def melodie_bytes() -> bytes:
    return melodie(RahmenStorage({}))


# Wie im Betrieb normalisiert (Einreichung, Upload im Setup).
GESCHICHTE = normalize_recording(make_tone_mp3(2.0, 440))
ERSATZ = normalize_recording(make_tone_mp3(1.0, 550))
ABSPANN = normalize_recording(make_tone_mp3(1.5, 660))


def _kalender(db: Session, **felder) -> tuple[Campaign, CreativeTonie]:
    campaign = Campaign(name="Familie", **felder)
    db.add(campaign)
    db.commit()
    return make_calendar_tonie(db, campaign=campaign)


def _ersatz(db: Session, campaign: Campaign) -> Beitrag:
    person = Person(email="ersatz@example.test", display_name="Ersatz")
    db.add(person)
    db.flush()
    beitrag = Beitrag(
        person_id=person.id,
        title="Ersatzgeschichte",
        audio_object_key="audio/ersatz.mp3",
        approved_at=datetime(2026, 11, 1),
    )
    db.add(beitrag)
    db.flush()
    campaign.replacement_beitrag_id = beitrag.id
    db.commit()
    return beitrag


def _storage(melodie_bytes: bytes, **dateien: bytes) -> RahmenStorage:
    objects = {MELODIE_KEY: melodie_bytes, ABSPANN_KEY: ABSPANN}
    objects.update({f"audio/{name}.mp3": data for name, data in dateien.items()})
    return RahmenStorage(objects)


def _lauf(db, cloud, storage, tonie, *, run_type="vorabend", day=5, **kw):
    client = make_client(cloud.handler)
    return run_delivery(db, client, storage, tonie, run_type=run_type, target_day=day, **kw)


def _eintrag(db: Session) -> DeliveryRun:
    return db.execute(select(DeliveryRun)).scalars().one()


def _rahmen(melodie_bytes: bytes) -> float:
    return (
        probe_duration_seconds(melodie_bytes)
        + PAUSE_SEKUNDEN
        + probe_duration_seconds(ABSPANN)
        + PAUSE_SEKUNDEN
    )


def _dauer(data: bytes) -> float:
    return probe_duration_seconds(data)


# --- Charakterisierung: der heutige Ersatzbeitrag-Pfad (rahmenlos) ------------


def test_characterisation_rejected_story_falls_back_to_replacement(db_session, melodie_bytes):
    campaign, tonie = _kalender(db_session)
    get_einstellungen(db_session).rahmen_aus = True
    db_session.commit()
    _slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)
    ersatz = _ersatz(db_session, campaign)
    storage = _storage(melodie_bytes, **{"beitrag-5": GESCHICHTE, "ersatz": ERSATZ})
    cloud = FakeCloud(reject={1})

    outcome = _lauf(db_session, cloud, storage, tonie)

    assert outcome.success is True
    assert outcome.used_beitrag_id == ersatz.id
    entry = _eintrag(db_session)
    assert entry.outcome == "ersatzbeitrag"
    assert entry.reason == ERSATZ_REASON
    assert len(cloud.uploads) == 2
    assert [c["title"] for c in cloud.chapters] == [
        "Ersatzgeschichte",
        *(c["title"] for c in STOCK),
    ]
    assert beitrag.id != ersatz.id


# --- Kaskade ---------------------------------------------------------------


def test_vorabend_uploads_story_with_melodie_and_abspann(db_session, melodie_bytes):
    """AE4"""
    campaign, tonie = _kalender(db_session, abspann_key=ABSPANN_KEY)
    make_person_slot_beitrag(db_session, campaign, day=5)
    storage = _storage(melodie_bytes, **{"beitrag-5": GESCHICHTE})
    cloud = FakeCloud()

    _lauf(db_session, cloud, storage, tonie)

    assert len(cloud.uploads) == 1
    erwartet = _dauer(GESCHICHTE) + _rahmen(melodie_bytes)
    assert abs(cloud.upload_seconds()[0] - erwartet) <= TOLERANZ
    assert len(cloud.patches) == 1
    assert cloud.patches[0]["chapters"] == [{"title": "Tuerchen 5 Titel", "file": "file-1"}, *STOCK]
    entry = _eintrag(db_session)
    assert entry.outcome == "erfolg"
    assert not entry.reason


def test_app_chapters_hold_duration_of_framed_file(db_session, melodie_bytes):
    campaign, tonie = _kalender(db_session, abspann_key=ABSPANN_KEY)
    make_person_slot_beitrag(db_session, campaign, day=5)
    storage = _storage(melodie_bytes, **{"beitrag-5": GESCHICHTE})
    cloud = FakeCloud()

    _lauf(db_session, cloud, storage, tonie)

    db_session.refresh(tonie)
    [kapitel] = load_app_chapters(tonie.app_chapters)
    assert kapitel.seconds == pytest.approx(cloud.upload_seconds()[0], abs=0.01)
    assert kapitel.seconds > _dauer(GESCHICHTE) + 5


def test_run_writes_only_the_melodie_and_keeps_story_bytes(db_session):
    campaign, tonie = _kalender(db_session)
    make_person_slot_beitrag(db_session, campaign, day=5)
    storage = RahmenStorage({"audio/beitrag-5.mp3": GESCHICHTE})
    cloud = FakeCloud()

    _lauf(db_session, cloud, storage, tonie)

    assert storage.puts == [MELODIE_KEY]
    assert storage.objects["audio/beitrag-5.mp3"] == GESCHICHTE
    assert cloud.upload_seconds()[0] > _dauer(GESCHICHTE) + 5


def test_missing_story_delivers_replacement_with_frame(db_session, melodie_bytes):
    """AE5"""
    campaign, tonie = _kalender(db_session, abspann_key=ABSPANN_KEY)
    db_session.add(Slot(campaign_id=campaign.id, day=5))
    _ersatz(db_session, campaign)
    storage = _storage(melodie_bytes, ersatz=ERSATZ)
    cloud = FakeCloud()

    _lauf(db_session, cloud, storage, tonie)

    assert len(cloud.uploads) == 1
    erwartet = _dauer(ERSATZ) + _rahmen(melodie_bytes)
    assert abs(cloud.upload_seconds()[0] - erwartet) <= TOLERANZ
    assert _eintrag(db_session).outcome == "ersatzbeitrag"


def test_broken_vorspann_delivers_story_without_frame(db_session, melodie_bytes):
    """AE6"""
    campaign, tonie = _kalender(db_session, vorspann_key=VORSPANN_KEY)
    make_person_slot_beitrag(db_session, campaign, day=5)
    storage = _storage(melodie_bytes, **{"beitrag-5": GESCHICHTE})
    storage.objects[VORSPANN_KEY] = make_wrong_format_mp3()
    cloud = FakeCloud()

    _lauf(db_session, cloud, storage, tonie)

    assert len(cloud.uploads) == 1
    assert cloud.uploads[0] == GESCHICHTE
    entry = _eintrag(db_session)
    assert entry.outcome == "erfolg"
    assert entry.reason == HINWEIS_BAU

    message = build_summary_message(
        to_address="admin@example.test",
        evening=date(2026, 12, 4),
        run_type="vorabend",
        rows=[
            SummaryRow(
                calendar=campaign.name,
                tonie_name="Tonie",
                tonie_id=tonie.tonie_id,
                result=entry.outcome,
                target_day=entry.target_day,
                reason=entry.reason,
            )
        ],
    )
    assert "Ohne Rahmen ausgeliefert" in plain_text(message)


def test_rejected_framed_chapter_retries_same_story_without_frame(db_session, melodie_bytes):
    """AE12"""
    campaign, tonie = _kalender(db_session, abspann_key=ABSPANN_KEY)
    _slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)
    ersatz = _ersatz(db_session, campaign)
    storage = _storage(melodie_bytes, **{"beitrag-5": GESCHICHTE, "ersatz": ERSATZ})
    cloud = FakeCloud(reject={1})

    outcome = _lauf(db_session, cloud, storage, tonie)

    assert len(cloud.uploads) == 2
    assert cloud.upload_seconds()[0] > _dauer(GESCHICHTE) + 5
    assert cloud.uploads[1] == GESCHICHTE
    assert outcome.used_beitrag_id == beitrag.id != ersatz.id
    entry = _eintrag(db_session)
    assert entry.outcome == "erfolg"
    assert entry.reason.startswith(HINWEIS_ABGELEHNT)
    assert entry.reason == HINWEIS_ABGELEHNT + "Verarbeitungsfehler: wrongFormat)."
    assert [c["title"] for c in cloud.chapters] == [
        "Tuerchen 5 Titel",
        *(c["title"] for c in STOCK),
    ]


def test_story_failing_without_frame_rejection_lets_replacement_keep_frame(
    db_session, melodie_bytes
):
    langer = normalize_recording(make_tone_mp3(4.0, 440))
    campaign, tonie = _kalender(db_session, abspann_key=ABSPANN_KEY)
    make_person_slot_beitrag(db_session, campaign, day=5)
    ersatz = _ersatz(db_session, campaign)
    storage = _storage(melodie_bytes, **{"beitrag-5": langer, "ersatz": ERSATZ})
    rahmen = _rahmen(melodie_bytes)
    # Platz fuer Ersatz mit Rahmen, nicht fuer die Geschichte mit Rahmen.
    frei = (_dauer(ERSATZ) + rahmen + _dauer(langer) + rahmen) / 2
    cloud = FakeCloud(max_seconds=STOCK_SECONDS + frei, reject={1})

    outcome = _lauf(db_session, cloud, storage, tonie)

    assert len(cloud.uploads) == 2
    assert cloud.uploads[0] == langer
    assert abs(cloud.upload_seconds()[1] - (_dauer(ERSATZ) + rahmen)) <= TOLERANZ
    assert outcome.used_beitrag_id == ersatz.id
    entry = _eintrag(db_session)
    assert entry.outcome == "ersatzbeitrag"
    assert "Ohne Rahmen" not in entry.reason


def test_frame_without_space_delivers_story_without_frame(db_session, melodie_bytes):
    """AE7"""
    campaign, tonie = _kalender(db_session, abspann_key=ABSPANN_KEY)
    make_person_slot_beitrag(db_session, campaign, day=5)
    _ersatz(db_session, campaign)
    storage = _storage(melodie_bytes, **{"beitrag-5": GESCHICHTE, "ersatz": ERSATZ})
    cloud = FakeCloud(max_seconds=STOCK_SECONDS + _dauer(GESCHICHTE) + 2)

    _lauf(db_session, cloud, storage, tonie)

    assert cloud.uploads == [GESCHICHTE]
    entry = _eintrag(db_session)
    assert entry.outcome == "erfolg"
    assert entry.reason == HINWEIS_PLATZ


def test_no_space_even_without_frame_behaves_as_today(db_session, melodie_bytes):
    campaign, tonie = _kalender(db_session, abspann_key=ABSPANN_KEY)
    make_person_slot_beitrag(db_session, campaign, day=5)
    storage = _storage(melodie_bytes, **{"beitrag-5": GESCHICHTE})
    cloud = FakeCloud(max_seconds=STOCK_SECONDS + 0.5)

    outcome = _lauf(db_session, cloud, storage, tonie)

    assert outcome.success is False
    assert cloud.uploads == []
    assert cloud.patches == []
    entry = _eintrag(db_session)
    assert entry.outcome == "fehlschlag"
    assert entry.reason.startswith("Kein Platz auf dem Tonie")


def test_cloud_rejecting_everything_uploads_at_most_three_files(db_session, melodie_bytes):
    campaign, tonie = _kalender(db_session, abspann_key=ABSPANN_KEY)
    make_person_slot_beitrag(db_session, campaign, day=5)
    _ersatz(db_session, campaign)
    storage = _storage(melodie_bytes, **{"beitrag-5": GESCHICHTE, "ersatz": ERSATZ})
    cloud = FakeCloud(reject="alle")

    outcome = _lauf(db_session, cloud, storage, tonie)

    assert outcome.success is False
    assert len(cloud.uploads) == 3
    assert cloud.uploads[1] == GESCHICHTE
    assert cloud.uploads[2] == ERSATZ
    entry = _eintrag(db_session)
    assert entry.outcome == "fehlschlag"
    assert entry.reason.startswith("Tagesbeitrag: Verarbeitungsfehler: wrongFormat; Ersatzbeitrag:")
    assert [c["id"] for c in cloud.chapters] == [c["id"] for c in STOCK]


def test_rejected_frame_replacement_success_carries_story_reason(db_session, melodie_bytes):
    campaign, tonie = _kalender(db_session, abspann_key=ABSPANN_KEY)
    make_person_slot_beitrag(db_session, campaign, day=5)
    ersatz = _ersatz(db_session, campaign)
    storage = _storage(melodie_bytes, **{"beitrag-5": GESCHICHTE, "ersatz": ERSATZ})
    cloud = FakeCloud(reject={1, 2})

    outcome = _lauf(db_session, cloud, storage, tonie)

    assert outcome.used_beitrag_id == ersatz.id
    assert cloud.uploads[2] == ERSATZ
    entry = _eintrag(db_session)
    assert entry.outcome == "ersatzbeitrag"
    assert entry.reason == (
        ERSATZ_REASON + " · " + HINWEIS_ABGELEHNT + "Verarbeitungsfehler: wrongFormat)."
    )


def test_manual_run_uploads_without_frame(db_session, melodie_bytes):
    """AE10"""
    campaign, tonie = _kalender(db_session, abspann_key=ABSPANN_KEY)
    _s1, erster = make_person_slot_beitrag(db_session, campaign, day=5)
    _s2, zweiter = make_person_slot_beitrag(db_session, campaign, day=6)
    storage = _storage(melodie_bytes, **{"beitrag-5": GESCHICHTE, "beitrag-6": ERSATZ})
    cloud = FakeCloud()

    outcome = _lauf(
        db_session,
        cloud,
        storage,
        tonie,
        run_type="manuell",
        day=None,
        manual_beitrag_ids=[erster.id, zweiter.id],
    )

    assert outcome.success is True
    assert cloud.uploads == [GESCHICHTE, ERSATZ]


def test_rahmen_aus_uploads_without_frame_and_without_hint(db_session, melodie_bytes):
    campaign, tonie = _kalender(db_session, abspann_key=ABSPANN_KEY)
    get_einstellungen(db_session).rahmen_aus = True
    db_session.commit()
    make_person_slot_beitrag(db_session, campaign, day=5)
    _ersatz(db_session, campaign)
    storage = _storage(melodie_bytes, **{"beitrag-5": GESCHICHTE, "ersatz": ERSATZ})
    cloud = FakeCloud()

    _lauf(db_session, cloud, storage, tonie)
    _lauf(db_session, cloud, storage, tonie, run_type="probelauf", day=None)

    assert cloud.uploads == [GESCHICHTE, ERSATZ]
    reasons = [r.reason or "" for r in db_session.execute(select(DeliveryRun)).scalars()]
    assert len(reasons) == 2
    assert all("Ohne Rahmen" not in r and "Rahmen ließ" not in r for r in reasons)


def test_second_run_same_evening_does_not_reupload_after_abspann_change(db_session, melodie_bytes):
    campaign, tonie = _kalender(db_session)
    make_person_slot_beitrag(db_session, campaign, day=5)
    storage = _storage(melodie_bytes, **{"beitrag-5": GESCHICHTE})
    cloud = FakeCloud()
    _lauf(db_session, cloud, storage, tonie)
    campaign.abspann_key = ABSPANN_KEY
    db_session.commit()

    outcome = _lauf(db_session, cloud, storage, tonie, run_type="kontrolllauf")

    assert outcome.success is True
    assert len(cloud.uploads) == 1
    assert len(cloud.patches) == 1


# --- Trockenlauf und Probelauf (KTD5) ---------------------------------------


def test_probelauf_uploads_framed_replacement_without_patch(db_session, melodie_bytes):
    campaign, tonie = _kalender(db_session, abspann_key=ABSPANN_KEY)
    _ersatz(db_session, campaign)
    storage = _storage(melodie_bytes, ersatz=ERSATZ)
    cloud = FakeCloud()

    outcome = _lauf(db_session, cloud, storage, tonie, run_type="probelauf", day=None)

    assert outcome.success is True
    assert cloud.patches == []
    erwartet = _dauer(ERSATZ) + _rahmen(melodie_bytes)
    assert abs(cloud.upload_seconds()[0] - erwartet) <= TOLERANZ
    assert "Rahmen ließ" not in outcome.reason


def test_probelauf_with_broken_vorspann_uploads_without_frame(db_session, melodie_bytes):
    campaign, tonie = _kalender(db_session, vorspann_key=VORSPANN_KEY)
    _ersatz(db_session, campaign)
    storage = _storage(melodie_bytes, ersatz=ERSATZ)
    storage.objects[VORSPANN_KEY] = make_wrong_format_mp3()
    cloud = FakeCloud()

    outcome = _lauf(db_session, cloud, storage, tonie, run_type="probelauf", day=None)

    assert outcome.success is True
    assert cloud.uploads == [ERSATZ]
    assert cloud.patches == []
    assert outcome.reason.endswith(" · " + HINWEIS_PROBE)
    assert _eintrag(db_session).reason == outcome.reason


def test_trockenlauf_uploads_framed_story(db_session, melodie_bytes):
    campaign, tonie = _kalender(db_session, abspann_key=ABSPANN_KEY)
    make_person_slot_beitrag(db_session, campaign, day=5)
    storage = _storage(melodie_bytes, **{"beitrag-5": GESCHICHTE})
    cloud = FakeCloud()

    outcome = _lauf(db_session, cloud, storage, tonie, run_type="trockenlauf")

    assert outcome.success is True
    assert cloud.patches == []
    erwartet = _dauer(GESCHICHTE) + _rahmen(melodie_bytes)
    assert abs(cloud.upload_seconds()[0] - erwartet) <= TOLERANZ


# --- rahmen_sekunden und "Platz pruefen" (R10, AE7) -------------------------


def test_rahmen_sekunden_sums_parts_and_pauses(db_session, melodie_bytes):
    campaign, _tonie = _kalender(db_session, abspann_key=ABSPANN_KEY)
    storage = _storage(melodie_bytes)

    sekunden = rahmen_sekunden(db_session, storage, campaign)

    erwartet = _dauer(melodie_bytes) + _dauer(ABSPANN) + 2 * PAUSE_SEKUNDEN
    assert abs(sekunden - erwartet) <= 0.1


def test_rahmen_sekunden_zero_without_frame(db_session, melodie_bytes):
    ohne, _tonie = _kalender(db_session, vorspann_aus=True)
    mit = Campaign(name="Zweiter", abspann_key=ABSPANN_KEY)
    db_session.add(mit)
    db_session.commit()
    storage = _storage(melodie_bytes)

    assert rahmen_sekunden(db_session, storage, ohne) == 0.0
    get_einstellungen(db_session).rahmen_aus = True
    db_session.commit()
    assert rahmen_sekunden(db_session, storage, mit) == 0.0


def _platz_seite(admin_client, cloud: FakeCloud, storage: RahmenStorage):
    app = admin_client.app
    app.dependency_overrides[get_toniecloud_factory] = lambda: FixedFactory(
        make_client(cloud.handler)
    )
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_run_starter] = lambda: lambda run: run()
    try:
        return admin_client.get("/admin/auslieferung?platz=1")
    finally:
        app.dependency_overrides.clear()


def test_space_check_adds_frame_to_each_beitrag(admin_client, db_session, melodie_bytes):
    campaign, _tonie = _kalender(db_session, abspann_key=ABSPANN_KEY)
    make_person_slot_beitrag(db_session, campaign, day=5)
    storage = _storage(melodie_bytes, **{"beitrag-5": GESCHICHTE})
    cloud = FakeCloud(max_seconds=STOCK_SECONDS + _dauer(GESCHICHTE) + 2)

    response = _platz_seite(admin_client, cloud, storage)

    assert response.status_code == 200
    assert "Rahmen dieses Kalenders:" in response.text
    assert "Diese Beiträge passen nicht hinein:" in response.text
    assert "Tuerchen 5 Titel" in response.text.split("Diese Beiträge passen nicht hinein:")[1]


def test_space_check_with_broken_vorspann_calculates_without_frame(
    admin_client, db_session, melodie_bytes
):
    campaign, _tonie = _kalender(db_session, vorspann_key=VORSPANN_KEY)
    make_person_slot_beitrag(db_session, campaign, day=5)
    storage = _storage(melodie_bytes, **{"beitrag-5": GESCHICHTE})
    storage.objects[VORSPANN_KEY] = make_wrong_format_mp3()
    cloud = FakeCloud(max_seconds=STOCK_SECONDS + _dauer(GESCHICHTE) + 2)

    response = _platz_seite(admin_client, cloud, storage)

    assert response.status_code == 200
    assert "Der Rahmen dieses Kalenders lässt sich nicht bauen." in response.text
    assert "Alle freigegebenen Beiträge passen." in response.text
