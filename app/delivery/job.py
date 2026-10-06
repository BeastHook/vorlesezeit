"""Auslieferungsvorgang (U5): den Tag des Folgetags aufspielen, pruefen,
notfalls den Ersatzbeitrag nachschieben, in jedem Fall melden.

`run_delivery` ist bewusst deterministisch und ohne Wanduhr-Zugriff -- den
Zieltag bestimmt der Aufrufer (app/delivery/trigger.py), damit diese
Funktion vollstaendig durchgetestet werden kann, ohne die Zeit zu faelschen.

Idempotenz (Approach-Punkt 3-4): jeder Lauf liest den Live-Zustand des
Tonie und endet ohne Aktion, wenn dort bereits der erwartete Beitrag liegt.
Der gespeicherte "Verifiziert-Zustand" (CreativeTonie.verified_*) ist dafuer nur
eine Abkuerzung -- er verbindet unsere Beitrag.id mit der von der
Toniecloud vergebenen (opaken) Kapitel-id ueber Prozessgrenzen hinweg
(siehe .claude/skills/toniecloud-api/SKILL.md, "Chapter-Identitaet"). Er
ist widerlegbar: passt der Live-Zustand nicht mehr, gilt er als ungueltig.

Mehrkalender U7 (KTD3, R2/R3): jeder Lauf gilt genau einem Creative Tonie.
App-Kapitel und Verifiziert-Zustand liegen an dessen `creative_tonies`-Zeile;
Inhalt und Ersatzbeitrag kommen aus dem Kalender des Tonie. Gespiegelte Tonies
eines Kalenders teilen das App-Kapitel, nicht den Bestand.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.admin.state import auftrag_slots, deliverable_beitrag, is_deliverable
from app.delivery.audio import apply_cut, probe_duration_seconds
from app.delivery.chapters import (
    AppChapter,
    dump_app_chapters,
    load_app_chapters,
    missing_space,
    space_for,
    stock_loss,
    stock_of,
)
from app.models import Beitrag, Campaign, CreativeTonie, DeliveryRun, Slot
from app.storage import ObjectStorage
from app.toniecloud.client import (
    KontoUnavailable,
    TermsOfUseRequiredError,
    TonieCloudClient,
    TonieCloudError,
    TonieCloudFactory,
    UnavailableClient,
    UnsupportedFormatError,
    validate_format,
    verify_upload,
)
from app.toniecloud.models import Chapter, CreativeTonieState

logger = logging.getLogger(__name__)

RunType = Literal[
    "vorabend",
    "kontrolllauf",
    "anstoss",
    "manuell",
    "trockenlauf",
    "probelauf",
    "aufraeumen",
    # U7/R37: App-Kapitel sofort entfernen, wenn der Admin den Tonie von
    # seinem Kalender trennt.
    "abraeumen",
]

# U10: offener Verlaufseintrag, geschrieben vor dem ersten Toniecloud-Aufruf.
RUN_STARTED = "gestartet"
# Offener Eintrag ohne gehaltene Sperre -- der Lauf endete nie (Neustart).
RUN_ABORTED = "abgebrochen"
# Schluessel in Session.info: der offene Eintrag, den `_record` abschliesst.
_OPEN_ENTRY = "delivery_open_entry"


@dataclass
class DeliveryOutcome:
    success: bool
    run_type: RunType
    target_day: int | None
    used_beitrag_id: int | None
    used_replacement: bool
    reason: str | None = None
    title_mismatches: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    # U7: Toniecloud-ID des bespielten Tonie (Zeile der Sammelmeldung, U8).
    tonie_id: str | None = None
    # U8: ob der Lauf den Tonie wirklich neu bespielt hat (kein idempotenter
    # Lauf) -- ein Kontrolllauf meldet sich nur dann einzeln (KTD11).
    changed_tonie: bool = False


class NoCalendarError(ValueError):
    """KTD10: ohne jeden Kalender gibt es keinen Platzhalter fuer
    `delivery_runs.campaign_id` -- der Lauf wird verweigert."""

    def __init__(self) -> None:
        super().__init__("Bitte zuerst einen Kalender anlegen.")


class TonieBusyError(RuntimeError):
    """R25: fuer diesen Tonie laeuft gerade ein Lauf."""

    def __init__(self) -> None:
        super().__init__("Für diesen Tonie läuft gerade ein Lauf. Bitte später erneut versuchen.")


_tonie_locks: dict[str, threading.Lock] = {}
_tonie_locks_guard = threading.Lock()


def _lock_for(tonie_id: str) -> threading.Lock:
    """Ein Lock je Creative Tonie (Approach-Punkt 14). In-Prozess genuegt,
    da die App als einzelner Uvicorn-Prozess ohne --workers laeuft (KTD11)."""
    with _tonie_locks_guard:
        return _tonie_locks.setdefault(tonie_id, threading.Lock())


def run_lock(tonie: CreativeTonie) -> threading.Lock:
    """Die Sperre des Tonie (KTD9: je Tonie-ID, Laeufe verschiedener Tonies
    laufen parallel)."""
    return _lock_for(tonie.tonie_id)


def tonie_run_active(tonie_id: str) -> bool:
    """R25: ob gerade ein Lauf fuer diesen Tonie die Sperre haelt --
    nicht blockierend, fuer das Setup (U11)."""
    return _lock_for(tonie_id).locked()


def client_for(
    factory: TonieCloudFactory, db: Session, tonie: CreativeTonie
) -> TonieCloudClient | UnavailableClient:
    """R24: der Client entsteht beim Start des Laufs aus den dann gueltigen
    Zugangsdaten und bleibt fuer den ganzen Lauf derselbe (Schnappschuss);
    eine Passwortaenderung waehrenddessen erreicht erst den naechsten Lauf.
    Ohne nutzbares Konto ("neu eingeben", keins hinterlegt) scheitert der
    Lauf ohne Anmeldeversuch mit benanntem Grund (U6 Rueckfallregel)."""
    try:
        return factory.for_tonie(db, tonie)
    except KontoUnavailable as exc:
        return UnavailableClient(str(exc))


def placeholder_campaign_id(db: Session) -> int | None:
    """KTD10: Laeufe auf einen Tonie ohne Kalender tragen den ersten Kalender
    der Instanz -- nur weil `delivery_runs.campaign_id` NOT NULL ist. Keine
    Abfrage liest ihn fuer Faelligkeit, Versuche oder Zustand."""
    return db.execute(select(Campaign.id).order_by(Campaign.id).limit(1)).scalar_one_or_none()


def _campaign_id_for(db: Session, tonie: CreativeTonie) -> int | None:
    """Der Kalender des Tonie, sonst der Platzhalter (KTD10)."""
    return tonie.campaign_id or placeholder_campaign_id(db)


def open_run(
    db: Session,
    tonie: CreativeTonie,
    run_type: RunType,
    target_day: int | None,
    *,
    evening: date | None = None,
    started_at: datetime | None = None,
    beitrag_ids: Sequence[int] | None = None,
    campaign_id: int | None = None,
) -> DeliveryRun:
    """U10 Schritt 4: Verlaufseintrag "gestartet", bevor der Lauf die
    Toniecloud beruehrt. `_record` schliesst ihn am Ende des Laufs ab.
    `campaign_id` ist der Kalender des Tonie, sonst der Platzhalter (KTD10)."""
    campaign_id = campaign_id or _campaign_id_for(db, tonie)
    if campaign_id is None:
        raise NoCalendarError()
    entry = DeliveryRun(
        campaign_id=campaign_id,
        tonie_id=tonie.tonie_id,
        run_type=run_type,
        target_day=target_day,
        evening=evening,
        started_at=started_at or datetime.now(UTC).replace(tzinfo=None),
        outcome=RUN_STARTED,
        beitrag_ids=",".join(str(i) for i in beitrag_ids) if beitrag_ids else None,
    )
    db.add(entry)
    db.commit()
    return entry


def run_in_background(
    session_factory: sessionmaker[Session],
    client: TonieCloudClient,
    storage: ObjectStorage,
    tonie_pk: int,
    entry_id: int,
    lock: threading.Lock,
    *,
    manual_beitrag_ids: Sequence[int] | None = None,
    after: Callable[[DeliveryOutcome], None] | None = None,
) -> None:
    """U10 Schritte 2-3: Hintergrundlauf mit eigener Datenbanksitzung. Die
    Anfrage hat `lock` bereits genommen; hier wird sie in jedem Fall
    freigegeben. `after` ist die Meldungsentscheidung (U11). Scheitert schon
    das Lesen aus der Datenbank, bleibt der Eintrag offen -- der naechste
    faellige Aufruf verbucht ihn als "abgebrochen"."""
    try:
        with session_factory() as db:
            tonie = db.get(CreativeTonie, tonie_pk)
            entry = db.get(DeliveryRun, entry_id)
            outcome = run_locked(db, client, storage, tonie, entry, manual_beitrag_ids)
            _report(db, entry, outcome, after)
    except Exception:
        logger.exception("Auslieferungslauf ohne Abschluss beendet: eintrag=%s", entry_id)
    finally:
        lock.release()


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _error_detail(exc: BaseException) -> str:
    """Toniecloud-Fehler tragen schon eine lesbare Meldung, alles andere den Typnamen."""
    return str(exc) if isinstance(exc, TonieCloudError) else _describe(exc)


def _verify_with_stock(
    patch_state: CreativeTonieState,
    final_state: CreativeTonieState,
    new_titles: Sequence[str],
    stock: Sequence[Chapter],
) -> tuple[str | None, tuple[tuple[str, str], ...]]:
    """U16: erst die Kapitelfolge pruefen (verify_upload), dann ob der Bestand
    vollstaendig hinter den neuen Kapiteln steht (R49). Liefert den Grund des
    Fehlschlags oder None, dazu die Titel-Abweichungen des Erfolgsfalls."""
    result = verify_upload(
        patch_state, final_state, expected_titles=[*new_titles, *(c.title for c in stock)]
    )
    if not result.success:
        return result.reason, ()
    return stock_loss(stock, final_state, len(new_titles)), result.title_mismatches


def _report(
    db: Session,
    entry: DeliveryRun,
    outcome: DeliveryOutcome,
    after: Callable[[DeliveryOutcome], None] | None,
) -> None:
    """KTD18: ein Mailfehler aendert den Ausgang des Laufs nicht -- er wird
    geloggt und am Verlaufseintrag vermerkt."""
    if after is None:
        return
    try:
        after(outcome)
    except Exception as exc:
        logger.exception("Abendmeldung nicht verschickt: eintrag=%s", entry.id)
        note = f"Abendmeldung nicht verschickt ({_describe(exc)})."
        entry.reason = f"{entry.reason} · {note}" if entry.reason else note
        db.commit()


def named_title(beitrag: Beitrag, slot: Slot | None = None) -> str | None:
    """R42: Kapitelname, sonst Titel des Auftrags, sonst Einreichungstitel.
    Mehrkalender U7: der Auftragstitel ersetzt das Altfeld `Slot.title`;
    `slot` bleibt nur fuer den Rueckfall "Tuerchen <Tag>" (chapter_title_for)."""
    auftrag_title = beitrag.auftrag.title if beitrag.auftrag is not None else None
    return beitrag.chapter_title or auftrag_title or beitrag.title


def chapter_title_for(beitrag: Beitrag, slot: Slot | None) -> str:
    """R42: Fallback-Kette, wenn kein admin-gepflegter Kapitelname gesetzt ist.
    `slot` ist der Kalendertag, fuer den geliefert wird."""
    if title := named_title(beitrag, slot):
        return title
    if slot is not None:
        return f"Tuerchen {slot.day}"
    return "Tuerchen"


def first_slot(beitrag: Beitrag) -> Slot | None:
    """Der frueheste Kalendertag des Auftrags -- fuer den Rueckfallnamen
    eines Beitrags, der nicht fuer einen bestimmten Tag geliefert wird."""
    if beitrag.auftrag is None:
        return None
    slots = auftrag_slots(beitrag.auftrag)
    return slots[0] if slots else None


def run_delivery(
    db: Session,
    client: TonieCloudClient | UnavailableClient,
    storage: ObjectStorage,
    tonie: CreativeTonie,
    *,
    run_type: RunType,
    target_day: int | None = None,
    manual_beitrag_ids: Sequence[int] | None = None,
    after: Callable[[DeliveryOutcome], None] | None = None,
) -> DeliveryOutcome:
    """Synchroner Lauf (Skripte, JSON-Routen): wartet auf die Sperre."""
    with run_lock(tonie):
        # Der Aufrufer hat den Tonie vor dem Warten auf die Sperre geladen
        # (expire_on_commit=False). Ein Lauf in einer anderen Sitzung kann
        # inzwischen neue App-Kapitel gespeichert haben -- frisch lesen, sonst
        # gaelten sie als Bestand.
        db.refresh(tonie)
        entry = open_run(db, tonie, run_type, target_day, beitrag_ids=manual_beitrag_ids)
        outcome = run_locked(db, client, storage, tonie, entry, manual_beitrag_ids)
        _report(db, entry, outcome, after)
        return outcome


def detach_tonie(
    db: Session,
    client: TonieCloudClient | UnavailableClient,
    tonie: CreativeTonie,
    *,
    after: Callable[[DeliveryOutcome], None] | None = None,
) -> DeliveryOutcome | None:
    """R37: trennt den Tonie von seinem Kalender und raeumt das App-Kapitel
    sofort ab (Laufart `abraeumen`, Bestand bleibt). Bei Erfolg ist das
    Gedaechtnis leer; scheitert das Abraeumen, bleibt es mit
    `abraeumen_offen` stehen, und der Aufraeumlauf am 25.12. entfernt es.

    Nicht blockierend: haelt ein Lauf die Sperre, `TonieBusyError` und nichts
    geaendert (R25). Ohne gemerktes App-Kapitel nur das Trennen, kein Lauf
    (None). Synchron -- das Abraeumen ist ein PATCH ohne Transcoding."""
    lock = run_lock(tonie)
    if not lock.acquire(blocking=False):
        raise TonieBusyError()
    try:
        db.refresh(tonie)
        calendar_id = tonie.campaign_id
        tonie.campaign_id = None
        tonie.verified_beitrag_id = None
        tonie.verified_chapter_id = None
        tonie.verified_for_day = None
        if not load_app_chapters(tonie.app_chapters):
            db.commit()
            return None
        # Vor dem Lauf gesetzt, damit auch ein abgebrochener Lauf das
        # Kapitel fuer den 25.12. vormerkt; der Erfolg nimmt es zurueck.
        tonie.abraeumen_offen = True
        db.commit()
        entry = open_run(db, tonie, "abraeumen", None, campaign_id=calendar_id)
        outcome = run_locked(db, client, None, tonie, entry)
        _report(db, entry, outcome, after)
        return outcome
    finally:
        lock.release()


def run_locked(
    db: Session,
    client: TonieCloudClient | UnavailableClient,
    storage: ObjectStorage | None,
    tonie: CreativeTonie,
    entry: DeliveryRun,
    manual_beitrag_ids: Sequence[int] | None = None,
) -> DeliveryOutcome:
    """Fuehrt den Lauf zu `entry` aus; der Aufrufer haelt die Sperre.

    KTD18: der Rand des Auslieferungsvorgangs -- jede unerwartete Ausnahme
    schliesst den Eintrag als Fehlschlag mit Ausnahmeklasse und Nachricht."""
    db.info[_OPEN_ENTRY] = entry
    run_type, target_day = entry.run_type, entry.target_day
    try:
        return _run_locked(db, client, storage, tonie, run_type, target_day, manual_beitrag_ids)
    except Exception as exc:
        logger.exception("Auslieferungslauf mit unerwartetem Fehler: eintrag=%s", entry.id)
        db.rollback()
        # rollback() laesst Session.info stehen, aber `_record` hat den Eintrag
        # schon entnommen, wenn erst dessen commit() scheitert.
        db.info[_OPEN_ENTRY] = entry
        return _record(db, tonie, run_type, target_day, False, _describe(exc))
    finally:
        db.info.pop(_OPEN_ENTRY, None)


def _run_locked(
    db: Session,
    client: TonieCloudClient,
    storage: ObjectStorage,
    tonie: CreativeTonie,
    run_type: RunType,
    target_day: int | None,
    manual_beitrag_ids: Sequence[int] | None,
) -> DeliveryOutcome:
    tonie_id = tonie.tonie_id

    if run_type == "manuell":
        return _run_manual(db, client, storage, tonie, manual_beitrag_ids or [])
    if run_type in ("aufraeumen", "abraeumen"):
        return _run_cleanup(db, client, tonie, run_type)

    campaign = tonie.campaign
    if campaign is None:
        return _record(
            db, tonie, run_type, target_day, False, "Der Tonie gehört zu keinem Kalender."
        )
    if run_type in ("trockenlauf", "probelauf"):
        return _run_dry_run(db, client, storage, tonie, campaign, run_type, target_day)

    if run_type == "kontrolllauf" and _manual_run_since_vorabend(db, tonie, target_day):
        return _record(
            db,
            tonie,
            run_type,
            target_day,
            True,
            "Nach der Vorabend-Auslieferung lief ein manueller Lauf; erst der naechste "
            "Vorabend-Lauf raeumt wieder auf ein Kapitel zurueck.",
            outcome_label="uebersprungen",
        )

    # U10 Schritt 1: Zieltag und Inhalt stehen fest, bevor eine Anmeldung entsteht.
    if target_day is None:
        return _record(db, tonie, run_type, None, False, "Kein Zieltag (ausserhalb des Advents).")

    slot = _calendar_day(db, campaign, target_day)
    beitrag, used_replacement, missing_reason = _expected_beitrag(db, campaign, slot, target_day)

    if beitrag is None:
        return _record(db, tonie, run_type, target_day, False, missing_reason)

    household_id = client.find_household_id(tonie_id)
    baseline = client.get_state(household_id, tonie_id)
    app = load_app_chapters(tonie.app_chapters)

    if _is_idempotent(tonie, target_day, beitrag, baseline):
        return _record(
            db,
            tonie,
            run_type,
            target_day,
            True,
            None,
            used_beitrag_id=beitrag.id,
            used_replacement=used_replacement,
        )

    attempt = _attempt_single_chapter(
        client, storage, beitrag, slot, household_id, tonie_id, baseline, app
    )
    any_patch_happened = attempt.patched

    if attempt.success:
        _mark_verified(tonie, target_day, beitrag.id, attempt.chapter_id, attempt.seconds)
        return _record(
            db,
            tonie,
            run_type,
            target_day,
            True,
            missing_reason if used_replacement else None,
            used_beitrag_id=beitrag.id,
            used_replacement=used_replacement,
            title_mismatches=attempt.title_mismatches,
            changed_tonie=True,
        )

    rep_beitrag = None if used_replacement else _usable_replacement(db, campaign)
    if rep_beitrag is not None:
        attempt2 = _attempt_single_chapter(
            client, storage, rep_beitrag, None, household_id, tonie_id, baseline, app
        )
        any_patch_happened = any_patch_happened or attempt2.patched
        if attempt2.success:
            _mark_verified(tonie, target_day, rep_beitrag.id, attempt2.chapter_id, attempt2.seconds)
            return _record(
                db,
                tonie,
                run_type,
                target_day,
                True,
                f"Tagesbeitrag fehlgeschlagen ({attempt.reason}); Ersatzbeitrag ausgeliefert.",
                used_beitrag_id=rep_beitrag.id,
                used_replacement=True,
                title_mismatches=attempt2.title_mismatches,
                changed_tonie=True,
            )
        combined_reason = f"Tagesbeitrag: {attempt.reason}; Ersatzbeitrag: {attempt2.reason}"
    else:
        combined_reason = attempt.reason

    if any_patch_happened:
        combined_reason = _restore_baseline(
            client, household_id, tonie_id, baseline, combined_reason
        )

    return _record(db, tonie, run_type, target_day, False, combined_reason)


def _calendar_day(db: Session, campaign: Campaign, day: int) -> Slot | None:
    return db.execute(
        select(Slot).where(Slot.campaign_id == campaign.id, Slot.day == day)
    ).scalar_one_or_none()


def _restore_baseline(
    client: TonieCloudClient,
    household_id: str,
    tonie_id: str,
    baseline: CreativeTonieState,
    reason: str | None,
) -> str | None:
    """Spielt den Laufbeginn-Stand zurueck. Scheitert das selbst, nennt der
    Eintrag beide Fehler und ausdruecklich den unbekannten Tonie-Zustand
    (U11 Schritt 4)."""
    try:
        client.replace_chapters(household_id, tonie_id, list(baseline.chapters))
    except Exception as exc:
        logger.exception("Zurueckspielen des Rueckfallstands fehlgeschlagen")
        return (
            f"{reason}; Zurueckspielen des Rueckfallstands fehlgeschlagen ({_describe(exc)}). "
            "Tonie-Zustand unbekannt, bitte in der Tonie-App pruefen."
        )
    return reason


def _expected_beitrag(
    db: Session, campaign: Campaign, slot: Slot | None, target_day: int
) -> tuple[Beitrag | None, bool, str | None]:
    """Freigegebener Beitrag des Auftrags an diesem Kalendertag (R8: derselbe
    in jedem Kalender des Auftrags), sonst der Ersatzbeitrag des Kalenders
    (R21, R38). Approach-Punkt 8: ein fehlender Tagesbeitrag ist kein
    Nichtstun, sondern derselbe Ersatzbeitrag-Pfad mit benanntem Tag."""
    beitrag = deliverable_beitrag(slot) if slot is not None else None
    if beitrag is not None:
        return beitrag, False, None

    replacement = _usable_replacement(db, campaign)
    if replacement is not None:
        return replacement, True, f"Kein freigegebener Beitrag fuer Tag {target_day}."

    return (
        None,
        True,
        f"Kein freigegebener Beitrag fuer Tag {target_day} und kein freigegebener "
        "Ersatzbeitrag hinterlegt.",
    )


def _usable_replacement(db: Session, campaign: Campaign) -> Beitrag | None:
    """R11 gilt auch fuer den Ersatzbeitrag: nur freigegeben, nicht abgelehnt,
    nicht geloest (app/admin/state.py::is_deliverable)."""
    if campaign.replacement_beitrag_id is None:
        return None
    beitrag = db.get(Beitrag, campaign.replacement_beitrag_id)
    return beitrag if is_deliverable(beitrag) else None


def _manual_run_since_vorabend(db: Session, tonie: CreativeTonie, target_day: int | None) -> bool:
    """Ob nach dem letzten Vorabend-Lauf fuer `target_day` ein erfolgreicher
    manueller Lauf oder Anstoss auf diesen Tonie kam -- dessen Inhalt bleibt
    bis zum naechsten Vorabend-Lauf (Plan, Key Decision "eigene Kapitel an
    Platz 1"). KTD10: je Tonie."""
    vorabend_at = db.execute(
        select(func.max(DeliveryRun.started_at)).where(
            DeliveryRun.tonie_id == tonie.tonie_id,
            DeliveryRun.run_type == "vorabend",
            DeliveryRun.target_day == target_day,
        )
    ).scalar_one()
    if vorabend_at is None:
        return False
    return (
        db.execute(
            select(DeliveryRun.id).where(
                DeliveryRun.tonie_id == tonie.tonie_id,
                DeliveryRun.run_type.in_(("manuell", "anstoss")),
                DeliveryRun.outcome.in_(("erfolg", "ersatzbeitrag")),
                DeliveryRun.started_at > vorabend_at,
            )
        ).first()
        is not None
    )


def _is_idempotent(
    tonie: CreativeTonie, target_day: int, beitrag: Beitrag, baseline: CreativeTonieState
) -> bool:
    if tonie.verified_for_day != target_day or tonie.verified_beitrag_id != beitrag.id:
        return False
    if baseline.transcoding or baseline.transcoding_errors:
        return False
    # U16: der Bestand dahinter entscheidet nicht (KTD20).
    return bool(baseline.chapters) and baseline.chapters[0].id == tonie.verified_chapter_id


@dataclass
class _AttemptResult:
    success: bool
    reason: str | None
    chapter_id: str | None
    patched: bool  # ob ueberhaupt ein PATCH stattfand -- entscheidet, ob ein Rollback noetig ist
    title_mismatches: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    seconds: float | None = None  # Dauer des aufgespielten Kapitels (U16, Platzrechnung)


def _attempt_single_chapter(
    client: TonieCloudClient,
    storage: ObjectStorage,
    beitrag: Beitrag,
    slot: Slot | None,
    household_id: str,
    tonie_id: str,
    baseline: CreativeTonieState,
    app: Sequence[AppChapter],
) -> _AttemptResult:
    """Ersetzt die App-Kapitel durch genau ein neues an Platz 1; der Bestand
    aus dem Rueckfallstand bleibt unveraendert dahinter (U16, R49). Zu wenig
    Platz wird vor dem Hochladen erkannt -- dann kein Upload, kein PATCH (R50)."""
    try:
        limits = client.get_config()
        data = storage.get(beitrag.audio_object_key)
        data = apply_cut(
            data, start_seconds=beitrag.cut_start_seconds, end_seconds=beitrag.cut_end_seconds
        )
        seconds = probe_duration_seconds(data)
        space_reason = missing_space(space_for(baseline, app, limits), [seconds])
        if space_reason is not None:
            return _AttemptResult(False, space_reason, None, False)
        filename = f"beitrag-{beitrag.id}.mp3"
        validate_format(filename, limits)
        target = client.create_file()
        client.upload_bytes(target, data, filename=filename, content_type="audio/mpeg")
    except TermsOfUseRequiredError:
        return _AttemptResult(
            False, "Nutzungsbedingungen muessen erneut bestaetigt werden.", None, False
        )
    except UnsupportedFormatError as exc:
        return _AttemptResult(False, f"Format abgelehnt: {exc}", None, False)
    except TonieCloudError as exc:
        return _AttemptResult(False, f"Hochladen fehlgeschlagen: {exc}", None, False)
    except Exception as exc:
        # U11: auch Speicher-, ffmpeg- und HTTP-Fehler sind ein gescheiterter
        # Versuch -- danach darf der Ersatzbeitrag einspringen bzw. der
        # Rueckfallstand zurueck, statt dass der Lauf ohne Aufraeumen abbricht.
        return _AttemptResult(False, f"Hochladen fehlgeschlagen: {_describe(exc)}", None, False)

    title = chapter_title_for(beitrag, slot)
    stock = stock_of(baseline, app)
    try:
        patch_state = client.replace_chapters(
            household_id, tonie_id, [Chapter(title=title, file=target.file_id), *stock]
        )
        final_state = client.wait_until_processed(household_id, tonie_id)
    except TonieCloudError as exc:
        return _AttemptResult(False, f"Ersetzen fehlgeschlagen: {exc}", None, True)
    except Exception as exc:
        return _AttemptResult(False, f"Ersetzen fehlgeschlagen: {_describe(exc)}", None, True)

    failure, title_mismatches = _verify_with_stock(patch_state, final_state, [title], stock)
    if failure is not None:
        return _AttemptResult(False, failure, None, True)

    chapter_id = final_state.chapters[0].id
    return _AttemptResult(True, None, chapter_id, True, title_mismatches, seconds)


def _run_manual(
    db: Session,
    client: TonieCloudClient,
    storage: ObjectStorage,
    tonie: CreativeTonie,
    beitrag_ids: Sequence[int],
) -> DeliveryOutcome:
    """R42: geordnete Liste freigegebener Beitraege in einem PATCH, an Platz 1
    vor dem unveraenderten Bestand (U16, R49). Alle Beitraege werden vor dem
    ersten Upload gemessen; reicht der Platz nicht, kein Upload, kein PATCH
    (R50). Scheitert der Lauf oder geht Bestand verloren, wird sofort der
    Laufbeginn-Stand zurueckgespielt -- kein Ersatzbeitrag (Approach-Punkt 13,
    R49). Fasst verified_* nicht an, merkt sich aber die neuen Kapitel als
    App-Kapitel (U16). U7/R6/R39: jeder verknuepfte Tonie, auch einer ohne
    Kalender -- dort ersetzt der Lauf nur dessen eigene App-Kapitel."""
    beitraege = [db.get(Beitrag, bid) for bid in beitrag_ids]
    missing = [f"#{bid}" for bid, beitrag in zip(beitrag_ids, beitraege) if beitrag is None]
    if missing:
        return _record(
            db,
            tonie,
            "manuell",
            None,
            False,
            f"Beitrag nicht mehr vorhanden: {', '.join(missing)}.",
        )

    tonie_id = tonie.tonie_id
    household_id = client.find_household_id(tonie_id)
    baseline = client.get_state(household_id, tonie_id)

    app = load_app_chapters(tonie.app_chapters)
    stock = stock_of(baseline, app)
    limits = client.get_config()
    prepared: list[tuple[Beitrag, bytes, float]] = []
    for beitrag in beitraege:
        try:
            data = storage.get(beitrag.audio_object_key)
            data = apply_cut(
                data, start_seconds=beitrag.cut_start_seconds, end_seconds=beitrag.cut_end_seconds
            )
            prepared.append((beitrag, data, probe_duration_seconds(data)))
        except Exception as exc:
            return _record(
                db,
                tonie,
                "manuell",
                None,
                False,
                f"Hochladen fehlgeschlagen: {_describe(exc)}",
                beitrag_ids=list(beitrag_ids),
            )

    space_reason = missing_space(space_for(baseline, app, limits), [s for _, _, s in prepared])
    if space_reason is not None:
        return _record(
            db, tonie, "manuell", None, False, space_reason, beitrag_ids=list(beitrag_ids)
        )

    chapters: list[Chapter] = []
    titles: list[str] = []
    for beitrag, data, _seconds in prepared:
        try:
            filename = f"beitrag-{beitrag.id}.mp3"
            validate_format(filename, limits)
            target = client.create_file()
            client.upload_bytes(target, data, filename=filename, content_type="audio/mpeg")
        except Exception as exc:
            detail = _error_detail(exc)
            return _record(
                db,
                tonie,
                "manuell",
                None,
                False,
                f"Hochladen fehlgeschlagen: {detail}",
                beitrag_ids=list(beitrag_ids),
            )
        title = chapter_title_for(beitrag, first_slot(beitrag))
        titles.append(title)
        chapters.append(Chapter(title=title, file=target.file_id))

    try:
        patch_state = client.replace_chapters(household_id, tonie_id, [*chapters, *stock])
        final_state = client.wait_until_processed(household_id, tonie_id)
    except Exception as exc:
        detail = _error_detail(exc)
        reason = _restore_baseline(
            client, household_id, tonie_id, baseline, f"Ersetzen fehlgeschlagen: {detail}"
        )
        return _record(db, tonie, "manuell", None, False, reason, beitrag_ids=list(beitrag_ids))

    failure, title_mismatches = _verify_with_stock(patch_state, final_state, titles, stock)
    if failure is not None:
        reason = _restore_baseline(client, household_id, tonie_id, baseline, failure)
        return _record(db, tonie, "manuell", None, False, reason, beitrag_ids=list(beitrag_ids))

    tonie.app_chapters = dump_app_chapters(
        [AppChapter(c.id, s) for c, (_, _, s) in zip(final_state.chapters, prepared)]
    )
    return _record(
        db,
        tonie,
        "manuell",
        None,
        True,
        None,
        used_beitrag_id=beitraege[0].id if beitraege else None,
        beitrag_ids=list(beitrag_ids),
        title_mismatches=title_mismatches,
    )


def _run_cleanup(
    db: Session,
    client: TonieCloudClient,
    tonie: CreativeTonie,
    run_type: Literal["aufraeumen", "abraeumen"],
) -> DeliveryOutcome:
    """R51 (Aufraeumen am 25.12.) und R37 (Abraeumen beim Trennen): entfernt
    die App-Kapitel, danach traegt der Tonie nur den Bestand. Ohne App-Kapitel
    auf dem Tonie ist nichts zu tun (kein PATCH). Scheitert das Ersetzen oder
    geht Bestand verloren, kommt der Laufbeginn-Stand zurueck und die
    App-Kapitel bleiben gemerkt (beim Trennen samt `abraeumen_offen`)."""
    tonie_id = tonie.tonie_id
    household_id = client.find_household_id(tonie_id)
    baseline = client.get_state(household_id, tonie_id)
    stock = stock_of(baseline, load_app_chapters(tonie.app_chapters))

    if len(stock) != len(baseline.chapters):
        try:
            patch_state = client.replace_chapters(household_id, tonie_id, stock)
            final_state = client.wait_until_processed(household_id, tonie_id)
        except Exception as exc:
            detail = _error_detail(exc)
            reason = _restore_baseline(
                client, household_id, tonie_id, baseline, f"Aufraeumen fehlgeschlagen: {detail}"
            )
            return _record(db, tonie, run_type, None, False, reason)
        failure, _ = _verify_with_stock(patch_state, final_state, [], stock)
        if failure is not None:
            reason = _restore_baseline(client, household_id, tonie_id, baseline, failure)
            return _record(db, tonie, run_type, None, False, reason)

    tonie.app_chapters = None
    tonie.abraeumen_offen = False
    tonie.verified_beitrag_id = None
    tonie.verified_chapter_id = None
    tonie.verified_for_day = None
    return _record(db, tonie, run_type, None, True, None)


def _run_dry_run(
    db: Session,
    client: TonieCloudClient,
    storage: ObjectStorage,
    tonie: CreativeTonie,
    campaign: Campaign,
    run_type: Literal["trockenlauf", "probelauf"],
    target_day: int | None,
) -> DeliveryOutcome:
    """R45: anmelden, Grenzwerte lesen, Datei anlegen, hochladen -- ohne
    Ersetzen. Kein Household-Bezug noetig, da weder Kapitelliste noch
    Zustand gelesen werden. Fasst verified_* nicht an.

    Der Probelauf (R48) ist derselbe Pfad ohne Zieltag, mit dem
    Ersatzbeitrag als Inhalt."""
    if run_type == "probelauf":
        beitrag = _usable_replacement(db, campaign)
        # Der Ersatzbeitrag ist hier der vorgesehene Inhalt, kein Einspringen.
        used_replacement = False
        if beitrag is None:
            return _record(
                db,
                tonie,
                run_type,
                None,
                False,
                "Probelauf: kein freigegebener Ersatzbeitrag hinterlegt (R21) -- "
                "ohne ihn gibt es keinen Inhalt zum Pruefen.",
            )
    else:
        if target_day is None:
            return _record(
                db, tonie, run_type, None, False, "Kein Zieltag (ausserhalb des Advents)."
            )

        slot = _calendar_day(db, campaign, target_day)
        beitrag, used_replacement, missing_reason = _expected_beitrag(
            db, campaign, slot, target_day
        )
        if beitrag is None:
            return _record(db, tonie, run_type, target_day, False, missing_reason)

    try:
        limits = client.get_config()
        data = storage.get(beitrag.audio_object_key)
        data = apply_cut(
            data, start_seconds=beitrag.cut_start_seconds, end_seconds=beitrag.cut_end_seconds
        )
        filename = f"beitrag-{beitrag.id}.mp3"
        validate_format(filename, limits)
        target = client.create_file()
        client.upload_bytes(target, data, filename=filename, content_type="audio/mpeg")
    except TonieCloudError as exc:
        return _record(db, tonie, run_type, target_day, False, f"Hochladen fehlgeschlagen: {exc}")

    label = "Probelauf" if run_type == "probelauf" else "Trockenlauf"
    return _record(
        db,
        tonie,
        run_type,
        target_day,
        True,
        f"{label}: Zugang, Formatvorpruefung und 409-Fall geprueft -- "
        "Verarbeitung und Kapitelliste NICHT geprueft (kein Ersetzen).",
        used_beitrag_id=beitrag.id,
        used_replacement=used_replacement,
    )


def _mark_verified(
    tonie: CreativeTonie,
    target_day: int,
    beitrag_id: int,
    chapter_id: str | None,
    seconds: float | None,
) -> None:
    tonie.verified_beitrag_id = beitrag_id
    tonie.verified_chapter_id = chapter_id
    tonie.verified_for_day = target_day
    # U16: das aufgespielte Kapitel ist ab jetzt App-Kapitel, kein Bestand (KTD20).
    tonie.app_chapters = (
        dump_app_chapters([AppChapter(chapter_id, seconds)]) if chapter_id else None
    )


def _record(
    db: Session,
    tonie: CreativeTonie,
    run_type: RunType,
    target_day: int | None,
    success: bool,
    reason: str | None,
    *,
    used_beitrag_id: int | None = None,
    used_replacement: bool = False,
    beitrag_ids: list[int] | None = None,
    title_mismatches: tuple[tuple[str, str], ...] = (),
    outcome_label: str | None = None,
    changed_tonie: bool = False,
) -> DeliveryOutcome:
    if outcome_label is None and success:
        outcome_label = "ersatzbeitrag" if used_replacement else "erfolg"
    elif outcome_label is None:
        outcome_label = "fehlschlag"
    # R44: auch automatische Laeufe nennen im Verlauf den betroffenen Inhalt.
    if beitrag_ids is None and used_beitrag_id is not None:
        beitrag_ids = [used_beitrag_id]

    joined_ids = ",".join(str(i) for i in beitrag_ids) if beitrag_ids else None

    # U10: den offenen Eintrag dieses Laufs abschliessen statt einen zweiten anzulegen.
    entry = db.info.pop(_OPEN_ENTRY, None)
    if entry is not None:
        entry.target_day = target_day
        entry.outcome = outcome_label
        entry.reason = reason
        entry.beitrag_ids = joined_ids or entry.beitrag_ids
    else:
        db.add(
            DeliveryRun(
                campaign_id=_campaign_id_for(db, tonie),
                tonie_id=tonie.tonie_id,
                run_type=run_type,
                target_day=target_day,
                started_at=datetime.now(UTC).replace(tzinfo=None),
                outcome=outcome_label,
                reason=reason,
                beitrag_ids=joined_ids,
            )
        )
    db.commit()

    return DeliveryOutcome(
        success=success,
        run_type=run_type,
        target_day=target_day,
        used_beitrag_id=used_beitrag_id,
        used_replacement=used_replacement,
        reason=reason,
        title_mismatches=title_mismatches,
        tonie_id=tonie.tonie_id,
        changed_tonie=changed_tonie,
    )
