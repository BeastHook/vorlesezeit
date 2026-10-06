"""U1: der Start bricht fruehzeitig und mit klarer Meldung ab.

Test scenarios aus dem Plan:
- Der Zustandsendpunkt antwortet (Version + Zeitbasis).
- Der Start bricht ab, wenn eine erforderliche Umgebungsvariable fehlt.
- Der Start bricht ab, wenn ffmpeg im Image fehlt.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import check_ffmpeg, create_app
from app.config import ConfigError

# configured_env-Fixture kommt aus tests/conftest.py (geteilt mit den
# anderen Testdateien seit U2/U3).


def test_status_reports_version_and_timezone(configured_env):
    with TestClient(create_app()) as client:
        response = client.get("/status")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["timezone"] == "Europe/Berlin"
    assert "version" in body


def test_startup_aborts_when_required_env_var_missing(configured_env, monkeypatch):
    monkeypatch.delenv("STORAGE_BUCKET")

    with pytest.raises(ConfigError, match="STORAGE_BUCKET"):
        with TestClient(create_app()):
            pass


def test_startup_aborts_when_ffmpeg_missing(monkeypatch):
    monkeypatch.setattr("app.shutil.which", lambda _name: None)

    with pytest.raises(RuntimeError, match="ffmpeg"):
        check_ffmpeg()


# U14/KTD19: Pruefadresse der Sicherung. Die Sicherung schreibt nach jedem
# Erfolg ihren Zeitpunkt (ISO, UTC) in eine Marke; die App meldet nur deren
# Alter und antwortet mit einem Fehlerstatus, sobald sie aelter als 36 h ist.

SECRET = {"X-Trigger-Secret": "test-trigger-secret"}


@pytest.fixture
def marker(configured_env, tmp_path, monkeypatch):
    path = tmp_path / "last-success"
    monkeypatch.setenv("BACKUP_MARKER_PATH", str(path))
    return path


def _write_marker(path, age: timedelta) -> None:
    path.write_text((datetime.now(UTC) - age).isoformat())


def test_backup_check_fresh_marker_is_200(marker):
    _write_marker(marker, timedelta(hours=2))

    with TestClient(create_app()) as client:
        response = client.get("/backup/check", headers=SECRET)

    assert response.status_code == 200
    assert response.json().keys() == {"alter_stunden"}
    assert 1.9 < response.json()["alter_stunden"] < 2.1


def test_backup_check_stale_marker_is_5xx(marker):
    _write_marker(marker, timedelta(hours=37))

    with TestClient(create_app()) as client:
        response = client.get("/backup/check", headers=SECRET)

    assert response.status_code >= 500
    assert 36.9 < response.json()["alter_stunden"] < 37.1


def test_backup_check_missing_marker_is_5xx(marker):
    with TestClient(create_app()) as client:
        response = client.get("/backup/check", headers=SECRET)

    assert response.status_code >= 500
    assert response.json() == {"alter_stunden": None}


@pytest.mark.parametrize("headers", [{}, {"X-Trigger-Secret": "falsch"}])
def test_backup_check_without_or_wrong_secret_is_401(marker, headers):
    _write_marker(marker, timedelta(hours=2))

    with TestClient(create_app()) as client:
        response = client.get("/backup/check", headers=headers)

    assert response.status_code == 401
    assert "alter_stunden" not in response.text


def test_tab_icon_is_served_and_linked_on_every_page_family(configured_env):
    with TestClient(create_app()) as client:
        icon = client.get("/static/favicon.svg")
        assert icon.status_code == 200
        assert icon.headers["content-type"].startswith("image/svg+xml")
        assert 'rel="icon"' in client.get("/login").text


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/login/confirm?token=abc.def", "/login/confirm?token=[entfernt]"),
        (
            "/login/confirm?next=/record/free&token=abc.def",
            "/login/confirm?next=/record/free&token=[entfernt]",
        ),
        ("/admin/kalender", "/admin/kalender"),
    ],
)
def test_access_log_never_contains_magic_link_tokens(path, expected):
    """Uvicorns Zugriffslog schreibt die volle Adresse; ein Magic-Link-Token
    darin reicht zum Anmelden."""
    import logging

    from app import RedactTokenFilter

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:0", "GET", path, "1.1", 200),
        None,
    )
    assert RedactTokenFilter().filter(record) is True
    assert record.args[2] == expected
