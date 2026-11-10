"""U9: Familienarchiv (R22, R23, R34) und Audio ueber die App (R28, KTD17).

Plain `def`: die Audio-Route ruft boto3 synchron auf (Known Pitfall
"async def mit blockierenden Aufrufen").
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import settings
from app.admin.common import berlin_now
from app.admin.state import ADVENT_YEAR
from app.archive.visibility import _family_query, build_archive, can_hear, last_open_day, web_title
from app.auth.dependencies import get_current_person, get_db
from app.mail.magic_link import german_date
from app.models import Antwort, Beitrag, Person
from app.storage import audio_response
from app.templating import templates
from app.zip_download import clean_name_part, zip_response

router = APIRouter(prefix="/archiv")


@router.get("", response_class=HTMLResponse)
def archive(
    request: Request,
    person: Person = Depends(get_current_person),
    db: Session = Depends(get_db),
):
    archive = build_archive(db, person, berlin_now(request).date())
    context = {"person": person, "archive": archive}
    if archive.antwort is not None:
        kind = settings.person_kind_name(db, person.id)
        context["antwort_title"] = f"Antwort von {kind}" if kind else "Antwort"
        context["antwort_date"] = german_date(archive.antwort.uploaded_at)
    return templates.TemplateResponse(request, "archive/index.html", context)


@router.get("/download")
def archive_download(
    request: Request,
    person: Person = Depends(get_current_person),
    db: Session = Depends(get_db),
):
    """Jahresrueckblick: die Geschichten aus "Aus der Familie" als ZIP, so
    gespeichert wie die Archiv-Wiedergabe sie liefert."""
    query, day = _family_query(person)
    rows = db.execute(query.where(day <= last_open_day(berlin_now(request).date()))).all()
    rows.sort(key=lambda row: (row[1], web_title(row[0]), row[0].id))
    entries = []
    for beitrag, beitrag_day in rows:
        parts = [f"Tag {beitrag_day:02d}", web_title(beitrag), beitrag.person.display_name]
        name = " - ".join(clean_name_part(p) for p in parts if p)
        entries.append((name, beitrag))
    return zip_response(
        request.app.state.storage,
        entries,
        f"Vorlesezeit-Geschichten-{ADVENT_YEAR}.zip",
        "Diese Geschichten ließen sich nicht laden:",
    )


@router.get("/antwort")
def archive_antwort(
    request: Request,
    person: Person = Depends(get_current_person),
    db: Session = Depends(get_db),
):
    """Die Antwort vom Kind an die angemeldete Person -- ohne ID, damit keine
    fremde Antwort adressierbar ist. Der Admin hat nie eine."""
    antwort = None
    if not person.is_admin:
        antwort = db.execute(
            select(Antwort).where(Antwort.person_id == person.id)
        ).scalar_one_or_none()
    if antwort is None:
        raise HTTPException(status_code=404)
    return audio_response(
        request.app.state.storage, antwort.audio_object_key, request.headers.get("range")
    )


@router.get("/{beitrag_id}/audio")
def archive_audio(
    beitrag_id: int,
    request: Request,
    person: Person = Depends(get_current_person),
    db: Session = Depends(get_db),
):
    beitrag = db.get(Beitrag, beitrag_id)
    # 404 statt 403: ein noch verschlossener Beitrag verraet nicht, dass es ihn gibt.
    if beitrag is None or not can_hear(db, person, beitrag, berlin_now(request).date()):
        raise HTTPException(status_code=404)
    return audio_response(
        request.app.state.storage, beitrag.audio_object_key, request.headers.get("range")
    )
