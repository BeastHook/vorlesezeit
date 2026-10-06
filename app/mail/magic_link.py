"""Magic-Link-Mail (U3), seit Mehrkalender U5 ueber den gemeinsamen Helfer
app.mail.smtp.send_message (Zugang aus den Einstellungen, Zeitgrenze, TLS).

Die Einladung (F6) traegt denselben Link, aber einen eigenen, langen Text mit
der Projektbeschreibung; die Anmeldung ueber "Anmelden" bleibt kurz. Beide
nennen Linkgueltigkeit und Aufnahmefrist getrennt -- eigene Einstellungen.
"""

from __future__ import annotations

from datetime import date
from email.message import EmailMessage

from sqlalchemy.orm import Session

from app import settings
from app.config import Config
from app.mail.html import attach_html, render_text
from app.mail.smtp import gruss, send_message

MONTHS_DE = (
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
)


def german_date(value: date) -> str:
    return f"{value.day}. {MONTHS_DE[value.month - 1]}"


def build_magic_link_message(
    *,
    to_address: str,
    login_url: str,
    valid_until: date,
    recording_deadline: date,
    sender_name: str | None = None,
    from_address: str | None = None,
) -> EmailMessage:
    context = {
        "login_url": login_url,
        "valid_until": german_date(valid_until),
        "deadline": german_date(recording_deadline),
        "gruss": gruss(sender_name).rstrip("\n"),
    }
    message = EmailMessage()
    message["Subject"] = "Dein Schlüssel zum Adventskalender"
    if from_address:
        message["From"] = from_address
    message["To"] = to_address
    message.set_content(render_text("mail/zugang.txt", context) + "\n")
    attach_html(
        message,
        "mail/zugang.html",
        {
            **context,
            "subject": message["Subject"],
            "heading": "Schön, dass du dabei bist",
            "preheader": "Dein persönlicher Link zum Familien-Adventskalender.",
        },
        ornament="stern",
    )
    return message


def send_magic_link_mail(
    config: Config, *, session: Session, to_address: str, login_url: str
) -> None:
    message = build_magic_link_message(
        to_address=to_address,
        login_url=login_url,
        valid_until=settings.magic_link_valid_until(session),
        recording_deadline=settings.recording_deadline(session),
        sender_name=settings.admin_display_name(session),
    )
    send_message(config, session, message)


def build_invitation_message(
    *,
    to_address: str,
    login_url: str,
    valid_until: date,
    recording_deadline: date,
    kind_name: str | None = None,
    sender_name: str | None = None,
    from_address: str | None = None,
) -> EmailMessage:
    # Saetze ohne Verb im Numerus des Namens: "Emma & Lukas" passt ueberall.
    fuer = kind_name or "die Familie"
    context = {
        "login_url": login_url,
        "valid_until": german_date(valid_until),
        "deadline": german_date(recording_deadline),
        "kind": kind_name,
        "gruss": gruss(sender_name).rstrip("\n"),
    }
    message = EmailMessage()
    message["Subject"] = f"Ein Adventskalender für {fuer}, und du liest vor"
    if from_address:
        message["From"] = from_address
    message["To"] = to_address
    message.set_content(render_text("mail/einladung.txt", context) + "\n")
    attach_html(
        message,
        "mail/einladung.html",
        {
            **context,
            "subject": message["Subject"],
            "heading": f"Ein Adventskalender für {fuer}",
            "preheader": "Du liest vor: so funktioniert der Adventskalender.",
        },
        ornament="stern",
    )
    return message


def send_invitation_mail(
    config: Config, *, session: Session, to_address: str, login_url: str
) -> None:
    message = build_invitation_message(
        to_address=to_address,
        login_url=login_url,
        valid_until=settings.magic_link_valid_until(session),
        recording_deadline=settings.recording_deadline(session),
        kind_name=settings.kind_name(session),
        sender_name=settings.admin_display_name(session),
    )
    send_message(config, session, message)
