"""Job 034: Sammel-Erinnerung im Reiter Personen.

Der Knopf "Alle mit offenen Türchen erinnern (n)" fuehrt auf eine Rueckfrageseite
mit den Namen; der Versand schickt dieselbe Erinnerung wie der Einzelknopf an jede
gewoehnliche Person mit mindestens einem offenen Tuerchen, ohne widerrufene
Personen (`revoked_at` gesetzt). Danach ein Hinweisbalken
"n verschickt, m fehlgeschlagen (Namen)"; `reminded_at` und Log wie beim
Einzelversand.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Person
from tests.test_admin_people import (
    _assign,
    _campaign,
    _freeze_today,
    _person,
    _submit,
    mails,  # noqa: F401 -- Fixture
)

CONFIRM = "/admin/personen/alle-erinnern"


def _setup(db: Session) -> dict[str, Person]:
    """Klaus und Ben haben offene Tuerchen, Bea hat alles abgegeben, Gisela ist
    widerrufen (mit offenem Tuerchen), Nora hat kein Tuerchen."""
    campaign = _campaign(db)
    klaus = _person(db, "klaus@example.test", "Klaus")
    bea = _person(db, "bea@example.test", "Bea")
    gisela = _person(db, "gisela@example.test", "Gisela")
    ben = _person(db, "ben@example.test", "Ben")
    nora = _person(db, "nora@example.test", "Nora")
    _assign(db, campaign, klaus, 14, 15)
    _submit(db, campaign, klaus, 14)
    _assign(db, campaign, bea, 3)
    _submit(db, campaign, bea, 3)
    _assign(db, campaign, gisela, 5)
    gisela.revoked_at = datetime(2026, 10, 30, 9, 0)
    gisela.access_version = 1
    _assign(db, campaign, ben, 7)
    db.commit()
    return {"klaus": klaus, "bea": bea, "gisela": gisela, "ben": ben, "nora": nora}


def _boom_for(*emails: str):
    def send(config, *, session, **kwargs):
        if kwargs["to_address"] in emails:
            raise OSError("smtp down")
        send.sent.append(kwargs)

    send.sent = []
    return send


# --- Knopf in der Liste ------------------------------------------------------


def test_list_shows_button_with_count_of_eligible_persons(
    admin_client: TestClient, db_session: Session
):
    _setup(db_session)

    html = admin_client.get("/admin/personen").text

    assert "Alle mit offenen Türchen erinnern (2)" in html
    assert f'href="{CONFIRM}"' in html


def test_list_hides_button_without_eligible_persons(admin_client: TestClient, db_session: Session):
    campaign = _campaign(db_session)
    bea = _person(db_session, "bea@example.test", "Bea")
    _assign(db_session, campaign, bea, 3)
    _submit(db_session, campaign, bea, 3)
    _person(db_session, "nora@example.test", "Nora")

    html = admin_client.get("/admin/personen").text

    assert "Alle mit offenen Türchen erinnern" not in html
    assert f'href="{CONFIRM}"' not in html


# --- Rueckfrageseite -----------------------------------------------------------


def test_confirmation_page_lists_exactly_eligible_names(
    admin_client: TestClient,
    db_session: Session,
    mails,  # noqa: F811
):
    _setup(db_session)

    response = admin_client.get(CONFIRM)

    assert response.status_code == 200
    html = response.text
    assert "Klaus" in html
    assert "Ben" in html
    for name in ("Bea", "Gisela", "Nora"):
        assert name not in html
    assert f'method="post" action="{CONFIRM}"' in html
    assert 'href="/admin/personen"' in html  # Abbrechen
    assert mails["reminder"] == []


def test_confirmation_page_without_eligible_persons_has_no_form(
    admin_client: TestClient, db_session: Session
):
    _person(db_session, "nora@example.test", "Nora")

    response = admin_client.get(CONFIRM)

    assert response.status_code == 200
    assert "Niemand hat offene Türchen." in response.text
    assert f'action="{CONFIRM}"' not in response.text


def test_non_admin_gets_403(person_client: TestClient, db_session: Session, mails):  # noqa: F811
    _setup(db_session)

    assert person_client.get(CONFIRM).status_code == 403
    assert person_client.post(CONFIRM).status_code == 403
    assert mails["reminder"] == []


# --- Versand -------------------------------------------------------------------


def test_send_reaches_exactly_eligible_persons_like_single_reminder(
    admin_client: TestClient,
    db_session: Session,
    mails,  # noqa: F811
):
    persons = _setup(db_session)

    response = admin_client.post(CONFIRM)

    assert response.status_code == 200  # nach Redirect auf /admin/personen
    sent = {m["to_address"]: m for m in mails["reminder"]}
    assert sorted(sent) == ["ben@example.test", "klaus@example.test"]
    klaus = sent["klaus@example.test"]
    assert klaus["person_id"] == persons["klaus"].id
    assert klaus["display_name"] == "Klaus"
    assert klaus["open_days"] == [15]
    assert "/login/confirm?token=" in klaus["login_url"]
    assert sent["ben@example.test"]["open_days"] == [7]


def test_send_success_flash_and_reminded_at(
    admin_client: TestClient,
    db_session: Session,
    mails,  # noqa: F811
    monkeypatch,
):
    persons = _setup(db_session)
    _freeze_today(monkeypatch, 2026, 11, 20)

    html = admin_client.post(CONFIRM).text

    assert 'data-result="ok"' in html
    assert "Erinnerungen: 2 verschickt, 0 fehlgeschlagen." in html
    db_session.expire_all()
    for key in ("klaus", "ben"):
        reminded = db_session.get(Person, persons[key].id).reminded_at
        assert reminded.date().isoformat() == "2026-11-20"
    for key in ("bea", "gisela", "nora"):
        assert db_session.get(Person, persons[key].id).reminded_at is None
    # Der Widerruf bleibt bestehen; Gisela bekam keine Mail.
    gisela = db_session.get(Person, persons["gisela"].id)
    assert gisela.revoked_at is not None
    assert gisela.access_version == 1


def test_partial_failure_names_failed_persons_and_logs_like_single(
    admin_client: TestClient, db_session: Session, monkeypatch, caplog
):
    persons = _setup(db_session)
    send = _boom_for("klaus@example.test")
    monkeypatch.setattr("app.admin.people.send_reminder_mail", send)

    with caplog.at_level(logging.INFO):
        html = admin_client.post(CONFIRM).text

    assert 'data-result="err"' in html
    assert "Erinnerungen: 1 verschickt, 1 fehlgeschlagen (Klaus)." in html
    assert [m["to_address"] for m in send.sent] == ["ben@example.test"]
    db_session.expire_all()
    assert db_session.get(Person, persons["klaus"].id).reminded_at is None
    assert db_session.get(Person, persons["ben"].id).reminded_at is not None
    assert (
        f"Erinnerungs-Mail fehlgeschlagen: person={persons['klaus'].id} fehler=OSError"
        in caplog.text
    )
    # R20: kein Ausnahmetext, keine Adresse, kein Link im Log.
    assert "smtp down" not in caplog.text
    assert "klaus@example.test" not in caplog.text
    assert "token=" not in caplog.text


def test_all_failed_names_all_in_list_order(
    admin_client: TestClient, db_session: Session, monkeypatch
):
    _setup(db_session)
    monkeypatch.setattr(
        "app.admin.people.send_reminder_mail",
        _boom_for("klaus@example.test", "ben@example.test"),
    )

    html = admin_client.post(CONFIRM).text

    assert "Erinnerungen: 0 verschickt, 2 fehlgeschlagen (Klaus und Ben)." in html


def test_send_without_eligible_persons_sends_nothing(
    admin_client: TestClient,
    db_session: Session,
    mails,  # noqa: F811
):
    campaign = _campaign(db_session)
    bea = _person(db_session, "bea@example.test", "Bea")
    _assign(db_session, campaign, bea, 3)
    _submit(db_session, campaign, bea, 3)

    html = admin_client.post(CONFIRM).text

    assert 'data-result="err"' in html
    assert "Niemand hat offene Türchen, keine Erinnerung verschickt." in html
    assert mails["reminder"] == []


def test_admin_is_never_reminded(
    admin_client: TestClient,
    db_session: Session,
    mails,  # noqa: F811
):
    from sqlalchemy import select

    from app.admin.setup import add_calendar_day, create_auftrag

    campaign = _campaign(db_session)
    admin = db_session.execute(select(Person).where(Person.is_admin.is_(True))).scalar_one()
    slot = next(s for s in campaign.slots if s.day == 9)
    auftrag = create_auftrag(db_session, person_id=admin.id)
    add_calendar_day(
        db_session,
        auftrag.id,
        slot.id,
        now=datetime(2026, 10, 1, 12, tzinfo=ZoneInfo("Europe/Berlin")),
        delivery_time=datetime(2026, 10, 1, 20).time(),
    )
    db_session.commit()

    admin_client.post(CONFIRM)

    assert mails["reminder"] == []
