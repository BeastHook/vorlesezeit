"""U9: Familienarchiv (R22, R23, R34) und Audio ueber die App (R28, KTD17).

Plain `def`: die Audio-Route ruft boto3 synchron auf (Known Pitfall
"async def mit blockierenden Aufrufen").
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.admin.common import berlin_now
from app.archive.visibility import build_archive, can_hear
from app.auth.dependencies import get_current_person, get_db
from app.models import Beitrag, Person
from app.storage import audio_response
from app.templating import templates

router = APIRouter(prefix="/archiv")


@router.get("", response_class=HTMLResponse)
def archive(
    request: Request,
    person: Person = Depends(get_current_person),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request,
        "archive/index.html",
        {"person": person, "archive": build_archive(db, person, berlin_now(request).date())},
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
