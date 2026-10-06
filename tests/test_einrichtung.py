"""Mehrkalender U4: Einrichtungslink (R30, KTD7, AE12).

Test scenarios aus dem Plan:
- Frische Instanz ohne SMTP: Start erzeugt genau einen Link, das Log enthaelt
  den Pfad, die Datenbank nur den Hash.
- AE12: "letzte Mail scheiterte" gesetzt -> Start erzeugt einen Link, ein
  aelterer ist danach ungueltig.
- Nutzbarer SMTP-Zugang: kein Link.
- Gueltiger Link per POST: Admin-Sitzung, Weiterleitung ins Setup; zweiter
  Gebrauch ergibt "Link ungueltig".
- Abgelaufener Link (Uhr per Dependency verschoben): ungueltig.
- GET allein legt keine Sitzung an.
- Unbekannter Token: ungueltig, ohne Rueckmeldung ueber existierende Links.
- Zugriffslog redigiert den Pfad-Token.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import RedactTokenFilter, create_app
from app.auth.einrichtung import get_einrichtung_now
from app.models import Einrichtungslink, Einstellungen

SMTP_VARS = ("SMTP_HOST", "SMTP_PORT", "SMTPUSER", "SMTPPW", "SMTP_FROM_ADDRESS")
_PATH = re.compile(r"/einrichtung/([A-Za-z0-9_-]+)")


def _tokens(caplog) -> list[str]:
    return [m for r in caplog.records for m in _PATH.findall(r.getMessage())]


def _links(db: Session) -> list[Einrichtungslink]:
    db.expire_all()
    return list(db.scalars(select(Einrichtungslink).order_by(Einrichtungslink.id)))


@pytest.fixture
def no_smtp_env(configured_env, monkeypatch):
    for name in SMTP_VARS:
        monkeypatch.delenv(name)
    return configured_env


@pytest.fixture
def fresh(no_smtp_env, caplog):
    """Frische Instanz ohne SMTP; liefert (client, token aus dem Log)."""
    with caplog.at_level(logging.INFO, logger="app"):
        with TestClient(create_app()) as client:
            [token] = _tokens(caplog)
            yield client, token


def test_fresh_instance_without_smtp_logs_exactly_one_link_db_holds_only_hash(
    fresh, db_session: Session, caplog
):
    _, token = fresh

    [link] = _links(db_session)
    assert link.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert link.used_at is None and link.invalidated_at is None
    assert link.expires_at - link.created_at == timedelta(hours=24)
    # Der Klartext steht in keiner Spalte.
    for row in db_session.execute(select(Einrichtungslink.__table__)).all():
        assert token not in " ".join(str(v) for v in row)
    setup_records = caplog.get_records("setup")
    [record] = [r for r in setup_records if "/einrichtung/" in r.getMessage()]
    assert record.name.startswith("app")
    assert "an die eigene Adresse" in record.getMessage()


def test_usable_smtp_creates_no_link(configured_env, db_session: Session, caplog):
    with caplog.at_level(logging.INFO, logger="app"):
        with TestClient(create_app()):
            pass

    assert _tokens(caplog) == []
    assert _links(db_session) == []


def test_failed_last_mail_creates_new_link_and_invalidates_older_ae12(
    configured_env, db_session: Session, caplog
):
    with TestClient(create_app()):
        pass  # SMTP aus der Umgebung uebernommen, nutzbar
    older = "aelterer-token-aus-einem-frueheren-start"
    now = datetime.now(UTC).replace(tzinfo=None)
    db_session.add(
        Einrichtungslink(
            token_hash=hashlib.sha256(older.encode()).hexdigest(),
            created_at=now,
            expires_at=now + timedelta(hours=24),
        )
    )
    db_session.get(Einstellungen, 1).mail_failed_at = now
    db_session.commit()

    with caplog.at_level(logging.INFO, logger="app"):
        with TestClient(create_app()) as client:
            [token] = _tokens(caplog)
            assert client.post(f"/einrichtung/{older}").status_code == 400
            assert client.post(f"/einrichtung/{token}", follow_redirects=False).status_code == 303

    old, new = _links(db_session)
    assert old.invalidated_at is not None
    assert new.invalidated_at is None


def test_smtp_not_decryptable_creates_link(
    configured_env, db_session: Session, caplog, monkeypatch
):
    with TestClient(create_app()):
        pass
    # Anderer (erfundener) Schluessel: das gespeicherte Passwort ist "neu eingeben".
    monkeypatch.setenv("CREDENTIALS_KEY", "MTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTE=")

    with caplog.at_level(logging.INFO, logger="app"):
        with TestClient(create_app()):
            pass

    assert len(_tokens(caplog)) == 1


def test_get_shows_confirmation_without_session(fresh):
    client, token = fresh

    page = client.get(f"/einrichtung/{token}")

    assert page.status_code == 200
    assert f'action="/einrichtung/{token}"' in page.text
    assert 'method="post"' in page.text
    assert client.get("/admin", follow_redirects=False).status_code == 303
    # Der GET hat den Link nicht verbraucht.
    assert client.post(f"/einrichtung/{token}", follow_redirects=False).status_code == 303


def test_post_creates_admin_session_and_redirects_to_setup(fresh, db_session: Session):
    client, token = fresh

    response = client.post(f"/einrichtung/{token}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/setup"
    assert client.get("/admin").status_code == 200
    [link] = _links(db_session)
    assert link.used_at is not None


def test_second_use_is_invalid(fresh):
    client, token = fresh
    client.post(f"/einrichtung/{token}", follow_redirects=False)
    client.cookies.clear()

    again = client.post(f"/einrichtung/{token}", follow_redirects=False)

    assert again.status_code == 400
    assert "ungültig" in again.text
    assert client.get("/admin", follow_redirects=False).status_code == 303
    assert client.get(f"/einrichtung/{token}").status_code == 400


def test_expired_link_is_invalid(fresh):
    client, token = fresh
    client.app.dependency_overrides[get_einrichtung_now] = lambda: (
        datetime.now(UTC) + timedelta(hours=24, minutes=1)
    )

    assert client.get(f"/einrichtung/{token}").status_code == 400
    response = client.post(f"/einrichtung/{token}", follow_redirects=False)

    assert response.status_code == 400
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_unknown_token_looks_like_any_other_invalid_link(fresh):
    client, token = fresh
    client.post(f"/einrichtung/{token}", follow_redirects=False)
    client.cookies.clear()

    used = client.post(f"/einrichtung/{token}")
    unknown = client.post("/einrichtung/gibt-es-nicht")

    assert unknown.status_code == used.status_code == 400
    assert unknown.text == used.text


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/einrichtung/abc-DEF_123", "/einrichtung/[entfernt]"),
        ("/einrichtung/abc-DEF_123?x=1", "/einrichtung/[entfernt]?x=1"),
        ("/admin/einrichtung", "/admin/einrichtung"),
    ],
)
def test_access_log_redacts_setup_token(path, expected):
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:0", "POST", path, "1.1", 303),
        None,
    )
    assert RedactTokenFilter().filter(record) is True
    assert record.args[2] == expected
