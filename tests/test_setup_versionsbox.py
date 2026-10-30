"""Ruhige Versionszeile oben im Reiter Setup (Factory-Job 024).

Die Abfrage laeuft gegen einen `httpx.MockTransport`, nie gegen GitHub.
"""

from __future__ import annotations

import httpx
import pytest

import app
from app import update_hinweis

BOX = 'data-role="version-box"'


@pytest.fixture
def github(monkeypatch):
    """Attrappe fuer GitHub; `tag` bestimmt den Tag des neuesten Release."""

    class Github:
        tag = "v0.5.0"

        def handler(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"tag_name": self.tag})

    fake = Github()
    monkeypatch.setattr(update_hinweis, "transport", httpx.MockTransport(fake.handler))
    return fake


def _setup_page(admin_client) -> httpx.Response:
    """Zwei Aufrufe: der erste stoesst die Abfrage an, der zweite zeigt ihren Stand."""
    admin_client.get("/admin/setup")
    update_hinweis._thread.join(timeout=5)
    return admin_client.get("/admin/setup")


def _box(html: str) -> str:
    start = html.index(BOX)
    return html[start : html.index("</section>", start)]


def test_box_shows_installed_version(admin_client, github, monkeypatch):
    monkeypatch.setattr(app, "__version__", "0.3.0")
    page = _setup_page(admin_client)
    assert page.status_code == 200
    assert "Version 0.3.0" in _box(page.text)


def test_box_stands_above_section_nav(admin_client, github, monkeypatch):
    monkeypatch.setattr(app, "__version__", "0.3.0")
    html = _setup_page(admin_client).text
    assert html.index(BOX) < html.index('class="setup-nav"')


def test_newer_release_shows_indicator_with_tag(admin_client, github, monkeypatch):
    monkeypatch.setattr(app, "__version__", "0.3.0")
    box = _box(_setup_page(admin_client).text)
    assert 'data-result="version-neu"' in box
    assert "Neue Version v0.5.0 auf GitHub" in box
    assert 'data-result="version-aktuell"' not in box


def test_same_release_shows_current(admin_client, github, monkeypatch):
    monkeypatch.setattr(app, "__version__", "0.3.0")
    github.tag = "v0.3.0"
    box = _box(_setup_page(admin_client).text)
    assert 'data-result="version-aktuell"' in box
    assert 'data-result="version-neu"' not in box


def test_older_release_shows_current(admin_client, github, monkeypatch):
    monkeypatch.setattr(app, "__version__", "0.3.0")
    github.tag = "v0.0.1"
    box = _box(_setup_page(admin_client).text)
    assert 'data-result="version-aktuell"' in box


def test_without_query_result_shows_unknown(admin_client, monkeypatch):
    """conftest laesst jede Abfrage mit 404 scheitern: kein Stand bekannt."""
    monkeypatch.setattr(app, "__version__", "0.3.0")
    box = _box(_setup_page(admin_client).text)
    assert 'data-result="version-unbekannt"' in box
    assert 'data-result="version-neu"' not in box
    assert 'data-result="version-aktuell"' not in box


def test_dev_version_shows_unknown(admin_client, github, monkeypatch):
    monkeypatch.setattr(app, "__version__", "0.0.0-dev")
    box = _box(_setup_page(admin_client).text)
    assert "Version 0.0.0-dev" in box
    assert 'data-result="version-unbekannt"' in box


def test_box_only_on_setup_tab(admin_client, github, monkeypatch):
    monkeypatch.setattr(app, "__version__", "0.3.0")
    _setup_page(admin_client)
    assert BOX not in admin_client.get("/admin/kalender").text


def test_banner_stays_on_setup_page(admin_client, github, monkeypatch):
    monkeypatch.setattr(app, "__version__", "0.3.0")
    html = _setup_page(admin_client).text
    assert 'data-result="update"' in html


@pytest.mark.parametrize(
    ("latest", "running", "stand"),
    [
        ("v0.5.0", "0.3.0", "neu"),
        ("v0.3.0", "0.3.0", "aktuell"),
        ("v0.1.0", "0.3.0", "aktuell"),
        (None, "0.3.0", "unbekannt"),
        ("v9.9.9", "0.0.0-dev", "unbekannt"),
    ],
)
def test_versionsstand(monkeypatch, latest, running, stand):
    monkeypatch.setattr(update_hinweis, "_latest", latest)
    monkeypatch.setattr(app, "__version__", running)
    assert update_hinweis.versionsstand() == stand
    assert update_hinweis.neuester_tag() == latest
