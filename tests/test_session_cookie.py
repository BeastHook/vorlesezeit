"""Sitzungs-Cookie nur ueber HTTPS (Secure-Flag), ausser SESSION_COOKIE_INSECURE=1.

Die Tests melden sich ueber /login/confirm an und lesen das Set-Cookie der
Antwort. Ohne die Variable laeuft der Client ueber https://testserver, damit
er ein Secure-Cookie auch zuruecksendet.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import create_app
from app.auth.tokens import create_magic_link_token
from app.config import load_config
from app.db import create_db_engine, make_session_factory
from app.models import Person


def _admin_token() -> str:
    config = load_config()
    session = make_session_factory(create_db_engine(config))()
    try:
        admin = session.execute(select(Person).where(Person.is_admin.is_(True))).scalar_one()
        return create_magic_link_token(config, admin)
    finally:
        session.close()


def _session_cookie_header(client: TestClient) -> str:
    response = client.post("/login/confirm", data={"token": _admin_token()}, follow_redirects=False)
    assert response.status_code == 303
    headers = [h for h in response.headers.get_list("set-cookie") if h.startswith("session=")]
    assert len(headers) == 1
    return headers[0]


def _flags(header: str) -> list[str]:
    return [part.strip().lower() for part in header.split(";")[1:]]


def test_session_cookie_is_secure_without_variable(configured_env, monkeypatch):
    monkeypatch.delenv("SESSION_COOKIE_INSECURE", raising=False)
    with TestClient(create_app(), base_url="https://testserver") as client:
        header = _session_cookie_header(client)

    assert "secure" in _flags(header)
    assert "httponly" in _flags(header)
    assert "samesite=lax" in _flags(header)


def test_session_cookie_is_secure_with_other_value(configured_env, monkeypatch):
    monkeypatch.setenv("SESSION_COOKIE_INSECURE", "0")
    with TestClient(create_app(), base_url="https://testserver") as client:
        header = _session_cookie_header(client)

    assert "secure" in _flags(header)


def test_session_cookie_is_not_secure_with_insecure_variable(configured_env, monkeypatch):
    monkeypatch.setenv("SESSION_COOKIE_INSECURE", "1")
    with TestClient(create_app()) as client:
        header = _session_cookie_header(client)
        # Ueber http://testserver sendet der Client das Cookie zurueck.
        assert client.get("/admin", follow_redirects=False).status_code == 200

    assert "secure" not in _flags(header)
    assert "httponly" in _flags(header)


def test_test_environment_sets_insecure_variable(configured_env):
    assert configured_env["SESSION_COOKIE_INSECURE"] == "1"


def test_existing_session_cookie_stays_valid(configured_env, monkeypatch):
    # Ein Cookie aus der App ohne Secure-Flag (heutiges Verhalten) gilt weiter.
    monkeypatch.setenv("SESSION_COOKIE_INSECURE", "1")
    with TestClient(create_app()) as old_client:
        old_header = _session_cookie_header(old_client)
    value = old_header.split(";")[0].split("=", 1)[1]

    monkeypatch.delenv("SESSION_COOKIE_INSECURE")
    with TestClient(create_app(), base_url="https://testserver") as client:
        client.cookies.set("session", value)
        response = client.get("/admin", follow_redirects=False)

    assert response.status_code == 200


def test_magic_link_from_before_stays_valid(configured_env, monkeypatch):
    # Ein vor der Umstellung erzeugter Magic-Link meldet nach der Umstellung an.
    monkeypatch.setenv("SESSION_COOKIE_INSECURE", "1")
    with TestClient(create_app()):
        token = _admin_token()

    monkeypatch.delenv("SESSION_COOKIE_INSECURE")
    with TestClient(create_app(), base_url="https://testserver") as client:
        response = client.post("/login/confirm", data={"token": token}, follow_redirects=False)
        assert response.status_code == 303
        assert client.get("/admin", follow_redirects=False).status_code == 200
