"""Morgen-Mails an die Vorlesenden (Advent-Anreicherung U7, R12-R14, R16, R20,
KTD6, KTD7, KTD9).

Am Morgen von Tag N bekommt jede Person, deren Geschichte der juengste
abschliessende Lauf je Tonie erfolgreich aufgespielt hat, eine Tages-Mail; am
Morgen danach, wenn N ihr frueheste Kalendertag ist, eine Archiv-Mail mit
Anmeldelink. Die Zeile in `morgenmails` ist die Sperre gegen Doppelversand.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from email.message import EmailMessage
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import settings
from app.auth.tokens import create_magic_link_token
from app.config import Config
from app.mail.html import attach_html, render_text
from app.mail.smtp import gruss, send_message
from app.models import Auftrag, Beitrag, Campaign, DeliveryRun, Morgenmail, Person, Slot

logger = logging.getLogger(__name__)

MORNING_START = time(7, 0)
MORNING_END = time(11, 0)
_RUN_TYPES = ("vorabend", "kontrolllauf", "anstoss")
_NOT_FINAL = ("gestartet", "uebersprungen")


def is_morning(now: datetime) -> bool:
    """Der Morgen-Zweig: 1. bis 25. Dezember, 07:00 bis 11:00 (Europe/Berlin)."""
    return now.month == 12 and 1 <= now.day <= 25 and MORNING_START <= now.time() < MORNING_END


def send_morning_mails(config: Config, db: Session, now: datetime) -> int:
    """Verschickt die faelligen Morgen-Mails und liefert ihre Anzahl."""
    now = now.astimezone(ZoneInfo(config.timezone))
    sent = 0
    if now.day <= 24:
        sent += _send_for_day(config, db, now, date(now.year, 12, now.day), "heute")
    if now.day >= 2:
        sent += _send_for_day(config, db, now, date(now.year, 12, now.day - 1), "archiv")
    return sent


def _send_for_day(config: Config, db: Session, now: datetime, day: date, mailart: str) -> int:
    address = settings.get_einstellungen(db).oeffentliche_adresse
    sent = 0
    for person, auftrag, kinds in _recipients(db, config, day, mailart):
        if mailart == "archiv" and not address:
            logger.warning(
                "Archiv-Mail entfällt, öffentliche Adresse unbekannt: person=%s", person.id
            )
            continue
        marker = Morgenmail(
            person_id=person.id,
            mailart=mailart,
            tag=day,
            sent_at=datetime.now(UTC).replace(tzinfo=None),
        )
        db.add(marker)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            continue
        try:
            message = _build_message(config, db, person, auftrag, kinds, mailart, address)
            send_message(config, db, message)
        except Exception as exc:
            db.rollback()
            db.delete(marker)
            db.commit()
            logger.warning(
                "Morgen-Mail fehlgeschlagen: person=%s mailart=%s fehler=%s",
                person.id,
                mailart,
                type(exc).__name__,
            )
            continue
        sent += 1
    return sent


def _recipients(
    db: Session, config: Config, day: date, mailart: str
) -> list[tuple[Person, Auftrag, list[str]]]:
    """KTD7: je Person die Geschichte mit der kleinsten Auftrag-ID und die
    Kindernamen der Kalender, deren Tonie sie erfolgreich ausgeliefert hat."""
    tz = ZoneInfo(config.timezone)
    since = (
        datetime.combine(day - timedelta(days=1), time(0, 0), tzinfo=tz)
        .astimezone(UTC)
        .replace(tzinfo=None)
    )
    runs = db.execute(
        select(DeliveryRun)
        .where(
            DeliveryRun.target_day == day.day,
            DeliveryRun.run_type.in_(_RUN_TYPES),
            DeliveryRun.outcome.not_in(_NOT_FINAL),
            DeliveryRun.started_at >= since,
        )
        .order_by(DeliveryRun.started_at, DeliveryRun.id)
    ).scalars()
    last_per_tonie = {run.tonie_id: run for run in runs}

    # Auftrag-ID -> Kalender-IDs der erfolgreichen Tonies
    stories: dict[int, set[int]] = {}
    for run in last_per_tonie.values():
        if run.outcome != "erfolg" or not run.beitrag_ids:
            continue
        ids = [int(part) for part in run.beitrag_ids.split(",") if part]
        for auftrag_id in db.execute(
            select(Beitrag.auftrag_id).where(Beitrag.id.in_(ids), Beitrag.auftrag_id.is_not(None))
        ).scalars():
            stories.setdefault(auftrag_id, set()).add(run.campaign_id)

    if mailart == "archiv" and stories:
        first_days = dict(
            db.execute(
                select(Slot.auftrag_id, func.min(Slot.day))
                .where(Slot.auftrag_id.in_(stories))
                .group_by(Slot.auftrag_id)
            ).all()
        )
        stories = {a: c for a, c in stories.items() if first_days.get(a) == day.day}

    chosen: dict[int, tuple[Person, Auftrag, list[str]]] = {}
    for auftrag in db.execute(
        select(Auftrag).where(Auftrag.id.in_(stories)).order_by(Auftrag.id)
    ).scalars():
        person = auftrag.person_id and db.get(Person, auftrag.person_id)
        if not person or person.revoked_at is not None or person.id in chosen:
            continue
        chosen[person.id] = (person, auftrag, _kind_names(db, stories[auftrag.id]))
    return list(chosen.values())


def _kind_names(db: Session, campaign_ids: set[int]) -> list[str]:
    """R16: ohne leere und doppelte Namen, nach aufsteigender Kalender-ID."""
    names: list[str] = []
    for campaign in db.execute(
        select(Campaign).where(Campaign.id.in_(campaign_ids)).order_by(Campaign.id)
    ).scalars():
        name = settings.calendar_kind_name(db, campaign)
        if name and name not in names:
            names.append(name)
    return names


def _build_message(
    config: Config,
    db: Session,
    person: Person,
    auftrag: Auftrag,
    kinds: list[str],
    mailart: str,
    address: str | None,
) -> EmailMessage:
    kind = settings.join_names(kinds)
    greeting = f"Hallo {person.display_name}," if person.display_name else "Hallo,"
    context = {
        "greeting": greeting,
        "kind": kind,
        "title": auftrag.title,
        "gruss": gruss(settings.admin_display_name(db)).rstrip("\n"),
    }
    message = EmailMessage()
    message["To"] = person.email
    if mailart == "heute":
        message["Subject"] = "Heute ist deine Geschichte dran"
        heading, preheader, ornament = (
            message["Subject"],
            "Danke, dass du vorgelesen hast.",
            "stern",
        )
    else:
        message["Subject"] = "Deine Geschichte ist jetzt im Archiv"
        heading, preheader, ornament = "Jetzt im Archiv", "Hör sie dir an.", "tannenzweig"
        params = urlencode({"token": create_magic_link_token(config, person), "next": "/archiv"})
        context["archiv_url"] = f"{address}/login/confirm?{params}"
    message.set_content(render_text(f"mail/{mailart}.txt", context) + "\n")
    attach_html(
        message,
        f"mail/{mailart}.html",
        {**context, "subject": message["Subject"], "heading": heading, "preheader": preheader},
        ornament=ornament,
    )
    return message
