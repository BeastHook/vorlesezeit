"""Deutsche 404-Seite fuer Browser, unveraenderte Antworten fuer alle anderen."""

from __future__ import annotations

from fastapi.testclient import TestClient

HTML = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


def test_unknown_path_in_browser_gets_german_page(client: TestClient) -> None:
    response = client.get("/gibt-es-nicht", headers=HTML)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert 'lang="de"' in response.text
    assert "<title>Seite nicht gefunden" in response.text
    assert "Diese Seite gibt es nicht" in response.text
    assert 'href="/"' in response.text


def test_unknown_path_without_html_accept_stays_json(client: TestClient) -> None:
    response = client.get("/gibt-es-nicht")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_route_raised_404_in_browser_gets_german_page(person_client: TestClient) -> None:
    response = person_client.get("/archiv/999999/audio", headers=HTML)
    assert response.status_code == 404
    assert "Diese Seite gibt es nicht" in response.text


def test_login_redirect_unchanged(client: TestClient) -> None:
    response = client.get("/", headers=HTML, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
