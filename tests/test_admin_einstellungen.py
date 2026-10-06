"""Mehrkalender U11: Reiter Setup (R17-R26, R37, R41-R43; KTD13, KTD16).

Alle Zugangsdaten sind erfundene Platzhalter. Die Toniecloud antwortet ueber
`httpx.MockTransport`, der SMTP-Check ist ein Aufzeichner -- keine echte
Verbindung."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import create_app
from app import settings as st
from app.admin import einstellungen
from app.admin.setup import new_campaign
from app.auth.tokens import create_magic_link_token
from app.crypto import decrypt
from app.delivery.job import _lock_for, client_for
from app.delivery.trigger import get_now, get_toniecloud_factory
from app.mail.smtp import SmtpPruefung
from app.models import (
    Abendmeldung,
    Auftrag,
    Beitrag,
    Campaign,
    CreativeTonie,
    DeliveryRun,
    Einstellungen,
    Person,
    Slot,
    TonieKonto,
)
from app.toniecloud.client import TonieCloudFactory, check_konto
from tests.test_delivery import (
    STOCK,
    TONIE_ID,
    FixedFactory,
    build_handler,
    creative_tonie_response,
)
from tests.test_delivery import make_client as make_tonie_client
from tests.test_toniecloud import AccountTransport

KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
ORIGIN = {"Origin": "http://testserver"}
TONIE_A = "AAAA0000000004E0"
TONIE_B = "BBBB000000007A12"


def berlin(*args) -> datetime:
    return datetime(*args, tzinfo=ZoneInfo("Europe/Berlin"))


@pytest.fixture
def transport() -> AccountTransport:
    return AccountTransport()


@pytest.fixture
def setup_client(admin_client: TestClient, transport: AccountTransport) -> TestClient:
    """Admin mit passendem Origin; die Konto-Pruefung laeuft gegen den Mock."""
    admin_client.headers.update(ORIGIN)
    mock = httpx.MockTransport(transport)
    admin_client.app.dependency_overrides[einstellungen.get_konto_check] = lambda: (
        lambda user, password: check_konto(user, password, transport=mock, sleep=lambda s: None)
    )
    yield admin_client
    admin_client.app.dependency_overrides.clear()


class SmtpCalls(list):
    """Aufzeichner fuer `check_smtp`; `result` ist die naechste Antwort."""

    result = SmtpPruefung(True)


@pytest.fixture
def smtp_calls(monkeypatch) -> SmtpCalls:
    calls = SmtpCalls()

    def fake_check(**kwargs):
        calls.append(kwargs)
        return calls.result

    monkeypatch.setattr(einstellungen, "check_smtp", fake_check)
    return calls


def _konto(db_session, username: str, password: str, label: str = "") -> TonieKonto:
    return st.create_konto(
        db_session, KEY, username=username, password=password, label=label, now=berlin(2026, 10, 1)
    )


def _calendar(db_session, name: str) -> Campaign:
    campaign = Campaign(name=name)
    db_session.add(campaign)
    db_session.commit()
    return campaign


def _tonie(db_session, tonie_id: str, *, konto=None, campaign=None, **fields) -> CreativeTonie:
    tonie = CreativeTonie(
        tonie_id=tonie_id,
        name=fields.pop("name", "Kinderzimmer"),
        konto_id=konto.id if konto else None,
        campaign_id=campaign.id if campaign else None,
        **fields,
    )
    db_session.add(tonie)
    db_session.commit()
    return tonie


# --- tonies-Konten (R19, R20) ---------------------------------------------------


def test_new_konto_with_valid_check_is_saved_and_tonies_linked(setup_client, transport, db_session):
    transport.tonies_by_user["oma@example.test"] = [
        {"id": TONIE_A, "name": "Omas Tonie"},
        {"id": TONIE_B, "name": "Tonie im Auto"},
    ]

    response = setup_client.post(
        "/admin/setup/konten",
        data={"username": "oma@example.test", "password": "platzhalter-oma-pw"},
    )

    assert response.status_code == 200
    assert "Anmeldung gelungen" in response.text
    assert "Omas Tonie" in response.text and "Tonie im Auto" in response.text
    assert "platzhalter-oma-pw" not in response.text
    konto = db_session.execute(
        select(TonieKonto).where(TonieKonto.username == "oma@example.test")
    ).scalar_one()
    assert konto.checked_at is not None
    assert decrypt(KEY, konto.password) == "platzhalter-oma-pw"
    tonies = db_session.execute(select(CreativeTonie).order_by(CreativeTonie.tonie_id)).scalars()
    assert [(t.tonie_id, t.konto_id, t.campaign_id) for t in tonies] == [
        (TONIE_A, konto.id, None),
        (TONIE_B, konto.id, None),
    ]
    page = setup_client.get("/admin/setup")
    assert "platzhalter-oma-pw" not in page.text
    assert "Omas Tonie" in page.text


def test_new_konto_with_rejected_check_saves_nothing_and_names_reason(
    setup_client, transport, db_session
):
    transport.rejected.add("oma@example.test")

    response = setup_client.post(
        "/admin/setup/konten", data={"username": "oma@example.test", "password": "falsch-pw"}
    )

    assert "Anmeldung fehlgeschlagen" in response.text
    assert "falsch-pw" not in response.text
    assert (
        db_session.execute(
            select(TonieKonto).where(TonieKonto.username == "oma@example.test")
        ).first()
        is None
    )


def test_new_konto_check_timeout_has_own_reason_and_saves_nothing(setup_client, db_session):
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("zu langsam", request=request)

    mock = httpx.MockTransport(timeout)
    setup_client.app.dependency_overrides[einstellungen.get_konto_check] = lambda: (
        lambda user, password: check_konto(user, password, transport=mock, sleep=lambda s: None)
    )

    response = setup_client.post(
        "/admin/setup/konten", data={"username": "oma@example.test", "password": "pw"}
    )

    assert "Zeitüberschreitung" in response.text
    assert (
        db_session.execute(
            select(TonieKonto).where(TonieKonto.username == "oma@example.test")
        ).first()
        is None
    )
    assert db_session.execute(select(CreativeTonie)).first() is None


def test_empty_password_on_edit_keeps_stored_password(setup_client, transport, db_session):
    konto = _konto(db_session, "opa@example.test", "platzhalter-alt")

    response = setup_client.post(
        f"/admin/setup/konten/{konto.id}/pruefen",
        data={"username": "opa@example.test", "password": ""},
    )

    assert response.status_code == 200
    assert transport.login_passwords() == ["platzhalter-alt"]
    db_session.expire_all()
    assert decrypt(KEY, db_session.get(TonieKonto, konto.id).password) == "platzhalter-alt"


def test_changed_username_with_empty_password_is_refused_without_check(setup_client, db_session):
    konto = _konto(db_session, "opa@example.test", "platzhalter-alt")
    calls = []
    setup_client.app.dependency_overrides[einstellungen.get_konto_check] = lambda: (
        lambda user, password: calls.append((user, password))
    )

    response = setup_client.post(
        f"/admin/setup/konten/{konto.id}/pruefen",
        data={"username": "fremd@example.test", "password": ""},
    )

    assert response.status_code == 200
    assert "Neue E-Mail: bitte das Passwort neu eingeben." in response.text
    assert calls == []
    db_session.expire_all()
    saved = db_session.get(TonieKonto, konto.id)
    assert saved.username == "opa@example.test"
    assert decrypt(KEY, saved.password) == "platzhalter-alt"


def test_changed_password_failing_check_keeps_old_one(setup_client, transport, db_session):
    konto = _konto(db_session, "opa@example.test", "platzhalter-alt")
    transport.rejected.add("opa@example.test")

    response = setup_client.post(
        f"/admin/setup/konten/{konto.id}/pruefen",
        data={"username": "opa@example.test", "password": "platzhalter-neu"},
    )

    assert "Anmeldung fehlgeschlagen" in response.text
    assert "bisherigen Zugangsdaten bleiben aktiv" in response.text
    db_session.expire_all()
    assert decrypt(KEY, db_session.get(TonieKonto, konto.id).password) == "platzhalter-alt"


def test_konto_needing_reentry_is_marked(setup_client, db_session):
    konto = _konto(db_session, "opa@example.test", "platzhalter-alt")
    konto.needs_reentry = True
    db_session.commit()

    page = setup_client.get("/admin/setup")

    assert "Passwort neu eingeben" in page.text


# --- Tonie-Zuordnung (R5, R25, R37) ---------------------------------------------


def test_run_holding_lock_refuses_moving_and_detaching_other_tonies_free_ae6(
    setup_client, db_session
):
    konto = _konto(db_session, "opa@example.test", "pw")
    sommer, berger = _calendar(db_session, "Familie Sommer"), _calendar(db_session, "Berger")
    busy = _tonie(db_session, TONIE_A, konto=konto, campaign=sommer)
    other = _tonie(db_session, TONIE_B, konto=konto, campaign=sommer, name="Wohnzimmer")
    lock = _lock_for(TONIE_A)
    lock.acquire()
    try:
        page = setup_client.get("/admin/setup")
        assert "wird gerade bespielt" in page.text
        moved = setup_client.post(
            f"/admin/setup/tonies/{busy.id}/umhaengen", data={"kalender": str(berger.id)}
        )
        detached = setup_client.post(
            f"/admin/setup/tonies/{busy.id}/umhaengen", data={"kalender": ""}
        )
        credentials = setup_client.post(
            f"/admin/setup/konten/{konto.id}/pruefen",
            data={"username": "opa@example.test", "password": "neu"},
        )
        freed = setup_client.post(
            f"/admin/setup/tonies/{other.id}/umhaengen", data={"kalender": str(berger.id)}
        )
    finally:
        lock.release()

    assert "läuft gerade ein Lauf" in moved.text
    assert "läuft gerade ein Lauf" in detached.text
    assert "läuft gerade ein Lauf" in credentials.text
    db_session.expire_all()
    assert db_session.get(CreativeTonie, busy.id).campaign_id == sommer.id
    assert decrypt(KEY, db_session.get(TonieKonto, konto.id).password) == "pw"
    assert freed.status_code == 200
    assert db_session.get(CreativeTonie, other.id).campaign_id == berger.id


def test_assigning_tonie_of_a_calendar_to_a_second_needs_moving_r5(setup_client, db_session):
    konto = _konto(db_session, "opa@example.test", "pw")
    sommer, berger = _calendar(db_session, "Familie Sommer"), _calendar(db_session, "Berger")
    tonie = _tonie(db_session, TONIE_A, konto=konto, campaign=sommer)

    response = setup_client.post(
        f"/admin/setup/konten/{konto.id}/zuordnung",
        data={f"kalender_{tonie.id}": str(berger.id)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/admin/setup/tonies/{tonie.id}/umhaengen")
    db_session.expire_all()
    assert db_session.get(CreativeTonie, tonie.id).campaign_id == sommer.id
    confirm = setup_client.get(response.headers["location"])
    assert "Umhängen" in confirm.text and "Berger" in confirm.text


def test_assigning_free_tonie_to_calendar_is_direct(setup_client, db_session):
    konto = _konto(db_session, "opa@example.test", "pw")
    sommer = _calendar(db_session, "Familie Sommer")
    tonie = _tonie(db_session, TONIE_A, konto=konto)

    setup_client.post(
        f"/admin/setup/konten/{konto.id}/zuordnung", data={f"kalender_{tonie.id}": str(sommer.id)}
    )

    db_session.expire_all()
    assert db_session.get(CreativeTonie, tonie.id).campaign_id == sommer.id


def test_detach_page_asks_before_cleanup(setup_client, db_session):
    konto = _konto(db_session, "opa@example.test", "pw")
    sommer = _calendar(db_session, "Familie Sommer")
    tonie = _tonie(db_session, TONIE_A, konto=konto, campaign=sommer, name="Wohnzimmer")

    page = setup_client.get(f"/admin/setup/tonies/{tonie.id}/umhaengen?kalender=")

    assert "Wohnzimmer trennen?" in page.text
    assert "Trennen und Kapitel abräumen" in page.text


def _with_tonie_client(client: TestClient, handler) -> None:
    client.app.dependency_overrides[get_toniecloud_factory] = lambda: FixedFactory(
        make_tonie_client(handler)
    )


def test_detach_route_removes_app_chapter_ae11(setup_client, db_session):
    konto = _konto(db_session, "opa@example.test", "pw")
    sommer = _calendar(db_session, "Familie Sommer")
    tonie = _tonie(
        db_session, TONIE_ID, konto=konto, campaign=sommer, app_chapters='[{"id": "app-5"}]'
    )
    app5 = {"id": "app-5", "title": "Tag 5", "file": "app-5"}
    handler, patches = build_handler(
        tonie_gets=[
            creative_tonie_response(chapters=[app5, *STOCK]),
            creative_tonie_response(chapters=STOCK),
        ],
        tonie_patches=[creative_tonie_response(chapters=STOCK)],
        file_ids=(),
    )
    _with_tonie_client(setup_client, handler)

    response = setup_client.post(f"/admin/setup/tonies/{tonie.id}/umhaengen", data={"kalender": ""})

    assert response.status_code == 200
    assert [c["id"] for c in patches[0]["chapters"]] == ["familie-1", "familie-2"]
    db_session.expire_all()
    tonie = db_session.get(CreativeTonie, tonie.id)
    assert (tonie.campaign_id, tonie.app_chapters, tonie.abraeumen_offen) == (None, None, False)
    assert "getrennt" in response.text


def test_detach_route_with_failing_cleanup_keeps_memory_and_hints(setup_client, db_session):
    konto = _konto(db_session, "opa@example.test", "pw")
    sommer, berger = _calendar(db_session, "Familie Sommer"), _calendar(db_session, "Berger")
    tonie = _tonie(
        db_session, TONIE_ID, konto=konto, campaign=sommer, app_chapters='[{"id": "app-5"}]'
    )
    app5 = {"id": "app-5", "title": "Tag 5", "file": "app-5"}
    handler, patches = build_handler(
        tonie_gets=[creative_tonie_response(chapters=[app5, *STOCK])],
        tonie_patches=[httpx.Response(500), creative_tonie_response(chapters=[app5, *STOCK])],
        file_ids=(),
    )
    _with_tonie_client(setup_client, handler)

    response = setup_client.post(
        f"/admin/setup/tonies/{tonie.id}/umhaengen", data={"kalender": str(berger.id)}
    )

    assert len(patches) == 2  # Abraeumen, dann Rueckfallstand
    assert "konnte nicht entfernt werden" in response.text
    db_session.expire_all()
    tonie = db_session.get(CreativeTonie, tonie.id)
    assert tonie.abraeumen_offen is True
    assert tonie.campaign_id == berger.id


def test_tonie_known_via_konto1_assigned_from_konto2_list_switches_konto(
    setup_client, transport, db_session
):
    konto1 = _konto(db_session, "eins@example.test", "pw-eins")
    tonie = _tonie(db_session, TONIE_A, konto=konto1)
    transport.tonies_by_user["zwei@example.test"] = [{"id": TONIE_A, "name": "Kinderzimmer"}]

    checked = setup_client.post(
        "/admin/setup/konten", data={"username": "zwei@example.test", "password": "pw-zwei"}
    )
    konto2 = db_session.execute(
        select(TonieKonto).where(TonieKonto.username == "zwei@example.test")
    ).scalar_one()
    db_session.expire_all()
    assert db_session.get(CreativeTonie, tonie.id).konto_id == konto1.id  # nicht doppelt
    assert db_session.execute(select(CreativeTonie)).scalars().all() == [tonie]
    assert 'name="wechsel"' in checked.text

    setup_client.post(
        f"/admin/setup/konten/{konto2.id}/zuordnung",
        data={"wechsel": str(tonie.id), f"kalender_{tonie.id}": ""},
    )

    db_session.expire_all()
    tonie = db_session.get(CreativeTonie, tonie.id)
    assert tonie.konto_id == konto2.id
    factory = TonieCloudFactory(KEY, transport=httpx.MockTransport(transport), sleep=lambda s: None)
    transport.logins.clear()
    client_for(factory, db_session, tonie).list_creative_tonies()
    assert transport.logins == ["zwei@example.test"]


# --- Mailversand (R19, R20) -------------------------------------------------------


def test_smtp_failing_testmail_keeps_old_access_ae3(setup_client, smtp_calls, db_session):
    smtp_calls.result = SmtpPruefung(False, "Anmeldung fehlgeschlagen")

    response = setup_client.post(
        "/admin/setup/smtp",
        data={
            "host": "smtp.neu.test",
            "port": "465",
            "user": "neu-user",
            "password": "platzhalter-smtp-neu",
            "from_address": "neu@example.test",
        },
    )

    assert "Anmeldung fehlgeschlagen" in response.text
    assert "platzhalter-smtp-neu" not in response.text
    assert smtp_calls[0]["to_address"] == "admin@example.test"
    db_session.expire_all()
    row = db_session.get(Einstellungen, 1)
    assert (row.smtp_host, row.smtp_user) == ("smtp-relay.brevo.com", "test-smtp-user")
    assert decrypt(KEY, row.smtp_password) == "test-smtp-pass"


def test_smtp_host_changed_with_empty_password_is_refused_without_connection(
    setup_client, smtp_calls, db_session
):
    response = setup_client.post(
        "/admin/setup/smtp",
        data={
            "host": "smtp.fremd.test",
            "port": "587",
            "user": "test-smtp-user",
            "password": "",
            "from_address": "vorlesezeit@example.test",
        },
    )

    assert smtp_calls == []
    assert "Bitte das Passwort für den neuen Zugang eingeben" in response.text
    db_session.expire_all()
    assert db_session.get(Einstellungen, 1).smtp_host == "smtp-relay.brevo.com"


def test_smtp_unchanged_with_empty_password_checks_and_marks_checked(
    setup_client, smtp_calls, db_session
):
    response = setup_client.post(
        "/admin/setup/smtp",
        data={
            "host": "smtp-relay.brevo.com",
            "port": "587",
            "user": "test-smtp-user",
            "password": "",
            "from_address": "vorlesezeit@example.test",
        },
    )

    assert smtp_calls[0]["password"] == "test-smtp-pass"
    assert "test-smtp-pass" not in response.text
    db_session.expire_all()
    row = db_session.get(Einstellungen, 1)
    assert row.smtp_checked_at is not None
    assert json.loads(row.herkunft)["smtp_host"]["ungeprueft"] is False


def test_smtp_new_values_with_successful_check_are_saved(setup_client, smtp_calls, db_session):
    setup_client.post(
        "/admin/setup/smtp",
        data={
            "host": "smtp.neu.test",
            "port": "465",
            "user": "neu-user",
            "password": "platzhalter-smtp-neu",
            "from_address": "neu@example.test",
        },
    )

    assert smtp_calls[0]["host"] == "smtp.neu.test"
    db_session.expire_all()
    row = db_session.get(Einstellungen, 1)
    assert (row.smtp_host, row.smtp_port, row.smtp_user) == ("smtp.neu.test", 465, "neu-user")
    assert decrypt(KEY, row.smtp_password) == "platzhalter-smtp-neu"


# --- Termine, Name, Kalender (R17, R22, R26, R43) ------------------------------------


def test_delivery_time_outside_window_is_refused(setup_client, db_session):
    response = setup_client.post(
        "/admin/setup/termine",
        data={
            "delivery_time": "06:30",
            "recording_deadline": "2026-11-24",
            "invitation_date": "",
            "magic_link_valid_until": "2026-12-31",
        },
    )

    assert "zwischen 17:00 und 23:00" in response.text
    db_session.expire_all()
    assert db_session.get(Einstellungen, 1).delivery_time == "20:00"


def test_delivery_time_change_in_open_window_applies_tomorrow(setup_client, db_session):
    setup_client.app.dependency_overrides[get_now] = lambda: berlin(2026, 12, 5, 20, 30)

    response = setup_client.post(
        "/admin/setup/termine",
        data={
            "delivery_time": "21:00",
            "recording_deadline": "2026-11-24",
            "invitation_date": "2026-11-01",
            "magic_link_valid_until": "2026-12-31",
        },
    )

    assert "gilt ab morgen" in response.text
    db_session.expire_all()
    row = db_session.get(Einstellungen, 1)
    assert (row.delivery_time, row.delivery_time_pending) == ("20:00", "21:00")
    assert row.invitation_date.isoformat() == "2026-11-01"


def test_display_name_is_saved(setup_client, db_session):
    setup_client.post("/admin/setup/name", data={"admin_display_name": "Luca"})

    db_session.expire_all()
    assert db_session.get(Einstellungen, 1).admin_display_name == "Luca"


def test_child_name_is_saved_and_cleared(setup_client, db_session):
    setup_client.post(
        "/admin/setup/name", data={"admin_display_name": "Luca", "kind_name": " Emma & Lukas "}
    )
    db_session.expire_all()
    row = db_session.get(Einstellungen, 1)
    assert (row.admin_display_name, row.kind_name) == ("Luca", "Emma & Lukas")
    assert 'value="Emma &amp; Lukas"' in setup_client.get("/admin/setup").text

    setup_client.post("/admin/setup/name", data={"admin_display_name": "Luca", "kind_name": ""})
    db_session.expire_all()
    assert db_session.get(Einstellungen, 1).kind_name is None


def test_create_calendar_with_24_days_and_rename(setup_client, db_session):
    setup_client.post("/admin/setup/kalender", data={"name": "Familie Berger"})
    setup_client.post("/admin/setup/kalender", data={"name": "Patenkinder"})

    calendars = db_session.execute(select(Campaign).order_by(Campaign.id)).scalars().all()
    assert [c.name for c in calendars] == ["Familie Berger", "Patenkinder"]
    for calendar in calendars:
        days = db_session.execute(select(Slot.day).where(Slot.campaign_id == calendar.id))
        assert sorted(days.scalars()) == list(range(1, 25))

    setup_client.post(f"/admin/setup/kalender/{calendars[0].id}", data={"name": "Bergers"})

    db_session.expire_all()
    assert db_session.get(Campaign, calendars[0].id).name == "Bergers"


# --- Herkunft (R41, R42) --------------------------------------------------------------


def test_startwert_hints_ungeprueft_after_seed(setup_client):
    page = setup_client.get("/admin/setup")

    assert "ungeprüft" in page.text
    assert "aus der Umgebung übernommen" in page.text


def test_deviating_environment_is_shown(configured_env, monkeypatch, config, db_session):
    with TestClient(create_app()):
        pass  # erster Start uebernimmt die Startwerte
    monkeypatch.setenv("SMTP_HOST", "smtp.anders.test")
    with TestClient(create_app(), headers=ORIGIN) as client:
        admin = db_session.execute(select(Person).where(Person.is_admin.is_(True))).scalar_one()
        client.post("/login/confirm", data={"token": create_magic_link_token(config, admin)})

        page = client.get("/admin/setup")

    assert "Umgebung weicht ab" in page.text


# --- Konto loeschen ----------------------------------------------------------------


def _env_konto(db_session) -> TonieKonto:
    """Das beim App-Start aus der Umgebung uebernommene Konto (R28)."""
    return db_session.execute(
        select(TonieKonto).where(TonieKonto.username == "test-tonie-user")
    ).scalar_one()


def test_delete_konto_removes_konto_and_its_free_tonies(setup_client, db_session, config):
    konto = _env_konto(db_session)
    tonie = _tonie(db_session, TONIE_A, konto=konto, name="Omas Tonie")
    other = _konto(db_session, "zwei@example.test", "pw")
    kept = _tonie(db_session, TONIE_B, konto=other, name="Tonie im Auto")
    konto_id, tonie_pk = konto.id, tonie.id

    confirm = setup_client.get(f"/admin/setup/konten/{konto.id}/loeschen")
    response = setup_client.post(f"/admin/setup/konten/{konto.id}/loeschen")

    assert "Omas Tonie" in confirm.text and "Konto löschen" in confirm.text
    assert response.status_code == 200
    assert "gelöscht" in response.text
    db_session.expire_all()
    assert db_session.get(TonieKonto, konto_id) is None
    assert db_session.get(CreativeTonie, tonie_pk) is None
    assert db_session.get(CreativeTonie, kept.id).konto_id == other.id
    data = st.herkunft(db_session)
    assert data["tonie_username"]["konto_id"] is None
    assert data["tonie_password"]["konto_id"] is None
    # R28: kein Neu-Seed aus der Umgebung beim naechsten Start.
    st.seed_from_env(db_session, config, now=berlin(2026, 10, 4))
    usernames = db_session.scalars(select(TonieKonto.username)).all()
    assert usernames == ["zwei@example.test"]


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"campaign": True}, "zuerst trennen"),
        ({"app_chapters": '[{"id": "app-5"}]'}, "Kapitel der App"),
        ({"abraeumen_offen": True}, "Kapitel der App"),
    ],
)
def test_delete_konto_refused_while_a_tonie_is_in_use(setup_client, db_session, fields, reason):
    konto = _konto(db_session, "opa@example.test", "pw")
    fields = dict(fields)
    if fields.pop("campaign", False):
        fields["campaign"] = _calendar(db_session, "Familie Sommer")
    tonie = _tonie(db_session, TONIE_A, konto=konto, **fields)

    confirm = setup_client.get(f"/admin/setup/konten/{konto.id}/loeschen")
    response = setup_client.post(f"/admin/setup/konten/{konto.id}/loeschen")

    assert reason in confirm.text
    assert "Konto löschen</button>" not in confirm.text
    assert reason in response.text
    db_session.expire_all()
    assert db_session.get(TonieKonto, konto.id) is not None
    assert db_session.get(CreativeTonie, tonie.id) is not None


def test_delete_konto_refused_while_a_run_holds_the_lock(setup_client, db_session):
    konto = _konto(db_session, "opa@example.test", "pw")
    _tonie(db_session, TONIE_A, konto=konto)
    lock = _lock_for(TONIE_A)
    lock.acquire()
    try:
        response = setup_client.post(f"/admin/setup/konten/{konto.id}/loeschen")
    finally:
        lock.release()

    assert "läuft gerade ein Lauf" in response.text
    db_session.expire_all()
    assert db_session.get(TonieKonto, konto.id) is not None


def test_delete_unknown_konto_is_404(setup_client):
    assert setup_client.get("/admin/setup/konten/999/loeschen").status_code == 404
    assert setup_client.post("/admin/setup/konten/999/loeschen").status_code == 404


# --- Kalender loeschen -------------------------------------------------------------


def _slot(db_session, campaign: Campaign, day: int) -> Slot:
    return db_session.execute(
        select(Slot).where(Slot.campaign_id == campaign.id, Slot.day == day)
    ).scalar_one()


def _run(db_session, campaign: Campaign, run_type: str) -> DeliveryRun:
    run = DeliveryRun(
        campaign_id=campaign.id,
        tonie_id=TONIE_A,
        run_type=run_type,
        started_at=datetime(2026, 10, 1, 18, 0),
        outcome="erfolg",
    )
    db_session.add(run)
    db_session.commit()
    return run


def _two_calendars(db_session) -> tuple[Campaign, Campaign]:
    familie = new_campaign(db_session, name="Familie")
    berger = new_campaign(db_session, name="Berger")
    db_session.commit()
    return familie, berger


def test_delete_calendar_removes_days_and_history_keeps_recordings(setup_client, db_session):
    familie, berger = _two_calendars(db_session)
    admin = db_session.execute(select(Person).where(Person.is_admin.is_(True))).scalar_one()
    only = Auftrag(title="Nur bei Berger")
    both = Auftrag(title="In beiden")
    db_session.add_all([only, both])
    db_session.flush()
    _slot(db_session, berger, 3).auftrag_id = only.id
    _slot(db_session, berger, 2).auftrag_id = both.id
    _slot(db_session, familie, 1).auftrag_id = both.id
    beitrag = Beitrag(person_id=admin.id, auftrag_id=only.id, title="Sterne")
    db_session.add(beitrag)
    db_session.flush()
    berger.replacement_beitrag_id = beitrag.id
    db_session.add(Abendmeldung(evening=datetime(2026, 10, 1).date(), sent_at=datetime.now()))
    db_session.commit()
    own = _run(db_session, berger, "vorabend").id
    foreign = _run(db_session, familie, "vorabend").id
    berger_id, familie_id = berger.id, familie.id

    confirm = setup_client.get(f"/admin/setup/kalender/{berger.id}/loeschen")
    response = setup_client.post(f"/admin/setup/kalender/{berger.id}/loeschen")

    assert "Nur bei Berger" in confirm.text and "In beiden" not in confirm.text
    assert "Verlauf" in confirm.text
    assert response.status_code == 200 and "gelöscht" in response.text
    db_session.expire_all()
    assert db_session.get(Campaign, berger_id) is None
    remaining = db_session.scalars(select(Slot).where(Slot.campaign_id == berger_id)).all()
    assert remaining == []
    assert db_session.get(Auftrag, only.id).slots == []  # Entwurf
    assert [s.campaign_id for s in db_session.get(Auftrag, both.id).slots] == [familie_id]
    assert db_session.get(Beitrag, beitrag.id) is not None
    assert db_session.get(DeliveryRun, own) is None
    assert db_session.get(DeliveryRun, foreign) is not None
    assert db_session.scalars(select(Abendmeldung)).all() != []


def test_delete_placeholder_calendar_moves_runs_without_day_to_next(setup_client, db_session):
    familie, berger = _two_calendars(db_session)
    moved = [_run(db_session, familie, t).id for t in ("manuell", "abraeumen", "aufraeumen")]
    dropped = _run(db_session, familie, "vorabend").id

    setup_client.post(f"/admin/setup/kalender/{familie.id}/loeschen")

    db_session.expire_all()
    assert [db_session.get(DeliveryRun, r).campaign_id for r in moved] == [berger.id] * 3
    assert db_session.get(DeliveryRun, dropped) is None


def test_delete_calendar_refused_while_a_tonie_belongs_to_it(setup_client, db_session):
    familie, berger = _two_calendars(db_session)
    _tonie(db_session, TONIE_A, campaign=berger, name="Wohnzimmer")

    confirm = setup_client.get(f"/admin/setup/kalender/{berger.id}/loeschen")
    response = setup_client.post(f"/admin/setup/kalender/{berger.id}/loeschen")

    assert "Wohnzimmer" in confirm.text and "Kalender löschen</button>" not in confirm.text
    assert "zuerst trennen" in response.text
    db_session.expire_all()
    assert db_session.get(Campaign, berger.id) is not None


def test_last_calendar_cannot_be_deleted(setup_client, db_session):
    familie = new_campaign(db_session, name="Familie")
    db_session.commit()

    confirm = setup_client.get(f"/admin/setup/kalender/{familie.id}/loeschen")
    response = setup_client.post(f"/admin/setup/kalender/{familie.id}/loeschen")

    assert "letzte Kalender" in confirm.text and "Kalender löschen</button>" not in confirm.text
    assert "letzte Kalender" in response.text
    db_session.expire_all()
    assert db_session.get(Campaign, familie.id) is not None


def test_chosen_calendar_deleted_falls_back_safely(setup_client, db_session):
    familie, berger = _two_calendars(db_session)
    setup_client.post("/admin/tonie", data={"auswahl": f"k:{berger.id}", "zurueck": "/admin"})

    setup_client.post(f"/admin/setup/kalender/{berger.id}/loeschen")

    assert setup_client.get("/admin").status_code == 200
    assert setup_client.get("/admin/setup").status_code == 200


def test_delete_unknown_calendar_is_404(setup_client):
    assert setup_client.get("/admin/setup/kalender/999/loeschen").status_code == 404
    assert setup_client.post("/admin/setup/kalender/999/loeschen").status_code == 404


# --- Schutz (KTD13, Admin) ----------------------------------------------------------

POST_ROUTES = [
    "/admin/setup/kalender",
    "/admin/setup/kalender/1",
    "/admin/setup/konten",
    "/admin/setup/konten/1/pruefen",
    "/admin/setup/konten/1/zuordnung",
    "/admin/setup/tonies/1/umhaengen",
    "/admin/setup/kalender/1/loeschen",
    "/admin/setup/konten/1/loeschen",
    "/admin/setup/smtp",
    "/admin/setup/termine",
    "/admin/setup/name",
]


@pytest.mark.parametrize("route", POST_ROUTES)
@pytest.mark.parametrize("origin", [None, "https://boese.example", "null"])
def test_post_without_matching_origin_is_403(admin_client, smtp_calls, route, origin):
    headers = {} if origin is None else {"Origin": origin}

    response = admin_client.post(route, data={"name": "X"}, headers=headers)

    assert response.status_code == 403
    assert smtp_calls == []


def test_origin_compares_against_forwarded_scheme(admin_client, db_session):
    """Hinter dem Tunnel (uvicorn --proxy-headers) ist das Schema https."""
    response = admin_client.post(
        "/admin/setup/name",
        data={"admin_display_name": "Luca"},
        headers={"Origin": "https://testserver"},
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "route",
    [
        "/admin/setup",
        "/admin/setup/tonies/1/umhaengen?kalender=",
        "/admin/setup/kalender/1/loeschen?",
        "/admin/setup/konten/1/loeschen?",
    ]
    + POST_ROUTES,
)
def test_ordinary_person_gets_403_everywhere(person_client, route):
    if route.startswith("/admin/setup/") and "?" not in route:
        response = person_client.post(route, data={"name": "X"}, headers=ORIGIN)
    else:
        response = person_client.get(route)
    assert response.status_code == 403


def test_not_logged_in_is_sent_to_login(client):
    response = client.post("/admin/setup/name", headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
