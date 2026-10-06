"""Zentrale Berechtigungspruefung (R28, R39): deny-by-default.

get_current_person/require_admin laufen als FastAPI-Dependency vor jeder
geschuetzten Route. authorize_auftrag_access/authorize_beitrag_access/
authorize_audio_access sind eigenstaendig testbare Funktionen -- in dieser
Session (U2/U3) gibt es noch keine Slot-/Beitrag-Detailroute, die sie
wirklich braucht (die entstehen mit U7-U9); sie werden hier bereits gebaut
und getestet, damit die spaeteren Routen sie nur noch als Depends(...)
einhaengen.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import session_scope
from app.models import Auftrag, Beitrag, Person


def get_db(request: Request) -> Iterator[Session]:
    yield from session_scope(request.app.state.session_factory)


def get_current_person(request: Request, db: Session = Depends(get_db)) -> Person:
    person_id = request.session.get("person_id")
    if person_id is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})

    person = db.get(Person, person_id)
    if person is None or person.access_version != request.session.get("access_version"):
        request.session.clear()
        raise HTTPException(status_code=303, headers={"Location": "/login"})

    return person


def require_admin(person: Person = Depends(get_current_person)) -> Person:
    if not person.is_admin:
        raise HTTPException(status_code=403, detail="Nur fuer den Admin.")
    return person


def authorize_auftrag_access(person: Person, auftrag: Auftrag) -> None:
    """Mehrkalender U12 (KTD14): die Familie adressiert den Auftrag."""
    if person.is_admin or auftrag.person_id == person.id:
        return
    raise HTTPException(status_code=403, detail="Kein Zugriff auf diese Geschichte.")


def authorize_beitrag_access(person: Person, beitrag: Beitrag) -> None:
    if person.is_admin or beitrag.person_id == person.id:
        return
    raise HTTPException(status_code=403, detail="Kein Zugriff auf diesen Beitrag.")


def authorize_audio_access(person: Person, beitrag: Beitrag) -> str:
    """R28: nach derselben Pruefung wie jeder andere Beitrags-Zugriff wird
    der Objektschluessel freigegeben; ausgeliefert wird ueber die App (KTD17)."""
    authorize_beitrag_access(person, beitrag)
    if beitrag.audio_object_key is None:
        raise HTTPException(status_code=404, detail="Kein Audio hinterlegt.")
    return beitrag.audio_object_key


def family_auftrag(db: Session, person: Person, auftrag_id: int) -> Auftrag:
    """Ein Auftrag, wie ihn die Familie adressiert (KTD14): ein Entwurf ohne
    Kalendertag ist fuer sie nicht da (R36, 404), ein fremder verboten (403)."""
    auftrag = db.get(Auftrag, auftrag_id)
    if auftrag is None or not auftrag.slots:
        raise HTTPException(status_code=404, detail="Geschichte nicht gefunden.")
    authorize_auftrag_access(person, auftrag)
    return auftrag
