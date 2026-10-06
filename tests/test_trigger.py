"""U10: Auslösung für Zeitplan und externen Anstoß (KTD13).

Test-first. Die Zustandsbestimmung ist reine Logik über Uhrzeit und Verlauf;
der Hintergrundstart laeuft gegen die Fake-Toniecloud aus
tests/test_delivery.py. Die Uhr, der Starter des Hintergrundlaufs und der
SMTP-Versand werden je Test ersetzt -- nie das echte Konto, nie echte Mail.
"""

from __future__ import annotations

import logging
import smtplib
import socket
import threading
import time as time_module
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from app import settings
from app.delivery.job import _lock_for
from app.delivery.trigger import (
    Due,
    determine_due,
    get_now,
    get_run_starter,
    get_storage,
    get_toniecloud_factory,
)
from app.models import Beitrag, Campaign, CreativeTonie, DeliveryRun, Person, TonieKonto
from app.toniecloud.client import TOKEN_URL
from tests.mailutil import plain_text
from tests.test_delivery import (
    AUDIO,
    TONIE_ID,
    TONIE_ID_2,
    TONIE_URL,
    FakeStorage,
    FixedFactory,
    build_handler,
    creative_tonie_response,
    make_calendar_tonie,
    make_client,
    make_person_slot_beitrag,
)

BERLIN = ZoneInfo("Europe/Berlin")
SECRET = {"X-Trigger-Secret": "test-trigger-secret"}
REAL_SMTP = smtplib.SMTP


def berlin(*args) -> datetime:
    return datetime(*args, tzinfo=BERLIN)


def utc_naive(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(tzinfo=None)


def no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"Unerwarteter Toniecloud-Aufruf: {request.method} {request.url}")


def delivered_chapter(chapter_id: str = "server-id-a") -> dict:
    return {"id": chapter_id, "title": "Tuerchen 6 Titel", "file": chapter_id}


def delivery_handler():
    """Ein vollstaendiger, erfolgreicher Vorabend-Lauf fuer Tag 6."""
    return build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=[]),
            creative_tonie_response(transcoding=False, chapters=[delivered_chapter()]),
        ],
        tonie_patches=[
            creative_tonie_response(
                transcoding=True,
                chapters=[{"id": "server-id-a", "title": "Tuerchen 6 Titel", "file": "file-a"}],
            )
        ],
    )


class FakeSMTP:
    sent: list = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def starttls(self, **kwargs) -> None:
        pass

    def login(self, *args) -> None:
        pass

    def send_message(self, message) -> None:
        FakeSMTP.sent.append(message)


class Harness:
    """Buendelt Uhr, Fake-Toniecloud und Starter fuer die Ausloese-Route."""

    def __init__(self, client, config) -> None:
        self.client = client
        self.app = client.app
        self.config = config
        self.logins = 0
        self.pending: list = []
        self.mode = "inline"
        self.install(no_network, {})
        self.app.dependency_overrides[get_run_starter] = lambda: self._start

    def _start(self, fn) -> None:
        if self.mode == "inline":
            fn()
        else:
            self.pending.append(fn)

    def run_pending(self) -> None:
        while self.pending:
            self.pending.pop(0)()

    def install(self, handler, files: dict[str, bytes]) -> None:
        def counting(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and str(request.url) == TOKEN_URL:
                self.logins += 1
            return handler(request)

        fake = make_client(counting)
        self.app.dependency_overrides[get_toniecloud_factory] = lambda: FixedFactory(fake)
        self.app.dependency_overrides[get_storage] = lambda: FakeStorage(files)

    def delivery_time(self, value: time) -> None:
        """KTD16: die Lieferzeit kommt aus den Einstellungen, nicht aus Config."""
        with self.app.state.session_factory() as s:
            settings.set_delivery_time(s, value, now=berlin(2026, 10, 1, 12, 0))

    def call(self, at: datetime, *, source: str = "zeitplan", headers: dict | None = None):
        self.app.dependency_overrides[get_now] = lambda: at
        return self.client.post(
            "/delivery/trigger",
            headers=SECRET | {"X-Trigger-Source": source} if headers is None else headers,
        )


@pytest.fixture
def harness(client, config, monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    h = Harness(client, config)
    yield h
    client.app.dependency_overrides.clear()
    # Ein nie ausgefuehrter, zurueckgestellter Lauf haelt die Sperre noch --
    # sie ist prozessweit und darf nicht in den naechsten Test durchsickern.
    lock = _lock_for(TONIE_ID)
    if lock.locked():
        lock.release()


@pytest.fixture
def campaign(db_session) -> Campaign:
    campaign, _tonie = make_calendar_tonie(db_session)
    return campaign


@pytest.fixture
def tonie(db_session, campaign) -> CreativeTonie:
    return db_session.execute(
        select(CreativeTonie).where(CreativeTonie.tonie_id == TONIE_ID)
    ).scalar_one()


def runs(db_session) -> list[DeliveryRun]:
    db_session.expire_all()
    return list(db_session.execute(select(DeliveryRun).order_by(DeliveryRun.id)).scalars())


def seed_run(
    db_session,
    campaign,
    run_type,
    target_day,
    outcome,
    at: datetime,
    evening: date,
    tonie_id: str = TONIE_ID,
):
    db_session.add(
        DeliveryRun(
            campaign_id=campaign.id,
            tonie_id=tonie_id,
            run_type=run_type,
            target_day=target_day,
            started_at=utc_naive(at),
            outcome=outcome,
            evening=evening,
        )
    )
    db_session.commit()


def make_replacement(db_session, campaign) -> Beitrag:
    person = Person(email="ersatz@example.test", display_name="Ersatz")
    db_session.add(person)
    db_session.flush()
    beitrag = Beitrag(
        person_id=person.id,
        title="Ersatzgeschichte",
        audio_object_key="audio/ersatz.mp3",
        approved_at=datetime(2026, 10, 1),
    )
    db_session.add(beitrag)
    db_session.flush()
    campaign.replacement_beitrag_id = beitrag.id
    db_session.commit()
    return beitrag


# --- Zustandsbestimmung aus der Uhr (rein) ------------------------------------


def test_due_before_evening_window_is_none():
    assert determine_due(berlin(2026, 12, 5, 16, 59), time(20, 0)) is None


def test_due_between_window_start_and_delivery_time_is_none():
    assert determine_due(berlin(2026, 12, 5, 19, 59), time(20, 0)) is None


def test_due_at_delivery_time_is_vorabend_for_next_day():
    due = determine_due(berlin(2026, 12, 5, 20, 0), time(20, 0))
    assert (due.run_type, due.target_day, due.evening) == ("vorabend", 6, date(2026, 12, 5))


def test_due_two_hours_after_delivery_is_kontrolllauf():
    due = determine_due(berlin(2026, 12, 5, 22, 0), time(20, 0))
    assert (due.run_type, due.target_day) == ("kontrolllauf", 6)


def test_due_window_closes_2h45_after_delivery():
    assert determine_due(berlin(2026, 12, 5, 22, 44), time(20, 0)).run_type == "kontrolllauf"
    assert determine_due(berlin(2026, 12, 5, 22, 45), time(20, 0)) is None
    assert determine_due(berlin(2026, 12, 5, 23, 30), time(20, 0)) is None


def test_due_after_midnight_belongs_to_evening_before():
    due = determine_due(berlin(2026, 12, 6, 1, 0), time(23, 0))
    assert (due.run_type, due.target_day, due.evening) == ("kontrolllauf", 6, date(2026, 12, 5))
    late_eve = determine_due(berlin(2026, 12, 5, 23, 30), time(23, 0))
    assert (late_eve.run_type, late_eve.target_day) == ("vorabend", 6)
    assert determine_due(berlin(2026, 12, 6, 1, 50), time(23, 0)) is None


def test_due_in_november_is_probelauf_without_target_day():
    due = determine_due(berlin(2026, 11, 5, 20, 0), time(20, 0))
    assert (due.run_type, due.target_day, due.evening) == ("probelauf", None, date(2026, 11, 5))
    # Der Probelauf ist ein Lauf je Abend zur Lieferzeit, kein Kontrolllauf.
    assert determine_due(berlin(2026, 11, 5, 22, 0), time(20, 0)) is None


def test_due_on_november_30_is_real_eve_delivery_for_day_one():
    due = determine_due(berlin(2026, 11, 30, 20, 0), time(20, 0))
    assert (due.run_type, due.target_day) == ("vorabend", 1)


def test_due_outside_november_and_advent_is_none():
    assert determine_due(berlin(2026, 10, 15, 20, 0), time(20, 0)) is None
    assert determine_due(berlin(2026, 12, 24, 20, 0), time(20, 0)) is None


def test_determine_due_on_christmas_day_is_cleanup():
    due = determine_due(berlin(2026, 12, 25, 20, 0), time(20, 0))
    assert due == Due("aufraeumen", date(2026, 12, 25), None)


def test_determine_due_cleanup_has_no_control_run():
    assert determine_due(berlin(2026, 12, 25, 22, 0), time(20, 0)) is None


def test_determine_due_on_boxing_day_is_not_due():
    assert determine_due(berlin(2026, 12, 26, 20, 0), time(20, 0)) is None


# --- Route: nicht faellig, Geheimnis, Logzeile --------------------------------


def test_call_at_1659_in_advent_is_204_without_run(harness, campaign, db_session):
    response = harness.call(berlin(2026, 12, 5, 16, 59))

    assert response.status_code == 204
    assert runs(db_session) == []
    assert harness.logins == 0


def test_call_on_october_15_is_204_without_probelauf(harness, campaign, db_session):
    make_replacement(db_session, campaign)

    response = harness.call(berlin(2026, 10, 15, 20, 0))

    assert response.status_code == 204
    assert runs(db_session) == []


def test_call_at_2330_with_delivery_2000_is_204(harness, campaign, db_session):
    assert harness.call(berlin(2026, 12, 5, 23, 30)).status_code == 204
    assert runs(db_session) == []


@pytest.mark.parametrize("headers", [{}, {"X-Trigger-Secret": "falsch"}])
def test_wrong_or_missing_secret_is_401_without_state_log(
    harness, campaign, db_session, caplog, headers
):
    caplog.set_level(logging.INFO, logger="app.delivery.trigger")

    response = harness.call(berlin(2026, 12, 5, 20, 0), headers=headers)

    assert response.status_code == 401
    assert [r for r in caplog.records if "zustand=" in r.getMessage()] == []
    assert runs(db_session) == []
    assert harness.logins == 0


@pytest.mark.parametrize(
    ("at", "source", "state"),
    [
        (berlin(2026, 12, 5, 16, 59), "zeitplan", "nicht fällig"),
        (berlin(2026, 12, 5, 20, 0), "extern", "gestartet"),
    ],
)
def test_every_call_logs_exactly_one_line_with_source_and_state(
    harness, campaign, db_session, caplog, at, source, state
):
    make_person_slot_beitrag(db_session, campaign, day=6)
    harness.mode = "deferred"
    caplog.set_level(logging.INFO, logger="app.delivery.trigger")

    harness.call(at, source=source)

    lines = [r.getMessage() for r in caplog.records if r.name == "app.delivery.trigger"]
    assert len(lines) == 1
    assert f"quelle={source}" in lines[0]
    assert f"zustand={state}" in lines[0]


# --- Route: Start, laeuft, gleichzeitig --------------------------------------


def test_first_due_call_starts_eve_run_in_background_and_answers_202(harness, campaign, db_session):
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=6)
    handler, patches = delivery_handler()
    harness.install(handler, {beitrag.audio_object_key: AUDIO})
    harness.mode = "deferred"

    response = harness.call(berlin(2026, 12, 5, 20, 0))

    assert response.status_code == 202
    [entry] = runs(db_session)
    # Vor dem ersten Toniecloud-Aufruf steht der Eintrag "gestartet".
    assert (entry.run_type, entry.target_day, entry.outcome) == ("vorabend", 6, "gestartet")
    assert entry.evening == date(2026, 12, 5)
    assert harness.logins == 0

    harness.run_pending()

    [entry] = runs(db_session)
    assert entry.outcome == "erfolg"
    assert len(patches) == 1
    assert harness.logins == 1
    assert not _lock_for(TONIE_ID).locked()
    # R26/R4: auch bei Erfolg eine Meldung -- als Sammelmeldung des Abends.
    [mail] = FakeSMTP.sent
    assert "Abendmeldung" in mail["Subject"]


def test_call_while_run_is_open_is_202_without_second_run(harness, campaign, db_session):
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=6)
    handler, patches = delivery_handler()
    harness.install(handler, {beitrag.audio_object_key: AUDIO})
    harness.mode = "deferred"
    assert harness.call(berlin(2026, 12, 5, 20, 0)).status_code == 202

    response = harness.call(berlin(2026, 12, 5, 20, 5), source="extern")

    assert response.status_code == 202
    assert len(harness.pending) == 1
    assert len(runs(db_session)) == 1
    harness.run_pending()
    assert len(patches) == 1


def test_two_simultaneous_due_calls_start_exactly_one_run(harness, campaign, db_session):
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=6)
    handler, patches = delivery_handler()
    harness.install(handler, {beitrag.audio_object_key: AUDIO})
    harness.mode = "deferred"
    barrier = threading.Barrier(2, timeout=5)

    def clock() -> datetime:
        barrier.wait()  # beide Anfragen erreichen die Sperre zur selben Zeit
        return berlin(2026, 12, 5, 20, 15)

    harness.app.dependency_overrides[get_now] = clock
    statuses: list[int] = []

    def post(source: str) -> None:
        response = harness.client.post(
            "/delivery/trigger", headers=SECRET | {"X-Trigger-Source": source}
        )
        statuses.append(response.status_code)

    threads = [threading.Thread(target=post, args=(s,)) for s in ("zeitplan", "extern")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(statuses) == [202, 202]
    assert len(harness.pending) == 1
    harness.run_pending()
    assert len(runs(db_session)) == 1
    assert harness.logins == 1
    assert len(patches) == 1


def test_real_background_thread_answers_before_run_finishes(harness, campaign, db_session):
    """Ohne ersetzten Starter: die Antwort kommt, waehrend der Lauf im
    eigenen Thread noch am Objektspeicher haengt."""
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=6)
    handler, patches = delivery_handler()
    harness.install(handler, {})
    release = threading.Event()

    class SlowStorage(FakeStorage):
        def get(self, key: str) -> bytes:
            release.wait(timeout=5)
            return AUDIO

    harness.app.dependency_overrides[get_storage] = lambda: SlowStorage({})
    del harness.app.dependency_overrides[get_run_starter]

    response = harness.call(berlin(2026, 12, 5, 20, 0))

    assert response.status_code == 202
    assert runs(db_session)[0].outcome == "gestartet"
    release.set()
    for thread in threading.enumerate():
        if thread.name.startswith("auslieferung-"):
            thread.join(timeout=5)
    assert runs(db_session)[0].outcome == "erfolg"
    assert len(patches) == 1


# --- Route: Verlauf -> erledigt, fehlgeschlagen, Wiederholung -----------------


def test_call_after_successful_eve_run_is_200_without_login_or_entry_and_one_summary(
    harness, campaign, db_session
):
    """U8/KTD11: der Lauf ist erledigt, seine Sammelmeldung noch offen (der
    eingesetzte Verlauf hat keine Abschlusspruefung ausgeloest) -- der Aufruf
    holt sie genau einmal nach, ohne neuen Lauf."""
    make_person_slot_beitrag(db_session, campaign, day=6)
    seed_run(
        db_session, campaign, "vorabend", 6, "erfolg", berlin(2026, 12, 5, 20, 0), date(2026, 12, 5)
    )

    response = harness.call(berlin(2026, 12, 5, 20, 30))

    assert response.status_code == 200
    assert harness.logins == 0
    assert len(runs(db_session)) == 1
    [mail] = FakeSMTP.sent
    assert "Abendmeldung" in mail["Subject"]
    assert harness.call(berlin(2026, 12, 5, 20, 45)).status_code == 200
    assert len(FakeSMTP.sent) == 1


def test_kontrolllauf_starts_once_checks_live_then_200(harness, campaign, tonie, db_session):
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=6)
    seed_run(
        db_session, campaign, "vorabend", 6, "erfolg", berlin(2026, 12, 5, 20, 0), date(2026, 12, 5)
    )
    tonie.verified_for_day = 6
    tonie.verified_beitrag_id = beitrag.id
    tonie.verified_chapter_id = "server-id-a"
    db_session.commit()
    handler, patches = build_handler(
        tonie_gets=[creative_tonie_response(chapters=[delivered_chapter()])], tonie_patches=[]
    )
    harness.install(handler, {beitrag.audio_object_key: AUDIO})

    first = harness.call(berlin(2026, 12, 5, 22, 0))
    second = harness.call(berlin(2026, 12, 5, 22, 5))

    assert (first.status_code, second.status_code) == (202, 200)
    control = [r for r in runs(db_session) if r.run_type == "kontrolllauf"]
    assert [(r.target_day, r.outcome) for r in control] == [(6, "erfolg")]
    assert harness.logins == 1  # einmal wirklich live geprueft
    assert patches == []


def test_failed_eve_run_blocks_retry_for_15_minutes(harness, campaign, db_session):
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=6)
    seed_run(
        db_session,
        campaign,
        "vorabend",
        6,
        "fehlschlag",
        berlin(2026, 12, 5, 20, 0),
        date(2026, 12, 5),
    )

    early = harness.call(berlin(2026, 12, 5, 20, 5))
    assert early.status_code == 502
    assert len(runs(db_session)) == 1
    assert harness.logins == 0

    handler, _ = delivery_handler()
    harness.install(handler, {beitrag.audio_object_key: AUDIO})
    retry = harness.call(berlin(2026, 12, 5, 20, 15))

    assert retry.status_code == 202
    assert [r.outcome for r in runs(db_session)] == ["fehlschlag", "erfolg"]


def test_after_three_failed_attempts_every_call_is_502_without_login(harness, campaign, db_session):
    make_person_slot_beitrag(db_session, campaign, day=6)
    for minute in (0, 15, 30):
        seed_run(
            db_session,
            campaign,
            "vorabend",
            6,
            "fehlschlag",
            berlin(2026, 12, 5, 20, minute),
            date(2026, 12, 5),
        )

    for at in (berlin(2026, 12, 5, 21, 0), berlin(2026, 12, 5, 21, 30)):
        assert harness.call(at).status_code == 502
    assert harness.logins == 0
    assert len(runs(db_session)) == 3


def test_orphaned_started_entry_counts_as_aborted_attempt(harness, campaign, db_session):
    """Ein Neustart mitten im Lauf hinterlaesst einen offenen Eintrag ohne
    gehaltene Sperre -- er zaehlt als gescheiterter Versuch."""
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=6)
    seed_run(
        db_session,
        campaign,
        "vorabend",
        6,
        "gestartet",
        berlin(2026, 12, 5, 20, 0),
        date(2026, 12, 5),
    )

    early = harness.call(berlin(2026, 12, 5, 20, 5))

    assert early.status_code == 502
    [orphan] = runs(db_session)
    assert orphan.outcome == "abgebrochen"
    assert "abgebrochen" in (orphan.reason or "").lower()

    handler, _ = delivery_handler()
    harness.install(handler, {beitrag.audio_object_key: AUDIO})
    assert harness.call(berlin(2026, 12, 5, 20, 15)).status_code == 202
    assert [r.outcome for r in runs(db_session)] == ["abgebrochen", "erfolg"]


def test_failing_thread_start_releases_lock_and_closes_entry(harness, campaign, db_session):
    """Scheitert schon der Start des Hintergrundlaufs, gibt niemand sonst die
    Sperre frei -- ohne Uebergabe bliebe jeder spaetere Aufruf bei "läuft"."""
    make_person_slot_beitrag(db_session, campaign, day=6)

    def broken_start(fn) -> None:
        raise RuntimeError("kein Thread mehr frei")

    harness.app.dependency_overrides[get_run_starter] = lambda: broken_start

    with pytest.raises(RuntimeError, match="kein Thread mehr frei"):
        harness.call(berlin(2026, 12, 5, 20, 0))

    lock = _lock_for(TONIE_ID)
    assert lock.acquire(blocking=False)
    lock.release()
    [entry] = runs(db_session)
    assert entry.outcome == "fehlschlag"
    assert "RuntimeError" in (entry.reason or "")
    assert harness.logins == 0


@pytest.mark.parametrize("manual_type", ["manuell", "anstoss"])
def test_eve_retry_does_not_overwrite_successful_manual_run(
    harness, campaign, db_session, manual_type
):
    """Plan (U5 Schritt 13, Key Decision manueller Lauf): ein erfolgreicher
    manueller Lauf nach dem Vorabend desselben Tages bleibt bis zum
    naechsten Abend -- auch eine Wiederholung des Vorabend-Laufs greift nicht ein."""
    make_person_slot_beitrag(db_session, campaign, day=6)
    seed_run(
        db_session,
        campaign,
        "vorabend",
        6,
        "fehlschlag",
        berlin(2026, 12, 5, 20, 0),
        date(2026, 12, 5),
    )
    seed_run(
        db_session,
        campaign,
        manual_type,
        6,
        "erfolg",
        berlin(2026, 12, 5, 20, 5),
        None,
    )

    response = harness.call(berlin(2026, 12, 5, 20, 20))

    assert response.status_code == 200
    assert harness.logins == 0
    assert [(r.run_type, r.outcome) for r in runs(db_session)] == [
        ("vorabend", "fehlschlag"),
        (manual_type, "erfolg"),
    ]


def test_run_holding_lock_longer_than_10_minutes_is_502_and_logged(
    harness, campaign, db_session, caplog
):
    make_person_slot_beitrag(db_session, campaign, day=6)
    seed_run(
        db_session,
        campaign,
        "vorabend",
        6,
        "gestartet",
        berlin(2026, 12, 5, 20, 0),
        date(2026, 12, 5),
    )
    caplog.set_level(logging.INFO, logger="app.delivery.trigger")
    lock = _lock_for(TONIE_ID)
    assert lock.acquire(blocking=False)
    try:
        running = harness.call(berlin(2026, 12, 5, 20, 9))
        hanging = harness.call(berlin(2026, 12, 5, 20, 11))
    finally:
        lock.release()

    assert running.status_code == 202
    assert hanging.status_code == 502
    assert any("Lauf hängt" in r.getMessage() for r in caplog.records)
    # Der haengende Lauf wird nicht als abgebrochen verbucht, solange er die Sperre haelt.
    assert [r.outcome for r in runs(db_session)] == ["gestartet"]
    assert harness.logins == 0


# --- Probelauf (R48) und Monatsgrenzen -----------------------------------------


def test_november_probelauf_with_replacement_leaves_tonie_and_verified_state(
    harness, campaign, tonie, db_session
):
    replacement = make_replacement(db_session, campaign)
    tonie.verified_for_day = 3
    tonie.verified_chapter_id = "chap-3"
    db_session.commit()
    # Kein GET/PATCH auf den Tonie erlaubt -- build_handler wirft sonst.
    handler, patches = build_handler(tonie_gets=[], tonie_patches=[])
    harness.install(handler, {replacement.audio_object_key: AUDIO})

    response = harness.call(berlin(2026, 11, 5, 20, 0))

    assert response.status_code == 202
    [entry] = runs(db_session)
    assert (entry.run_type, entry.target_day, entry.outcome) == ("probelauf", None, "erfolg")
    assert entry.evening == date(2026, 11, 5)
    assert entry.beitrag_ids == str(replacement.id)
    assert patches == []
    db_session.expire_all()
    refreshed = db_session.get(CreativeTonie, tonie.id)
    assert (refreshed.verified_for_day, refreshed.verified_chapter_id) == (3, "chap-3")
    assert len(FakeSMTP.sent) == 1
    assert "Probelauf" in FakeSMTP.sent[0]["Subject"]
    assert harness.call(berlin(2026, 11, 5, 20, 30)).status_code == 200


def test_november_probelauf_without_replacement_fails_with_named_reason(
    harness, campaign, db_session
):
    first = harness.call(berlin(2026, 11, 5, 20, 0))

    assert first.status_code == 202
    [entry] = runs(db_session)
    assert (entry.run_type, entry.outcome) == ("probelauf", "fehlschlag")
    assert "Ersatzbeitrag" in entry.reason
    assert harness.logins == 0
    assert len(FakeSMTP.sent) == 1
    assert harness.call(berlin(2026, 11, 5, 20, 5)).status_code == 502


def test_november_30_is_real_eve_run_for_day_one(harness, campaign, db_session):
    make_person_slot_beitrag(db_session, campaign, day=1)
    harness.mode = "deferred"

    assert harness.call(berlin(2026, 11, 30, 20, 0)).status_code == 202

    [entry] = runs(db_session)
    assert (entry.run_type, entry.target_day) == ("vorabend", 1)


def test_late_delivery_time_kontrolllauf_after_midnight_keeps_target_day(
    harness, campaign, db_session
):
    """Lieferzeit 23:00: der Kontrolllauf um 01:00 prueft Tag 6, nicht Tag 7."""
    harness.delivery_time(time(23, 0))
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=6)
    make_person_slot_beitrag(db_session, campaign, day=7)
    handler, _ = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=[]),
            creative_tonie_response(transcoding=False, chapters=[delivered_chapter()]),
            creative_tonie_response(chapters=[delivered_chapter()]),
        ],
        tonie_patches=[
            creative_tonie_response(
                transcoding=True,
                chapters=[{"id": "server-id-a", "title": "Tuerchen 6 Titel", "file": "file-a"}],
            )
        ],
    )
    harness.install(handler, {beitrag.audio_object_key: AUDIO})

    assert harness.call(berlin(2026, 12, 5, 23, 0)).status_code == 202
    assert harness.call(berlin(2026, 12, 6, 1, 0)).status_code == 202
    assert harness.call(berlin(2026, 12, 6, 1, 50)).status_code == 204

    assert [(r.run_type, r.target_day, r.outcome) for r in runs(db_session)] == [
        ("vorabend", 6, "erfolg"),
        ("kontrolllauf", 6, "erfolg"),
    ]


# --- U11: jeder Lauf endet in einem Verlaufseintrag und einer Meldungsentscheidung (KTD18)


EVE = berlin(2026, 12, 5, 20, 0)


def failing_on(method: str, url: str, exc: Exception, base):
    """Wie `base`, aber ein bestimmter Aufruf wirft `exc` (z. B. Netzwerkfehler)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == method and str(request.url) == url:
            raise exc
        return base(request)

    return handler


def assert_failed_with_report(db_session, *needles: str) -> DeliveryRun:
    [entry] = runs(db_session)
    assert entry.outcome == "fehlschlag"
    for needle in needles:
        assert needle in (entry.reason or "")
    assert not _lock_for(TONIE_ID).locked()
    assert len(FakeSMTP.sent) == 1
    assert "Fehlschlag" in FakeSMTP.sent[0]["Subject"]
    return entry


def test_login_rejected_closes_entry_as_failure_and_reports(harness, campaign, db_session):
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=6)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and str(request.url) == TOKEN_URL:
            return httpx.Response(401)
        return no_network(request)

    harness.install(handler, {beitrag.audio_object_key: AUDIO})

    assert harness.call(EVE).status_code == 202

    assert_failed_with_report(db_session, "LoginRejectedError", "HTTP 401")


def test_get_state_network_error_closes_entry_as_failure_and_reports(harness, campaign, db_session):
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=6)
    base, patches = build_handler(tonie_gets=[], tonie_patches=[])
    harness.install(
        failing_on("GET", TONIE_URL, httpx.ConnectError("Netz weg"), base),
        {beitrag.audio_object_key: AUDIO},
    )

    harness.call(EVE)

    assert_failed_with_report(db_session, "ConnectError", "Netz weg")
    assert patches == []


def test_storage_failure_closes_entry_as_failure_leaves_tonie_untouched(
    harness, campaign, db_session
):
    make_person_slot_beitrag(db_session, campaign, day=6)
    handler, patches = build_handler(
        tonie_gets=[creative_tonie_response(chapters=[])], tonie_patches=[]
    )
    harness.install(handler, {})  # der Speicher liefert die Audiodatei nicht

    harness.call(EVE)

    assert_failed_with_report(db_session, "KeyError")
    assert patches == []


def test_failed_restore_names_both_causes_and_unknown_tonie_state(harness, campaign, db_session):
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=6)
    baseline = [{"id": "old-id", "title": "Alter Beitrag", "file": "old-blob"}]
    handler, patches = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=baseline),
            creative_tonie_response(
                transcoding=False, chapters=[], transcoding_errors=[{"reason": "wrongFormat"}]
            ),
        ],
        tonie_patches=[
            creative_tonie_response(transcoding=True, chapters=[]),
            httpx.Response(500),  # das Zurueckspielen scheitert selbst
        ],
    )
    harness.install(handler, {beitrag.audio_object_key: AUDIO})

    harness.call(EVE)

    entry = assert_failed_with_report(db_session, "wrongFormat", "HTTPStatusError")
    assert "Tonie-Zustand unbekannt" in entry.reason
    assert "Tonie-Zustand unbekannt" in plain_text(FakeSMTP.sent[0])
    assert len(patches) == 2


class FailingSMTP(FakeSMTP):
    def send_message(self, message) -> None:
        raise OSError("SMTP-Server nicht erreichbar")


def test_mail_error_after_successful_eve_run_keeps_success_and_notes_it(
    harness, campaign, db_session, monkeypatch
):
    monkeypatch.setattr("smtplib.SMTP", FailingSMTP)
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=6)
    handler, _ = delivery_handler()
    harness.install(handler, {beitrag.audio_object_key: AUDIO})

    assert harness.call(EVE).status_code == 202

    [entry] = runs(db_session)
    assert entry.outcome == "erfolg"
    assert "Abendmeldung nicht verschickt" in (entry.reason or "")
    assert "SMTP-Server nicht erreichbar" in entry.reason
    assert harness.call(berlin(2026, 12, 5, 20, 5)).status_code == 200


def test_smtp_server_not_answering_aborts_after_timeout_and_run_ends(
    harness, campaign, db_session, monkeypatch
):
    """Ein SMTP-Server, der die Verbindung annimmt und schweigt, haelt den
    Lauf nicht fest: echtes smtplib gegen einen lokalen stummen Socket."""
    monkeypatch.setattr("smtplib.SMTP", REAL_SMTP)
    monkeypatch.setattr("app.mail.smtp.SMTP_TIMEOUT_SECONDS", 0.5)

    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(1)
    try:
        with harness.app.state.session_factory() as s:
            einstellungen = settings.get_einstellungen(s)
            einstellungen.smtp_host = "127.0.0.1"
            einstellungen.smtp_port = silent.getsockname()[1]
            s.commit()
        _, beitrag = make_person_slot_beitrag(db_session, campaign, day=6)
        handler, _ = delivery_handler()
        harness.install(handler, {beitrag.audio_object_key: AUDIO})

        started = time_module.monotonic()
        harness.call(EVE)
        elapsed = time_module.monotonic() - started
    finally:
        silent.close()

    assert elapsed < 5
    [entry] = runs(db_session)
    assert entry.outcome == "erfolg"
    assert "Abendmeldung nicht verschickt" in (entry.reason or "")
    assert not _lock_for(TONIE_ID).locked()


# --- Aufraeumlauf am 25.12. (U16, R51) -----------------------------------------


def test_christmas_evening_cleanup_runs_from_trigger_records_history_and_reports(
    harness, campaign, tonie, db_session
):
    tonie.app_chapters = '[{"id": "app-24", "seconds": 1.0}]'
    db_session.commit()
    app24 = {"id": "app-24", "title": "Tag 24", "file": "app-24"}
    stock = [{"id": "familie-1", "title": "Familie Eins", "file": "familie-1"}]
    handler, patches = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=[app24, *stock]),
            creative_tonie_response(chapters=stock),
        ],
        tonie_patches=[creative_tonie_response(chapters=stock)],
        file_ids=(),
    )
    harness.install(handler, {})

    response = harness.call(berlin(2026, 12, 25, 20, 0))

    assert response.status_code == 202
    [entry] = runs(db_session)
    assert (entry.run_type, entry.target_day, entry.outcome) == ("aufraeumen", None, "erfolg")
    assert entry.evening == date(2026, 12, 25)
    assert [c["id"] for c in patches[0]["chapters"]] == ["familie-1"]
    assert len(FakeSMTP.sent) == 1
    assert "Aufräumen nach dem Advent" in FakeSMTP.sent[0]["Subject"]
    # Ein spaeterer Takt desselben Abends startet keinen zweiten Lauf.
    assert harness.call(berlin(2026, 12, 25, 20, 30)).status_code == 200
    assert len(runs(db_session)) == 1


# --- Mehrkalender U7: Ausloesung je Tonie (KTD9, KTD10, R3, R24, R31, R38) -----------


@pytest.fixture
def tonie2(db_session, campaign) -> CreativeTonie:
    """Ein gespiegelter zweiter Tonie im selben Kalender."""
    _, tonie = make_calendar_tonie(db_session, tonie_id=TONIE_ID_2, campaign=campaign)
    yield tonie
    lock = _lock_for(TONIE_ID_2)
    if lock.locked():
        lock.release()


def install_two(harness, handler1, handler2, files: dict[str, bytes]) -> None:
    fake1, fake2 = make_client(handler1), make_client(handler2)
    harness.app.dependency_overrides[get_toniecloud_factory] = lambda: FixedFactory(
        fake1, {TONIE_ID_2: fake2}
    )
    harness.app.dependency_overrides[get_storage] = lambda: FakeStorage(files)


def eve_handler(tonie_id: str, chapter_id: str):
    new = {"id": chapter_id, "title": "Tuerchen 6 Titel", "file": chapter_id}
    return build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=[]),
            creative_tonie_response(chapters=[new]),
        ],
        tonie_patches=[creative_tonie_response(transcoding=True, chapters=[new])],
        tonie_id=tonie_id,
    )


def failing_first_file_post(base):
    """Der erste Upload (`POST /file`) scheitert, jeder weitere gelingt."""
    failed: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and str(request.url).endswith("/file") and not failed:
            failed.append(request)
            return httpx.Response(500)
        return base(request)

    return handler


def test_upload_failure_on_tonie_2_uses_replacement_there_only_ae1(
    harness, campaign, tonie2, db_session
):
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=6)
    replacement = make_replacement(db_session, campaign)
    h1, patches1 = eve_handler(TONIE_ID, "t1-tag")
    ersatz = {"id": "t2-ersatz", "title": "Ersatzgeschichte", "file": "t2-ersatz"}
    base2, patches2 = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=[]),
            creative_tonie_response(chapters=[ersatz]),
        ],
        tonie_patches=[creative_tonie_response(transcoding=True, chapters=[ersatz])],
        tonie_id=TONIE_ID_2,
    )
    install_two(
        harness,
        h1,
        failing_first_file_post(base2),
        {beitrag.audio_object_key: AUDIO, replacement.audio_object_key: AUDIO},
    )

    assert harness.call(EVE).status_code == 202

    entries = {r.tonie_id: r for r in runs(db_session)}
    assert set(entries) == {TONIE_ID, TONIE_ID_2}
    assert (entries[TONIE_ID].outcome, entries[TONIE_ID].beitrag_ids) == ("erfolg", str(beitrag.id))
    assert (entries[TONIE_ID_2].outcome, entries[TONIE_ID_2].beitrag_ids) == (
        "ersatzbeitrag",
        str(replacement.id),
    )
    assert [c["title"] for c in patches1[0]["chapters"]] == ["Tuerchen 6 Titel"]
    assert [c["title"] for c in patches2[0]["chapters"]] == ["Ersatzgeschichte"]
    assert harness.call(berlin(2026, 12, 5, 20, 30)).status_code == 200


def _hold(tonie_id: str):
    lock = _lock_for(tonie_id)
    assert lock.acquire(blocking=False)
    return lock


@pytest.mark.parametrize(
    ("tonie2_history", "expected"),
    [("drei Fehlschlaege", 502), ("erfolg", 202)],
)
def test_worst_state_of_running_and_other_tonie(
    harness, campaign, tonie2, db_session, tonie2_history, expected
):
    """KTD9: laufend + gescheitert -> 502, laufend + erledigt -> 202."""
    make_person_slot_beitrag(db_session, campaign, day=6)
    outcomes = ["fehlschlag"] * 3 if tonie2_history == "drei Fehlschlaege" else ["erfolg"]
    for minute, outcome in zip((0, 15, 30), outcomes):
        seed_run(
            db_session,
            campaign,
            "vorabend",
            6,
            outcome,
            berlin(2026, 12, 5, 20, minute),
            date(2026, 12, 5),
            tonie_id=TONIE_ID_2,
        )
    lock = _hold(TONIE_ID)
    try:
        response = harness.call(berlin(2026, 12, 5, 21, 0))
    finally:
        lock.release()

    assert response.status_code == expected
    assert harness.logins == 0


def test_all_tonies_done_is_200(harness, campaign, tonie2, db_session):
    for tonie_id in (TONIE_ID, TONIE_ID_2):
        seed_run(
            db_session,
            campaign,
            "vorabend",
            6,
            "erfolg",
            berlin(2026, 12, 5, 20, 0),
            date(2026, 12, 5),
            tonie_id=tonie_id,
        )

    assert harness.call(berlin(2026, 12, 5, 20, 30)).status_code == 200
    assert harness.logins == 0


def test_three_failures_on_tonie_2_do_not_block_tonie_1(harness, campaign, tonie2, db_session):
    """Versuchszaehler je Tonie (KTD10): Tonie 1 startet trotzdem."""
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=6)
    for minute in (0, 15, 30):
        seed_run(
            db_session,
            campaign,
            "vorabend",
            6,
            "fehlschlag",
            berlin(2026, 12, 5, 20, minute),
            date(2026, 12, 5),
            tonie_id=TONIE_ID_2,
        )
    harness.mode = "deferred"

    response = harness.call(berlin(2026, 12, 5, 21, 0))

    assert response.status_code == 502  # schlechtester Zustand: Tonie 2
    started = [r for r in runs(db_session) if r.outcome == "gestartet"]
    assert [(r.tonie_id, r.run_type, r.target_day) for r in started] == [(TONIE_ID, "vorabend", 6)]
    assert len(harness.pending) == 1


def test_kontrolllauf_target_day_comes_from_own_tonie_vorabend(
    harness, campaign, tonie2, db_session
):
    """Kontrolllauf je Tonie: der Vorabend-Lauf von Tonie 1 zaehlt nicht fuer Tonie 2."""
    make_person_slot_beitrag(db_session, campaign, day=6)
    seed_run(
        db_session,
        campaign,
        "vorabend",
        6,
        "erfolg",
        berlin(2026, 12, 5, 20, 0),
        date(2026, 12, 5),
    )
    seed_run(
        db_session,
        campaign,
        "kontrolllauf",
        6,
        "erfolg",
        berlin(2026, 12, 5, 22, 0),
        date(2026, 12, 5),
    )
    harness.mode = "deferred"

    response = harness.call(berlin(2026, 12, 5, 22, 5))

    assert response.status_code == 202
    [started] = [r for r in runs(db_session) if r.outcome == "gestartet"]
    assert (started.tonie_id, started.run_type, started.target_day) == (
        TONIE_ID_2,
        "kontrolllauf",
        6,
    )


def test_run_of_tonie_1_holds_its_lock_while_tonie_2_starts(harness, campaign, tonie2, db_session):
    make_person_slot_beitrag(db_session, campaign, day=6)
    harness.mode = "deferred"
    lock = _hold(TONIE_ID)
    try:
        response = harness.call(EVE)
        assert _lock_for(TONIE_ID_2).locked()  # an den zurueckgestellten Lauf uebergeben
    finally:
        lock.release()

    assert response.status_code == 202
    assert [(r.tonie_id, r.outcome) for r in runs(db_session)] == [(TONIE_ID_2, "gestartet")]


def test_calendar_without_tonie_starts_nothing(harness, db_session):
    """R38: kein Lauf, kein Verlaufseintrag, keine Meldung."""
    calendar = Campaign(name="Ohne Tonie")
    db_session.add(calendar)
    db_session.commit()
    make_person_slot_beitrag(db_session, calendar, day=6)
    make_replacement(db_session, calendar)

    assert harness.call(EVE).status_code == 204
    assert harness.call(berlin(2026, 11, 5, 20, 0)).status_code == 204
    assert runs(db_session) == []
    assert FakeSMTP.sent == []


def test_christmas_cleanup_on_detached_tonie_with_abraeumen_offen(harness, db_session):
    """R37: das beim Trennen gescheiterte Abraeumen holt der 25.12. nach,
    obwohl der Tonie keinen Kalender mehr hat. Ein Tonie ohne Kalender und
    ohne Kennzeichen (AE13) bleibt unberuehrt."""
    db_session.add(Campaign())
    db_session.add_all(
        [
            CreativeTonie(
                tonie_id=TONIE_ID,
                app_chapters='[{"id": "app-24", "seconds": 1.0}]',
                abraeumen_offen=True,
            ),
            CreativeTonie(tonie_id=TONIE_ID_2, app_chapters='[{"id": "frei-1", "seconds": 1.0}]'),
        ]
    )
    db_session.commit()
    app24 = {"id": "app-24", "title": "Tag 24", "file": "app-24"}
    stock = [{"id": "familie-1", "title": "Familie Eins", "file": "familie-1"}]
    handler, patches = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=[app24, *stock]),
            creative_tonie_response(chapters=stock),
        ],
        tonie_patches=[creative_tonie_response(chapters=stock)],
        file_ids=(),
    )
    install_two(harness, handler, no_network, {})

    assert harness.call(berlin(2026, 12, 25, 20, 0)).status_code == 202

    [entry] = runs(db_session)
    assert (entry.tonie_id, entry.run_type, entry.outcome) == (TONIE_ID, "aufraeumen", "erfolg")
    assert [c["id"] for c in patches[0]["chapters"]] == ["familie-1"]
    db_session.expire_all()
    cleaned = db_session.execute(
        select(CreativeTonie).where(CreativeTonie.tonie_id == TONIE_ID)
    ).scalar_one()
    assert (cleaned.app_chapters, cleaned.abraeumen_offen) == (None, False)
    kept = db_session.execute(
        select(CreativeTonie).where(CreativeTonie.tonie_id == TONIE_ID_2)
    ).scalar_one()
    assert kept.app_chapters == '[{"id": "frei-1", "seconds": 1.0}]'
    assert harness.call(berlin(2026, 12, 25, 20, 30)).status_code == 200


def test_detach_failure_then_christmas_cleanup_removes_chapter(
    harness, campaign, tonie, db_session
):
    """Ganzer Weg: Trennen, Abraeumen scheitert, der 25.12. entfernt das Kapitel."""
    from app.delivery.job import detach_tonie

    tonie.app_chapters = '[{"id": "app-5", "seconds": 1.0}]'
    db_session.commit()
    app5 = {"id": "app-5", "title": "Tag 5", "file": "app-5"}
    failing, _ = build_handler(
        tonie_gets=[creative_tonie_response(chapters=[app5])],
        tonie_patches=[httpx.Response(500), creative_tonie_response(chapters=[app5])],
        file_ids=(),
    )
    assert detach_tonie(db_session, make_client(failing), tonie).success is False
    handler, patches = build_handler(
        tonie_gets=[creative_tonie_response(chapters=[app5]), creative_tonie_response(chapters=[])],
        tonie_patches=[creative_tonie_response(chapters=[])],
        file_ids=(),
    )
    harness.install(handler, {})

    assert harness.call(berlin(2026, 12, 25, 20, 0)).status_code == 202

    assert [(r.run_type, r.outcome) for r in runs(db_session)] == [
        ("abraeumen", "fehlschlag"),
        ("aufraeumen", "erfolg"),
    ]
    assert patches[0]["chapters"] == []
    db_session.expire_all()
    refreshed = db_session.get(CreativeTonie, tonie.id)
    assert (refreshed.app_chapters, refreshed.abraeumen_offen) == (None, False)


def test_delivery_time_comes_from_settings_without_restart(harness, campaign, db_session):
    make_person_slot_beitrag(db_session, campaign, day=6)
    harness.mode = "deferred"
    harness.delivery_time(time(21, 0))

    assert harness.call(berlin(2026, 12, 5, 20, 30)).status_code == 204
    assert harness.call(berlin(2026, 12, 5, 21, 0)).status_code == 202


def _konto(harness, username: str, password: str) -> int:
    with harness.app.state.session_factory() as s:
        return settings.create_konto(
            s,
            harness.config.credentials_key,
            username=username,
            password=password,
            now=berlin(2026, 10, 1, 12, 0),
        ).id


def test_konto_needing_reentry_fails_with_named_reason_and_502(harness, tonie, db_session):
    from app.toniecloud.client import TonieCloudFactory

    make_person_slot_beitrag(db_session, tonie.campaign, day=6)
    konto_id = _konto(harness, "familie@example.test", "pw-alt")
    with harness.app.state.session_factory() as s:
        s.get(TonieKonto, konto_id).needs_reentry = True
        s.get(CreativeTonie, tonie.id).konto_id = konto_id
        s.commit()
    factory = TonieCloudFactory(
        harness.config.credentials_key, transport=httpx.MockTransport(no_network)
    )
    harness.app.dependency_overrides[get_toniecloud_factory] = lambda: factory

    assert harness.call(EVE).status_code == 502

    [entry] = runs(db_session)
    assert (entry.tonie_id, entry.outcome) == (TONIE_ID, "fehlschlag")
    assert "neu eingegeben" in entry.reason
    assert len(FakeSMTP.sent) == 1
    assert not _lock_for(TONIE_ID).locked()


def test_password_change_during_run_reaches_only_the_next_run(harness, tonie, db_session):
    """R24: der Lauf arbeitet mit den Zugangsdaten seines Starts."""
    from app.toniecloud.client import TonieCloudFactory

    _, beitrag = make_person_slot_beitrag(db_session, tonie.campaign, day=6)
    konto_id = _konto(harness, "familie@example.test", "pw-alt")
    with harness.app.state.session_factory() as s:
        s.get(CreativeTonie, tonie.id).konto_id = konto_id
        s.commit()
    delivered = delivered_chapter()
    inner, _patches = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=[]),
            creative_tonie_response(chapters=[delivered]),
            creative_tonie_response(chapters=[delivered]),  # Kontrolllauf
        ],
        tonie_patches=[creative_tonie_response(transcoding=True, chapters=[delivered])],
    )
    passwords: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and str(request.url) == TOKEN_URL:
            body = request.content.decode()
            passwords.append("neu" if "pw-neu" in body else "alt")
        return inner(request)

    factory = TonieCloudFactory(
        harness.config.credentials_key,
        transport=httpx.MockTransport(handler),
        sleep=lambda s: None,
    )
    harness.app.dependency_overrides[get_toniecloud_factory] = lambda: factory
    harness.app.dependency_overrides[get_storage] = lambda: FakeStorage(
        {beitrag.audio_object_key: AUDIO}
    )
    harness.mode = "deferred"

    assert harness.call(EVE).status_code == 202
    with harness.app.state.session_factory() as s:
        settings.set_konto_password(s, harness.config.credentials_key, konto_id, "pw-neu", now=EVE)
    harness.run_pending()
    assert [r.outcome for r in runs(db_session)] == ["erfolg"]

    harness.mode = "inline"
    assert harness.call(berlin(2026, 12, 5, 22, 0)).status_code == 202

    assert passwords == ["alt", "neu"]
    assert [r.outcome for r in runs(db_session)] == ["erfolg", "erfolg"]
