"""Ablehnungs-Mail (U8, R13/R27, AE5).

Reiner Text, kein HTML: Kommentar und Name sind Nutzereingaben und landen
unverarbeitet im Textkoerper -- ohne HTML-Teil gibt es nichts zu escapen.
Versand wie app/mail/magic_link.py; der Text endet mit dem Anzeigenamen des
Admins (Mehrkalender U5, R26).
"""

from __future__ import annotations

from email.message import EmailMessage

from sqlalchemy.orm import Session

from app import settings
from app.config import Config
from app.mail.html import attach_html, render_text
from app.mail.smtp import gruss, send_message


def build_rejection_message(
    *,
    to_address: str,
    display_name: str,
    title: str | None,
    comment: str,
    login_url: str,
    sender_name: str | None = None,
    from_address: str | None = None,
) -> EmailMessage:
    # R10: die Familie sieht keine Tuerchennummer, sondern den Titel.
    what = f"deine Aufnahme „{title}“" if title else "deine Nachricht"
    greeting = f"Hallo {display_name}," if display_name else "Hallo,"
    context = {
        "greeting": greeting,
        "what": what,
        "comment": comment.strip(),
        "login_url": login_url,
        "gruss": gruss(sender_name).rstrip("\n"),
    }

    message = EmailMessage()
    message["Subject"] = "Magst du deine Geschichte noch einmal erzählen?"
    if from_address:
        message["From"] = from_address
    message["To"] = to_address
    message.set_content(render_text("mail/neu_aufnehmen.txt", context) + "\n")
    attach_html(
        message,
        "mail/neu_aufnehmen.html",
        {
            **context,
            "subject": message["Subject"],
            "heading": "Noch einmal, mit Gefühl",
            "preheader": "Lass dir ruhig Zeit.",
        },
        ornament="kerze",
    )
    return message


def send_rejection_mail(
    config: Config,
    *,
    session: Session,
    to_address: str,
    display_name: str,
    title: str | None,
    comment: str,
    login_url: str,
) -> None:
    message = build_rejection_message(
        to_address=to_address,
        display_name=display_name,
        title=title,
        comment=comment,
        login_url=login_url,
        sender_name=settings.admin_display_name(session),
    )
    send_message(config, session, message)
