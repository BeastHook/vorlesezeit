"""Mehrkalender U8: Sammel-Abendmeldung je Abend (R4, KTD11).

Test-first. Eine Mail pro Abend mit einer Zeile je faelligem Tonie, sobald
alle faelligen Tonies einen Endzustand ihres Vorabend-Laufs haben oder die
Vorabend-Phase vorbei ist. Fehlschlaege und Kontrolllaeufe mit Eingriff
melden sich zusaetzlich sofort einzeln. Uhr, Fake-Toniecloud und SMTP wie in
tests/test_trigger.py -- nie das echte Konto, nie echte Mail.
"""

from __future__ import annotations

import threading
import time as time_module
from datetime import date

import httpx
import pytest
from sqlalchemy import select

from app.delivery import trigger as trigger_module
from app.delivery.job import DeliveryOutcome, _lock_for
from app.delivery.trigger import get_storage, get_toniecloud_factory, send_evening_summary
from app.mail.report import RUN_TYPE_LABELS, should_send_report
from app.models import Abendmeldung, Campaign, CreativeTonie, Slot
from app.toniecloud.client import TOKEN_URL
from tests.mailutil import html_text, plain_text
from tests.test_delivery import (
    AUDIO,
    TONIE_ID,
    TONIE_ID_2,
    FakeStorage,
    FixedFactory,
    build_handler,
    creative_tonie_response,
    make_calendar_tonie,
    make_client,
    make_person_slot_beitrag,
)
from tests.test_trigger import (
    FailingSMTP,
    FakeSMTP,
    Harness,
    berlin,
    delivered_chapter,
    eve_handler,
    make_replacement,
    no_network,
    seed_run,
)

TONIE_ID_3 = "9ABCKLMNO00304E0"
EVENING = date(2026, 12, 5)


@pytest.fixture
def h(client, config, monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    harness = Harness(client, config)
    yield harness
    client.app.dependency_overrides.clear()
    for tonie_id in (TONIE_ID, TONIE_ID_2, TONIE_ID_3):
        lock = _lock_for(tonie_id)
        if lock.locked():
            lock.release()


@pytest.fixture
def family(db_session) -> tuple[Campaign, CreativeTonie, CreativeTonie]:
    """Kalender "Familie" mit zwei gespiegelten Tonies."""
    campaign, t1 = make_calendar_tonie(db_session, name="Kinderzimmer")
    _, t2 = make_calendar_tonie(db_session, tonie_id=TONIE_ID_2, campaign=campaign, name="Oma")
    return campaign, t1, t2


def install(h, handlers: dict[str, object], files: dict[str, bytes]) -> None:
    clients = {tonie_id: make_client(handler) for tonie_id, handler in handlers.items()}
    first = next(iter(clients.values()))
    h.app.dependency_overrides[get_toniecloud_factory] = lambda: FixedFactory(first, clients)
    h.app.dependency_overrides[get_storage] = lambda: FakeStorage(files)


def summaries() -> list:
    return [m for m in FakeSMTP.sent if "Abendmeldung" in m["Subject"]]


def singles() -> list:
    return [m for m in FakeSMTP.sent if "Abendmeldung" not in m["Subject"]]


def summary_rows(db_session) -> list[Abendmeldung]:
    db_session.expire_all()
    return list(db_session.execute(select(Abendmeldung)).scalars())


# --- Meldungsentscheidung je Lauf (rein) ---------------------------------------


def _outcome(run_type, success=True, changed=False) -> DeliveryOutcome:
    return DeliveryOutcome(
        success=success,
        run_type=run_type,
        target_day=6,
        used_beitrag_id=1,
        used_replacement=False,
        changed_tonie=changed,
    )


@pytest.mark.parametrize("run_type", ["vorabend", "probelauf", "aufraeumen"])
def test_successful_evening_phase_run_sends_no_single_report(run_type):
    assert should_send_report(_outcome(run_type)) is False


@pytest.mark.parametrize("run_type", ["vorabend", "kontrolllauf", "probelauf", "anstoss"])
def test_failure_always_sends_single_report(run_type):
    assert should_send_report(_outcome(run_type, success=False)) is True


def test_kontrolllauf_reports_only_when_it_changed_the_tonie():
    assert should_send_report(_outcome("kontrolllauf", changed=True)) is True
    assert should_send_report(_outcome("kontrolllauf", changed=False)) is False


def test_run_type_labels_know_abraeumen():
    assert "abraeumen" in RUN_TYPE_LABELS


def test_summary_mail_has_status_strip_and_same_text():
    from datetime import date

    from app.mail.report import SummaryRow, build_summary_message

    rows = [SummaryRow("Familie", "Tonie", "abcd1234", "fehlschlag", 6, "Zeitueberschreitung")]
    m = build_summary_message(
        to_address="a@e.t", evening=date(2026, 12, 5), run_type="vorabend", rows=rows
    )
    assert "Abendmeldung 05.12.2026" in m["Subject"]
    assert "Ursache/Hinweis: Zeitueberschreitung" in plain_text(m)
    html = html_text(m)
    assert "#b5412c" in html  # Fehler -> Ziegelrot-Streifen
    assert "Zeitueberschreitung" in html
    assert not [p for p in m.walk() if p.get_content_type() == "image/png"]


# --- Sammelmeldung ------------------------------------------------------------


def test_three_successful_tonies_send_exactly_one_mail_with_three_rows(h, family, db_session):
    campaign, _t1, _t2 = family
    slot, beitrag = make_person_slot_beitrag(db_session, campaign, day=6)
    oma = Campaign(name="Bei Oma")
    db_session.add(oma)
    db_session.flush()
    # R8: derselbe Auftrag liegt auch im zweiten Kalender am 6.
    db_session.add(Slot(campaign_id=oma.id, day=6, auftrag_id=slot.auftrag_id))
    db_session.commit()
    make_calendar_tonie(db_session, tonie_id=TONIE_ID_3, campaign=oma, name="Gaestezimmer")
    install(
        h,
        {
            TONIE_ID: eve_handler(TONIE_ID, "t1")[0],
            TONIE_ID_2: eve_handler(TONIE_ID_2, "t2")[0],
            TONIE_ID_3: eve_handler(TONIE_ID_3, "t3")[0],
        },
        {beitrag.audio_object_key: AUDIO},
    )

    assert h.call(berlin(2026, 12, 5, 20, 0)).status_code == 202

    assert len(FakeSMTP.sent) == 1
    [mail] = summaries()
    assert "05.12.2026" in mail["Subject"]
    assert "3 Tonies" in mail["Subject"]
    assert "Tuerchen" not in mail["Subject"]
    body = plain_text(mail)
    for calendar, name, tonie_id in (
        (campaign.name, "Kinderzimmer", TONIE_ID),
        (campaign.name, "Oma", TONIE_ID_2),
        ("Bei Oma", "Gaestezimmer", TONIE_ID_3),
    ):
        [line] = [line for line in body.splitlines() if f"{name} (" in line]
        assert calendar in line
        assert "Erfolg" in line
        assert "Türchen 6" in line
        assert tonie_id[-4:] in line
        assert tonie_id not in body  # Tonie-IDs nur maskiert
    # Ein spaeterer Takt desselben Abends verschickt nichts mehr.
    assert h.call(berlin(2026, 12, 5, 20, 30)).status_code == 200
    assert len(FakeSMTP.sent) == 1
    assert [r.evening for r in summary_rows(db_session)] == [EVENING]


def test_two_simultaneous_checks_send_only_one_mail(h, family, db_session, monkeypatch):
    campaign, _t1, _t2 = family
    for tonie_id in (TONIE_ID, TONIE_ID_2):
        seed_run(
            db_session, campaign, "vorabend", 6, "erfolg", berlin(2026, 12, 5, 20, 0), EVENING,
            tonie_id=tonie_id,
        )  # fmt: skip

    class SlowSMTP(FakeSMTP):
        def send_message(self, message) -> None:
            time_module.sleep(0.3)  # beide Pruefungen ueberlappen sicher
            super().send_message(message)

    monkeypatch.setattr("smtplib.SMTP", SlowSMTP)
    barrier = threading.Barrier(2, timeout=5)
    # Beide Pruefungen haben "noch nicht verschickt" gelesen und alle Tonies
    # als fertig gesehen, bevor eine von beiden einfuegt.
    both_checked = threading.Barrier(2, timeout=5)
    real_result = trigger_module._eve_result

    def eve_result(db, tonie, due):
        result = real_result(db, tonie, due)
        if tonie.tonie_id == TONIE_ID_2:  # der letzte Tonie der Pruefung
            both_checked.wait()
        return result

    monkeypatch.setattr(trigger_module, "_eve_result", eve_result)
    errors: list[BaseException] = []

    def check() -> None:
        barrier.wait()
        try:
            send_evening_summary(h.config, h.app.state.session_factory, EVENING)
        except BaseException as exc:  # pragma: no cover - nur Diagnose
            errors.append(exc)

    threads = [threading.Thread(target=check) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(summaries()) == 1
    assert len(summary_rows(db_session)) == 1


def test_failure_sends_single_report_now_and_row_in_later_summary(h, family, db_session):
    campaign, _t1, _t2 = family
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=6)

    def login_rejected(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and str(request.url) == TOKEN_URL:
            return httpx.Response(401)
        return no_network(request)

    install(
        h,
        {TONIE_ID: eve_handler(TONIE_ID, "t1")[0], TONIE_ID_2: login_rejected},
        {beitrag.audio_object_key: AUDIO},
    )

    assert h.call(berlin(2026, 12, 5, 20, 0)).status_code == 202
    # Sofort: genau die Einzelmeldung des Fehlschlags, noch keine Sammelmeldung.
    [single] = FakeSMTP.sent
    assert "Fehlschlag" in single["Subject"]
    assert "Abendmeldung" not in single["Subject"]

    h.call(berlin(2026, 12, 5, 20, 15))
    assert summaries() == []
    h.call(berlin(2026, 12, 5, 20, 30))  # dritter Versuch: Endzustand

    assert len(singles()) == 3
    [mail] = summaries()
    body = plain_text(mail)
    [ok] = [line for line in body.splitlines() if "Kinderzimmer (" in line]
    [failed] = [line for line in body.splitlines() if "Oma (" in line]
    assert "Erfolg" in ok
    assert "Fehlschlag" in failed
    assert "LoginRejectedError" in body


def test_phase_end_names_tonie_without_end_state_as_no_result(h, family, db_session):
    campaign, _t1, _t2 = family
    seed_run(db_session, campaign, "vorabend", 6, "erfolg", berlin(2026, 12, 5, 20, 0), EVENING)
    # Beide Tonies sind beschaeftigt: kein Kontrolllauf startet, keine Toniecloud.
    locks = [_lock_for(TONIE_ID), _lock_for(TONIE_ID_2)]
    for lock in locks:
        assert lock.acquire(blocking=False)
    try:
        assert h.call(berlin(2026, 12, 5, 21, 30)).status_code == 202
        assert FakeSMTP.sent == []  # Vorabend-Phase laeuft noch
        h.call(berlin(2026, 12, 5, 22, 0))
    finally:
        for lock in locks:
            lock.release()

    [mail] = FakeSMTP.sent
    body = plain_text(mail)
    [ok] = [line for line in body.splitlines() if "Kinderzimmer (" in line]
    [missing] = [line for line in body.splitlines() if "Oma (" in line]
    assert "Erfolg" in ok
    assert "kein Ergebnis" in missing


def test_evening_without_due_tonies_sends_no_mail(h, db_session):
    db_session.add(Campaign(name="Ohne Tonie"))
    db_session.commit()

    for at in (berlin(2026, 12, 5, 20, 0), berlin(2026, 12, 5, 22, 30)):
        assert h.call(at).status_code == 204
    send_evening_summary(h.config, h.app.state.session_factory, EVENING)

    assert FakeSMTP.sent == []
    assert summary_rows(db_session) == []


def test_kontrolllauf_repair_after_summary_sends_exactly_one_more_mail(h, family, db_session):
    campaign, t1, _t2 = family
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=6)
    for tonie_id in (TONIE_ID, TONIE_ID_2):
        seed_run(
            db_session, campaign, "vorabend", 6, "erfolg", berlin(2026, 12, 5, 20, 0), EVENING,
            tonie_id=tonie_id,
        )  # fmt: skip
    t1.verified_for_day = 6
    t1.verified_beitrag_id = beitrag.id
    t1.verified_chapter_id = "server-id-a"
    db_session.commit()

    assert h.call(berlin(2026, 12, 5, 20, 30)).status_code == 200
    assert len(summaries()) == 1 and len(FakeSMTP.sent) == 1

    # Tonie 1 ist unveraendert (kein Eingriff), Tonie 2 hat das Kapitel verloren.
    idle, idle_patches = build_handler(
        tonie_gets=[creative_tonie_response(chapters=[delivered_chapter()])], tonie_patches=[]
    )
    repair, repair_patches = eve_handler(TONIE_ID_2, "t2-neu")
    install(h, {TONIE_ID: idle, TONIE_ID_2: repair}, {beitrag.audio_object_key: AUDIO})

    assert h.call(berlin(2026, 12, 5, 22, 0)).status_code == 202

    assert (len(idle_patches), len(repair_patches)) == (0, 1)
    assert len(FakeSMTP.sent) == 2
    [single] = singles()
    assert "Kontrolllauf" in single["Subject"]
    assert "Erfolg" in single["Subject"]
    assert TONIE_ID_2[-4:] in plain_text(single)


def test_kontrolllauf_without_change_sends_no_mail(h, db_session):
    campaign, tonie = make_calendar_tonie(db_session)
    _, beitrag = make_person_slot_beitrag(db_session, campaign, day=6)
    seed_run(db_session, campaign, "vorabend", 6, "erfolg", berlin(2026, 12, 5, 20, 0), EVENING)
    db_session.add(Abendmeldung(evening=EVENING, sent_at=berlin(2026, 12, 5, 20, 5)))
    tonie.verified_for_day = 6
    tonie.verified_beitrag_id = beitrag.id
    tonie.verified_chapter_id = "server-id-a"
    db_session.commit()
    handler, patches = build_handler(
        tonie_gets=[creative_tonie_response(chapters=[delivered_chapter()])], tonie_patches=[]
    )
    h.install(handler, {beitrag.audio_object_key: AUDIO})

    assert h.call(berlin(2026, 12, 5, 22, 0)).status_code == 202

    assert patches == []
    assert FakeSMTP.sent == []


def test_failed_summary_send_removes_row_and_next_trigger_sends_again(
    h, family, db_session, monkeypatch
):
    campaign, _t1, _t2 = family
    for tonie_id in (TONIE_ID, TONIE_ID_2):
        seed_run(
            db_session, campaign, "vorabend", 6, "erfolg", berlin(2026, 12, 5, 20, 0), EVENING,
            tonie_id=tonie_id,
        )  # fmt: skip
    monkeypatch.setattr("smtplib.SMTP", FailingSMTP)

    assert h.call(berlin(2026, 12, 5, 20, 30)).status_code == 200
    assert FakeSMTP.sent == []
    assert summary_rows(db_session) == []

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    assert h.call(berlin(2026, 12, 5, 20, 45)).status_code == 200

    assert len(summaries()) == 1
    assert len(summary_rows(db_session)) == 1


def test_summary_row_names_replacement_and_missing_day_ae10(h, db_session):
    """AE10/R26: springt der Ersatzbeitrag ein, nennt die Zeile den fehlenden Tag."""
    campaign, _tonie = make_calendar_tonie(db_session)
    replacement = make_replacement(db_session, campaign)
    ersatz = {"id": "t1-ersatz", "title": "Ersatzgeschichte", "file": "t1-ersatz"}
    handler, _ = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=[]),
            creative_tonie_response(chapters=[ersatz]),
        ],
        tonie_patches=[creative_tonie_response(transcoding=True, chapters=[ersatz])],
    )
    h.install(handler, {replacement.audio_object_key: AUDIO})

    h.call(berlin(2026, 12, 4, 20, 0))

    [mail] = FakeSMTP.sent
    body = plain_text(mail)
    assert "Ersatzbeitrag" in body
    assert "Kein freigegebener Beitrag fuer Tag 5." in body
