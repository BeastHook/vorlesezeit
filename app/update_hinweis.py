"""Hinweis "Update verfuegbar" im Admin-Bereich (One-Klick-Setup Baustein 4).

Hoechstens einmal je 24 Stunden je Prozess liest die App das neueste
GitHub-Release (nur lesen, ohne Zugangsdaten). Die Abfrage laeuft in einem
Daemon-Thread, angestossen von `app/admin/common.py::render`; die Seite liest
nur den gemerkten Stand. Eine gescheiterte Abfrage aendert nichts.
"""

from __future__ import annotations

import logging
import re
import threading
import time

import httpx

logger = logging.getLogger(__name__)

RELEASES_URL = "https://api.github.com/repos/BeastHook/vorlesezeit/releases/latest"
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
TIMEOUT_SECONDS = 5

_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")

# Tests setzen hier einen httpx.MockTransport (tests/conftest.py).
transport: httpx.BaseTransport | None = None

_lock = threading.Lock()
_checked_at: float | None = None
_latest: str | None = None
_thread: threading.Thread | None = None


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION.fullmatch(text)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def is_newer(latest: str, running: str) -> bool:
    """Nicht als X.Y.Z lesbar (auch 0.0.0-dev) zaehlt nie als neuer."""
    latest_v, running_v = parse_version(latest), parse_version(running)
    return latest_v is not None and running_v is not None and latest_v > running_v


def fetch_latest_tag() -> str | None:
    """Der Tag des neuesten Release; None bei jedem Fehlschlag."""
    try:
        with httpx.Client(transport=transport, timeout=TIMEOUT_SECONDS) as client:
            response = client.get(RELEASES_URL, headers={"Accept": "application/vnd.github+json"})
    except httpx.HTTPError as exc:
        logger.info("Update-Abfrage fehlgeschlagen: fehler=%s", type(exc).__name__)
        return None
    if response.status_code != 200:
        logger.info("Update-Abfrage fehlgeschlagen: status=%s", response.status_code)
        return None
    try:
        tag = response.json()["tag_name"]
    except (ValueError, KeyError, TypeError) as exc:
        logger.info("Update-Abfrage fehlgeschlagen: fehler=%s", type(exc).__name__)
        return None
    if not isinstance(tag, str) or parse_version(tag) is None:
        logger.info("Update-Abfrage fehlgeschlagen: fehler=Tag")
        return None
    return tag


def _check() -> None:
    global _latest
    tag = fetch_latest_tag()
    if tag is not None:
        _latest = tag


def anstossen() -> None:
    """Startet die Abfrage, wenn sie faellig ist; auch eine gescheiterte
    zaehlt als Abfrage."""
    global _checked_at, _thread
    with _lock:
        now = time.monotonic()
        if _checked_at is not None and now - _checked_at < CHECK_INTERVAL_SECONDS:
            return
        _checked_at = now
        _thread = threading.Thread(target=_check, name="update-hinweis", daemon=True)
        _thread.start()


def update_verfuegbar() -> bool:
    from app import __version__

    return _latest is not None and is_newer(_latest, __version__)
