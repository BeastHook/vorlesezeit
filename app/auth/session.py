"""Sitzung: signierte Cookie (Starlette SessionMiddleware), kein Server-Store.

Traegt {person_id, access_version}. Widerruf (R39) braucht deshalb keine
eigene Sitzungstabelle: die zentrale Pruefung in app/auth/dependencies.py
vergleicht den mitgefuehrten Zaehler gegen den aktuellen DB-Stand.
"""

from __future__ import annotations

from starlette.requests import Request

from app.models import Person


def login(request: Request, person: Person) -> None:
    request.session["person_id"] = person.id
    request.session["access_version"] = person.access_version


def logout(request: Request) -> None:
    request.session.clear()
