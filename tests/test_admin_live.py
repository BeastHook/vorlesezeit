"""Knopf-Rueckmeldung und Live-Status (Mockup
docs/design/feedback-live-status-mockup.html, abgenommen 2026-10-03).

Kein JS-Testrahmen: geprueft werden die Statusadressen und die Markierungen,
an denen app/static/admin.js ansetzt."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.orm import Session

from app.delivery.job import _lock_for
from app.models import Campaign, CreativeTonie, DeliveryRun
from tests.test_admin_einstellungen import ORIGIN, TONIE_A, _calendar, _konto, _tonie
from tests.test_delivery import TONIE_ID, TONIE_ID_2, make_calendar_tonie

STATUS = "/admin/auslieferung/status"
SETUP_STATUS = "/admin/setup/status"


@pytest.fixture
def campaign(db_session: Session) -> Campaign:
    campaign, _tonie = make_calendar_tonie(db_session)
    return campaign


def _run(db_session, campaign, *, tonie_id=TONIE_ID, outcome="gestartet", **fields) -> DeliveryRun:
    run = DeliveryRun(
        campaign_id=campaign.id,
        tonie_id=tonie_id,
        run_type=fields.pop("run_type", "anstoss"),
        target_day=fields.pop("target_day", 5),
        started_at=datetime(2026, 12, 4, 14, 0),
        outcome=outcome,
        **fields,
    )
    db_session.add(run)
    db_session.commit()
    return run


# --- Zugriff --------------------------------------------------------------------


@pytest.mark.parametrize("url", [STATUS, SETUP_STATUS])
def test_status_for_ordinary_person_is_403(person_client, url):
    assert person_client.get(url).status_code == 403


@pytest.mark.parametrize("url", [STATUS, SETUP_STATUS])
def test_status_not_logged_in_is_sent_to_login(client, url):
    response = client.get(url, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- Verlauf -------------------------------------------------------------------------


def test_status_reports_running_run_then_its_result(admin_client, db_session, campaign):
    run = _run(db_session, campaign)

    data = admin_client.get(STATUS, params={"ids": str(run.id)}).json()

    assert data["running"] is True
    [row] = data["runs"]
    assert row["id"] == run.id
    assert row["running"] is True
    assert (row["outcome_text"], row["outcome_class"]) == ("läuft", "tag tag-neutral")

    run.outcome = "fehlschlag"
    run.reason = "Hochladen fehlgeschlagen"
    db_session.commit()

    data = admin_client.get(STATUS, params={"ids": str(run.id)}).json()

    assert data["running"] is False
    [row] = data["runs"]
    assert row["running"] is False
    assert (row["outcome_text"], row["outcome_class"]) == ("fehlgeschlagen", "tag tag-outline")
    assert row["content"] == "Hochladen fehlgeschlagen"


def test_status_finished_run_shows_same_label_as_table(admin_client, db_session, campaign):
    run = _run(db_session, campaign)
    run.outcome = "erfolg"
    db_session.commit()

    [row] = admin_client.get(STATUS, params={"ids": str(run.id)}).json()["runs"]
    page = admin_client.get("/admin/auslieferung").text

    assert row["outcome_text"] == "erfolgreich"
    assert f'<span class="{row["outcome_class"]}" data-cell="outcome">erfolgreich</span>' in page


def test_status_ignores_runs_of_other_tonies_and_bad_ids(admin_client, db_session, campaign):
    db_session.add(CreativeTonie(tonie_id=TONIE_ID_2, campaign_id=campaign.id))
    db_session.commit()
    other = _run(db_session, campaign, tonie_id=TONIE_ID_2)

    data = admin_client.get(STATUS, params={"ids": f"{other.id},x"}).json()

    assert data == {"runs": [], "running": False}
    assert TONIE_ID_2 not in admin_client.get(STATUS, params={"ids": str(other.id)}).text


def test_page_marks_running_rows_and_polls_only_while_running(admin_client, db_session, campaign):
    done = _run(db_session, campaign, outcome="erfolg")
    page = admin_client.get("/admin/auslieferung").text
    assert f'data-run-id="{done.id}"' in page
    assert "data-live-runs" not in page
    assert "data-running" not in page

    running = _run(db_session, campaign)
    page = admin_client.get("/admin/auslieferung").text
    assert f'data-live-runs="{STATUS}"' in page
    assert f'data-run-id="{running.id}" data-running' in page
    assert "Aktualisiert sich automatisch" in page


# --- Setup ---------------------------------------------------------------------------


def test_setup_status_lists_busy_tonies_until_lock_released(admin_client, db_session):
    konto = _konto(db_session, "opa@example.test", "pw")
    busy = _tonie(db_session, TONIE_A, konto=konto, campaign=_calendar(db_session, "Sommer"))
    lock = _lock_for(TONIE_A)
    lock.acquire()
    try:
        during = admin_client.get(SETUP_STATUS).json()
        page = admin_client.get("/admin/setup").text
    finally:
        lock.release()
    after = admin_client.get(SETUP_STATUS).json()

    assert during == {"busy": [busy.id]}
    assert after == {"busy": []}
    assert TONIE_A not in str(during)
    assert f'data-live-setup="{SETUP_STATUS}"' in page
    assert f'data-busy-tonie="{busy.id}"' in page
    assert "data-live-setup" not in admin_client.get("/admin/setup").text


# --- Knopf-Rueckmeldung --------------------------------------------------------------


@pytest.mark.parametrize("url", ["/admin/auslieferung", "/admin/setup", "/admin/personen"])
def test_admin_pages_load_admin_script(admin_client, campaign, url):
    page = admin_client.get(url).text
    assert '<script src="/static/admin.js?v=' in page
    assert "setup.js" not in page


def test_admin_script_is_served(admin_client):
    response = admin_client.get("/static/admin.js")
    assert response.status_code == 200
    assert "sessionStorage" in response.text


def test_flash_carries_result_for_the_button(admin_client, db_session):
    page = admin_client.post(
        "/admin/setup/name", data={"admin_display_name": "Luca"}, headers=ORIGIN
    ).text
    assert 'class="flash ok" role="status" data-result="ok"' in page

    page = admin_client.post("/admin/setup/kalender", data={"name": " "}, headers=ORIGIN).text
    assert 'class="flash err" role="status" data-result="err"' in page


def test_inline_check_error_counts_as_failed_result(admin_client, db_session):
    """Setup-Pruefungen rendern ihren Fehler direkt (ohne Hinweisbalken)."""
    response = admin_client.post(
        "/admin/setup/konten", data={"username": "", "password": ""}, headers=ORIGIN
    )
    assert 'class="check-note" role="alert" data-result="err"' in response.text


def test_run_buttons_have_start_busy_text(admin_client, campaign):
    page = admin_client.get("/admin/auslieferung").text
    assert page.count('data-busy="Wird gestartet …"') >= 2
    assert 'data-busy="Prüfung läuft …"' in page  # Platz pruefen
