"""U3: Magic-Link-Anmeldung ueber die echten Routen.

Test scenarios aus dem Plan:
- Eine Testperson bestaetigt den Link, ist angemeldet.
- Ein reiner Aufruf der Bestaetigungsseite ohne Absenden verbraucht den Token
  nicht; der anschliessende echte Klick funktioniert.
- Innerhalb der Frist ist der Link beliebig oft nutzbar.
- Die Neuanforderung antwortet fuer eine unbekannte Adresse genauso wie fuer
  eine bekannte.
- Ein Link, der nach dem konfigurierten Enddatum geoeffnet wird, wird
  abgelehnt und bietet die Neuanforderung.
- Covers AE14. Nach einem Widerruf wird eine zuvor gueltige Sitzung
  abgelehnt, und ein zuvor versendeter, noch nicht abgelaufener Link fuehrt
  nicht mehr zur Anmeldung.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import settings
from app.auth.tokens import ExpiredMagicLinkToken, create_magic_link_token, verify_magic_link_token
from app.config import Config
from app.models import Person

NOW = datetime(2026, 10, 2, 12, tzinfo=ZoneInfo("Europe/Berlin"))


def test_login_page_has_snow(client: TestClient):
    assert 'class="schnee"' in client.get("/login").text


def _create_person(db_session: Session, email: str = "verwandte@example.test") -> Person:
    person = Person(email=email, display_name="Verwandte")
    db_session.add(person)
    db_session.commit()
    db_session.refresh(person)
    return person


def test_request_login_link_sends_mail_for_known_address(
    client: TestClient, db_session: Session, monkeypatch
):
    person = _create_person(db_session)
    sent = {}

    def fake_send(config, *, session, to_address, login_url):
        sent["to_address"] = to_address
        sent["login_url"] = login_url

    monkeypatch.setattr("app.auth.routes.send_magic_link_mail", fake_send)

    response = client.post("/login", data={"email": person.email})

    assert response.status_code == 200
    assert sent["to_address"] == person.email
    assert "/login/confirm?token=" in sent["login_url"]


@pytest.mark.parametrize(
    "typed", ["Verwandte@example.test", " verwandte@example.test ", "VERWANDTE@Example.Test\n"]
)
def test_request_login_link_ignores_case_and_surrounding_spaces(
    client: TestClient, db_session: Session, monkeypatch, typed
):
    """Handy-Tastaturen schreiben den ersten Buchstaben gross und haengen
    Leerzeichen an -- die Adresse muss trotzdem gefunden werden."""
    person = _create_person(db_session)
    sent = {}
    monkeypatch.setattr(
        "app.auth.routes.send_magic_link_mail",
        lambda config, *, session, to_address, login_url: sent.setdefault("to", to_address),
    )

    client.post("/login", data={"email": typed})

    assert sent["to"] == person.email


def test_request_login_link_finds_address_stored_with_capitals(
    client: TestClient, db_session: Session, monkeypatch
):
    person = _create_person(db_session, email="Oma.Inge@Example.test")
    sent = {}
    monkeypatch.setattr(
        "app.auth.routes.send_magic_link_mail",
        lambda config, *, session, to_address, login_url: sent.setdefault("to", to_address),
    )

    client.post("/login", data={"email": "oma.inge@example.test"})

    assert sent["to"] == person.email


def test_request_login_link_logs_outcome_without_address(
    client: TestClient, db_session: Session, monkeypatch, caplog
):
    person = _create_person(db_session)
    monkeypatch.setattr("app.auth.routes.send_magic_link_mail", lambda *a, **k: None)

    with caplog.at_level("INFO", logger="app.auth.routes"):
        client.post("/login", data={"email": person.email})
        client.post("/login", data={"email": "unbekannt@example.test"})

    messages = [record.getMessage() for record in caplog.records]
    assert f"Magic Link verschickt: Person #{person.id}" in messages
    assert "Magic Link angefordert fuer unbekannte Adresse" in messages
    assert not any("example.test" in message for message in messages)


def test_request_login_link_identical_response_for_unknown_address(
    client: TestClient, db_session: Session, monkeypatch
):
    person = _create_person(db_session)
    monkeypatch.setattr("app.auth.routes.send_magic_link_mail", lambda *a, **k: None)

    known = client.post("/login", data={"email": person.email})
    unknown = client.post("/login", data={"email": "unbekannt@example.test"})

    assert known.status_code == unknown.status_code == 200
    assert known.text == unknown.text


def test_confirm_get_does_not_create_session(
    client: TestClient, config: Config, db_session: Session
):
    person = _create_person(db_session)
    token = create_magic_link_token(config, person)

    client.get(f"/login/confirm?token={token}")
    home = client.get("/", follow_redirects=False)

    assert home.status_code == 303
    assert home.headers["location"] == "/login"


def test_confirm_post_creates_session_and_link_is_reusable(
    client: TestClient, config: Config, db_session: Session
):
    person = _create_person(db_session)
    token = create_magic_link_token(config, person)

    first = client.post("/login/confirm", data={"token": token})
    assert first.status_code in (200, 303)
    # Ohne offenen Auftrag zeigt R4 die Wahl zwischen den eigenen Geschichten
    # und freier Einreichung (U7) -- die Sitzung selbst ist der Punkt dieses Tests.
    home = client.get("/")
    assert home.status_code == 200
    assert "Meine Geschichten" in home.text

    # Innerhalb der Frist ist der Link beliebig oft nutzbar (KTD6).
    second = client.post("/login/confirm", data={"token": token})
    assert second.status_code in (200, 303)


def test_expired_link_rejected_by_token_verification(config: Config, db_session: Session):
    person = _create_person(db_session)
    token = create_magic_link_token(config, person)

    # Mehrkalender U5: die Frist kommt aus dem Einstellungsdienst.
    settings.set_value(db_session, "magic_link_valid_until", date(2020, 1, 1), now=NOW)

    try:
        verify_magic_link_token(config, db_session, token)
        assert False, "sollte ExpiredMagicLinkToken werfen"
    except ExpiredMagicLinkToken:
        pass


def test_link_expiry_uses_berlin_date_not_server_date(
    config: Config, db_session: Session, monkeypatch
):
    """R29: 31.12. 23:30 UTC ist in Berlin schon der 1.1. -- der Link ist
    abgelaufen, auch wenn die Serveruhr (UTC) noch den 31.12. zeigt."""
    import datetime as dt

    person = _create_person(db_session)
    token = create_magic_link_token(config, person)
    instant = dt.datetime(2026, 12, 31, 23, 30, tzinfo=dt.UTC)

    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz)

    monkeypatch.setattr("app.auth.tokens.datetime", FrozenDateTime)
    settings.set_value(db_session, "magic_link_valid_until", date(2026, 12, 31), now=NOW)

    try:
        verify_magic_link_token(config, db_session, token)
        assert False, "sollte ExpiredMagicLinkToken werfen"
    except ExpiredMagicLinkToken:
        pass


@pytest.mark.parametrize(
    ("next_path", "expected"),
    [
        ("/record/free", "/record/free"),
        ("//evil.example/record/free", "/"),
        ("https://evil.example/record/free", "/"),
        ("/record/../admin", "/"),
        ("/record/auftrag/7?x=1", "/"),
        # KTD14: alte Tuerchen-Adressen werden nicht mehr befolgt.
        ("/record/slot/7", "/"),
        ("/admin", "/"),
        ("", "/"),
    ],
)
def test_confirm_redirects_only_to_whitelisted_recording_path_r27(
    client: TestClient, config: Config, db_session: Session, next_path: str, expected: str
):
    person = _create_person(db_session)
    token = create_magic_link_token(config, person)

    page = client.get("/login/confirm", params={"token": token, "next": next_path})
    assert page.status_code == 200
    response = client.post(
        "/login/confirm", data={"token": token, "next": next_path}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == expected


def test_confirm_redirects_to_auftrag_only_while_assigned_to_person_r27(
    client: TestClient, config: Config, db_session: Session
):
    """Review-Befund U9: nach einer Neuvergabe (R35) fuehrt ein alter
    Ablehnungslink nicht auf eine rohe 403-Seite, sondern auf die Startseite.
    KTD14/R36: dasselbe fuer einen Entwurf ohne Kalendertag."""
    from app.models import Auftrag, Campaign, Slot

    person = _create_person(db_session)
    other = _create_person(db_session, email="andere@example.test")
    campaign = Campaign()
    db_session.add(campaign)
    own = Auftrag(person_id=person.id)
    reassigned = Auftrag(person_id=other.id)
    draft = Auftrag(person_id=person.id)
    db_session.add_all([own, reassigned, draft])
    db_session.flush()
    db_session.add_all(
        [
            Slot(campaign_id=campaign.id, day=4, auftrag=own),
            Slot(campaign_id=campaign.id, day=5, auftrag=reassigned),
        ]
    )
    db_session.commit()
    token = create_magic_link_token(config, person)

    def target(next_path: str) -> str:
        response = client.post(
            "/login/confirm", data={"token": token, "next": next_path}, follow_redirects=False
        )
        assert response.status_code == 303
        return response.headers["location"]

    assert target(f"/record/auftrag/{own.id}") == f"/record/auftrag/{own.id}"
    assert target(f"/record/auftrag/{reassigned.id}") == "/"
    assert target(f"/record/auftrag/{draft.id}") == "/"
    assert target("/record/auftrag/999999") == "/"


def test_confirm_page_carries_next_into_form(
    client: TestClient, config: Config, db_session: Session
):
    person = _create_person(db_session)
    token = create_magic_link_token(config, person)

    page = client.get("/login/confirm", params={"token": token, "next": "/record/auftrag/7"})

    assert 'name="next" value="/record/auftrag/7"' in page.text


def test_expired_link_via_http_offers_new_request(
    client: TestClient, config: Config, db_session: Session
):
    person = _create_person(db_session)
    token = create_magic_link_token(config, person)

    # Enddatum im Einstellungsdienst der laufenden App nachtraeglich in die
    # Vergangenheit setzen -- wirkt ohne Neustart (Mehrkalender U5; KTD6:
    # festes Enddatum, kein rollendes Fenster).
    settings.set_value(db_session, "magic_link_valid_until", date(2020, 1, 1), now=NOW)

    response = client.get(f"/login/confirm?token={token}")

    assert response.status_code == 400
    assert "/login" in response.text


def test_revocation_invalidates_session_and_pending_link(
    client: TestClient, config: Config, db_session: Session
):
    """Covers AE14."""
    person = _create_person(db_session)
    token = create_magic_link_token(config, person)
    client.post("/login/confirm", data={"token": token})
    assert client.get("/").status_code == 200

    # Admin widerruft: Zaehler erhoehen (R39).
    person.access_version += 1
    db_session.commit()

    still_logged_in = client.get("/", follow_redirects=False)
    assert still_logged_in.status_code == 303

    pending_link_response = client.get(f"/login/confirm?token={token}")
    assert pending_link_response.status_code == 400


def test_confirm_get_greets_person_by_name_without_logging_in(
    client: TestClient, config: Config, db_session: Session
):
    person = _create_person(db_session)
    token = create_magic_link_token(config, person)

    response = client.get(f"/login/confirm?token={token}")

    assert response.status_code == 200
    assert "Willkommen, Verwandte" in response.text
    assert "Anmelden und vorlesen" in response.text
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 303
    assert home.headers["location"] == "/login"


def test_confirm_get_names_child(client: TestClient, config: Config, db_session: Session):
    from datetime import datetime

    from app import settings

    settings.set_value(db_session, "kind_name", "Emma", now=datetime(2026, 10, 4))
    token = create_magic_link_token(config, _create_person(db_session))

    response = client.get(f"/login/confirm?token={token}")

    assert "Du bist beim Adventskalender für Emma dabei." in response.text


def test_confirm_get_without_child_keeps_family_wording(
    client: TestClient, config: Config, db_session: Session
):
    token = create_magic_link_token(config, _create_person(db_session))

    response = client.get(f"/login/confirm?token={token}")

    assert "Du bist beim Adventskalender der Familie dabei." in response.text


def test_confirm_get_escapes_display_name(client: TestClient, config: Config, db_session: Session):
    person = Person(email="x@example.test", display_name="<script>x</script>")
    db_session.add(person)
    db_session.commit()
    token = create_magic_link_token(config, person)

    response = client.get(f"/login/confirm?token={token}")

    assert "<script>x</script>" not in response.text
    assert "&lt;script&gt;" in response.text
