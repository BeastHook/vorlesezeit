"""U12: SQLite-Nebenlaeufigkeit (KTD16).

Charakterisierung zuerst: eine Einreichung und ein Auslieferungslauf
speichern gleichzeitig -- ueber die echten Codepfade (Aufnahme-Route mit
echtem ffmpeg + S3-Speicher, Ausloese-Route mit echtem Hintergrund-Thread und
eigener Sitzung), gegen die temporaere Datei-Datenbank aus conftest.py. Nur
die Toniecloud ist ein Fake, und der haelt absichtlich an.

Die Sonde `_assert_no_open_write_transaction` prueft bei jedem
Toniecloud-Aufruf ueber eine fremde Verbindung, dass der Lauf keine
Transaktion offen haelt (U12 Schritt 3). `BEGIN EXCLUSIVE` steht dort nur im
Test, nie in der App (KTD16).
"""

from __future__ import annotations

import sqlite3
import threading
import time as time_module

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.tokens import create_magic_link_token
from app.db import create_db_engine, init_db, make_session_factory
from app.delivery.job import _lock_for
from app.delivery.trigger import get_now, get_storage, get_toniecloud_factory
from app.models import Auftrag, Beitrag, Campaign, DeliveryRun, Person, Slot
from app.recording import views
from tests.fixtures.audio import make_tone_webm
from tests.test_delivery import (
    AUDIO,
    TONIE_ID,
    TONIE_ID_2,
    FakeStorage,
    FixedFactory,
    make_calendar_tonie,
    make_client,
    make_person_slot_beitrag,
)
from tests.test_trigger import SECRET, FakeSMTP, berlin, delivery_handler, eve_handler

# Laenger als die fruehere Standard-Wartefrist des Treibers (5 s): haelt ein
# Lauf eine Sperre ueber den Toniecloud-Aufruf, scheitert die Einreichung.
HOLD_SECONDS = 6
# "Ohne Wartezeit": deutlich unter jeder Sperrfrist.
PROMPT_SECONDS = 2


def _assert_no_open_write_transaction(database_path: str) -> None:
    probe = sqlite3.connect(database_path, timeout=1, isolation_level=None)
    try:
        probe.execute("BEGIN EXCLUSIVE")
        probe.execute("ROLLBACK")
    finally:
        probe.close()


@pytest.fixture
def delivery_app(client, config, monkeypatch):
    """Ausloese-Route mit echtem Hintergrund-Thread; Uhr und Toniecloud
    ersetzt, SMTP ersetzt (keine echte Mail)."""
    FakeSMTP.sent = []
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    app = client.app
    app.dependency_overrides[get_now] = lambda: berlin(2026, 12, 5, 20, 0)
    yield app
    app.dependency_overrides.clear()
    lock = _lock_for(TONIE_ID)
    if lock.locked():
        lock.release()


def _install_toniecloud(app, config, handler, files: dict[str, bytes]) -> None:
    fake = make_client(handler)
    app.dependency_overrides[get_toniecloud_factory] = lambda: FixedFactory(fake)
    app.dependency_overrides[get_storage] = lambda: FakeStorage(files)


def _join_delivery_threads() -> None:
    for thread in threading.enumerate():
        if thread.name.startswith("auslieferung-"):
            thread.join(timeout=15)


def _setup(db_session) -> tuple[Beitrag, Auftrag]:
    """Kalender mit Tag 6 (freigegeben, wird ausgeliefert) und einem Auftrag
    an Tag 7 fuer die eingeloggte Verwandte aus `person_client`."""
    campaign, _tonie = make_calendar_tonie(db_session)
    _, beitrag6 = make_person_slot_beitrag(db_session, campaign, day=6)
    person = db_session.execute(
        select(Person).where(Person.email == "verwandte@example.test")
    ).scalar_one()
    auftrag7 = Auftrag(person_id=person.id)
    db_session.add(Slot(campaign_id=campaign.id, day=7, auftrag=auftrag7))
    db_session.commit()
    return beitrag6, auftrag7


def _submit(person_client: TestClient, auftrag: Auftrag, title: str = "Gleichzeitig"):
    return person_client.post(
        f"/record/auftrag/{auftrag.id}",
        files={"audio": ("aufnahme.webm", make_tone_webm(seconds=1.0), "audio/webm")},
        data={"title": title, "reported_type": "audio/webm"},
    )


def _entries(db_session) -> list[DeliveryRun]:
    db_session.expire_all()
    return list(db_session.execute(select(DeliveryRun).order_by(DeliveryRun.id)).scalars())


def test_submission_during_slow_delivery_run_both_persist(
    person_client, delivery_app, config, db_session
):
    """Plan-Szenario 1: die Einreichung committet, waehrend der Lauf mitten
    im Toniecloud-Aufruf haengt; danach schliesst der Lauf seinen Eintrag."""
    beitrag6, auftrag7 = _setup(db_session)
    inner, _patches = delivery_handler()
    in_toniecloud = threading.Event()
    release = threading.Event()
    probe_errors: list[Exception] = []

    def slow_handler(request: httpx.Request) -> httpx.Response:
        try:
            _assert_no_open_write_transaction(config.database_path)
        except sqlite3.OperationalError as exc:
            probe_errors.append(exc)
        if request.method == "PATCH":
            in_toniecloud.set()
            release.wait(timeout=HOLD_SECONDS)
        return inner(request)

    _install_toniecloud(delivery_app, config, slow_handler, {beitrag6.audio_object_key: AUDIO})

    response = person_client.post("/delivery/trigger", headers=SECRET)
    assert response.status_code == 202
    assert in_toniecloud.wait(timeout=10)

    submission = _submit(person_client, auftrag7)

    release.set()
    _join_delivery_threads()

    assert submission.status_code == 200, submission.text
    assert probe_errors == []
    db_session.expire_all()
    assert (
        db_session.execute(select(Beitrag).where(Beitrag.auftrag_id == auftrag7.id))
        .scalar_one()
        .title
        == "Gleichzeitig"
    )
    assert [e.outcome for e in _entries(db_session)] == ["erfolg"]


def test_slow_normalization_does_not_block_page_load_or_trigger(
    person_client, delivery_app, config, db_session, monkeypatch
):
    """Plan-Szenario 3: waehrend eine Einreichung in der Normalisierung
    steckt, antworten eine Seitenanfrage einer anderen Person und die
    Ausloesung sofort, und der Lauf schliesst seinen Eintrag ab."""
    beitrag6, auftrag7 = _setup(db_session)
    inner, _patches = delivery_handler()
    _install_toniecloud(delivery_app, config, inner, {beitrag6.audio_object_key: AUDIO})

    normalizing = threading.Event()
    release = threading.Event()
    real_normalize = views.normalize_recording

    def slow_normalize(raw: bytes) -> bytes:
        normalizing.set()
        release.wait(timeout=HOLD_SECONDS)
        return real_normalize(raw)

    monkeypatch.setattr(views, "normalize_recording", slow_normalize)

    other = Person(email="andere@example.test", display_name="Andere")
    db_session.add(other)
    db_session.commit()
    other_client = TestClient(delivery_app)
    other_client.post("/login/confirm", data={"token": create_magic_link_token(config, other)})

    results: dict[str, httpx.Response] = {}
    submitter = threading.Thread(
        target=lambda: results.update(sub=_submit(person_client, auftrag7))
    )
    submitter.start()
    assert normalizing.wait(timeout=10)

    started = time_module.monotonic()
    page = other_client.get("/record/campaign")
    page_seconds = time_module.monotonic() - started

    started = time_module.monotonic()
    trigger = person_client.post("/delivery/trigger", headers=SECRET)
    trigger_seconds = time_module.monotonic() - started
    _join_delivery_threads()

    assert submitter.is_alive(), "Normalisierung sollte noch haengen"
    release.set()
    submitter.join(timeout=15)

    assert page.status_code == 200
    assert page_seconds < PROMPT_SECONDS
    assert trigger.status_code == 202
    assert trigger_seconds < PROMPT_SECONDS
    assert results["sub"].status_code == 200, results["sub"].text
    assert [e.outcome for e in _entries(db_session)] == ["erfolg"]
    db_session.expire_all()
    assert db_session.execute(select(Beitrag).where(Beitrag.auftrag_id == auftrag7.id)).scalar_one()


def test_read_then_foreign_commit_then_write_succeeds(config):
    """Plan-Szenario 2: der Treiber oeffnet beim Lesen keine Transaktion --
    Sitzung A liest, B schreibt und committet, A schreibt. Haelt fest, dass
    das so bleibt (keine geaenderte Transaktionsbehandlung, KTD16)."""
    engine = create_db_engine(config)
    init_db(engine)
    factory = make_session_factory(engine)
    campaign_id = None
    with factory() as setup:
        campaign = Campaign()
        setup.add(campaign)
        setup.commit()
        campaign_id = campaign.id

    with factory() as session_a, factory() as session_b:
        session_a.execute(select(Campaign)).scalars().all()
        assert not session_a.connection().connection.dbapi_connection.in_transaction

        session_b.add(Person(email="b@example.test", display_name="B"))
        session_b.commit()

        session_a.get(Campaign, campaign_id).creative_tonie_id = TONIE_ID
        session_a.commit()

    with factory() as check:
        assert check.get(Campaign, campaign_id).creative_tonie_id == TONIE_ID
        assert check.execute(select(Person).where(Person.email == "b@example.test")).scalar_one()
    engine.dispose()


def test_database_uses_wal_and_every_connection_waits_30_seconds(config):
    """Plan-Szenario 4: WAL liegt auf der Datei, die Wartefrist auf jeder
    Verbindung."""
    engine = create_db_engine(config)
    init_db(engine)
    with engine.connect() as first, engine.connect() as second:
        for conn in (first, second):
            assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 30000
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
    engine.dispose()


def test_submission_while_runs_of_two_tonies_block_both_persist(
    person_client, delivery_app, config, db_session
):
    """Mehrkalender U7 (KTD9): zwei Tonies laufen parallel in eigenen Threads
    und haengen beide im Toniecloud-Aufruf -- die Einreichung kommt durch,
    beide Laeufe schliessen danach ihren Eintrag ab."""
    beitrag6, auftrag7 = _setup(db_session)
    campaign = db_session.get(Slot, beitrag6.slot_id).campaign
    make_calendar_tonie(db_session, tonie_id=TONIE_ID_2, campaign=campaign)
    release = threading.Event()
    in_patch = threading.Barrier(3, timeout=10)
    probe_errors: list[Exception] = []

    def blocking(inner):
        def handler(request: httpx.Request) -> httpx.Response:
            try:
                _assert_no_open_write_transaction(config.database_path)
            except sqlite3.OperationalError as exc:
                probe_errors.append(exc)
            if request.method == "PATCH":
                in_patch.wait()
                release.wait(timeout=HOLD_SECONDS)
            return inner(request)

        return handler

    h1, _ = eve_handler(TONIE_ID, "t1")
    h2, _ = eve_handler(TONIE_ID_2, "t2")
    fake1, fake2 = make_client(blocking(h1)), make_client(blocking(h2))
    delivery_app.dependency_overrides[get_toniecloud_factory] = lambda: FixedFactory(
        fake1, {TONIE_ID_2: fake2}
    )
    delivery_app.dependency_overrides[get_storage] = lambda: FakeStorage(
        {beitrag6.audio_object_key: AUDIO}
    )
    try:
        assert person_client.post("/delivery/trigger", headers=SECRET).status_code == 202
        in_patch.wait()  # beide Laeufe stecken jetzt gleichzeitig im PATCH

        started = time_module.monotonic()
        submission = _submit(person_client, auftrag7)
        submit_seconds = time_module.monotonic() - started
    finally:
        release.set()
        _join_delivery_threads()
        lock = _lock_for(TONIE_ID_2)
        if lock.locked():
            lock.release()

    assert submission.status_code == 200, submission.text
    assert submit_seconds < HOLD_SECONDS
    assert probe_errors == []
    assert sorted((e.tonie_id, e.outcome) for e in _entries(db_session)) == [
        (TONIE_ID, "erfolg"),
        (TONIE_ID_2, "erfolg"),
    ]
