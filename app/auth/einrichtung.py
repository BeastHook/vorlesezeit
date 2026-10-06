"""Mehrkalender U4: Einmal-Einrichtungslink aus dem Containerlog (R30, KTD7).

Ohne nutzbaren SMTP-Zugang kann kein Magic Link verschickt werden -- auch nicht
an den Admin. Dann schreibt jeder Start einen neuen Link ins Log (nur den Pfad,
die Adresse haengt der Admin selbst davor: `X-Forwarded-Proto`-Pitfall). In
der Datenbank steht nur der SHA-256-Hash. Der Link ist 24 h gueltig, einmal
verwendbar, und jeder neue Start entwertet aeltere.

Wie /login/confirm verbraucht erst das Absenden der Bestaetigungsseite den
Link (Linkvorschauen); die Verwendung legt eine gewoehnliche Admin-Sitzung an.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import settings
from app.auth.dependencies import get_db
from app.auth.session import login as start_session
from app.models import Einrichtungslink, Person
from app.templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)

LINK_VALIDITY = timedelta(hours=24)
SETUP_PATH = "/admin/setup"


def get_einrichtung_now() -> datetime:
    """Uhr als Dependency, damit "abgelaufen" testbar ist."""
    return datetime.now(UTC)


def _naive_utc(moment: datetime) -> datetime:
    # SQLite speichert DateTime ohne Zeitzone; verglichen wird in UTC.
    return moment.astimezone(UTC).replace(tzinfo=None)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def smtp_usable(session: Session, key: str | None) -> bool:
    """R30: eingetragen, entschluesselbar und die letzte Mail nicht gescheitert."""
    if settings.smtp_zugang(session, key) is None:
        return False
    return settings.get_einstellungen(session).mail_failed_at is None


def issue_link_if_needed(session: Session, key: str | None, *, now: datetime) -> bool:
    """Beim Start: ist SMTP nicht nutzbar, aeltere Links entwerten, einen neuen
    erzeugen und seinen Pfad ins Log schreiben. Der Klartext steht nur in
    diesem einen Logeintrag. Liefert, ob ein Link entstand."""
    if smtp_usable(session, key):
        return False
    moment = _naive_utc(now)
    session.execute(
        update(Einrichtungslink)
        .where(Einrichtungslink.invalidated_at.is_(None), Einrichtungslink.used_at.is_(None))
        .values(invalidated_at=moment)
    )
    token = secrets.token_urlsafe(32)
    session.add(
        Einrichtungslink(
            token_hash=_hash(token), created_at=moment, expires_at=moment + LINK_VALIDITY
        )
    )
    session.commit()
    logger.warning(
        "Kein nutzbarer SMTP-Zugang. Einrichtungslink (24 h gueltig, einmal verwendbar), "
        "an die eigene Adresse haengen: /einrichtung/%s",
        token,
    )
    return True


def _valid_link(db: Session, token: str, now: datetime) -> Einrichtungslink | None:
    link = db.scalars(
        select(Einrichtungslink).where(Einrichtungslink.token_hash == _hash(token))
    ).first()
    if (
        link is None
        or link.used_at is not None
        or link.invalidated_at is not None
        or _naive_utc(now) >= link.expires_at
    ):
        return None
    return link


def _invalid(request: Request) -> HTMLResponse:
    # Eine Seite fuer jeden Grund -- keine Rueckmeldung, ob ein Link existiert.
    return templates.TemplateResponse(request, "auth/einrichtung_ungueltig.html", status_code=400)


@router.get("/einrichtung/{token}", response_class=HTMLResponse)
def show_einrichtung(
    request: Request,
    token: str,
    db: Session = Depends(get_db),
    now: datetime = Depends(get_einrichtung_now),
):
    if _valid_link(db, token, now) is None:
        return _invalid(request)
    return templates.TemplateResponse(request, "auth/einrichtung.html", {"token": token})


@router.post("/einrichtung/{token}", response_class=HTMLResponse)
def use_einrichtung(
    request: Request,
    token: str,
    db: Session = Depends(get_db),
    now: datetime = Depends(get_einrichtung_now),
):
    link = _valid_link(db, token, now)
    if link is None:
        return _invalid(request)
    admin = db.scalars(select(Person).where(Person.is_admin.is_(True))).one()
    link.used_at = _naive_utc(now)
    db.commit()
    start_session(request, admin)
    logger.info("Einrichtungslink verwendet: Admin-Sitzung angelegt")
    return RedirectResponse(url=SETUP_PATH, status_code=303)
