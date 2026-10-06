"""Admin-Reiter Auslieferung (U8 Schritte 5, 11).

Manueller Anstoss, Trockenlauf, Ersatzbeitrag, Verlauf. Die Tonie-
Verknuepfung lebt seit Mehrkalender U10 im Setup.

Alle Routen sind plain `def` (Known Pitfalls, "async def-Routen mit
blockierenden Aufrufen"). Das mehrfache Formularfeld des manuellen Laufs
liest deshalb eine async-Dependency.

U10 Schritt 6: Anstoss, Trockenlauf und manueller Lauf starten im
Hintergrund und leiten sofort auf den Verlauf -- hinter dem Tunnel braeche
eine zweiminuetige Anfrage nach 100 Sekunden ab. Der Ausgang steht danach im
Verlauf, nicht im Hinweisbalken.

Mehrkalender U7: Seite, Platz, Verlauf, Anstoss und Trockenlauf gelten dem
gewaehlten Tonie (`app/admin/state.py::current_tonie`, Umschalter U10); der
manuelle Lauf waehlt seinen Ziel-Tonie frei (R6).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.admin.common import (
    berlin_now,
    first_campaign,
    flash,
    redirect,
    render,
    selection,
    tonie_name,
)
from app.admin.state import (
    auftrag_slots,
    current_tonie,
    is_deliverable,
    linked_tonies,
    slot_state,
)
from app.auth.dependencies import get_db, require_admin
from app.delivery.chapters import (
    beitrag_seconds,
    format_minutes,
    load_app_chapters,
    missing_space,
    space_for,
)
from app.delivery.job import (
    NoCalendarError,
    RunType,
    chapter_title_for,
    client_for,
    first_slot,
    open_run,
    placeholder_campaign_id,
    run_lock,
)
from app.delivery.trigger import (
    dispatch_run,
    evening_of,
    get_run_starter,
    get_storage,
    get_toniecloud_factory,
    tomorrows_advent_day,
)
from app.models import Beitrag, CreativeTonie, DeliveryRun, Slot
from app.settings import delivery_time_for
from app.storage import ObjectStorage
from app.toniecloud.client import (
    KontoUnavailable,
    TonieCloudClient,
    TonieCloudError,
    TonieCloudFactory,
    UnavailableClient,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")

PAGE = "/admin/auslieferung"
HISTORY_LIMIT = 100

RUN_LABELS = {
    "vorabend": "Vorabend",
    "kontrolllauf": "Kontrolllauf",
    "anstoss": "Anstoß",
    "trockenlauf": "Trockenlauf",
    "probelauf": "Probelauf",
    "aufraeumen": "Aufräumen",
    "abraeumen": "Abräumen",
}

OUTCOME_TAGS = {
    "erfolg": ("erfolgreich", "tag tag-accent-2"),
    "ersatzbeitrag": ("Ersatzbeitrag", "tag"),
    "fehlschlag": ("fehlgeschlagen", "tag tag-outline"),
    "uebersprungen": ("übersprungen", "tag tag-neutral"),
    "gestartet": ("läuft", "tag tag-neutral"),
    "abgebrochen": ("abgebrochen", "tag tag-outline"),
}


def _beitrag_label(beitrag: Beitrag) -> str:
    person = beitrag.person.display_name or beitrag.person.email
    return f"{chapter_title_for(beitrag, first_slot(beitrag))} · {person}"


def _deliverable_beitraege(db: Session) -> list[Beitrag]:
    return list(
        db.execute(
            select(Beitrag)
            .where(
                Beitrag.approved_at.is_not(None),
                Beitrag.rejected_at.is_(None),
                Beitrag.detached_at.is_(None),
            )
            .order_by(Beitrag.id)
        ).scalars()
    )


def _days_label(beitrag: Beitrag) -> str:
    """R15: "Kalender · Tag" je Kalendertag des Auftrags, sonst "frei"."""
    if beitrag.auftrag is None:
        return "frei"
    days = [f"{s.campaign.name} · Tag {s.day}" for s in auftrag_slots(beitrag.auftrag)]
    return ", ".join(days) or "ohne Kalendertag"


def _tonie_label(tonie: CreativeTonie) -> str:
    name = tonie_name(tonie)
    calendar = tonie.campaign.name if tonie.campaign is not None else "ohne Kalender"
    return f"{name} · {calendar}"


def _history_rows(db: Session, tonie_id: str, timezone: str) -> list[dict]:
    runs = db.execute(
        select(DeliveryRun)
        .where(DeliveryRun.tonie_id == tonie_id)
        .order_by(DeliveryRun.started_at.desc(), DeliveryRun.id.desc())
        .limit(HISTORY_LIMIT)
    ).scalars()
    tz = ZoneInfo(timezone)
    return [_history_row(db, run, tz) for run in runs]


def _history_row(db: Session, run: DeliveryRun, tz: ZoneInfo) -> dict:
    """Eine Zeile des Verlaufs; dieselbe Form liefert der Live-Status."""
    local = run.started_at.replace(tzinfo=UTC).astimezone(tz)
    if run.run_type == "manuell":
        label = "Manueller Lauf"
    else:
        label = RUN_LABELS.get(run.run_type, run.run_type)
        if run.target_day is not None:
            label = f"{label} Tag {run.target_day}"
    parts = []
    for raw_id in (run.beitrag_ids or "").split(","):
        if not raw_id:
            continue
        beitrag = db.get(Beitrag, int(raw_id))
        parts.append(
            _beitrag_label(beitrag) if beitrag is not None else f"gelöschter Beitrag #{raw_id}"
        )
    content = ", ".join(parts)
    if run.reason:
        content = f"{content} · {run.reason}" if content else run.reason
    fallback = (run.outcome, "tag tag-neutral")
    outcome_text, outcome_class = OUTCOME_TAGS.get(run.outcome, fallback)
    return {
        "id": run.id,
        "running": run.outcome == "gestartet",
        "time": f"{local.day}.{local.month}. {local:%H:%M}",
        "label": label,
        "content": content or "kein Inhalt",
        "outcome_text": outcome_text,
        "outcome_class": outcome_class,
    }


def _start_in_background(
    request: Request,
    db: Session,
    factory: TonieCloudFactory,
    storage: ObjectStorage,
    start: Callable[[Callable[[], None]], None],
    tonie: CreativeTonie,
    run_type: RunType,
    *,
    target_day: int | None = None,
    beitrag_ids: Sequence[int] | None = None,
    reset_verified: bool = False,
):
    """Wie die Ausloesung (U10 Schritt 3): die Anfrage nimmt die Sperre des
    Tonie nicht-blockierend und reicht sie an den Hintergrundlauf weiter."""
    lock = run_lock(tonie)
    if not lock.acquire(blocking=False):
        flash(
            request,
            "Es läuft bereits ein Lauf für diesen Tonie. Sein Ergebnis erscheint im Verlauf.",
            "err",
        )
        return redirect(PAGE)
    try:
        client = client_for(factory, db, tonie)
        if reset_verified:
            tonie.verified_beitrag_id = None
            tonie.verified_chapter_id = None
            tonie.verified_for_day = None
        entry = open_run(db, tonie, run_type, target_day, beitrag_ids=beitrag_ids)
    except Exception:
        lock.release()
        raise

    # Ab hier gehoert die Sperre `dispatch_run` -- auch wenn der Start scheitert.
    dispatch_run(
        request, db, start, client, storage, tonie, entry, lock, manual_beitrag_ids=beitrag_ids
    )
    flash(request, "Der Lauf läuft im Hintergrund. Sein Ergebnis erscheint gleich im Verlauf.")
    return redirect(PAGE)


def _space_view(
    tonie: CreativeTonie,
    deliverable: Sequence[Beitrag],
    client: TonieCloudClient | UnavailableClient,
    storage: ObjectStorage,
) -> dict:
    """R50: Bestand und freier Platz aus einem frischen Read; freigegebene
    Beitraege, die nicht hineinpassen, mit Namen."""
    household_id = client.find_household_id(tonie.tonie_id)
    state = client.get_state(household_id, tonie.tonie_id)
    space = space_for(state, load_app_chapters(tonie.app_chapters), client.get_config())
    too_long = []
    for beitrag in deliverable:
        seconds = beitrag_seconds(
            storage.get(beitrag.audio_object_key),
            beitrag.cut_start_seconds,
            beitrag.cut_end_seconds,
        )
        if missing_space(space, [seconds]) is not None:
            too_long.append(f"{_beitrag_label(beitrag)} ({format_minutes(seconds)})")
    return {
        "stock_count": space.stock_count,
        "stock_minutes": format_minutes(space.stock_seconds),
        "free_minutes": format_minutes(space.free_seconds),
        "too_long": too_long,
    }


def _valid_day(day: int | None) -> bool:
    return day is not None and 1 <= day <= 24


def _no_tonie(request: Request):
    flash(request, "Kein Creative Tonie gewählt. Tonies ordnest du im Setup zu.", "err")
    return redirect(PAGE)


def _no_calendar(request: Request):
    flash(
        request,
        "Dieser Tonie gehört zu keinem Kalender. Für ihn gibt es nur den manuellen Lauf.",
        "err",
    )
    return redirect(PAGE)


@router.get("/auslieferung")
def deliveries_page(
    request: Request,
    platz: int = 0,
    db: Session = Depends(get_db),
    factory: TonieCloudFactory = Depends(get_toniecloud_factory),
    storage: ObjectStorage = Depends(get_storage),
    _admin=Depends(require_admin),
):
    """R50: der Tonie wird nur mit `platz=1` gelesen -- sonst keine
    Anmeldung bei jedem Aufruf (der Client meldet sich erst beim ersten
    Aufruf an)."""
    if first_campaign(db) is None:
        return redirect("/admin")
    # R14: Auslieferung, Platz und Verlauf gelten nur dem gewaehlten Tonie.
    chosen = selection(db, request)
    tonie = chosen.tonie if chosen is not None else None
    campaign = chosen.campaign if chosen is not None else None

    config = request.app.state.config
    now = berlin_now(request)
    delivery_time = delivery_time_for(db, evening_of(now))
    next_day = tomorrows_advent_day(now)
    next_slot = None
    next_state = None
    if next_day is not None and campaign is not None:
        next_slot = db.execute(
            select(Slot).where(Slot.campaign_id == campaign.id, Slot.day == next_day)
        ).scalar_one_or_none()
        if next_slot is not None:
            next_state = slot_state(
                db,
                next_slot,
                now=now,
                delivery_time=delivery_time,
                tonie_id=tonie.tonie_id if tonie is not None else None,
            )
    next_auftrag = next_slot.auftrag if next_slot is not None else None

    replacement = (
        db.get(Beitrag, campaign.replacement_beitrag_id)
        if campaign is not None and campaign.replacement_beitrag_id is not None
        else None
    )
    mirrored = (
        [tonie_name(t) for t in campaign.tonies if t.id != tonie.id]
        if tonie is not None and campaign is not None
        else []
    )
    deliverable = _deliverable_beitraege(db)

    space, space_error = None, None
    if platz and tonie is not None:
        try:
            space = _space_view(tonie, deliverable, factory.for_tonie(db, tonie), storage)
        except Exception as exc:
            logger.warning("Platzpruefung fehlgeschlagen: %s", exc)
            # Nur Fehler des Toniecloud-Clients liegen an der Toniecloud --
            # Speicher oder ffprobe nicht ihr anlasten.
            if isinstance(exc, KontoUnavailable):
                space_error = f"Platz konnte nicht geprüft werden. {exc}"
            elif isinstance(exc, (TonieCloudError, httpx.HTTPError)):
                space_error = (
                    "Platz konnte nicht geprüft werden. Die Toniecloud ist gerade nicht erreichbar."
                )
            else:
                space_error = "Platz konnte nicht geprüft werden. Details stehen im Server-Log."

    return render(
        request,
        "admin/auslieferung.html",
        {
            "delivery_time": delivery_time.strftime("%H:%M"),
            "next_day": next_day,
            "next_slot": next_slot,
            "next_title": next_auftrag.title if next_auftrag is not None else None,
            "next_person": next_auftrag.person if next_auftrag is not None else None,
            "next_state": next_state,
            "campaign": campaign,
            "tonie_name": tonie_name(tonie) if tonie is not None else None,
            "mirrored": mirrored,
            "default_day": next_day or 1,
            "replacement": replacement,
            "replacement_label": _beitrag_label(replacement) if replacement else None,
            "deliverable": [(b, _beitrag_label(b), _days_label(b)) for b in deliverable],
            "manual_targets": [(t, _tonie_label(t)) for t in linked_tonies(db)],
            "current_tonie": tonie,
            "history": _history_rows(db, tonie.tonie_id, config.timezone) if tonie else [],
            "space": space,
            "space_error": space_error,
        },
        tab="auslieferung",
    )


@router.get("/auslieferung/status")
def deliveries_status(
    request: Request,
    ids: str = "",
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    """Live-Status (Mockup 2026-10-03): Stand der angefragten Laeufe des
    gewaehlten Tonie, Beschriftung wie im Verlauf. Keine Tonie-ID."""
    tonie = current_tonie(db, request)
    wanted = [int(raw) for raw in ids.split(",") if raw.strip().isdigit()]
    rows = []
    if tonie is not None and wanted:
        tz = ZoneInfo(request.app.state.config.timezone)
        runs = db.execute(
            select(DeliveryRun)
            .where(DeliveryRun.id.in_(wanted), DeliveryRun.tonie_id == tonie.tonie_id)
            .order_by(DeliveryRun.id)
        ).scalars()
        rows = [_history_row(db, run, tz) for run in runs]
    live = [
        {key: row[key] for key in ("id", "running", "content", "outcome_text", "outcome_class")}
        for row in rows
    ]
    return {"runs": live, "running": any(row["running"] for row in live)}


@router.post("/auslieferung/jetzt")
def run_now_route(
    request: Request,
    day: int | None = Form(None),
    db: Session = Depends(get_db),
    factory: TonieCloudFactory = Depends(get_toniecloud_factory),
    storage: ObjectStorage = Depends(get_storage),
    start: Callable[[Callable[[], None]], None] = Depends(get_run_starter),
    _admin=Depends(require_admin),
):
    """Plan Schritt 5: der Anstoss setzt den Verifiziert-Zustand ausser
    Kraft (wie /delivery/run-now), aber fuer den gewaehlten Tag -- erst
    unter der Sperre, damit ein laufender Lauf ihn behaelt."""
    tonie = current_tonie(db, request)
    if tonie is None:
        return _no_tonie(request)
    if tonie.campaign is None:
        return _no_calendar(request)
    if not _valid_day(day):
        flash(request, "Bitte einen Tag zwischen 1 und 24 wählen.", "err")
        return redirect(PAGE)

    return _start_in_background(
        request,
        db,
        factory,
        storage,
        start,
        tonie,
        "anstoss",
        target_day=day,
        reset_verified=True,
    )


@router.post("/auslieferung/trockenlauf")
def dry_run_route(
    request: Request,
    day: int | None = Form(None),
    db: Session = Depends(get_db),
    factory: TonieCloudFactory = Depends(get_toniecloud_factory),
    storage: ObjectStorage = Depends(get_storage),
    start: Callable[[Callable[[], None]], None] = Depends(get_run_starter),
    _admin=Depends(require_admin),
):
    """R45 wie /delivery/dry-run; ohne gewaehlten Tag gilt der Folgetag."""
    tonie = current_tonie(db, request)
    if tonie is None:
        return _no_tonie(request)
    if tonie.campaign is None:
        return _no_calendar(request)
    target_day = day if _valid_day(day) else tomorrows_advent_day(berlin_now(request))

    return _start_in_background(
        request, db, factory, storage, start, tonie, "trockenlauf", target_day=target_day
    )


async def _form(request: Request) -> FormData:
    return await request.form()


def _order_key(form: FormData, beitrag_id: int) -> tuple[int, int, int]:
    try:
        return (0, int(form.get(f"order_{beitrag_id}", "")), beitrag_id)
    except ValueError:
        return (1, 0, beitrag_id)


@router.post("/auslieferung/manuell")
def manual_run_route(
    request: Request,
    form: FormData = Depends(_form),
    db: Session = Depends(get_db),
    factory: TonieCloudFactory = Depends(get_toniecloud_factory),
    storage: ObjectStorage = Depends(get_storage),
    start: Callable[[Callable[[], None]], None] = Depends(get_run_starter),
    _admin=Depends(require_admin),
):
    """R20/R42/AE24. Vertrag (auch fuer den Knopf im Aufnahmen-Reiter):
    `beitrag_id` mehrfach, Reihenfolge je `order_<id>`, Ziel-Tonie (R6) als
    `tonie` (`creative_tonies.id`), ohne Angabe der aktuelle Tonie."""
    if placeholder_campaign_id(db) is None:
        # KTD10: ohne Kalender kein Platzhalter fuer den Verlaufseintrag.
        flash(request, str(NoCalendarError()), "err")
        return redirect(PAGE)
    try:
        tonie = (
            db.get(CreativeTonie, int(form["tonie"]))
            if form.get("tonie")
            else current_tonie(db, request)
        )
    except ValueError:
        tonie = None
    if tonie is None:
        return _no_tonie(request)

    try:
        ids = list(dict.fromkeys(int(raw) for raw in form.getlist("beitrag_id")))
    except ValueError:
        ids = []
    if not ids:
        flash(request, "Bitte mindestens einen Beitrag auswählen.", "err")
        return redirect(PAGE)
    if not all(is_deliverable(db.get(Beitrag, bid)) for bid in ids):
        flash(
            request,
            "Nur freigegebene, nicht abgelöste Beiträge können aufgespielt werden.",
            "err",
        )
        return redirect(PAGE)

    ordered = sorted(ids, key=lambda bid: _order_key(form, bid))
    return _start_in_background(
        request, db, factory, storage, start, tonie, "manuell", beitrag_ids=ordered
    )
