"""Herkunftspruefung fuer alle abschickenden Anfragen (CSRF, One-Klick-Setup).

Freunde-Instanzen liegen unter <name>.vorlesezeit.app und sind damit fuer den
Browser dieselbe Website wie die eigene Instanz: SameSite=Lax schuetzt dort
nicht. Ein Formular von einer anderen Adresse wird deshalb abgewiesen.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

EIGEN = "http://testserver"
ABGEWIESEN = "Formular kommt nicht von dieser Seite."


def _login(client: TestClient, headers: dict[str, str]):
    return client.post("/login", data={"email": "x@example.test"}, headers=headers)


@pytest.mark.parametrize(
    "origin",
    [
        "https://mueller.vorlesezeit.app",
        "http://testserver.evil.example",
        "null",
        "https://testserver",
    ],
)
def test_fremde_herkunft_wird_abgewiesen(client: TestClient, origin: str):
    response = _login(client, {"Origin": origin})
    assert response.status_code == 403
    assert response.text == ABGEWIESEN


def test_eigene_herkunft_geht_durch(client: TestClient):
    assert _login(client, {"Origin": EIGEN}).status_code == 200


@pytest.mark.parametrize("site", ["same-site", "cross-site"])
def test_browser_ohne_origin_aber_mit_fremder_herkunft(client: TestClient, site: str):
    assert _login(client, {"Sec-Fetch-Site": site}).status_code == 403


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_browser_ohne_origin_von_hier(client: TestClient, site: str):
    assert _login(client, {"Sec-Fetch-Site": site}).status_code == 200


def test_ohne_browser_kopfzeilen_geht_durch(client: TestClient):
    # Zeitplan-Container, cron-job.org, curl: kein Origin, kein Sec-Fetch-Site.
    assert _login(client, {}).status_code == 200


def test_lesende_anfragen_werden_nie_geprueft(client: TestClient):
    response = client.get("/login", headers={"Origin": "https://mueller.vorlesezeit.app"})
    assert response.status_code == 200
