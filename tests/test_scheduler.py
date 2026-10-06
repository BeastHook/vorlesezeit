"""U14: Zeitplan-Container (deploy/scheduler/scheduler.py), ohne echtes Netz."""

from __future__ import annotations

import importlib.util
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "scheduler" / "scheduler.py"

_spec = importlib.util.spec_from_file_location("vorlesezeit_scheduler", SCRIPT)
scheduler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scheduler)

BERLIN = scheduler.BERLIN


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 12, 5, hour, minute, second, tzinfo=BERLIN)


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [(16, 55, False), (17, 0, True), (23, 30, True), (1, 45, True), (1, 50, False)],
)
def test_in_window(hour, minute, expected):
    assert scheduler.in_window(_at(hour, minute)) is expected


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (_at(17, 0), _at(17, 5)),
        (_at(17, 3, 42), _at(17, 5)),
        (_at(17, 4, 59), _at(17, 5)),
        (_at(23, 58), datetime(2026, 12, 6, 0, 0, tzinfo=BERLIN)),
    ],
)
def test_next_tick_rounds_to_next_five_minute_boundary(now, expected):
    assert scheduler.next_tick(now) == expected


def test_unreachable_app_is_logged_and_does_not_raise(monkeypatch, capsys):
    def refuse(request, timeout):
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", refuse)

    assert scheduler.call("http://app:8000/delivery/trigger", "geheim") is None
    assert "App nicht erreichbar" in capsys.readouterr().out


def test_http_error_status_is_returned(monkeypatch):
    def forbidden(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)

    assert scheduler.call("http://app:8000/delivery/trigger", "falsch") == 403
