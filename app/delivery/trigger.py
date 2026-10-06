"""Auslöse-Endpunkte fuer den Auslieferungsvorgang (U5, U10, KTD3/KTD13).

`/delivery/trigger` (Zeitplan + externer Anstoss, Geheimnis statt Sitzung --
Approach-Punkt 15) ist eine Zustandsmaschine, die sofort antwortet (204
nicht faellig, 202 gestartet/laeuft, 200 erledigt, 502 fehlgeschlagen/haengt);
den Lauf selbst startet sie im Hintergrund, mit Obergrenze fuer Wiederholungen
und Haengerkennung. Die JSON-Admin-Routen `/delivery/run-now`,
`/delivery/manual`, `/delivery/dry-run` laufen weiterhin synchron.

Mehrkalender U7 (KTD9/KTD10): Zustand, Versuche und Sperre gelten je Tonie;
jeder faellige Tonie bekommt seinen eigenen Hintergrundlauf, die Antwort ist
der schlechteste Zustand aller faelligen Tonies. Ein Kalender ohne Tonie loest
nichts aus (R38). Die Lieferzeit kommt aus den Einstellungen (KTD16).

Mehrkalender U8 (R4, KTD11): eine Sammel-Abendmeldung je Abend, geprueft nach
jedem Lauf der Vorabend-Phase und bei jedem Aufruf (`send_evening_summary`).
"""

from __future__ import annotations

import hmac
import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.admin.state import current_tonie
from app.auth.dependencies import get_db, require_admin
from app.config import Config
from app.delivery.calendar import advent_day_for, is_cleanup_evening, is_probe_evening
from app.delivery.job import (
    RUN_ABORTED,
    RUN_STARTED,
    DeliveryOutcome,
    NoCalendarError,
    _describe,
    _manual_run_since_vorabend,
    client_for,
    open_run,
    run_delivery,
    run_in_background,
    run_lock,
)
from app.mail.report import SummaryRow, build_summary_message, mask_tonie_id, send_report_mail
from app.mail.smtp import send_message
from app.models import Abendmeldung, CreativeTonie, DeliveryRun, Person
from app.settings import delivery_time_for
from app.storage import ObjectStorage
from app.toniecloud.client import TonieCloudClient, TonieCloudFactory, UnavailableClient

router = APIRouter(prefix="/delivery")
logger = logging.getLogger(__name__)

WINDOW_START = time(17, 0)
CONTROL_RUN_OFFSET = timedelta(hours=2)
# KTD13: das Fenster endet 2:45 nach der Lieferzeit, spaetestens um 01:45.
WINDOW_AFTER_DELIVERY = timedelta(hours=2, minutes=45)
RETRY_GAP = timedelta(minutes=15)
MAX_ATTEMPTS = 3
HANG_LIMIT = timedelta(minutes=10)

_SUCCESS_OUTCOMES = ("erfolg", "ersatzbeitrag", "uebersprungen")

AutoRunType = Literal["vorabend", "kontrolllauf", "probelauf", "aufraeumen"]
# KTD11: die Laufarten der Vorabend-Phase -- ihr Ergebnis steht in der Sammelmeldung.
SUMMARY_RUN_TYPES = ("vorabend", "probelauf", "aufraeumen")


@dataclass(frozen=True)
class Due:
    run_type: AutoRunType
    # Berliner Datum des Abends; nach Mitternacht der Vortag (KTD13).
    evening: date
    target_day: int | None


def evening_of(now: datetime) -> date:
    """KTD13: Berliner Datum des Abends; nach Mitternacht der Vortag."""
    return now.date() if now.time() >= WINDOW_START else now.date() - timedelta(days=1)


def determine_due(now: datetime, delivery_time: time) -> Due | None:
    """KTD13/R46/R48/R51: welche Laufart `now` (Europe/Berlin) gerade faellig
    macht, allein aus der Uhr. Der Kontrolllauf bekommt hier nur einen
    Vorschlag fuer den Zieltag; massgeblich ist der Vorabend-Lauf im Verlauf
    (U5 Schritt 6, siehe `_control_target_day`)."""
    evening = evening_of(now)
    delivery = datetime.combine(evening, delivery_time, tzinfo=now.tzinfo)
    if not delivery <= now < delivery + WINDOW_AFTER_DELIVERY:
        return None

    target_day = advent_day_for(evening + timedelta(days=1))
    if now < delivery + CONTROL_RUN_OFFSET:
        if target_day is not None:
            return Due("vorabend", evening, target_day)
        if is_probe_evening(evening):
            return Due("probelauf", evening, None)
        if is_cleanup_evening(evening):
            return Due("aufraeumen", evening, None)
        return None
    if target_day is not None:
        return Due("kontrolllauf", evening, target_day)
    return None


def tomorrows_advent_day(now: datetime) -> int | None:
    return advent_day_for((now + timedelta(days=1)).date())


def get_now(request: Request) -> datetime:
    """R29: einzige Zeitbasis; als Dependency, damit Tests die Uhr stellen."""
    return datetime.now(ZoneInfo(request.app.state.config.timezone))


def start_in_thread(run: Callable[[], None]) -> None:
    threading.Thread(target=run, name="auslieferung-lauf", daemon=True).start()


def get_run_starter() -> Callable[[Callable[[], None]], None]:
    """U10 Schritt 2: startet den Lauf im Hintergrund. Tests ersetzen das,
    um den Lauf deterministisch auszufuehren oder zurueckzustellen."""
    return start_in_thread


_factory_guard = threading.Lock()


def get_toniecloud_factory(request: Request) -> TonieCloudFactory:
    """KTD8: eine Fabrik je App (prozessweit), damit der Token-Zwischenspeicher
    je Konto Anfragen und Laeufe ueberdauert. Tests ersetzen diese Dependency."""
    state = request.app.state
    with _factory_guard:
        if not hasattr(state, "toniecloud_factory"):
            state.toniecloud_factory = TonieCloudFactory(state.config.credentials_key)
        return state.toniecloud_factory


def get_storage(request: Request) -> ObjectStorage:
    return request.app.state.storage


def _current_tonie_or_404(db: Session) -> CreativeTonie:
    tonie = current_tonie(db)
    if tonie is None:
        raise HTTPException(status_code=404, detail="Kein Creative Tonie verknüpft.")
    return tonie


# U10 Schritt 8: nicht faellig 204, gestartet/laeuft 202, erledigt 200,
# fehlgeschlagen/haengt 502. Alle Erfolgsfaelle bleiben 2xx.
_STATUS = {
    "nicht fällig": 204,
    "gestartet": 202,
    "läuft": 202,
    "erledigt": 200,
    "fehlgeschlagen": 502,
    "Lauf hängt": 502,
}
# KTD9: Rang fuer "schlechtester Zustand" -- 502 vor 202 vor 200 vor 204.
_RANK = {204: 0, 200: 1, 202: 2, 502: 3}


def worst_state(states: Sequence[str]) -> str:
    """KTD9: der Zustand mit dem schlechtesten Statuscode; ohne faelligen
    Tonie "nicht fällig"."""
    return max(states, key=lambda state: _RANK[_STATUS[state]], default="nicht fällig")


def due_tonies(db: Session, due: Due) -> list[CreativeTonie]:
    """R38/R39: nur Tonies mit Kalender. Am 25.12. raeumt die App auf jedem
    Tonie mit gemerktem App-Kapitel auf, der einen Kalender hat oder ein beim
    Trennen gescheitertes Abraeumen nachholt (`abraeumen_offen`); Tonies ohne
    Kalender behalten ihre Kapitel sonst dauerhaft. Wer an diesem Abend schon
    aufgeraeumt hat, bleibt faellig, damit der Zustand "erledigt" lautet."""
    query = select(CreativeTonie).order_by(CreativeTonie.id)
    if due.run_type == "aufraeumen":
        cleaned_tonight = select(DeliveryRun.tonie_id).where(
            DeliveryRun.run_type == "aufraeumen", DeliveryRun.evening == due.evening
        )
        query = query.where(
            (
                CreativeTonie.app_chapters.is_not(None)
                & (CreativeTonie.campaign_id.is_not(None) | CreativeTonie.abraeumen_offen.is_(True))
            )
            | CreativeTonie.tonie_id.in_(cleaned_tonight)
        )
    else:
        query = query.where(CreativeTonie.campaign_id.is_not(None))
    return list(db.execute(query).scalars())


@router.post("/trigger")
def trigger(
    request: Request,
    x_trigger_secret: str = Header(default=""),
    x_trigger_source: str = Header(default="unbekannt"),
    now: datetime = Depends(get_now),
    db: Session = Depends(get_db),
    factory: TonieCloudFactory = Depends(get_toniecloud_factory),
    storage: ObjectStorage = Depends(get_storage),
    start: Callable[[Callable[[], None]], None] = Depends(get_run_starter),
) -> Response:
    """Ausdruecklich NICHT ueber require_admin geschuetzt (Approach-Punkt
    15) -- traegt keine angemeldete Identitaet, nur das Geheimnis.

    KTD13: bildet einen Zustand ab, nicht einen gerade beendeten Lauf. Nur
    der Uebergang nach "gestartet" beruehrt die Toniecloud, und das im
    Hintergrund; die Antwort geht sofort."""
    config: Config = request.app.state.config
    check_trigger_secret(config, x_trigger_secret)

    due = determine_due(now, delivery_time_for(db, evening_of(now)))
    states = trigger_states(request, db, factory, storage, start, due, now) if due else {}
    state = worst_state(list(states.values()))
    try:
        send_evening_summary(config, request.app.state.session_factory, evening_of(now), now=now)
    except Exception:
        # Die Zeile ist wieder entfernt; der naechste Aufruf versucht es erneut.
        logger.exception("Sammel-Abendmeldung nicht verschickt: abend=%s", evening_of(now))

    logger.info(
        "Auslösung quelle=%s zustand=%s laufart=%s zieltag=%s tonies=%s",
        x_trigger_source,
        state,
        due.run_type if due else "-",
        due.target_day if due else "-",
        ",".join(f"{mask_tonie_id(tid)}:{s}" for tid, s in states.items()) or "-",
    )
    return Response(status_code=_STATUS[state])


def trigger_states(
    request: Request,
    db: Session,
    factory: TonieCloudFactory,
    storage: ObjectStorage,
    start: Callable[[Callable[[], None]], None],
    due: Due,
    now: datetime,
) -> dict[str, str]:
    """KTD9: Zustand je faelligem Tonie (Toniecloud-ID -> Zustand); jeder
    faellige Tonie ohne laufende Sperre bekommt seinen eigenen Lauf."""
    return {
        tonie.tonie_id: _decide_and_start(request, db, factory, storage, start, tonie, due, now)
        for tonie in due_tonies(db, due)
    }


def _attempts(db: Session, tonie: CreativeTonie, due: Due) -> list[DeliveryRun]:
    """Die Versuche dieses Tonie fuer Laufart und Abend, aeltester zuerst."""
    return list(
        db.execute(
            select(DeliveryRun)
            .where(
                DeliveryRun.tonie_id == tonie.tonie_id,
                DeliveryRun.run_type == due.run_type,
                DeliveryRun.evening == due.evening,
            )
            .order_by(DeliveryRun.started_at, DeliveryRun.id)
        ).scalars()
    )


def _decide_and_start(
    request: Request,
    db: Session,
    factory: TonieCloudFactory,
    storage: ObjectStorage,
    start: Callable[[Callable[[], None]], None],
    tonie: CreativeTonie,
    due: Due,
    now: datetime,
) -> str:
    """U10 Schritte 1-4: Zustand aus Sperre und Verlauf dieses Tonie. Die
    Anfrage nimmt die Sperre nicht-blockierend; nur wenn sie einen Lauf
    startet, reicht sie die Sperre an den Hintergrundlauf weiter, der sie
    freigibt."""
    now_utc = now.astimezone(UTC).replace(tzinfo=None)
    lock = run_lock(tonie)
    if not lock.acquire(blocking=False):
        started = _open_entry_started_at(db, tonie)
        if started is not None and now_utc - started > HANG_LIMIT:
            return "Lauf hängt"
        return "läuft"

    handed_over = False
    try:
        # Die Sperre ist frei: ein offener Eintrag stammt aus einem Lauf,
        # den ein Neustart abgebrochen hat.
        _close_orphans(db, tonie)
        if due.run_type == "kontrolllauf":
            due = Due(due.run_type, due.evening, _control_target_day(db, tonie, due))

        attempts = _attempts(db, tonie, due)
        if any(run.outcome in _SUCCESS_OUTCOMES for run in attempts):
            return "erledigt"
        # Plan (U5 Schritt 13, Key Decision manueller Lauf): nach einem
        # gescheiterten Vorabend-Lauf kam ein erfolgreicher manueller Lauf oder
        # Anstoss -- keine Wiederholung ueberschreibt ihn, erst der naechste
        # Abend raeumt auf. Bewusst kein Eintrag: ein "uebersprungen"-Vorabend
        # waere juenger als der manuelle Lauf, und der Kontrolllauf
        # (`_manual_run_since_vorabend`) wuerde ihn dann doch ueberschreiben.
        if (
            due.run_type == "vorabend"
            and attempts
            and _manual_run_since_vorabend(db, tonie, due.target_day)
        ):
            return "erledigt"
        if len(attempts) >= MAX_ATTEMPTS or (
            attempts and now_utc - attempts[-1].started_at < RETRY_GAP
        ):
            return "fehlgeschlagen"

        client = client_for(factory, db, tonie)
        entry = open_run(
            db,
            tonie,
            due.run_type,
            due.target_day,
            evening=due.evening,
            started_at=now_utc,
        )
        # Ab hier gehoert die Sperre `dispatch_run` -- auch wenn der Start scheitert.
        handed_over = True
        if isinstance(client, UnavailableClient):
            # Ohne nutzbares Konto steht das Ergebnis ohne Netz fest: gleich
            # hier abschliessen, damit Zeitplan und Wache sofort 502 sehen.
            dispatch_run(request, db, _run_now, client, storage, tonie, entry, lock)
            return "fehlgeschlagen"
        dispatch_run(request, db, start, client, storage, tonie, entry, lock)
        return "gestartet"
    finally:
        if not handed_over:
            lock.release()


def _run_now(run: Callable[[], None]) -> None:
    run()


def dispatch_run(
    request: Request,
    db: Session,
    start: Callable[[Callable[[], None]], None],
    client: TonieCloudClient | UnavailableClient,
    storage: ObjectStorage,
    tonie: CreativeTonie,
    entry: DeliveryRun,
    lock: threading.Lock,
    *,
    manual_beitrag_ids: Sequence[int] | None = None,
) -> None:
    """U10 Schritt 3: startet den Lauf zu `entry` im Hintergrund. Der
    Aufrufer hat `lock` genommen und uebergibt sie hier in jedem Fall: erst
    wenn `start` ohne Ausnahme zurueckkehrt, gibt der Hintergrundlauf sie
    frei. Scheitert schon der Start, gaebe sonst niemand die Sperre frei --
    dann schliesst diese Funktion `entry` als Fehlschlag mit Ursache, gibt
    die Sperre frei und reicht die Ausnahme weiter.
    KTD18: Admin-Laeufe folgen derselben Melderegel wie automatische."""
    config: Config = request.app.state.config
    session_factory = request.app.state.session_factory
    tonie_pk, entry_id, evening = tonie.id, entry.id, entry.evening
    try:
        start(
            lambda: run_in_background(
                session_factory,
                client,
                storage,
                tonie_pk,
                entry_id,
                lock,
                manual_beitrag_ids=manual_beitrag_ids,
                after=lambda outcome: report_run(config, session_factory, outcome, evening),
            )
        )
    except Exception as exc:
        logger.exception("Hintergrundlauf nicht gestartet: eintrag=%s", entry_id)
        try:
            entry.outcome = "fehlschlag"
            entry.reason = f"Hintergrundlauf nicht gestartet ({_describe(exc)})."
            db.commit()
        finally:
            lock.release()
        raise


def report_run(
    config: Config,
    session_factory: sessionmaker[Session],
    outcome: DeliveryOutcome,
    evening: date | None,
) -> None:
    """Meldungsentscheidung nach einem Lauf (KTD11): die Einzelmeldung, dann
    -- nach einem Lauf der Vorabend-Phase -- die Abschlusspruefung der
    Sammelmeldung. Scheitert eins, laeuft das andere trotzdem; der erste
    Fehler geht an `_report`, der ihn am Verlaufseintrag vermerkt."""
    errors: list[Exception] = []
    try:
        send_report_mail(config, session_factory, outcome)
    except Exception as exc:
        errors.append(exc)
    if evening is not None and outcome.run_type in SUMMARY_RUN_TYPES:
        try:
            send_evening_summary(config, session_factory, evening)
        except Exception as exc:
            if errors:
                logger.exception("Sammel-Abendmeldung nicht verschickt: abend=%s", evening)
            errors.append(exc)
    if errors:
        raise errors[0]


def send_evening_summary(
    config: Config,
    session_factory: sessionmaker[Session],
    evening: date,
    *,
    now: datetime | None = None,
) -> bool:
    """R4/KTD11: verschickt die Sammelmeldung des Abends genau einmal, sobald
    alle faelligen Tonies einen Endzustand ihres Vorabend-Laufs haben oder --
    nur mit `now` (Trigger-Aufruf) -- die Vorabend-Phase vorbei ist. Abende
    ohne Vorabend-Phase oder ohne faellige Tonies melden nichts.

    Das Einfuegen der `abendmeldungen`-Zeile ist die Sperre gegen
    Doppelversand aus parallelen Threads (eindeutig je Abend); scheitert der
    Versand, wird sie wieder entfernt und der Fehler weitergereicht."""
    with session_factory() as db:
        tz = ZoneInfo(config.timezone)
        delivery_time = delivery_time_for(db, evening)
        delivery = datetime.combine(evening, delivery_time, tzinfo=tz)
        due = determine_due(delivery, delivery_time)
        if due is None or due.run_type not in SUMMARY_RUN_TYPES:
            return False
        if now is not None and now < delivery:
            return False
        if db.execute(select(Abendmeldung.id).where(Abendmeldung.evening == evening)).first():
            return False
        tonies = due_tonies(db, due)
        if not tonies:
            return False
        results = {tonie.tonie_id: _eve_result(db, tonie, due) for tonie in tonies}
        phase_over = now is not None and now >= delivery + CONTROL_RUN_OFFSET
        if not phase_over and any(final is None for final, _ in results.values()):
            return False

        marker = Abendmeldung(evening=evening, sent_at=datetime.now(UTC).replace(tzinfo=None))
        db.add(marker)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return False

        rows = [_summary_row(tonie, due, *results[tonie.tonie_id]) for tonie in tonies]
        message = build_summary_message(
            to_address=config.admin_email, evening=evening, run_type=due.run_type, rows=rows
        )
        try:
            send_message(config, db, message)
        except Exception:
            db.rollback()
            db.delete(marker)
            db.commit()
            raise
        return True


def _eve_result(
    db: Session, tonie: CreativeTonie, due: Due
) -> tuple[DeliveryRun | None, DeliveryRun | None]:
    """(Endzustand, letzter abgeschlossener Versuch) des Vorabend-Laufs
    dieses Tonie -- dieselben Regeln wie `_decide_and_start`: Erfolg, ein
    erfolgreicher manueller Lauf nach einem Versuch, oder alle Versuche
    verbraucht."""
    attempts = _attempts(db, tonie, due)
    finished = [run for run in attempts if run.outcome != RUN_STARTED]
    last = finished[-1] if finished else None
    for run in attempts:
        if run.outcome in _SUCCESS_OUTCOMES:
            return run, run
    if finished and len(finished) == len(attempts):
        if len(attempts) >= MAX_ATTEMPTS or (
            due.run_type == "vorabend" and _manual_run_since_vorabend(db, tonie, due.target_day)
        ):
            return last, last
    return None, last


def _summary_row(
    tonie: CreativeTonie, due: Due, final: DeliveryRun | None, last: DeliveryRun | None
) -> SummaryRow:
    run = final or last
    return SummaryRow(
        calendar=tonie.campaign.name if tonie.campaign is not None else "ohne Kalender",
        tonie_name=tonie.name,
        tonie_id=tonie.tonie_id,
        result=run.outcome if run is not None else None,
        target_day=run.target_day if run is not None else due.target_day,
        reason=run.reason if run is not None else None,
    )


def check_trigger_secret(config: Config, secret: str) -> None:
    """Approach-Punkt 15: Geheimnis statt Sitzung, auch fuer /backup/check."""
    if not hmac.compare_digest(secret, config.trigger_secret):
        raise HTTPException(status_code=401, detail="Ungueltiges oder fehlendes Geheimnis.")


def _open_entry_started_at(db: Session, tonie: CreativeTonie) -> datetime | None:
    return db.execute(
        select(DeliveryRun.started_at)
        .where(DeliveryRun.tonie_id == tonie.tonie_id, DeliveryRun.outcome == RUN_STARTED)
        .order_by(DeliveryRun.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _close_orphans(db: Session, tonie: CreativeTonie) -> None:
    orphans = (
        db.execute(
            select(DeliveryRun).where(
                DeliveryRun.tonie_id == tonie.tonie_id, DeliveryRun.outcome == RUN_STARTED
            )
        )
        .scalars()
        .all()
    )
    for orphan in orphans:
        orphan.outcome = RUN_ABORTED
        orphan.reason = "Lauf abgebrochen, ohne Abschluss beendet (z. B. Neustart des Dienstes)."
    if orphans:
        db.commit()


def _control_target_day(db: Session, tonie: CreativeTonie, due: Due) -> int | None:
    """U5 Schritt 6: der Kontrolllauf uebernimmt den Zieltag vom erfolgreichen
    oder zuletzt versuchten Vorabend-Lauf dieses Abends, nie aus der Uhr.
    Lief am Abend keiner, gilt der Tag, den der Vorabend gehabt haette."""
    eve_runs = list(
        db.execute(
            select(DeliveryRun)
            .where(
                DeliveryRun.tonie_id == tonie.tonie_id,
                DeliveryRun.run_type == "vorabend",
                DeliveryRun.evening == due.evening,
            )
            .order_by(DeliveryRun.started_at.desc(), DeliveryRun.id.desc())
        ).scalars()
    )
    for run in eve_runs:
        if run.outcome in _SUCCESS_OUTCOMES:
            return run.target_day
    return eve_runs[0].target_day if eve_runs else due.target_day


@router.post("/run-now")
def run_now(
    request: Request,
    db: Session = Depends(get_db),
    factory: TonieCloudFactory = Depends(get_toniecloud_factory),
    storage: ObjectStorage = Depends(get_storage),
    admin: Person = Depends(require_admin),
) -> dict:
    """Approach-Punkt 7: setzt den Verifiziert-Zustand ausdruecklich ausser
    Kraft, damit ein inzwischen freigegebener Tagesbeitrag einen zuvor
    eingesprungenen Ersatzbeitrag ersetzt. U11: ein Anstoss, kein
    Vorabend-Lauf -- zaehlt nie als automatischer Lauf."""
    config: Config = request.app.state.config
    tonie = _current_tonie_or_404(db)
    tonie.verified_beitrag_id = None
    tonie.verified_chapter_id = None
    tonie.verified_for_day = None
    db.commit()

    now = datetime.now(ZoneInfo(config.timezone))
    target_day = tomorrows_advent_day(now)
    outcome = run_delivery(
        db,
        client_for(factory, db, tonie),
        storage,
        tonie,
        run_type="anstoss",
        target_day=target_day,
        after=lambda o: send_report_mail(config, request.app.state.session_factory, o),
    )
    return {"outcome": _outcome_label(outcome), "reason": outcome.reason}


class ManualDeliveryRequest(BaseModel):
    beitrag_ids: list[int]
    # R6: `creative_tonies.id` des Ziel-Tonie; ohne Angabe der aktuelle Tonie.
    tonie: int | None = None


@router.post("/manual")
def manual(
    request: Request,
    payload: ManualDeliveryRequest,
    db: Session = Depends(get_db),
    factory: TonieCloudFactory = Depends(get_toniecloud_factory),
    storage: ObjectStorage = Depends(get_storage),
    admin: Person = Depends(require_admin),
) -> dict:
    config: Config = request.app.state.config
    tonie = (
        db.get(CreativeTonie, payload.tonie)
        if payload.tonie is not None
        else _current_tonie_or_404(db)
    )
    if tonie is None:
        raise HTTPException(status_code=404, detail="Unbekannter Creative Tonie.")
    try:
        outcome = run_delivery(
            db,
            client_for(factory, db, tonie),
            storage,
            tonie,
            run_type="manuell",
            manual_beitrag_ids=payload.beitrag_ids,
            after=lambda o: send_report_mail(config, request.app.state.session_factory, o),
        )
    except NoCalendarError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"outcome": _outcome_label(outcome), "reason": outcome.reason}


@router.post("/dry-run")
def dry_run(
    request: Request,
    db: Session = Depends(get_db),
    factory: TonieCloudFactory = Depends(get_toniecloud_factory),
    storage: ObjectStorage = Depends(get_storage),
    admin: Person = Depends(require_admin),
) -> dict:
    config: Config = request.app.state.config
    tonie = _current_tonie_or_404(db)
    now = datetime.now(ZoneInfo(config.timezone))
    target_day = tomorrows_advent_day(now)
    outcome = run_delivery(
        db,
        client_for(factory, db, tonie),
        storage,
        tonie,
        run_type="trockenlauf",
        target_day=target_day,
        after=lambda o: send_report_mail(config, request.app.state.session_factory, o),
    )
    return {"outcome": _outcome_label(outcome), "reason": outcome.reason}


def _outcome_label(outcome) -> str:
    if not outcome.success:
        return "fehlschlag"
    return "ersatzbeitrag" if outcome.used_replacement else "erfolg"
