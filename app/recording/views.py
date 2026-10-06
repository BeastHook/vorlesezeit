"""Aufnahme-Endpunkte (U7; Mehrkalender U12): Uebersicht "Meine Geschichten",
Aufnahmeansicht (Auftrag und freie Einreichung), Einreichung, Bestaetigung.
Die Familie adressiert den Auftrag, nicht das Tuerchen (KTD14, R10).

Der Einstieg selbst (R3/R4, "wohin nach dem Login") sitzt in
app/auth/routes.py::home und nutzt earliest_open_auftrag aus routing.py --
diese Datei bedient nur die Ansichten, die von dort aus erreicht werden.
"""

from __future__ import annotations

import logging
import threading
import uuid

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import settings
from app.admin.setup import attach_beitrag
from app.archive.visibility import FALLBACK_TITLE, web_title
from app.auth.dependencies import family_auftrag, get_current_person, get_db
from app.mail.magic_link import german_date
from app.models import Auftrag, Beitrag, Person
from app.recording.normalize import EmptyRecordingError, normalize_recording
from app.recording.routing import (
    admin_names,
    earliest_open_auftrag,
    person_auftraege_overview,
    sanitize_title,
)
from app.storage import commit_or_discard, delete_quietly
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/record")

# Review-Fund: eine hochgeladene Datei ohne Obergrenze laesst ffmpeg beliebig
# lange auf beliebig grossem Material arbeiten. Grosszuegig genug fuer eine
# mehrminuetige Vorlesegeschichte.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

_submission_locks: dict[tuple[int, int | None], threading.Lock] = {}
_submission_locks_guard = threading.Lock()


def _lock_for(person_id: int, auftrag_id: int | None) -> threading.Lock:
    """Ein Lock je (Person, Auftrag) -- verhindert, dass zwei nahezu
    gleichzeitige Einreichungen fuer denselben Auftrag den Lese-dann-Schreib-
    Ablauf in _handle_submission gegenseitig ueberholen (Review-Fund). Wie
    app/delivery/job.py::_lock_for genuegt In-Prozess, da die App als
    einzelner Uvicorn-Prozess ohne --workers laeuft (KTD11); Threads statt
    ein Prozess sind der Grund, warum dieser Lock hier ueberhaupt noetig
    ist -- die Recording-Routen laufen synchron im Threadpool, nicht mehr
    seriell auf dem Event-Loop (siehe _handle_submission)."""
    key = (person_id, auftrag_id)
    with _submission_locks_guard:
        return _submission_locks.setdefault(key, threading.Lock())


def _render(request: Request, db: Session, name: str, context: dict) -> HTMLResponse:
    """Familienseiten nennen den Admin beim Anzeigenamen (R26) und das Kind."""
    return templates.TemplateResponse(
        request, name, {"admin": admin_names(db), "kind": settings.kind_name(db), **context}
    )


def _existing_beitrag(db: Session, *, auftrag_id: int, person_id: int) -> Beitrag | None:
    # populate_existing: die R10-Pruefung unter dem Lock muss eine inzwischen
    # erteilte Freigabe sehen, auch wenn der Beitrag aus der Vorpruefung noch
    # in der Session steckt.
    return db.execute(
        select(Beitrag)
        .where(Beitrag.auftrag_id == auftrag_id, Beitrag.person_id == person_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _is_approved(db: Session, *, auftrag_id: int, person_id: int) -> bool:
    existing = _existing_beitrag(db, auftrag_id=auftrag_id, person_id=person_id)
    return existing is not None and existing.approved_at is not None


def _already_approved_response() -> JSONResponse:
    return JSONResponse(
        {"error": "Diese Aufnahme ist schon freigegeben und laesst sich nicht mehr ersetzen."},
        status_code=409,
    )


def _handle_submission(
    request: Request,
    db: Session,
    person: Person,
    *,
    auftrag: Auftrag | None,
    audio: UploadFile,
    title: str,
    reported_type: str,
) -> JSONResponse:
    # Bewusst eine plain `def`-Route (kein `async def`): FastAPI/Starlette
    # dispatcht sie automatisch in den Threadpool, wie jede andere Route in
    # dieser App (app/auth/routes.py, app/admin/setup.py,
    # app/delivery/trigger.py). ffmpeg (normalize_recording) und der
    # Objektspeicher-Zugriff sind blockierende Aufrufe von mehreren
    # Sekunden Dauer -- als `async def` ohne Threadpool-Abgabe wuerden sie
    # den einzigen Event-Loop des Prozesses fuer alle anderen Anfragen
    # gleichzeitig einfrieren (Review-Fund, dreifach unabhaengig bestaetigt).
    storage = request.app.state.storage
    raw = audio.file.read()
    # KTD7: der clientseitig gemeldete Typ geht nur ins Log, nie in die
    # Formatentscheidung -- die trifft normalize_recording aus dem Inhalt.
    logger.info(
        "Aufnahme eingegangen: person=%s auftrag=%s reported_type=%r size=%d",
        person.id,
        auftrag.id if auftrag else None,
        reported_type,
        len(raw),
    )
    if len(raw) > MAX_UPLOAD_BYTES:
        return JSONResponse(
            {"error": "Die Aufnahme ist zu gross."},
            status_code=413,
        )

    if auftrag is not None and _is_approved(db, auftrag_id=auftrag.id, person_id=person.id):
        return _already_approved_response()

    try:
        normalized = normalize_recording(raw)
    except EmptyRecordingError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)

    key = f"beitraege/{person.id}/{uuid.uuid4().hex}.mp3"
    try:
        storage.put(key, normalized, content_type="audio/mpeg")
    except Exception:
        logger.exception(
            "Speichern der Aufnahme fehlgeschlagen: person=%s auftrag=%s",
            person.id,
            auftrag.id if auftrag else None,
        )
        return JSONResponse(
            {"error": "Die Aufnahme laesst sich gerade nicht speichern. Bitte erneut versuchen."},
            status_code=422,
        )

    clean_title = sanitize_title(title)
    old_key = None
    # Lock je (Person, Auftrag): serialisiert den Lese-dann-Schreib-Ablauf
    # gegen eine nahezu gleichzeitige zweite Einreichung derselben Person
    # fuer denselben Auftrag (Review-Fund) -- ohne diesen Lock koennten zwei
    # Threadpool-Threads beide "kein vorhandener Beitrag" lesen und zwei
    # Zeilen anlegen. Freie Einreichungen (auftrag=None) brauchen ihn nicht:
    # sie legen ohnehin immer eine neue Zeile an.
    with _lock_for(person.id, auftrag.id if auftrag else None):
        existing = (
            _existing_beitrag(db, auftrag_id=auftrag.id, person_id=person.id) if auftrag else None
        )
        if existing is not None and existing.approved_at is not None:
            # R10: nach der Freigabe nicht mehr ersetzbar. Zweite Pruefung
            # unter dem Lock, falls der Admin waehrend der Normalisierung
            # freigegeben hat -- die frisch abgelegte Datei wird verworfen.
            delete_quietly(storage, key)
            return _already_approved_response()
        if existing is not None:
            # R10: eine neue Aufnahme ersetzt die vorherige, solange sie
            # noch nicht freigegeben ist; eine Ablehnung (R13) ist damit
            # erledigt.
            old_key = existing.audio_object_key
            existing.audio_object_key = key
            existing.title = clean_title
            existing.rejected_at = None
            beitrag = existing
        elif auftrag is not None:
            # Derselbe Dienst wie der Admin-Upload: gilt an jedem Kalendertag
            # des Auftrags (R8).
            beitrag = attach_beitrag(db, auftrag, person_id=person.id, audio_object_key=key)
            beitrag.title = clean_title
        else:
            beitrag = Beitrag(person_id=person.id, audio_object_key=key, title=clean_title)
            db.add(beitrag)

        commit_or_discard(db, storage, key)
        db.refresh(beitrag)

    if old_key:
        # R40: eine per R10 ersetzte Aufnahme wird automatisch entfernt.
        # Best effort nach dem Commit -- ein Fehlschlag hier darf die
        # erfolgreiche Einreichung nicht ruinieren.
        delete_quietly(storage, old_key)

    # Nur der Pfad, nicht die volle URL: request.url_for() uebernimmt sonst
    # das vom Server beobachtete Schema, das hinter einem TLS-terminierenden
    # Vorbau ohne korrekt gesetzten X-Forwarded-Proto faelschlich http statt
    # https sein kann. Fuer eine Weiterleitung im selben Browser genuegt der
    # Pfad -- window.location.href loest ihn gegen die aktuelle Seite auf
    # (Review-Fund, siehe auch submit_url unten).
    if auftrag is not None:
        next_url = request.url_for("auftrag_confirmed", auftrag_id=auftrag.id).path
    else:
        next_url = request.url_for("free_confirmed", beitrag_id=beitrag.id).path
    return JSONResponse({"ok": True, "next_url": next_url})


@router.get("/campaign", response_class=HTMLResponse)
def campaign_overview(
    request: Request, person: Person = Depends(get_current_person), db: Session = Depends(get_db)
):
    return _render(
        request,
        db,
        "recording/campaign.html",
        {
            "person": person,
            "overview": person_auftraege_overview(db, person),
            "deadline_label": german_date(settings.recording_deadline(db)),
        },
    )


@router.get("/auftrag/{auftrag_id}", response_class=HTMLResponse)
def auftrag_recording(
    auftrag_id: int,
    request: Request,
    person: Person = Depends(get_current_person),
    db: Session = Depends(get_db),
):
    auftrag = family_auftrag(db, person, auftrag_id)
    return _render(
        request,
        db,
        "recording/record.html",
        {
            "person": person,
            "auftrag": auftrag,
            "has_suggestion": bool(auftrag.vorlesetext),
            "deadline_label": german_date(settings.recording_deadline(db)),
            # Nur der Pfad -- siehe Kommentar bei next_url in
            # _handle_submission zur selben Schema-Problematik.
            "submit_url": request.url_for("submit_auftrag_recording", auftrag_id=auftrag.id).path,
        },
    )


@router.post("/auftrag/{auftrag_id}", name="submit_auftrag_recording")
def submit_auftrag_recording(
    auftrag_id: int,
    request: Request,
    audio: UploadFile = File(...),
    title: str = Form(""),
    reported_type: str = Form(""),
    person: Person = Depends(get_current_person),
    db: Session = Depends(get_db),
):
    auftrag = family_auftrag(db, person, auftrag_id)
    return _handle_submission(
        request, db, person, auftrag=auftrag, audio=audio, title=title, reported_type=reported_type
    )


@router.get(
    "/auftrag/{auftrag_id}/confirmed", response_class=HTMLResponse, name="auftrag_confirmed"
)
def auftrag_confirmed(
    auftrag_id: int,
    request: Request,
    person: Person = Depends(get_current_person),
    db: Session = Depends(get_db),
):
    auftrag = family_auftrag(db, person, auftrag_id)
    beitrag = _existing_beitrag(db, auftrag_id=auftrag.id, person_id=person.id)
    chapter_title = web_title(beitrag) if beitrag else (auftrag.title or FALLBACK_TITLE)

    return _render(
        request,
        db,
        "recording/confirmed.html",
        {
            "person": person,
            "auftrag": auftrag,
            "beitrag": beitrag,
            "chapter_title": chapter_title,
            "next_auftrag": earliest_open_auftrag(db, person),
        },
    )


@router.get("/free", response_class=HTMLResponse)
def free_recording(
    request: Request, person: Person = Depends(get_current_person), db: Session = Depends(get_db)
):
    return _render(
        request,
        db,
        "recording/record.html",
        {
            "person": person,
            "auftrag": None,
            "has_suggestion": False,
            "submit_url": request.url_for("submit_free_recording").path,
        },
    )


@router.post("/free", name="submit_free_recording")
def submit_free_recording(
    request: Request,
    audio: UploadFile = File(...),
    title: str = Form(""),
    reported_type: str = Form(""),
    person: Person = Depends(get_current_person),
    db: Session = Depends(get_db),
):
    return _handle_submission(
        request, db, person, auftrag=None, audio=audio, title=title, reported_type=reported_type
    )


@router.get("/free/{beitrag_id}/confirmed", response_class=HTMLResponse, name="free_confirmed")
def free_confirmed(
    beitrag_id: int,
    request: Request,
    person: Person = Depends(get_current_person),
    db: Session = Depends(get_db),
):
    beitrag = db.get(Beitrag, beitrag_id)
    if beitrag is None or beitrag.person_id != person.id:
        return HTMLResponse("Nachricht nicht gefunden.", status_code=404)

    return _render(
        request,
        db,
        "recording/confirmed.html",
        {
            "person": person,
            "auftrag": None,
            "beitrag": beitrag,
            "chapter_title": beitrag.title or "Freie Nachricht",
            "next_auftrag": earliest_open_auftrag(db, person),
        },
    )
