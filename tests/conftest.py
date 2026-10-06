"""Geteilte Fixtures fuer Env-Konfiguration und DB-Session.

Ab U2/U3 brauchen mehrere Testdateien dieselbe vollstaendige Umgebung und
eine echte (temporaere) SQLite-Session -- hier an einer Stelle statt in
jeder Datei einzeln (vorher nur in test_startup.py).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import create_app
from app.auth.tokens import create_magic_link_token
from app.config import Config, load_config
from app.db import create_db_engine, init_db, make_session_factory
from app.models import Person

REQUIRED_ENV = {
    "APP_TIMEZONE": "Europe/Berlin",
    "STORAGE_ENDPOINT_URL": "http://localhost:9000",
    # Feste lokale Speicher-Entwicklungszugangsdaten aus docker-compose.yml --
    # keine echten Geheimnisse, dieselben Literale wie in test_storage.py.
    # Bis U7 gab es keinen Test, der ueber die volle App wirklich schreibt
    # (put/get); die vorherigen Platzhalterwerte waren nie gegen echtes
    # S3-Speicher gelaufen.
    "STORAGE_ACCESS_KEY": "vorlesezeit",
    "STORAGE_SECRET_KEY": "vorlesezeit-dev-secret",
    "STORAGE_BUCKET": "vorlesezeit-test",
    "SESSION_SECRET_KEY": "test-session-secret",
    "ADMIN_EMAIL": "admin@example.test",
    "MAGIC_LINK_VALID_UNTIL": "2026-12-31",
    "SMTP_HOST": "smtp-relay.brevo.com",
    "SMTP_PORT": "587",
    "SMTPUSER": "test-smtp-user",
    "SMTPPW": "test-smtp-pass",
    "SMTP_FROM_ADDRESS": "vorlesezeit@example.test",
    "TONIE_USERNAME": "test-tonie-user",
    "TONIE_PASSWORD": "test-tonie-pass",
    "TONIE_DELIVERY_TIME": "20:00",
    "TRIGGER_SECRET": "test-trigger-secret",
    # Mehrkalender U3: erfundener, fester Fernet-Schluessel (base64 von 32 x "0"),
    # damit die Startwerte samt Passwoertern uebernommen werden.
    "CREDENTIALS_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
}


@pytest.fixture
def configured_env(tmp_path, monkeypatch) -> dict[str, str]:
    env = dict(REQUIRED_ENV)
    env["DATABASE_PATH"] = str(tmp_path / "test.db")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


@pytest.fixture
def config(configured_env: dict[str, str]) -> Config:
    return load_config()


@pytest.fixture
def db_session(config: Config) -> Session:
    engine = create_db_engine(config)
    init_db(engine)
    session_factory = make_session_factory(engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(configured_env: dict[str, str]) -> TestClient:
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def admin_client(client: TestClient, config: Config, db_session: Session) -> TestClient:
    """Eingeloggter Admin -- die Person mit is_admin=True wird beim
    App-Start idempotent gegen ADMIN_EMAIL angelegt (R33)."""
    admin = db_session.execute(select(Person).where(Person.is_admin.is_(True))).scalar_one()
    token = create_magic_link_token(config, admin)
    client.post("/login/confirm", data={"token": token})
    return client


@pytest.fixture
def person_client(client: TestClient, config: Config, db_session: Session) -> TestClient:
    """Eingeloggte, gewoehnliche (nicht-Admin) Person."""
    person = Person(email="verwandte@example.test", display_name="Verwandte")
    db_session.add(person)
    db_session.commit()
    db_session.refresh(person)

    token = create_magic_link_token(config, person)
    client.post("/login/confirm", data={"token": token})
    return client
