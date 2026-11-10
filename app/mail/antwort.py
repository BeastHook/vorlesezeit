"""Mail an eine Person, fuer die der Admin eine Antwort vom Kind abgelegt hat.

Gleiches Muster wie app/mail/reminder.py. Der Link ist ein frischer
Magic-Link, der nach der Anmeldung ins Archiv fuehrt, wo die Antwort unter
"Antwort für dich" steht.
"""

from __future__ import annotations

from email.message import EmailMessage

from sqlalchemy.orm import Session

from app import settings
from app.config import Config
from app.mail.html import attach_html, render_text
from app.mail.smtp import gruss, send_message


def build_antwort_message(
    *,
    to_address: str,
    display_name: str,
    login_url: str,
    kind_name: str | None = None,
    sender_name: str | None = None,
    from_address: str | None = None,
) -> EmailMessage:
    context = {
        "greeting": f"Hallo {display_name}," if display_name else "Hallo,",
        "kind": kind_name,
        "login_url": login_url,
        "gruss": gruss(sender_name).rstrip("\n"),
    }

    message = EmailMessage()
    message["Subject"] = (
        f"{kind_name} hat dir geantwortet" if kind_name else "Eine Antwort für dich"
    )
    if from_address:
        message["From"] = from_address
    message["To"] = to_address
    message.set_content(render_text("mail/antwort.txt", context) + "\n")
    attach_html(
        message,
        "mail/antwort.html",
        {
            **context,
            "subject": message["Subject"],
            "heading": message["Subject"],
            "preheader": "Du findest sie im Archiv.",
        },
        ornament="stern",
    )
    return message


def send_antwort_mail(
    config: Config,
    *,
    session: Session,
    to_address: str,
    display_name: str,
    login_url: str,
    person_id: int,
) -> None:
    message = build_antwort_message(
        to_address=to_address,
        display_name=display_name,
        login_url=login_url,
        kind_name=settings.person_kind_name(session, person_id),
        sender_name=settings.admin_display_name(session),
    )
    send_message(config, session, message)
