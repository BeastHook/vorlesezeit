"""U8 Schritte 5 und 11: Admin-Reiter Auslieferung -- manueller Anstoss,
Trockenlauf, manueller Mehrkapitel-Lauf, Verlauf. Nur Fakes gegen die
Toniecloud (httpx.MockTransport), nie das echte Konto.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy.orm import Session

from app.admin.state import delivery_has_run
from app.delivery.job import _lock_for, run_delivery
from app.delivery.trigger import get_run_starter, get_storage, get_toniecloud_factory
from app.models import Beitrag, Campaign, CreativeTonie, DeliveryRun, Person
from app.toniecloud.client import TonieCloudClient
from tests.test_delivery import (
    AUDIO,
    STOCK,
    TONIE_ID,
    TONIE_ID_2,
    FakeStorage,
    FixedFactory,
    build_handler,
    config_response_with,
    creative_tonie_response,
    make_calendar_tonie,
    make_client,
    make_person_slot_beitrag,
)
from tests.test_trigger import FakeSMTP


def _no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"Unerwarteter Toniecloud-Aufruf: {request.method} {request.url}")


@pytest.fixture
def campaign(db_session: Session) -> Campaign:
    campaign, _tonie = make_calendar_tonie(db_session)
    return campaign


@pytest.fixture
def tonie(db_session: Session, campaign) -> CreativeTonie:
    return db_session.query(CreativeTonie).filter_by(tonie_id=TONIE_ID).one()


@pytest.fixture
def fake_tonie(admin_client, config, monkeypatch):
    """Setzt Fake-Client und Fake-Storage als Dependency-Override. Der
    Hintergrundlauf (U10) laeuft hier sofort im selben Thread, damit sein
    Ergebnis beim Folgen der Weiterleitung schon im Verlauf steht. Seit U11
    melden sich auch Admin-Laeufe per Mail -- nie echter SMTP-Versand."""
    app = admin_client.app
    FakeSMTP.sent = []
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)

    def install(handler, files: dict[str, bytes]) -> TonieCloudClient:
        fake_client = make_client(handler)
        app.dependency_overrides[get_toniecloud_factory] = lambda: FixedFactory(fake_client)
        app.dependency_overrides[get_storage] = lambda: FakeStorage(files)
        app.dependency_overrides[get_run_starter] = lambda: lambda run: run()
        return fake_client

    yield install
    app.dependency_overrides.clear()


def make_free_beitrag(
    db_session: Session, *, title: str, approved: bool = True, email: str = "frei@example.test"
) -> Beitrag:
    person = Person(email=email, display_name="Uropa Willi")
    db_session.add(person)
    db_session.flush()
    beitrag = Beitrag(
        person_id=person.id,
        title=title,
        audio_object_key=f"audio/frei-{title}.mp3",
        approved_at=datetime(2026, 11, 20) if approved else None,
    )
    db_session.add(beitrag)
    db_session.commit()
    db_session.refresh(beitrag)
    return beitrag


def processed(chapters: list[dict]) -> httpx.Response:
    return creative_tonie_response(transcoding=False, chapters=chapters)


def patched(chapters: list[dict]) -> httpx.Response:
    return creative_tonie_response(transcoding=True, chapters=chapters)


# --- Zugriff ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("get", "/admin/auslieferung"),
        ("post", "/admin/auslieferung/jetzt"),
        ("post", "/admin/auslieferung/trockenlauf"),
        ("post", "/admin/auslieferung/manuell"),
    ],
)
def test_non_admin_gets_403(person_client, method, url):
    response = getattr(person_client, method)(url)
    assert response.status_code == 403


def test_page_without_campaign_redirects_to_admin(admin_client):
    response = admin_client.get("/admin/auslieferung", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"


# --- Seite ----------------------------------------------------------------------


def test_page_shows_panels_and_r45_limit(admin_client, campaign):
    response = admin_client.get("/admin/auslieferung")

    assert response.status_code == 200
    text = response.text
    assert "täglich 20:00 (aus den Einstellungen, R46)" in text
    assert "nicht die Verarbeitung auf dem Tonie" in text
    assert "Ersatzbeitrag (R21)" in text
    assert "keiner hinterlegt" in text
    # Mehrkalender U10: die Verknuepfung lebt im Setup; der Tonie erscheint
    # ohne Namen nur maskiert (Umschalter und Geltungsbereich).
    assert "Tonie-Konto" not in text
    assert 'action="/admin/campaign/creative-tonie"' not in text
    assert "••••" + TONIE_ID[-4:] in text
    assert TONIE_ID not in text
    assert "Manueller Lauf mit mehreren Beiträgen (R20, R42)" in text
    assert "Verlauf" in text


def test_page_points_to_setup_when_no_tonie_linked(admin_client, db_session):
    db_session.add(Campaign())
    db_session.commit()

    text = admin_client.get("/admin/auslieferung").text

    assert "bespielt keinen Tonie" in text
    assert 'href="/admin/setup"' in text.split("</nav>", 1)[1]
    assert 'name="creative_tonie_id"' not in text
    assert admin_client.post(
        "/admin/campaign/creative-tonie", data={"creative_tonie_id": "x"}
    ).status_code in (404, 405)


def test_page_lists_only_approved_non_detached_beitraege_for_manual_run(
    admin_client, db_session, campaign
):
    _, approved = make_person_slot_beitrag(db_session, campaign, day=7)
    _, unapproved = make_person_slot_beitrag(db_session, campaign, day=8, approved=False)
    free = make_free_beitrag(db_session, title="Hallo aus Leipzig")
    detached = make_free_beitrag(db_session, title="Abgeloest", email="d@example.test")
    detached.detached_at = datetime(2026, 11, 21)
    db_session.commit()

    text = admin_client.get("/admin/auslieferung").text

    assert f'name="order_{approved.id}"' in text
    assert f'name="order_{free.id}"' in text
    assert f'name="order_{unapproved.id}"' not in text
    assert f'name="order_{detached.id}"' not in text
    assert "Tag 7" in text
    assert "frei" in text


def test_page_texts_describe_stock_behind_app_chapter(admin_client, campaign, monkeypatch):
    advent = datetime(2026, 12, 4, 12, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    monkeypatch.setattr("app.admin.deliveries.berlin_now", lambda request: advent)

    text = admin_client.get("/admin/auslieferung").text

    assert (
        "Der Lauf tauscht das Kapitel der App an Platz 1 aus. "
        "Alles andere auf dem Tonie bleibt dahinter."
    ) in text
    assert "ersetzt das heutige Kapitel vollständig" not in text
    assert (
        "Ankreuzen und Reihenfolge festlegen. Die Kapitel kommen vor den Bestand. "
        "Der nächste Vorabend-Lauf tauscht sie wieder gegen die Geschichte des Tages."
    ) in text
    assert "genau ein Kapitel zurück" not in text


# --- Platz auf dem Tonie (U16, R50) ---------------------------------------------


def _space_panel(text: str) -> str:
    start = text.index("Platz auf dem Tonie (R50)")
    return text[start : text.index("Platz prüfen", start)]


def test_deliveries_page_without_platz_does_not_call_toniecloud(admin_client, campaign, fake_tonie):
    fake_tonie(_no_network, {})

    response = admin_client.get("/admin/auslieferung")

    assert response.status_code == 200
    assert "Liest den Tonie und prüft alle freigegebenen Beiträge." in _space_panel(response.text)
    assert 'href="/admin/auslieferung?platz=1">Platz prüfen</a>' in response.text


def test_deliveries_page_platz_shows_stock_and_marks_too_long_beitrag(
    admin_client, db_session, campaign, fake_tonie
):
    beitrag = make_free_beitrag(db_session, title="Lang")
    handler, patches = build_handler(
        tonie_gets=[creative_tonie_response(chapters=STOCK, seconds_present=100.0)],
        tonie_patches=[],
        file_ids=(),
        config=config_response_with(max_seconds=100.5),
    )
    fake_tonie(handler, {beitrag.audio_object_key: AUDIO})

    response = admin_client.get("/admin/auslieferung?platz=1")

    panel = _space_panel(response.text)
    assert "Frei: 0,0 Min." in panel
    assert "Bestand: 2 Kapitel, 1,7 Min." in panel
    assert "Diese Beiträge passen nicht hinein:" in panel
    assert "Lang" in panel
    assert patches == []


def test_deliveries_page_platz_all_fit(admin_client, db_session, campaign, fake_tonie):
    beitrag = make_free_beitrag(db_session, title="Kurz")
    handler, _patches = build_handler(
        tonie_gets=[creative_tonie_response(chapters=STOCK, seconds_present=100.0)],
        tonie_patches=[],
        file_ids=(),
    )
    fake_tonie(handler, {beitrag.audio_object_key: AUDIO})

    response = admin_client.get("/admin/auslieferung?platz=1")

    panel = _space_panel(response.text)
    assert "Bestand: 2 Kapitel, 1,7 Min." in panel
    assert "Alle freigegebenen Beiträge passen." in panel
    assert "passen nicht hinein" not in panel


def test_deliveries_page_platz_with_toniecloud_down_shows_message(
    admin_client, campaign, fake_tonie
):
    fake_tonie(lambda request: httpx.Response(503), {})

    response = admin_client.get("/admin/auslieferung?platz=1")

    assert response.status_code == 200
    assert (
        "Platz konnte nicht geprüft werden. Die Toniecloud ist gerade nicht erreichbar."
        in _space_panel(response.text)
    )


def test_deliveries_page_platz_with_missing_audio_does_not_blame_toniecloud(
    admin_client, db_session, campaign, fake_tonie
):
    make_free_beitrag(db_session, title="Ohne Datei")
    handler, _patches = build_handler(
        tonie_gets=[creative_tonie_response(chapters=STOCK)],
        tonie_patches=[],
        file_ids=(),
    )
    fake_tonie(handler, {})

    response = admin_client.get("/admin/auslieferung?platz=1")

    assert response.status_code == 200
    panel = _space_panel(response.text)
    assert "Platz konnte nicht geprüft werden. Details stehen im Server-Log." in panel
    assert "Toniecloud ist gerade nicht erreichbar" not in panel


# --- Manueller Anstoss (Plan Schritt 5) ---------------------------------------------


def test_manual_trigger_of_verified_day_delivers_newly_approved_beitrag(
    admin_client, db_session, campaign, tonie, fake_tonie
):
    """Der manuelle Anstoss eines bereits verifizierten Tages liefert einen
    inzwischen freigegebenen Tagesbeitrag nach."""
    slot, old = make_person_slot_beitrag(db_session, campaign, day=5)
    tonie.verified_for_day = 5
    tonie.verified_beitrag_id = old.id
    tonie.verified_chapter_id = "old-chapter"
    # U16: das alte Kapitel stammt von der App (sonst waere es Bestand).
    tonie.app_chapters = '[{"id": "old-chapter", "seconds": null}]'
    old.approved_at = None
    new = Beitrag(
        person_id=old.person_id,
        auftrag=slot.auftrag,
        title="Neue Fassung",
        audio_object_key="audio/neu.mp3",
        approved_at=datetime(2026, 11, 25),
    )
    db_session.add(new)
    db_session.commit()

    handler, patch_bodies = build_handler(
        tonie_gets=[
            processed([{"id": "old-chapter", "title": "Tuerchen 5 Titel", "file": "x"}]),
            processed([{"id": "new-chapter", "title": "Neue Fassung", "file": "new-chapter"}]),
        ],
        tonie_patches=[
            patched([{"id": "new-chapter", "title": "Neue Fassung", "file": "file-a"}]),
        ],
    )
    fake_tonie(handler, {"audio/neu.mp3": AUDIO})

    response = admin_client.post("/admin/auslieferung/jetzt", data={"day": "5"})

    assert response.status_code == 200
    assert "erfolgreich</span>" in response.text
    assert len(patch_bodies) == 1
    assert patch_bodies[0]["chapters"][0]["title"] == "Neue Fassung"
    db_session.expire_all()
    refreshed = db_session.get(CreativeTonie, tonie.id)
    assert refreshed.verified_beitrag_id == new.id
    assert refreshed.verified_for_day == 5


def test_manual_trigger_failure_shows_named_reason_in_history(admin_client, campaign, fake_tonie):
    """U10: der Ausgang steht nicht mehr im Hinweisbalken der Anfrage (die
    antwortet vor dem Ende des Laufs), sondern im Verlauf."""
    handler, _ = build_handler(tonie_gets=[], tonie_patches=[])
    fake_tonie(handler, {})

    response = admin_client.post("/admin/auslieferung/jetzt", data={"day": "9"})

    assert "fehlgeschlagen</span>" in response.text
    assert "Kein freigegebener Beitrag fuer Tag 9" in response.text


def test_manual_trigger_is_recorded_as_anstoss_not_as_eve_delivery(
    admin_client, db_session, campaign, fake_tonie
):
    """Der Anstoss eines beliebigen Tages zaehlt nicht als dessen Vorabend-
    Auslieferung: R30-Ruecknahme und Kalenderzustand bleiben unberuehrt."""
    slot, _ = make_person_slot_beitrag(db_session, campaign, day=9, approved=False)
    handler, _ = build_handler(tonie_gets=[], tonie_patches=[])
    fake_tonie(handler, {})

    response = admin_client.post("/admin/auslieferung/jetzt", data={"day": "9"})

    assert "Anstoß Tag 9" in response.text
    run = db_session.query(DeliveryRun).one()
    assert run.run_type == "anstoss"
    early = datetime(2026, 10, 1, 12, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    assert delivery_has_run(db_session, slot, now=early, delivery_time=time(20, 0)) is False


def test_run_now_reset_keeps_app_chapters_review_focus_1(
    admin_client, db_session, campaign, tonie, fake_tonie
):
    """Review Focus 1: der Anstoss setzt den Verifiziert-Zustand zurueck,
    die gemerkten App-Kapitel aber nicht -- sonst bliebe das alte App-Kapitel
    beim naechsten Lauf als Bestand fuer immer stehen."""
    fake_tonie(_no_network, {})  # der Lauf scheitert vor jedem PATCH -- hier zaehlt nur der Reset
    tonie.verified_chapter_id = "app-alt"
    tonie.verified_for_day = 5
    tonie.app_chapters = '[{"id": "app-alt", "seconds": 1.0}]'
    db_session.commit()

    admin_client.post("/admin/auslieferung/jetzt", data={"day": "5"}, follow_redirects=False)

    db_session.expire_all()
    refreshed = db_session.get(CreativeTonie, tonie.id)
    assert refreshed.verified_chapter_id is None
    assert refreshed.app_chapters == '[{"id": "app-alt", "seconds": 1.0}]'


def test_run_now_after_reset_replaces_old_app_chapter_and_keeps_stock(
    admin_client, db_session, campaign, tonie, fake_tonie
):
    """Dasselbe von aussen: nach dem Zuruecksetzen tauscht der Anstoss das
    alte App-Kapitel aus, der Bestand bleibt dahinter."""
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)
    tonie.verified_chapter_id = "app-alt"
    tonie.verified_for_day = 5
    tonie.verified_beitrag_id = beitrag.id
    tonie.app_chapters = '[{"id": "app-alt", "seconds": 1.0}]'
    db_session.commit()
    new = {"id": "app-neu", "title": "Tuerchen 5 Titel", "file": "app-neu"}
    handler, patch_bodies = build_handler(
        tonie_gets=[
            processed([{"id": "app-alt", "title": "Alt", "file": "app-alt"}, *STOCK]),
            processed([new, *STOCK]),
        ],
        tonie_patches=[patched([{**new, "file": "file-a"}, *STOCK])],
    )
    fake_tonie(handler, {beitrag.audio_object_key: AUDIO})

    admin_client.post("/admin/auslieferung/jetzt", data={"day": "5"})

    assert [c.get("id") for c in patch_bodies[0]["chapters"]] == [None, "familie-1", "familie-2"]
    db_session.expire_all()
    refreshed = db_session.get(CreativeTonie, tonie.id)
    assert refreshed.app_chapters is not None
    assert '"app-neu"' in refreshed.app_chapters


# --- Hintergrundlauf (U10 Schritt 6) -------------------------------------------------


@pytest.mark.parametrize(
    ("url", "data", "run_type"),
    [
        ("/admin/auslieferung/jetzt", {"day": "5"}, "anstoss"),
        ("/admin/auslieferung/trockenlauf", {"day": "5"}, "trockenlauf"),
        ("/admin/auslieferung/manuell", "manual", "manuell"),
    ],
)
def test_admin_action_answers_immediately_and_result_appears_in_history(
    admin_client, db_session, campaign, fake_tonie, url, data, run_type
):
    """Die Anfrage wartet nicht auf die Toniecloud (hinter dem Tunnel
    braeche sie nach 100 s ab): sofortige Weiterleitung auf den Verlauf mit
    "laeuft", das Ergebnis erscheint dort nach Abschluss."""
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)
    if data == "manual":
        data = {"beitrag_id": str(beitrag.id), f"order_{beitrag.id}": "1"}
    handler, _ = build_handler(
        tonie_gets=[
            processed([]),
            processed([{"id": "c5", "title": "Tuerchen 5 Titel", "file": "c5"}]),
        ],
        tonie_patches=[patched([{"id": "c5", "title": "Tuerchen 5 Titel", "file": "file-a"}])],
    )
    fake_tonie(handler, {beitrag.audio_object_key: AUDIO})
    pending: list = []
    admin_client.app.dependency_overrides[get_run_starter] = lambda: pending.append

    response = admin_client.post(url, data=data, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/auslieferung"
    [run] = db_session.query(DeliveryRun).all()
    assert (run.run_type, run.outcome) == (run_type, "gestartet")
    page = admin_client.get("/admin/auslieferung").text
    assert "läuft im Hintergrund" in page
    assert "läuft</span>" in page

    [start_run] = pending
    start_run()

    db_session.expire_all()
    assert db_session.query(DeliveryRun).one().outcome == "erfolg"
    assert not _lock_for(TONIE_ID).locked()
    page = admin_client.get("/admin/auslieferung").text
    assert "läuft</span>" not in page
    assert "erfolgreich</span>" in page


def test_background_run_releases_lock_even_on_unexpected_exception(
    admin_client, db_session, campaign, fake_tonie
):
    """Die Sperre wird in jedem Fall freigegeben, und der Rand aus U11
    schliesst den offenen Eintrag als Fehlschlag mit Ausnahmeklasse ab."""
    make_person_slot_beitrag(db_session, campaign, day=5)
    fake_tonie(_no_network, {})

    admin_client.post("/admin/auslieferung/jetzt", data={"day": "5"})

    assert not _lock_for(TONIE_ID).locked()
    run = db_session.query(DeliveryRun).one()
    assert run.outcome == "fehlschlag"
    assert "AssertionError" in run.reason


def test_failing_thread_start_releases_lock_and_closes_entry(
    admin_client, db_session, campaign, fake_tonie
):
    """Scheitert der Start des Hintergrundlaufs, bleibt weder die Sperre
    gehalten noch der Eintrag als "läuft" stehen."""
    make_person_slot_beitrag(db_session, campaign, day=5)
    fake_tonie(_no_network, {})

    def broken_start(run) -> None:
        raise RuntimeError("kein Thread mehr frei")

    admin_client.app.dependency_overrides[get_run_starter] = lambda: broken_start

    with pytest.raises(RuntimeError, match="kein Thread mehr frei"):
        admin_client.post("/admin/auslieferung/jetzt", data={"day": "5"})

    lock = _lock_for(TONIE_ID)
    assert lock.acquire(blocking=False)
    lock.release()
    db_session.expire_all()
    run = db_session.query(DeliveryRun).one()
    assert run.outcome == "fehlschlag"
    assert "RuntimeError" in (run.reason or "")


def test_failed_admin_anstoss_sends_report_mail(admin_client, db_session, campaign, fake_tonie):
    """KTD18: Admin-Laeufe folgen derselben Melderegel wie automatische."""
    handler, _ = build_handler(tonie_gets=[], tonie_patches=[])
    fake_tonie(handler, {})

    admin_client.post("/admin/auslieferung/jetzt", data={"day": "9"})

    assert len(FakeSMTP.sent) == 1
    assert "Fehlschlag" in FakeSMTP.sent[0]["Subject"]


def test_successful_admin_anstoss_sends_no_mail(admin_client, db_session, campaign, fake_tonie):
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=5)
    handler, _ = build_handler(
        tonie_gets=[
            processed([]),
            processed([{"id": "c5", "title": "Tuerchen 5 Titel", "file": "c5"}]),
        ],
        tonie_patches=[patched([{"id": "c5", "title": "Tuerchen 5 Titel", "file": "file-a"}])],
    )
    fake_tonie(handler, {beitrag.audio_object_key: AUDIO})

    admin_client.post("/admin/auslieferung/jetzt", data={"day": "5"})

    assert db_session.query(DeliveryRun).one().outcome == "erfolg"
    assert FakeSMTP.sent == []


def test_admin_action_while_a_run_holds_the_lock_starts_nothing(
    admin_client, db_session, campaign, tonie, fake_tonie
):
    tonie.verified_for_day = 5
    tonie.verified_chapter_id = "chap-5"
    db_session.commit()
    fake_tonie(_no_network, {})
    lock = _lock_for(TONIE_ID)
    assert lock.acquire(blocking=False)
    try:
        response = admin_client.post("/admin/auslieferung/jetzt", data={"day": "5"})
    finally:
        lock.release()

    assert "flash err" in response.text
    assert "läuft bereits ein Lauf" in response.text
    assert db_session.query(DeliveryRun).count() == 0
    db_session.expire_all()
    # Der laufende Lauf behaelt seinen Verifiziert-Zustand.
    assert db_session.get(CreativeTonie, tonie.id).verified_chapter_id == "chap-5"


# --- Trockenlauf --------------------------------------------------------------------


def test_dry_run_leaves_verified_state_unchanged(
    admin_client, db_session, campaign, tonie, fake_tonie
):
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=4)
    tonie.verified_for_day = 3
    tonie.verified_beitrag_id = beitrag.id
    tonie.verified_chapter_id = "chap-3"
    db_session.commit()
    handler, patch_bodies = build_handler(tonie_gets=[], tonie_patches=[])
    fake_tonie(handler, {beitrag.audio_object_key: b"x"})

    response = admin_client.post("/admin/auslieferung/trockenlauf", data={"day": "4"})

    assert response.status_code == 200
    assert "nicht die Verarbeitung auf dem Tonie" in response.text
    assert patch_bodies == []
    db_session.expire_all()
    refreshed = db_session.get(CreativeTonie, tonie.id)
    assert (refreshed.verified_for_day, refreshed.verified_chapter_id) == (3, "chap-3")
    run = db_session.query(DeliveryRun).one()
    assert (run.run_type, run.target_day, run.outcome) == ("trockenlauf", 4, "erfolg")


# --- Manueller Lauf mit mehreren Beitraegen ------------------------------------------


def test_admin_delivers_free_submission_by_hand_and_no_automatic_run_picks_it(
    admin_client, db_session, campaign, tonie, fake_tonie, config
):
    """Der Admin spielt eine freie Einreichung von Hand auf; kein
    automatischer Lauf waehlt sie je aus."""
    free = make_free_beitrag(db_session, title="Hallo aus Leipzig")
    handler, patch_bodies = build_handler(
        tonie_gets=[
            processed([]),
            processed([{"id": "c1", "title": "Hallo aus Leipzig", "file": "c1"}]),
        ],
        tonie_patches=[patched([{"id": "c1", "title": "Hallo aus Leipzig", "file": "file-a"}])],
    )
    fake_tonie(handler, {free.audio_object_key: AUDIO})

    response = admin_client.post(
        "/admin/auslieferung/manuell",
        data={"beitrag_id": str(free.id), f"order_{free.id}": "1"},
    )

    assert "erfolgreich</span>" in response.text
    assert [c["title"] for c in patch_bodies[0]["chapters"]] == ["Hallo aus Leipzig"]

    # Kein automatischer Lauf -- fuer keinen Tag -- greift zur freien Einreichung.
    auto_handler, auto_patches = build_handler(tonie_gets=[], tonie_patches=[])
    auto_client = make_client(auto_handler)
    db_session.expire_all()
    fresh_tonie = db_session.get(CreativeTonie, tonie.id)
    for day in range(1, 25):
        outcome = run_delivery(
            db_session,
            auto_client,
            FakeStorage({}),
            fresh_tonie,
            run_type="vorabend",
            target_day=day,
        )
        assert outcome.used_beitrag_id != free.id
        assert not outcome.success
    assert auto_patches == []


def test_manual_run_uses_given_order_ae24(admin_client, db_session, campaign, fake_tonie):
    _, day_beitrag = make_person_slot_beitrag(db_session, campaign, day=3)
    free = make_free_beitrag(db_session, title="Freie Nachricht")
    handler, patch_bodies = build_handler(
        tonie_gets=[
            processed([]),
            processed(
                [
                    {"id": "c1", "title": "Freie Nachricht", "file": "c1"},
                    {"id": "c2", "title": "Tuerchen 3 Titel", "file": "c2"},
                ]
            ),
        ],
        tonie_patches=[
            patched(
                [
                    {"id": "c1", "title": "Freie Nachricht", "file": "file-a"},
                    {"id": "c2", "title": "Tuerchen 3 Titel", "file": "file-b"},
                ]
            )
        ],
        file_ids=("file-a", "file-b"),
    )
    fake_tonie(handler, {day_beitrag.audio_object_key: AUDIO, free.audio_object_key: AUDIO})

    response = admin_client.post(
        "/admin/auslieferung/manuell",
        data={
            "beitrag_id": [str(day_beitrag.id), str(free.id)],
            f"order_{day_beitrag.id}": "2",
            f"order_{free.id}": "1",
        },
    )

    assert "erfolgreich</span>" in response.text
    assert [c["title"] for c in patch_bodies[0]["chapters"]] == [
        "Freie Nachricht",
        "Tuerchen 3 Titel",
    ]
    run = db_session.query(DeliveryRun).one()
    assert run.beitrag_ids == f"{free.id},{day_beitrag.id}"


@pytest.mark.parametrize("variant", ["unapproved", "detached", "rejected", "missing", "none"])
def test_manual_run_refuses_ineligible_beitraege(
    admin_client, db_session, campaign, fake_tonie, variant
):
    free = make_free_beitrag(db_session, title="Kandidat")
    if variant == "unapproved":
        free.approved_at = None
    elif variant == "detached":
        free.detached_at = datetime(2026, 11, 22)
    elif variant == "rejected":
        free.rejected_at = datetime(2026, 11, 22)
    db_session.commit()
    fake_tonie(_no_network, {})

    if variant == "none":
        data: dict = {}
    elif variant == "missing":
        data = {"beitrag_id": "99999"}
    else:
        data = {"beitrag_id": str(free.id), f"order_{free.id}": "1"}
    response = admin_client.post("/admin/auslieferung/manuell", data=data)

    assert response.status_code == 200
    assert "flash err" in response.text
    assert db_session.query(DeliveryRun).count() == 0


# --- Verlauf (R44, AE21) --------------------------------------------------------------


def test_history_lists_failed_run_with_time_content_and_reason(admin_client, db_session, campaign):
    slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=6)
    slot.auftrag.title = "Das Licht im Fenster"
    db_session.add_all(
        [
            DeliveryRun(
                campaign_id=campaign.id,
                tonie_id=TONIE_ID,
                run_type="vorabend",
                target_day=6,
                started_at=datetime(2026, 12, 5, 19, 4),  # naiv UTC -> 20:04 Berlin
                outcome="fehlschlag",
                reason="Hochladen fehlgeschlagen: Zeitueberschreitung",
                beitrag_ids=str(beitrag.id),
            ),
            DeliveryRun(
                campaign_id=campaign.id,
                tonie_id=TONIE_ID,
                run_type="manuell",
                target_day=None,
                started_at=datetime(2026, 12, 6, 9, 0),
                outcome="erfolg",
                beitrag_ids="4242",
            ),
        ]
    )
    db_session.commit()

    text = admin_client.get("/admin/auslieferung").text

    assert "5.12. 20:04" in text
    assert "Vorabend Tag 6" in text
    assert "Das Licht im Fenster · Person 6" in text
    assert "Hochladen fehlgeschlagen: Zeitueberschreitung" in text
    assert "fehlgeschlagen</span>" in text
    assert "Manueller Lauf" in text
    assert "gelöschter Beitrag #4242" in text
    # neueste zuerst
    assert text.index("Manueller Lauf</td>") < text.index("Vorabend Tag 6")


# --- Mehrkalender U7: Ziel-Tonie und Verlauf je Tonie (R6, KTD10) -----------------------


def test_manual_run_targets_chosen_tonie_without_calendar(
    admin_client, db_session, campaign, fake_tonie
):
    """R6: der manuelle Lauf waehlt seinen Ziel-Tonie aus allen verknuepften
    Tonies, auch einem ohne Kalender (Platzhalter-Kalender im Verlauf)."""
    loose = CreativeTonie(tonie_id=TONIE_ID_2, name="Kinderzimmer")
    db_session.add(loose)
    db_session.commit()
    free = make_free_beitrag(db_session, title="Gruss")
    new = {"id": "c1", "title": "Gruss", "file": "c1"}
    handler, patches = build_handler(
        tonie_gets=[processed([]), processed([new])],
        tonie_patches=[patched([new])],
        tonie_id=TONIE_ID_2,
    )
    fake_tonie(handler, {free.audio_object_key: AUDIO})

    page = admin_client.get("/admin/auslieferung").text
    assert 'name="tonie"' in page
    assert f'value="{loose.id}"' in page
    assert "Kinderzimmer · ohne Kalender" in page

    admin_client.post(
        "/admin/auslieferung/manuell",
        data={"tonie": str(loose.id), "beitrag_id": str(free.id), f"order_{free.id}": "1"},
    )

    run = db_session.query(DeliveryRun).one()
    assert (run.tonie_id, run.campaign_id, run.outcome) == (TONIE_ID_2, campaign.id, "erfolg")
    assert len(patches) == 1


def test_manual_run_without_any_calendar_is_refused_with_hint(admin_client, db_session, fake_tonie):
    db_session.add(CreativeTonie(tonie_id=TONIE_ID_2))
    db_session.commit()
    free = make_free_beitrag(db_session, title="Gruss")
    fake_tonie(_no_network, {})

    response = admin_client.post(
        "/admin/auslieferung/manuell",
        data={"beitrag_id": str(free.id), f"order_{free.id}": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert db_session.query(DeliveryRun).count() == 0
    flash = admin_client.get("/admin").text
    assert "Bitte zuerst einen Kalender anlegen." in flash


def test_history_shows_only_runs_of_current_tonie(admin_client, db_session, campaign):
    db_session.add_all(
        [
            DeliveryRun(
                campaign_id=campaign.id,
                tonie_id=TONIE_ID,
                run_type="vorabend",
                target_day=6,
                started_at=datetime(2026, 12, 5, 19, 0),
                outcome="erfolg",
            ),
            DeliveryRun(
                campaign_id=campaign.id,
                tonie_id=TONIE_ID_2,
                run_type="vorabend",
                target_day=7,
                started_at=datetime(2026, 12, 6, 19, 0),
                outcome="fehlschlag",
            ),
        ]
    )
    db_session.commit()

    text = admin_client.get("/admin/auslieferung").text

    assert "Vorabend Tag 6" in text
    assert "Vorabend Tag 7" not in text
