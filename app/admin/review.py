"""Admin-Reiter Aufnahmen (U8 Schritte 2-4, 7-9).

Freigabe, Ablehnung, Ruecknahme, Zuschnitt, Kapitelname, Upload, Loeschen.

Alle Routen sind plain `def` (Known Pitfall: ffmpeg und boto3 blockieren,
Starlette schiebt `def`-Routen in den Threadpool).
"""

from __future__ import annotations

import logging
import math
import re
import tempfile
import uuid
import zipfile
from collections import Counter
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin.common import (
    berlin_now,
    campaign_slots,
    delivery_time_at,
    first_campaign,
    flash,
    get_campaign,
    login_url,
    redirect,
    render,
)
from app.admin.setup import (
    AuftragError,
    AuftragLockedError,
    add_calendar_day,
    attach_beitrag,
    create_auftrag,
    reject_beitrag,
    withdraw_approval,
)
from app.admin.state import (
    active_beitrag,
    auftrag_active_beitrag,
    auftrag_lock_slot,
    auftrag_slots,
    beitrag_state,
    calendars,
    chapter_title_open,
    slot_state,
)
from app.auth.dependencies import authorize_audio_access, get_db, require_admin
from app.delivery.job import chapter_title_for, named_title
from app.mail.rejection import send_rejection_mail
from app.models import Auftrag, Beitrag, Campaign, CreativeTonie, Person, Slot
from app.recording.normalize import EmptyRecordingError, normalize_recording
from app.recording.routing import sanitize_title
from app.recording.views import MAX_UPLOAD_BYTES
from app.storage import ObjectMissing, audio_response, commit_or_discard

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")

_TAG_CLASSES = {
    "eingereicht": "tag",
    "freigegeben": "tag tag-accent-2",
    "abgelehnt": "tag tag-outline",
    "ausgeliefert": "tag tag-strong",
}


def _utcnow() -> datetime:
    # Wie app/delivery/job.py: naive UTC-Zeitstempel in der DB.
    return datetime.now(UTC).replace(tzinfo=None)


def parse_time(raw: str) -> float | None:
    """Zeitfeld des Zuschnitts: "m:ss,d", "m:ss.d" oder reine Sekunden.
    Leer = keine Marke. Wirft ValueError bei allem anderen."""
    text = raw.strip().replace(",", ".")
    if not text:
        return None
    if ":" in text:
        minutes_part, seconds_part = text.split(":", 1)
        minutes = int(minutes_part)
        seconds = float(seconds_part)
        if minutes < 0 or not (0 <= seconds < 60):
            raise ValueError(raw)
        value = minutes * 60 + seconds
    else:
        value = float(text)
    if not math.isfinite(value) or value < 0:
        raise ValueError(raw)
    return value


def format_time(seconds: float) -> str:
    tenths = round(seconds * 10)
    return f"{tenths // 600}:{(tenths % 600) // 10:02d},{tenths % 10}"


def _back(beitrag_id: int, *, stamp: bool = False):
    suffix = "&stamp=1" if stamp else ""
    return redirect(f"/admin/aufnahmen?beitrag_id={beitrag_id}{suffix}")


def _get_beitrag(db: Session, beitrag_id: int) -> Beitrag | None:
    beitrag = db.get(Beitrag, beitrag_id)
    if beitrag is None or beitrag.audio_object_key is None:
        return None
    return beitrag


def _not_found() -> HTMLResponse:
    return HTMLResponse("Aufnahme nicht gefunden.", status_code=404)


def _first_slot(beitrag: Beitrag) -> Slot | None:
    """Der frueheste Kalendertag des Auftrags -- Bezug fuer Kapitelname und
    Ablehnungslink, solange die Oberflaeche einen Tag je Aufnahme zeigt (U10)."""
    slots = auftrag_slots(beitrag.auftrag) if beitrag.auftrag is not None else []
    return slots[0] if slots else None


def _day_order(beitrag: Beitrag) -> tuple[int, int]:
    """Nach fruehestem Kalendertag, ohne Tag zuletzt; neueste zuerst."""
    slot = _first_slot(beitrag)
    return (slot.day if slot else 99, -beitrag.id)


def _chips(db: Session, beitrag: Beitrag, *, now, delivery_time) -> list[dict]:
    """R15: alle Kalendertage des Auftrags als "Kalender · Tag"; ein Tag ist
    "ausgeliefert", wenn dort dieser Beitrag erfolgreich aufgespielt wurde."""
    if beitrag.auftrag is None:
        return []
    chips = []
    for slot in auftrag_slots(beitrag.auftrag):
        state = slot_state(db, slot, now=now, delivery_time=delivery_time)
        done = (
            state.key == "ausgeliefert"
            and state.beitrag is not None
            and state.beitrag.id == beitrag.id
        )
        chips.append({"label": f"{slot.campaign.name} · Tag {slot.day}", "done": done})
    return chips


def _row(db: Session, beitrag: Beitrag, *, now, delivery_time) -> dict:
    state = beitrag_state(beitrag)
    chips = _chips(db, beitrag, now=now, delivery_time=delivery_time)
    key = "ausgeliefert" if any(c["done"] for c in chips) else state.key
    who = beitrag.person.display_name or beitrag.person.email
    if beitrag.auftrag is None:
        where = "frei"
    elif not chips:
        where = "ohne Kalendertag"
    else:
        where = None
    return {
        "beitrag": beitrag,
        "name": chapter_title_for(beitrag, _first_slot(beitrag)),
        "meta": f"{where} · {who}" if where else who,
        "chips": chips,
        "who": who,
        "title_open": chapter_title_open(beitrag),
        "tag_label": "ausgeliefert" if key == "ausgeliefert" else state.label,
        "tag_class": _TAG_CLASSES[key],
        "state_key": state.key,
    }


@router.get("/aufnahmen", response_class=HTMLResponse)
def aufnahmen(
    request: Request,
    beitrag_id: int | None = None,
    db: Session = Depends(get_db),
    admin: Person = Depends(require_admin),
):
    """R15: Aufnahmen gelten fuer die ganze Instanz, unabhaengig vom
    gewaehlten Tonie; nur das Einspringen zielt auf dessen Kalender."""
    if first_campaign(db) is None:
        return redirect("/admin")
    now = berlin_now(request)
    delivery_time = delivery_time_at(db, now)
    campaign = get_campaign(db, request)

    day_beitraege = sorted(
        db.execute(
            select(Beitrag).where(
                Beitrag.auftrag_id.is_not(None), Beitrag.audio_object_key.is_not(None)
            )
        ).scalars(),
        key=_day_order,
    )
    # R14: freie Einreichungen nur im Eingang; nach R35 geloeste stehen
    # getrennt und lassen sich nur loeschen (R40).
    free_beitraege = (
        db.execute(
            select(Beitrag)
            .where(
                Beitrag.auftrag_id.is_(None),
                Beitrag.detached_at.is_(None),
                Beitrag.audio_object_key.is_not(None),
            )
            .order_by(Beitrag.id.desc())
        )
        .scalars()
        .all()
    )
    detached_beitraege = (
        db.execute(
            select(Beitrag)
            .where(
                Beitrag.auftrag_id.is_(None),
                Beitrag.detached_at.is_not(None),
                Beitrag.audio_object_key.is_not(None),
            )
            .order_by(Beitrag.id.desc())
        )
        .scalars()
        .all()
    )
    when = {"now": now, "delivery_time": delivery_time}
    day_rows = [_row(db, b, **when) for b in day_beitraege]
    free_rows = [_row(db, b, **when) for b in free_beitraege]

    all_rows = day_rows + free_rows
    selected = next((r for r in all_rows if r["beitrag"].id == beitrag_id), None)
    if selected is None and all_rows:
        selected = all_rows[0]

    detail = None
    if selected is not None:
        b = selected["beitrag"]
        lock = auftrag_lock_slot(db, b.auftrag, **when) if b.auftrag is not None else None
        detail = {
            **selected,
            "lock": lock,
            # M3: Siegel praegt sich nur unmittelbar nach der Freigabe (Ruling 13).
            "stamped": request.query_params.get("stamp") == "1",
            "audio_url": f"/admin/aufnahmen/{b.id}/audio",
            "chapter_value": selected["name"],
            "cut_start": format_time(b.cut_start_seconds)
            if b.cut_start_seconds is not None
            else "",
            "cut_end": format_time(b.cut_end_seconds) if b.cut_end_seconds is not None else "",
        }

    slots = campaign_slots(db, campaign.id) if campaign is not None else []
    upload_slots = [
        {
            "slot": s,
            "label": f"{s.campaign.name} · Tag {s.day} · "
            + (
                (s.auftrag.person.display_name or s.auftrag.person.email) + " (offen)"
                if s.auftrag is not None and s.auftrag.person is not None
                else "nicht zugewiesen"
            ),
        }
        for s in slots
        if active_beitrag(s) is None
    ]

    return render(
        request,
        "admin/aufnahmen.html",
        {
            "day_rows": day_rows,
            "free_rows": free_rows,
            "detached_beitraege": detached_beitraege,
            "detail": detail,
            "upload_slots": upload_slots,
        },
        tab="aufnahmen",
    )


@router.get("/aufnahmen/{beitrag_id}/audio")
def aufnahme_audio(
    beitrag_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: Person = Depends(require_admin),
):
    """R28/KTD17: Player und Wellenform hoeren ueber die App, nicht ueber
    den Speicher."""
    beitrag = db.get(Beitrag, beitrag_id)
    if beitrag is None:
        return _not_found()
    key = authorize_audio_access(admin, beitrag)
    return audio_response(request.app.state.storage, key, request.headers.get("range"))


# Pfadtrenner und was Windows in Dateinamen nicht erlaubt.
_UNSAFE_IN_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _download_name(beitrag: Beitrag, *, several_calendars: bool) -> str:
    """Tag, Titel und zugewiesene Person; freie und geloeste ohne Tag."""
    person = beitrag.person
    if beitrag.auftrag is None:
        where = "Gelöst" if beitrag.detached_at is not None else "Eingang"
    else:
        person = beitrag.auftrag.person or person
        slot = _first_slot(beitrag)
        if slot is None:
            where = "ohne Tag"
        else:
            where = f"Tag {slot.day:02d}"
            if several_calendars:
                where = f"{slot.campaign.name} {where}"
    parts = [where, named_title(beitrag) or "ohne Titel", person.display_name or person.email]
    return " - ".join(" ".join(_UNSAFE_IN_NAME.sub(" ", p).split()) for p in parts)


def _download_order(beitrag: Beitrag) -> tuple[int, int, int]:
    """Wie im Reiter: Tuerchen nach Tag, dann Eingang, dann geloeste."""
    if beitrag.auftrag is not None:
        return (0, *_day_order(beitrag))
    return (2 if beitrag.detached_at is not None else 1, 0, -beitrag.id)


@router.get("/aufnahmen/download")
def download(
    request: Request,
    ids: list[int] = Query(default=[]),
    db: Session = Depends(get_db),
    _admin: Person = Depends(require_admin),
):
    """ZIP aller (oder der gewaehlten) Aufnahmen, unveraendert aus dem
    Speicher. MP3 ist schon komprimiert, deshalb ZIP_STORED; die Datei
    wandert ab 10 MB aus dem Arbeitsspeicher auf die Platte."""
    query = select(Beitrag).where(Beitrag.audio_object_key.is_not(None))
    if ids:
        query = query.where(Beitrag.id.in_(ids))
    beitraege = sorted(db.execute(query).scalars(), key=_download_order)
    if ids and len(beitraege) != len(set(ids)):
        return _not_found()

    several = len(calendars(db)) > 1
    names = [_download_name(b, several_calendars=several) for b in beitraege]
    duplicates = {n for n, count in Counter(names).items() if count > 1}
    storage = request.app.state.storage

    spool = tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024)
    missing = []
    with zipfile.ZipFile(spool, "w", zipfile.ZIP_STORED) as archive:
        for beitrag, name in zip(beitraege, names, strict=True):
            if name in duplicates:
                name = f"{name} - {beitrag.id}"
            try:
                data = storage.read(beitrag.audio_object_key).data
            except ObjectMissing:
                logger.warning("Download: Datei fehlt im Speicher (Beitrag %s)", beitrag.id)
                missing.append(f"{name}.mp3")
                continue
            archive.writestr(f"{name}.mp3", data)
        if missing:
            text = "Diese Aufnahmen fehlen im Speicher:\n" + "\n".join(missing) + "\n"
            archive.writestr("FEHLT.txt", text)
    spool.seek(0)

    def chunks():
        with spool:
            while block := spool.read(1024 * 1024):
                yield block

    filename = f"Vorlesezeit-Aufnahmen-{berlin_now(request).date().isoformat()}.zip"
    return StreamingResponse(
        chunks(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/aufnahmen/upload")
def upload(
    request: Request,
    slot_id: int | None = Form(None),
    auftrag_id: int | None = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    admin: Person = Depends(require_admin),
):
    """R15/F5: der Admin legt eine Audiodatei an einen Auftrag -- dieselbe
    Normalisierung wie eine Aufnahme, danach eigens freizugeben. Ein leerer
    Kalendertag bekommt dafuer einen Auftrag ohne Person (U9). Alles bis zum
    Commit bleibt in einer Transaktion; jeder Abbruch davor verwirft sie mit
    der Sitzung."""
    if auftrag_id is not None:
        auftrag = db.get(Auftrag, auftrag_id)
        if auftrag is None:
            flash(request, "Diesen Auftrag gibt es nicht.", "err")
            return redirect("/admin/aufnahmen")
        where = "den Auftrag"
    else:
        slot = db.get(Slot, slot_id) if slot_id is not None else None
        if slot is None:
            flash(request, "Dieses Türchen gibt es nicht.", "err")
            return redirect("/admin/aufnahmen")
        auftrag = slot.auftrag
        where = f"Türchen {slot.day}"
        if auftrag is None:
            now = berlin_now(request)
            auftrag = create_auftrag(db)
            try:
                add_calendar_day(
                    db,
                    auftrag.id,
                    slot.id,
                    now=now,
                    delivery_time=delivery_time_at(db, now),
                )
            except AuftragError as exc:
                flash(request, str(exc), "err")
                return redirect("/admin/aufnahmen")
    if auftrag_active_beitrag(auftrag) is not None:
        flash(request, f"{where[:1].upper()}{where[1:]} hat schon eine Aufnahme.", "err")
        return redirect("/admin/aufnahmen")

    raw = file.file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        flash(request, "Die Datei ist zu groß.", "err")
        return redirect("/admin/aufnahmen")
    try:
        normalized = normalize_recording(raw)
    except EmptyRecordingError:
        flash(request, "Die Datei ist leer.", "err")
        return redirect("/admin/aufnahmen")
    except Exception:
        logger.exception("Upload nicht lesbar: auftrag=%s", auftrag.id)
        flash(request, "Die Datei lässt sich nicht als Audio lesen.", "err")
        return redirect("/admin/aufnahmen")

    key = f"beitraege/{admin.id}/{uuid.uuid4().hex}.mp3"
    try:
        request.app.state.storage.put(key, normalized, content_type="audio/mpeg")
    except Exception:
        logger.exception("Upload nicht gespeichert: auftrag=%s", auftrag.id)
        flash(request, "Die Datei lässt sich gerade nicht speichern.", "err")
        return redirect("/admin/aufnahmen")

    beitrag = attach_beitrag(db, auftrag, person_id=admin.id, audio_object_key=key)
    commit_or_discard(db, request.app.state.storage, key)
    flash(request, f"Datei an {where} gelegt. Bitte noch freigeben.")
    return _back(beitrag.id)


@router.post("/aufnahmen/{beitrag_id}/freigeben")
def freigeben(
    beitrag_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _admin: Person = Depends(require_admin),
):
    beitrag = _get_beitrag(db, beitrag_id)
    if beitrag is None:
        return _not_found()
    if beitrag.auftrag is not None:
        active = auftrag_active_beitrag(beitrag.auftrag)
        if active is not None and active.id != beitrag.id:
            flash(request, "Dieser Auftrag hat schon eine andere Aufnahme.", "err")
            return _back(beitrag.id)
    beitrag.approved_at = _utcnow()
    beitrag.rejected_at = None
    db.commit()
    flash(request, "Freigegeben.")
    return _back(beitrag.id, stamp=True)


@router.post("/aufnahmen/{beitrag_id}/ablehnen")
def ablehnen(
    beitrag_id: int,
    request: Request,
    comment: str = Form(""),
    db: Session = Depends(get_db),
    _admin: Person = Depends(require_admin),
):
    beitrag = _get_beitrag(db, beitrag_id)
    if beitrag is None:
        return _not_found()
    config = request.app.state.config
    now = berlin_now(request)
    try:
        reject_beitrag(db, beitrag.id, now=now, delivery_time=delivery_time_at(db, now))
    except AuftragError as exc:
        flash(request, str(exc), "err")
        return _back(beitrag.id)
    db.commit()

    person = beitrag.person
    if person.is_admin:
        # Eigener Upload (R15): keine Mail an sich selbst.
        flash(request, "Abgelehnt.")
        return _back(beitrag.id)

    # Frischer Magic-Link direkt zur Aufnahmeansicht des Auftrags (R27,
    # KTD14); ein freier Beitrag fuehrt zur freien Nachricht.
    auftrag = beitrag.auftrag
    target = f"/record/auftrag/{auftrag.id}" if auftrag is not None else "/record/free"
    try:
        send_rejection_mail(
            config,
            session=db,
            to_address=person.email,
            display_name=person.display_name,
            title=(auftrag.title or beitrag.title) if auftrag is not None else None,
            comment=comment,
            login_url=login_url(request, person, next_path=target),
        )
    except Exception:
        logger.exception("Ablehnungs-Mail fehlgeschlagen: beitrag=%s", beitrag.id)
        flash(
            request,
            "Abgelehnt, aber die Mail ließ sich nicht verschicken. Bitte selbst Bescheid geben.",
            "err",
        )
        return _back(beitrag.id)

    flash(request, f"Abgelehnt, Mail an {person.display_name or person.email} verschickt.")
    return _back(beitrag.id)


@router.post("/aufnahmen/{beitrag_id}/zuruecknehmen")
def zuruecknehmen(
    beitrag_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _admin: Person = Depends(require_admin),
):
    beitrag = _get_beitrag(db, beitrag_id)
    if beitrag is None:
        return _not_found()
    now = berlin_now(request)
    try:
        withdraw_approval(db, beitrag.id, now=now, delivery_time=delivery_time_at(db, now))
    except AuftragLockedError as exc:
        flash(
            request,
            f"Die Vorabend-Auslieferung für Türchen {exc.slot.day} ist bereits gelaufen "
            "oder der Auftrag ist ausgeliefert. Die Freigabe lässt sich nicht mehr "
            "zurücknehmen. Um den Inhalt des Tonie zu ersetzen, nutze „Jetzt auf den "
            "Tonie aufspielen“.",
            "err",
        )
        return _back(beitrag.id)
    except AuftragError as exc:
        flash(request, str(exc), "err")
        return _back(beitrag.id)
    db.commit()
    flash(request, "Freigabe zurückgenommen.")
    return _back(beitrag.id)


@router.post("/aufnahmen/{beitrag_id}/zuschnitt")
def zuschnitt(
    beitrag_id: int,
    request: Request,
    chapter_title: str = Form(""),
    cut_start: str = Form(""),
    cut_end: str = Form(""),
    action: str = Form("save"),
    db: Session = Depends(get_db),
    _admin: Person = Depends(require_admin),
):
    """R41/R42: nur Angaben speichern -- die abgelegte Datei bleibt
    unveraendert, geschnitten wird beim Aufspielen (app/delivery/audio.py)."""
    beitrag = _get_beitrag(db, beitrag_id)
    if beitrag is None:
        return _not_found()

    if action == "reset":
        start = end = None
    else:
        try:
            start = parse_time(cut_start)
            end = parse_time(cut_end)
        except ValueError:
            flash(request, "Die Zeiten sind nicht lesbar. Format z. B. 0:02,4.", "err")
            return _back(beitrag.id)
        if end is not None and end <= (start or 0.0):
            flash(request, "Das Ende muss nach dem Start liegen.", "err")
            return _back(beitrag.id)

    beitrag.chapter_title = sanitize_title(chapter_title)
    beitrag.cut_start_seconds = start
    beitrag.cut_end_seconds = end
    db.commit()
    flash(request, "Zuschnitt zurückgesetzt." if action == "reset" else "Gespeichert.")
    return _back(beitrag.id)


@router.get("/aufnahmen/{beitrag_id}/loeschen", response_class=HTMLResponse)
def loeschen_bestaetigen(
    beitrag_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _admin: Person = Depends(require_admin),
):
    beitrag = _get_beitrag(db, beitrag_id)
    if beitrag is None:
        return _not_found()
    return render(
        request,
        "admin/aufnahmen_loeschen.html",
        {
            "beitrag": beitrag,
            "name": chapter_title_for(beitrag, _first_slot(beitrag)),
            "days": ", ".join(
                f"{s.campaign.name} · Tag {s.day}" for s in auftrag_slots(beitrag.auftrag)
            )
            if beitrag.auftrag is not None
            else None,
        },
        tab="aufnahmen",
    )


@router.post("/aufnahmen/{beitrag_id}/loeschen")
def loeschen(
    beitrag_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _admin: Person = Depends(require_admin),
):
    """R40: Datei und Datensatz entfernen. Der Slot-Zustand leitet sich aus
    dem Auslieferungs-Verlauf ab und bleibt deshalb unberuehrt."""
    beitrag = _get_beitrag(db, beitrag_id)
    if beitrag is None:
        return _not_found()
    # R38: jeder Kalender hat seinen eigenen Ersatzbeitrag.
    if db.execute(select(Campaign.id).where(Campaign.replacement_beitrag_id == beitrag.id)).first():
        flash(request, "Der Ersatzbeitrag lässt sich nicht löschen.", "err")
        return _back(beitrag.id)

    # Erst die Datei: scheitert das, bleibt der Verweis darauf erhalten.
    try:
        request.app.state.storage.delete(beitrag.audio_object_key)
    except Exception:
        logger.exception("Loeschen der Datei fehlgeschlagen: beitrag=%s", beitrag.id)
        flash(request, "Die Datei ließ sich gerade nicht löschen. Bitte erneut versuchen.", "err")
        return _back(beitrag.id)

    verified = [
        *db.execute(select(Campaign).where(Campaign.verified_beitrag_id == beitrag.id)).scalars(),
        *db.execute(
            select(CreativeTonie).where(CreativeTonie.verified_beitrag_id == beitrag.id)
        ).scalars(),
    ]
    for holder in verified:
        holder.verified_beitrag_id = None
        holder.verified_chapter_id = None
        holder.verified_for_day = None
    db.flush()
    db.delete(beitrag)
    db.commit()
    flash(request, "Aufnahme endgültig gelöscht.")
    return redirect("/admin/aufnahmen")
