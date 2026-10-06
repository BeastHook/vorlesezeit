"""Zustandsableitung fuer die Admin-Flaeche (U8, R12, R30, R42; Mehrkalender U9).

Es gibt bewusst keine gespeicherte Zustandsspalte: der Stand eines Slots
ergibt sich aus Zuweisung, Beitraegen und dem Auslieferungs-Verlauf
(DeliveryRun). So kann kein gespeicherter Zustand gegen die Daten laufen --
etwa wenn ein ausgelieferter Beitrag spaeter geloescht wird (R40: der Slot
bleibt "ausgeliefert", weil der Verlaufseintrag bleibt).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Auftrag, Beitrag, Campaign, CreativeTonie, DeliveryRun, Person, Slot

# Laeufe, deren Erfolg den Beitrag eines Kalendertags auf den Tonie bringt
# (R12 "ausgeliefert"). Der Anstoss macht den Tag nicht fest, liefert aber aus.
DAY_DELIVERY_RUN_TYPES = ("vorabend", "kontrolllauf", "anstoss")

# Nur diese Laeufe bringen den Tagesbeitrag auf den Tonie. Der Trockenlauf
# ersetzt nichts (R45), der manuelle Lauf ist nicht tagesbezogen (R20).
AUTOMATIC_RUN_TYPES = ("vorabend", "kontrolllauf")

# Dieselbe Jahresannahme wie app/recording/views.py::_advent_date_label und
# app/delivery/calendar.py: die App laeuft fuer genau einen Advent.
ADVENT_YEAR = 2026

SLOT_LABELS = {
    "frei": "nicht zugewiesen",
    "offen": "offen",
    "eingereicht": "eingereicht",
    "freigegeben": "freigegeben",
    "ausgeliefert": "ausgeliefert & verifiziert",
    "ersatz": "mit Ersatzbeitrag",
    "fehlschlag": "Auslieferung fehlgeschlagen",
}

BEITRAG_LABELS = {
    "eingereicht": "eingereicht",
    "freigegeben": "freigegeben",
    "abgelehnt": "abgelehnt",
}

_OUTCOME_TO_KEY = {"erfolg": "ausgeliefert", "ersatzbeitrag": "ersatz", "fehlschlag": "fehlschlag"}


@dataclass
class SlotState:
    key: str
    label: str
    # Benannte Ursache bei "fehlschlag" bzw. Grund bei "ersatz" (R12).
    detail: str | None
    # U8 Schritt 1: Tag ohne freigegebenen Beitrag, dessen Auslieferung
    # noch aussteht -- wird in der Uebersicht hervorgehoben.
    missing: bool
    beitrag: Beitrag | None


@dataclass
class BeitragState:
    key: str
    label: str


def auftrag_slots(auftrag: Auftrag) -> list[Slot]:
    """Die Kalendertage des Auftrags, fruehester zuerst."""
    return sorted(auftrag.slots, key=lambda s: (s.day, s.campaign_id, s.id))


def auftrag_active_beitrag(auftrag: Auftrag) -> Beitrag | None:
    """Der eingereichte, nicht abgelehnte Beitrag des Auftrags; ein
    freigegebener geht vor. Geloeste Beitraege haengen nicht mehr am Auftrag."""
    candidates = [
        b for b in auftrag.beitraege if b.audio_object_key is not None and b.rejected_at is None
    ]
    for b in candidates:
        if b.approved_at is not None:
            return b
    return candidates[0] if candidates else None


def active_beitrag(slot: Slot) -> Beitrag | None:
    """R8: der Beitrag gehoert dem Auftrag und gilt an jedem seiner Tage."""
    return auftrag_active_beitrag(slot.auftrag) if slot.auftrag is not None else None


def is_deliverable(beitrag: Beitrag | None) -> bool:
    """R11 (nur Freigegebenes), R35 (abgeloeste Beitraege nie)."""
    return (
        beitrag is not None
        and beitrag.approved_at is not None
        and beitrag.rejected_at is None
        and beitrag.detached_at is None
    )


def deliverable_beitrag(slot: Slot) -> Beitrag | None:
    """AE8/R8: der freigegebene Beitrag, den die Auslieferung fuer diesen
    Kalendertag aufspielt -- in jedem Kalender des Auftrags derselbe, ohne
    neue Aufnahme."""
    beitrag = active_beitrag(slot)
    return beitrag if is_deliverable(beitrag) else None


def auftrag_is_open(auftrag: Auftrag) -> bool:
    """R11/R36: vergeben, an mindestens einem Kalendertag, noch ohne
    eingereichte (nicht abgelehnte) Aufnahme. Ein Entwurf ist nie offen."""
    return (
        auftrag.person_id is not None
        and bool(auftrag.slots)
        and auftrag_active_beitrag(auftrag) is None
    )


def auftraege_for(db: Session, person: Person) -> list[Auftrag]:
    """Die Auftraege der Person mit mindestens einem Kalendertag (R36),
    nach ihrem fruehesten Tag."""
    auftraege = db.execute(select(Auftrag).where(Auftrag.person_id == person.id)).scalars()
    with_days = [a for a in auftraege if a.slots]
    return sorted(with_days, key=lambda a: (auftrag_slots(a)[0].day, a.id))


def open_auftraege_for(db: Session, person: Person) -> list[Auftrag]:
    """R11: jeder offene Auftrag einmal, egal in wie vielen Kalendern er liegt."""
    return [a for a in auftraege_for(db, person) if auftrag_is_open(a)]


def latest_automatic_run(
    db: Session, slot: Slot, tonie_id: str | None = None
) -> DeliveryRun | None:
    """Der letzte entscheidende Lauf fuer diesen Kalendertag. Mit `tonie_id`
    (R14/KTD10: der gewaehlte Tonie) nur dessen Laeufe -- gespiegelte Tonies
    laufen mit demselben Startzeitpunkt; ohne ueber den ganzen Kalender."""
    query = select(DeliveryRun).where(
        DeliveryRun.campaign_id == slot.campaign_id,
        DeliveryRun.target_day == slot.day,
        DeliveryRun.run_type.in_(AUTOMATIC_RUN_TYPES),
        # Ein uebersprungener Kontrolllauf entscheidet nichts ueber den Slot.
        DeliveryRun.outcome.in_(_OUTCOME_TO_KEY),
    )
    if tonie_id is not None:
        query = query.where(DeliveryRun.tonie_id == tonie_id)
    return db.execute(
        query.order_by(DeliveryRun.started_at.desc(), DeliveryRun.id.desc()).limit(1)
    ).scalar_one_or_none()


def delivery_moment(day: int, delivery_time: time) -> datetime:
    """Vorabend-Auslieferung fuer Tag `day` (naiv, Europe/Berlin)."""
    evening = date(ADVENT_YEAR, 12, 1) + timedelta(days=day - 2)
    return datetime.combine(evening, delivery_time)


def delivery_has_run(db: Session, slot: Slot, *, now: datetime, delivery_time: time) -> bool:
    """R30: ob die Vorabend-Auslieferung dieses Slots gelaufen ist -- per
    Verlaufseintrag oder, falls der Lauf ausblieb, per Uhrzeit. `now` ist
    zeitzonenbewusst in Europe/Berlin."""
    if latest_automatic_run(db, slot) is not None:
        return True
    return now.replace(tzinfo=None) >= delivery_moment(slot.day, delivery_time)


def linked_tonies(db: Session) -> list[CreativeTonie]:
    """R6: alle verknuepften Tonies der Instanz, mit und ohne Kalender --
    Kalender-Tonies zuerst, in Kalender-Reihenfolge."""
    return list(
        db.execute(
            select(CreativeTonie)
            .outerjoin(Campaign, CreativeTonie.campaign_id == Campaign.id)
            .order_by(Campaign.id.is_(None), Campaign.id, CreativeTonie.id)
        ).scalars()
    )


# KTD12: die Wahl des Tonie-Umschalters in der Sitzung. Werte:
# "t:<creative_tonies.id>:<campaign_id oder 0>" fuer einen Tonie -- der
# Kalender gehoert dazu, damit ein getrennter oder umgehaengter Tonie auf die
# Vorgabe zurueckfaellt -- und "k:<campaign_id>" fuer einen Kalender ohne Tonie.
SELECTION_KEY = "admin_tonie"


@dataclass
class Selection:
    value: str
    tonie: CreativeTonie | None
    # Der Kalender, den Kalender und Geschichten zeigen; None bei einem Tonie
    # ohne Kalender.
    campaign: Campaign | None


def tonie_value(tonie: CreativeTonie) -> str:
    return f"t:{tonie.id}:{tonie.campaign_id or 0}"


def calendars(db: Session) -> list[Campaign]:
    return list(db.execute(select(Campaign).order_by(Campaign.id)).scalars())


def selections(db: Session) -> list[Selection]:
    """Alle Zeilen des Umschalters in Anzeigereihenfolge: je Kalender seine
    Tonies, ein Kalender ohne Tonie als eigene Zeile, danach die Tonies ohne
    Kalender. Die erste Zeile mit Tonie ist die Vorgabe."""
    rows = []
    for campaign in calendars(db):
        tonies = sorted(campaign.tonies, key=lambda t: t.id)
        rows += [Selection(tonie_value(t), t, campaign) for t in tonies]
        if not tonies:
            rows.append(Selection(f"k:{campaign.id}", None, campaign))
    loose = db.execute(
        select(CreativeTonie).where(CreativeTonie.campaign_id.is_(None)).order_by(CreativeTonie.id)
    ).scalars()
    rows += [Selection(tonie_value(t), t, None) for t in loose]
    return rows


def current_selection(
    db: Session, chosen: str | None = None, *, rows: list[Selection] | None = None
) -> Selection | None:
    """KTD12: die gueltige Wahl `chosen` (aus der Sitzung), sonst die Vorgabe:
    der erste Tonie des ersten Kalenders mit Tonie, ohne verknuepften
    Kalender-Tonie der erste Kalender, sonst der erste Tonie ohne Kalender.
    `rows` sind bereits geladene `selections(db)`."""
    if rows is None:
        rows = selections(db)
    for row in rows:
        if row.value == chosen:
            return row
    with_calendar_tonie = [r for r in rows if r.tonie is not None and r.campaign is not None]
    if with_calendar_tonie:
        return with_calendar_tonie[0]
    return rows[0] if rows else None


def chosen_value(request) -> str | None:
    """Die in der Sitzung gespeicherte Wahl; ohne Anfrage keine."""
    return request.session.get(SELECTION_KEY) if request is not None else None


def current_tonie(db: Session, request=None) -> CreativeTonie | None:
    """Der Tonie, dem Auslieferung, Platz und Verlauf folgen (R14). Einzige
    Stelle fuer diese Wahl. Ohne `request` (Geheimnis-Endpunkte) die Vorgabe."""
    selection = current_selection(db, chosen_value(request))
    return selection.tonie if selection is not None else None


def _calendar_tonie_ids(slot: Slot) -> list[str]:
    return [t.tonie_id for t in slot.campaign.tonies]


def is_slot_fixed(db: Session, slot: Slot, *, now: datetime, delivery_time: time) -> bool:
    """R13/KTD10: ein Kalendertag ist fest, sobald fuer ihn ein Vorabend-Lauf
    bei irgendeinem Tonie seines Kalenders gelaufen ist (jeder Ausgang, auch
    ein noch offener). Ein Kalender ohne Tonie hat keine Laeufe; sein Tag wird
    mit Ablauf der Lieferzeit fest. `now` ist zeitzonenbewusst (Europe/Berlin)."""
    tonie_ids = _calendar_tonie_ids(slot)
    if not tonie_ids:
        return now.replace(tzinfo=None) >= delivery_moment(slot.day, delivery_time)
    # Auch nach dem Kalender filtern: ein umgehaengter Tonie bringt seine
    # Vorabend-Laeufe aus dem alten Kalender mit, die dort dieselben
    # Tagesnummern betreffen.
    run = db.execute(
        select(DeliveryRun.id)
        .where(
            DeliveryRun.campaign_id == slot.campaign_id,
            DeliveryRun.tonie_id.in_(tonie_ids),
            DeliveryRun.run_type == "vorabend",
            DeliveryRun.target_day == slot.day,
        )
        .limit(1)
    ).first()
    return run is not None


def is_slot_delivered(db: Session, slot: Slot, auftrag: Auftrag) -> bool:
    """R12: ein erfolgreicher Lauf eines Tonie des Kalenders hat fuer diesen
    Kalendertag einen Beitrag des Auftrags aufgespielt."""
    beitrag_ids = {str(b.id) for b in auftrag.beitraege}
    tonie_ids = _calendar_tonie_ids(slot)
    if not beitrag_ids or not tonie_ids:
        return False
    runs = db.execute(
        select(DeliveryRun.beitrag_ids).where(
            DeliveryRun.campaign_id == slot.campaign_id,
            DeliveryRun.tonie_id.in_(tonie_ids),
            DeliveryRun.run_type.in_(DAY_DELIVERY_RUN_TYPES),
            DeliveryRun.target_day == slot.day,
            DeliveryRun.outcome == "erfolg",
        )
    ).scalars()
    return any(beitrag_ids & set((ids or "").split(",")) for ids in runs)


def auftrag_lock_slot(
    db: Session, auftrag: Auftrag, *, now: datetime, delivery_time: time
) -> Slot | None:
    """R12: der erste Kalendertag des Auftrags, der fest oder ausgeliefert
    ist -- solange es ihn gibt, sind Ablehnung, Ruecknahme, Neuvergabe und
    Loeschen des Auftrags gesperrt. None = nichts sperrt."""
    for slot in auftrag_slots(auftrag):
        if is_slot_fixed(db, slot, now=now, delivery_time=delivery_time) or is_slot_delivered(
            db, slot, auftrag
        ):
            return slot
    return None


def slot_state(
    db: Session, slot: Slot, *, now: datetime, delivery_time: time, tonie_id: str | None = None
) -> SlotState:
    """`tonie_id`: der gewaehlte Tonie (R14), sonst gilt der ganze Kalender."""
    beitrag = active_beitrag(slot)
    run = latest_automatic_run(db, slot, tonie_id)
    if run is not None and run.outcome in _OUTCOME_TO_KEY:
        key = _OUTCOME_TO_KEY[run.outcome]
        detail = run.reason if key in ("ersatz", "fehlschlag") else None
        return SlotState(key, SLOT_LABELS[key], detail, missing=False, beitrag=beitrag)

    if beitrag is not None:
        key = "freigegeben" if beitrag.approved_at is not None else "eingereicht"
    elif slot.auftrag is None or slot.auftrag.person_id is None:
        key = "frei"
    else:
        key = "offen"
    # `run` ist bereits gelesen; ohne Lauf entscheidet wie in
    # delivery_has_run allein die Uhrzeit.
    missing = (
        key != "freigegeben"
        and run is None
        and now.replace(tzinfo=None) < delivery_moment(slot.day, delivery_time)
    )
    return SlotState(key, SLOT_LABELS[key], None, missing=missing, beitrag=beitrag)


def beitrag_state(beitrag: Beitrag) -> BeitragState:
    if beitrag.rejected_at is not None:
        key = "abgelehnt"
    elif beitrag.approved_at is not None:
        key = "freigegeben"
    else:
        key = "eingereicht"
    return BeitragState(key, BEITRAG_LABELS[key])


def chapter_title_open(beitrag: Beitrag) -> bool:
    """R42/AE25: kein Kapitelname, kein Auftragstitel, kein eigener Titel --
    es greift der Rueckfallwert "Tuerchen <Tag>"."""
    auftrag_title = beitrag.auftrag.title if beitrag.auftrag is not None else None
    return not (beitrag.chapter_title or auftrag_title or beitrag.title)
