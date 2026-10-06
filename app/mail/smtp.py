"""SMTP-Versand ohne Provider-SDK: Port 465 implizites TLS, sonst STARTTLS.

Mehrkalender U5 (KTD7/KTD17): der Zugang kommt aus dem Einstellungsdienst, nicht
aus der Umgebung. Jeder Versand haelt fest, ob er scheiterte (`mail_failed_at`,
R30) -- daran haengt der Einrichtungslink beim naechsten Start. TLS immer mit
`ssl.create_default_context()` (Zertifikat und Hostname geprueft).
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app import settings
from app.config import Config
from app.mail.html import attach_html

# U11: ein haengender SMTP-Server haelt weder einen Lauf noch eine Anfrage fest.
SMTP_TIMEOUT_SECONDS = 30

ANMELDUNG_FEHLGESCHLAGEN = "Anmeldung fehlgeschlagen"
VERBINDUNG_FEHLGESCHLAGEN = "Verbindung fehlgeschlagen"
ZEITUEBERSCHREITUNG = "Zeitüberschreitung"
VERSAND_FEHLGESCHLAGEN = "Versand der Testmail fehlgeschlagen"


class SmtpNichtEingerichtet(Exception):
    """Kein nutzbarer SMTP-Zugang (nicht eingetragen oder "neu eingeben") --
    es gab keinen Verbindungsversuch."""


@dataclass(frozen=True)
class SmtpPruefung:
    ok: bool
    grund: str | None = None  # nie mit Passwort oder Serverantwort (R20)


def _deliver(host: str, port: int, user: str, password: str, message: EmailMessage) -> None:
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_SECONDS, context=context) as client:
            client.login(user, password)
            client.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SECONDS) as client:
            client.starttls(context=context)
            client.login(user, password)
            client.send_message(message)


def gruss(sender_name: str | None) -> str:
    """R26: Mailtexte an die Familie enden mit dem Anzeigenamen des Admins.
    Ohne Namen bewusst nur der Gruss -- jeder Ersatz ("Admin", "das Team")
    wuerde eine Person behaupten, die es so nicht gibt."""
    return f"Herzliche Grüße\n{sender_name}\n" if sender_name else "Herzliche Grüße\n"


def send_message(config: Config, session: Session, message: EmailMessage) -> None:
    """Versand ueber den gespeicherten Zugang. Setzt den Absender (mit dem
    Anzeigenamen des Admins, R26). Erfolg loescht `mail_failed_at`; Anmelde-
    oder Verbindungsfehler setzen es und werden weitergereicht. Ein vom
    Server abgelehnter Empfaenger betrifft nur diese Adresse, nicht den Zugang."""
    zugang = settings.smtp_zugang(session, config.credentials_key)
    if zugang is None:
        raise SmtpNichtEingerichtet("Kein nutzbarer SMTP-Zugang eingerichtet")
    name = settings.admin_display_name(session)
    del message["From"]
    message["From"] = formataddr((name, zugang.from_address)) if name else zugang.from_address

    row = settings.get_einstellungen(session)
    try:
        _deliver(zugang.host, zugang.port, zugang.user, zugang.password, message)
    except smtplib.SMTPRecipientsRefused:
        raise
    except (smtplib.SMTPException, OSError):
        row.mail_failed_at = datetime.now(ZoneInfo(config.timezone))
        session.commit()
        raise
    if row.mail_failed_at is not None:
        row.mail_failed_at = None
        session.commit()


def _is_timeout(exc: BaseException) -> bool:
    # smtplib verpackt eine Zeitueberschreitung beim Lesen in SMTPServerDisconnected.
    return isinstance(exc, TimeoutError) or isinstance(exc.__context__, TimeoutError)


def check_smtp(
    *, host: str, port: int, user: str, password: str, from_address: str, to_address: str
) -> SmtpPruefung:
    """R19: neue SMTP-Werte pruefen -- anmelden und eine Testmail an den Admin.
    Speichert nichts; das Uebernehmen nach Erfolg macht der Setup-Reiter (U11).
    Der Grund ist ein fester Text, nie die Ausnahme selbst (R20)."""
    message = EmailMessage()
    message["Subject"] = "Vorlesezeit: Testmail"
    message["From"] = from_address
    message["To"] = to_address
    message.set_content("Der Mailzugang funktioniert. Diese Mail kam aus der Einrichtung.\n")
    attach_html(
        message,
        "mail/testmail.html",
        {
            "subject": message["Subject"],
            "heading": "Der Mailzugang funktioniert",
            "preheader": "",
            "text": "Diese Mail kam aus der Einrichtung.",
        },
        ornament="stern",
    )
    try:
        _deliver(host, port, user, password, message)
    except smtplib.SMTPAuthenticationError:
        return SmtpPruefung(False, ANMELDUNG_FEHLGESCHLAGEN)
    except (smtplib.SMTPException, OSError) as exc:
        if _is_timeout(exc):
            return SmtpPruefung(False, ZEITUEBERSCHREITUNG)
        if isinstance(
            exc,
            smtplib.SMTPSenderRefused | smtplib.SMTPRecipientsRefused | smtplib.SMTPDataError,
        ):
            return SmtpPruefung(False, VERSAND_FEHLGESCHLAGEN)
        return SmtpPruefung(False, VERBINDUNG_FEHLGESCHLAGEN)
    return SmtpPruefung(True)
