"""Hinweis "Update verfuegbar" im Admin-Bereich (One-Klick-Setup Baustein 4).

Die Abfrage laeuft gegen einen `httpx.MockTransport`, nie gegen GitHub.
"""

from __future__ import annotations

import httpx
import pytest

import app
from app import update_hinweis

TEXT = "Update verfügbar, öffne die Startdatei."


@pytest.fixture
def github(monkeypatch):
    """Attrappe fuer GitHub; `reply` bestimmt die Antwort, `requests` zaehlt."""

    class Github:
        requests: list[httpx.Request] = []
        reply = staticmethod(lambda request: httpx.Response(200, json={"tag_name": "v9.9.9"}))

        def handler(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.reply(request)

    fake = Github()
    fake.requests = []
    monkeypatch.setattr(update_hinweis, "transport", httpx.MockTransport(fake.handler))
    return fake


def _admin_page(admin_client) -> httpx.Response:
    """Zwei Aufrufe: der erste stoesst die Abfrage an, der zweite zeigt ihren Stand."""
    admin_client.get("/admin/kalender")
    update_hinweis._thread.join(timeout=5)
    return admin_client.get("/admin/kalender")


def test_newer_release_shows_banner(admin_client, github, monkeypatch):
    monkeypatch.setattr(app, "__version__", "0.3.0")
    response = _admin_page(admin_client)
    assert response.status_code == 200
    assert TEXT in response.text
    assert 'data-result="update"' in response.text


def test_same_version_shows_no_banner(admin_client, github, monkeypatch):
    monkeypatch.setattr(app, "__version__", "0.3.0")
    github.reply = lambda request: httpx.Response(200, json={"tag_name": "v0.3.0"})
    assert TEXT not in _admin_page(admin_client).text


def test_older_release_shows_no_banner(admin_client, github, monkeypatch):
    monkeypatch.setattr(app, "__version__", "0.3.0")
    github.reply = lambda request: httpx.Response(200, json={"tag_name": "v0.0.1"})
    assert TEXT not in _admin_page(admin_client).text


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, json={"name": "Vorlesezeit 9.9.9"}),
        httpx.Response(200, json={"tag_name": "latest"}),
        httpx.Response(200, text="kein json"),
    ],
)
def test_bad_answer_shows_no_banner(admin_client, github, response):
    github.reply = lambda request: response
    page = _admin_page(admin_client)
    assert page.status_code == 200
    assert TEXT not in page.text


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
def test_network_error_shows_no_banner(admin_client, github, error):
    def fail(request):
        raise error("aus", request=request)

    github.reply = fail
    page = _admin_page(admin_client)
    assert page.status_code == 200
    assert TEXT not in page.text


def test_failure_logs_only_the_class_name(admin_client, github, caplog):
    def fail(request):
        raise httpx.ConnectError("geheimer Text", request=request)

    github.reply = fail
    with caplog.at_level("INFO", logger="app.update_hinweis"):
        _admin_page(admin_client)
    lines = [r.getMessage() for r in caplog.records if r.name == "app.update_hinweis"]
    assert lines == ["Update-Abfrage fehlgeschlagen: fehler=ConnectError"]


def test_two_pages_within_a_day_query_once(admin_client, github):
    _admin_page(admin_client)
    admin_client.get("/admin/personen")
    assert len(github.requests) == 1


def test_failed_query_is_not_repeated_within_a_day(admin_client, github):
    github.reply = lambda request: httpx.Response(500)
    _admin_page(admin_client)
    assert len(github.requests) == 1


def test_query_repeats_after_a_day(admin_client, github, monkeypatch):
    _admin_page(admin_client)
    monkeypatch.setattr(
        update_hinweis,
        "_checked_at",
        update_hinweis._checked_at - update_hinweis.CHECK_INTERVAL_SECONDS - 1,
    )
    _admin_page(admin_client)
    assert len(github.requests) == 2


def test_dev_version_shows_no_banner(admin_client, github, monkeypatch):
    monkeypatch.setattr(app, "__version__", "0.0.0-dev")
    assert TEXT not in _admin_page(admin_client).text


def test_version_comparison():
    assert update_hinweis.is_newer("0.10.0", "0.3.0")
    assert not update_hinweis.is_newer("0.3.0", "0.10.0")
    assert update_hinweis.is_newer("v1.0.0", "0.9.9")
    assert not update_hinweis.is_newer("v0.3.0", "0.3.0")
    assert not update_hinweis.is_newer("latest", "0.3.0")
    assert not update_hinweis.is_newer("v9.9.9", "0.0.0-dev")


def test_family_page_shows_no_banner(person_client, github):
    person_client.get("/")
    if update_hinweis._thread is not None:
        update_hinweis._thread.join(timeout=5)
    response = person_client.get("/")
    assert TEXT not in response.text
    assert github.requests == []


def test_status_has_no_new_key(admin_client, github):
    before = set(admin_client.get("/status").json())
    _admin_page(admin_client)
    after = admin_client.get("/status").json()
    assert set(after) == before
    assert TEXT not in str(after)


def test_query_goes_to_github_releases(admin_client, github):
    _admin_page(admin_client)
    request = github.requests[0]
    assert request.method == "GET"
    assert str(request.url) == "https://api.github.com/repos/BeastHook/vorlesezeit/releases/latest"
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert "authorization" not in request.headers
