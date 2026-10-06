"""Magic-Link-Anmeldung (U3): anfordern, bestaetigen, minimale Landeseite.

Ein reiner GET auf /login/confirm verbraucht nichts (KTD6, Mailscanner-
Schutz) -- erst das Absenden des Formulars erzeugt die Sitzung.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import settings
from app.auth.dependencies import family_auftrag, get_current_person, get_db
from app.auth.session import login as start_session
from app.auth.tokens import (
    ExpiredMagicLinkToken,
    InvalidMagicLinkToken,
    RevokedMagicLinkToken,
    create_magic_link_token,
    verify_magic_link_token,
)
from app.mail.magic_link import german_date, send_magic_link_mail
from app.models import Person
from app.recording.routing import earliest_open_auftrag
from app.templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)

# R27: der Link einer Ablehnungsmail fuehrt direkt zur Aufnahmeansicht. Nur
# genau diese Pfade sind als Ziel erlaubt -- kein offener Redirect. KTD14:
# die Familie adressiert den Auftrag; alte Tuerchen-Adressen gelten nicht mehr.
_NEXT_PATH = re.compile(r"/record/(auftrag/(\d+)|free)")


def _safe_next(next_path: str) -> str:
    return next_path if _NEXT_PATH.fullmatch(next_path) else "/"


def _login_target(db: Session, person: Person, next_path: str) -> str:
    """Wie _safe_next, und ein Auftrag nur, solange die Person ihn oeffnen
    darf -- nach einer Neuvergabe (R35) oder als Entwurf (R36) sonst eine
    rohe 403- bzw. 404-Seite."""
    match = _NEXT_PATH.fullmatch(next_path)
    if match is None:
        return "/"
    if match.group(2) is not None:
        try:
            family_auftrag(db, person, int(match.group(2)))
        except HTTPException:
            return "/"
    return next_path


@router.get("/login", response_class=HTMLResponse)
def show_login_form(request: Request):
    return templates.TemplateResponse(request, "auth/request_link.html")


@router.post("/login", response_class=HTMLResponse)
def request_login_link(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    config = request.app.state.config
    # Handy-Tastaturen schreiben den ersten Buchstaben gross und haengen
    # Leerzeichen an; Adressen werden deshalb ohne Rand und ohne Gross-/
    # Kleinschreibung verglichen.
    typed = email.strip().lower()
    person = db.execute(
        select(Person).where(func.lower(Person.email) == typed)
    ).scalar_one_or_none()
    if person is not None:
        token = create_magic_link_token(config, person)
        login_url = str(request.url_for("show_confirm_page")) + f"?token={token}"
        send_magic_link_mail(config, session=db, to_address=person.email, login_url=login_url)
        logger.info("Magic Link verschickt: Person #%s", person.id)
    else:
        # Ohne die Adresse selbst -- das Log soll keine Zugangsliste werden.
        logger.info("Magic Link angefordert fuer unbekannte Adresse")
    # KTD6: identische Antwort fuer unbekannte Adressen, damit die
    # Zugangsliste nicht auslesbar ist.
    return templates.TemplateResponse(request, "auth/link_sent.html")


@router.get("/login/confirm", response_class=HTMLResponse, name="show_confirm_page")
def show_confirm_page(request: Request, token: str, next: str = "", db: Session = Depends(get_db)):
    config = request.app.state.config
    try:
        # Prueft nur, verbraucht nichts und startet keine Sitzung (KTD6).
        person = verify_magic_link_token(config, db, token)
    except (InvalidMagicLinkToken, ExpiredMagicLinkToken, RevokedMagicLinkToken):
        return templates.TemplateResponse(request, "auth/link_invalid.html", status_code=400)
    return templates.TemplateResponse(
        request,
        "auth/confirm.html",
        {
            "token": token,
            "next": _safe_next(next),
            "person": person,
            "kind": settings.kind_name(db),
            "valid_until": german_date(settings.magic_link_valid_until(db)),
        },
    )


@router.post("/login/confirm", response_class=HTMLResponse)
def confirm_login(
    request: Request,
    token: str = Form(...),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    config = request.app.state.config
    try:
        person = verify_magic_link_token(config, db, token)
    except (InvalidMagicLinkToken, ExpiredMagicLinkToken, RevokedMagicLinkToken):
        return templates.TemplateResponse(request, "auth/link_invalid.html", status_code=400)

    start_session(request, person)
    return RedirectResponse(url=_login_target(db, person, next), status_code=303)


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    person: Person = Depends(get_current_person),
    db: Session = Depends(get_db),
):
    # R3: direkt zum offenen Auftrag mit dem fruehesten Kalendertag, sofern
    # einer ansteht (KTD14). R4: sonst die Wahl zwischen den eigenen
    # Geschichten und freier Einreichung.
    auftrag = earliest_open_auftrag(db, person)
    if auftrag is not None:
        return RedirectResponse(url=f"/record/auftrag/{auftrag.id}", status_code=303)
    return templates.TemplateResponse(request, "recording/choice.html", {"person": person})
