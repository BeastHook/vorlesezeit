"""Erinnerungsmail an eine Person mit offenen Tuerchen (U8, R43/AE20).

Gleiches Muster wie app/mail/magic_link.py: reiner Text (display_name ist
nutzergesteuert), SMTP ohne Provider-SDK. Der Link ist ein frischer
Magic-Link -- nach der Anmeldung fuehrt die Startseite direkt zum
fruehesten offenen Tuerchen (R3). Der Text endet mit dem Anzeigenamen des
Admins (Mehrkalender U5, R26).
"""

from __future__ import annotations

from email.message import EmailMessage

from sqlalchemy.orm import Session

from app import settings
from app.config import Config
from app.mail.html import attach_html, render_text
from app.mail.smtp import gruss, send_message


def build_reminder_message(
    *,
    to_address: str,
    display_name: str,
    open_days: list[int],
    login_url: str,
    kind_name: str | None = None,
    sender_name: str | None = None,
    from_address: str | None = None,
) -> EmailMessage:
    days = ", ".join(str(day) for day in open_days)
    greeting = f"Hallo {display_name},\n\n" if display_name else "Hallo,\n\n"
    context = {
        "greeting": greeting,
        "days": days,
        "login_url": login_url,
        "kind": kind_name,
        "gruss": gruss(sender_name).rstrip("\n"),
    }

    message = EmailMessage()
    message["Subject"] = "Ein Türchen wartet noch auf deine Stimme"
    if from_address:
        message["From"] = from_address
    message["To"] = to_address
    message.set_content(render_text("mail/erinnerung.txt", context) + "\n")
    attach_html(
        message,
        "mail/erinnerung.html",
        {
            **context,
            "greeting": greeting.rstrip("\n"),
            "subject": message["Subject"],
            "heading": f"Hinter Türchen {days} ist es noch still",
            "preheader": "Danke, dass du vorliest.",
        },
        ornament="kerze",
    )
    return message


def send_reminder_mail(
    config: Config,
    *,
    session: Session,
    to_address: str,
    display_name: str,
    open_days: list[int],
    login_url: str,
) -> None:
    message = build_reminder_message(
        to_address=to_address,
        display_name=display_name,
        open_days=open_days,
        login_url=login_url,
        kind_name=settings.kind_name(session),
        sender_name=settings.admin_display_name(session),
    )
    send_message(config, session, message)
