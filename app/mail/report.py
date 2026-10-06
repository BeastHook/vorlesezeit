"""Meldungen fuer den Auslieferungsvorgang (U5, R18/R26/R45; Mehrkalender U8, R4/KTD11).

Versand ueber app.mail.smtp.send_message (Zugang aus den Einstellungen, mit Zeitgrenze).

Einzelmeldung (`send_report_mail`): sofort bei jedem Lauf, der fehlschlaegt,
unabhaengig vom Auslöseweg (R18), und bei einem Kontrolllauf, der den Tonie
veraendert hat (Reparatur oder Ersatzbeitrag). Ein erfolgreicher Lauf der
Vorabend-Phase (vorabend/probelauf/aufraeumen) steht stattdessen in der
Sammelmeldung; ein Kontrolllauf ohne Eingriff und ein erfolgreicher Admin-Lauf
(Anstoss, manuell, Trockenlauf, Abraeumen) melden nichts.

Sammelmeldung (`build_summary_message`): eine Mail pro Abend mit einer Zeile je
faelligem Tonie; wann sie geht, entscheidet app/delivery/trigger.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage

from sqlalchemy.orm import Session, sessionmaker

from app.config import Config
from app.delivery.job import DeliveryOutcome
from app.mail.html import attach_html
from app.mail.magic_link import MONTHS_DE
from app.mail.smtp import send_message

RUN_TYPE_LABELS = {
    "vorabend": "Vorabend-Auslieferung",
    "kontrolllauf": "Kontrolllauf",
    "anstoss": "Manueller Anstoss",
    "manuell": "Manueller Lauf",
    "trockenlauf": "Trockenlauf",
    "probelauf": "Probelauf",
    "aufraeumen": "Aufräumen nach dem Advent",
    "abraeumen": "Abräumen nach dem Trennen",
}


def should_send_report(outcome: DeliveryOutcome) -> bool:
    """Einzelmeldung? Fehlschlag immer (R18); Erfolg nur beim Kontrolllauf mit
    Eingriff (KTD11) -- alle anderen Erfolge stehen in der Sammelmeldung oder
    im Verlauf."""
    if not outcome.success:
        return True
    return outcome.run_type == "kontrolllauf" and outcome.changed_tonie


def build_report_message(
    *, to_address: str, outcome: DeliveryOutcome, from_address: str | None = None
) -> EmailMessage:
    label = RUN_TYPE_LABELS.get(outcome.run_type, outcome.run_type)
    status = "Erfolg" if outcome.success else "Fehlschlag"
    subject = f"Vorlesezeit: {label} - {status}"
    if outcome.target_day is not None:
        subject += f" (Tuerchen {outcome.target_day})"

    lines = [f"{label}: {status}"]
    if outcome.tonie_id:
        lines.append(f"Tonie: {mask_tonie_id(outcome.tonie_id)}")
    if outcome.target_day is not None:
        lines.append(f"Zieltag: Tuerchen {outcome.target_day}")
    if outcome.used_replacement and outcome.success:
        lines.append("Es wurde der Ersatzbeitrag ausgeliefert.")
    if outcome.reason:
        lines.append(f"Ursache/Hinweis: {outcome.reason}")
    if outcome.title_mismatches:
        lines.append("Abweichende Kapiteltitel (Lauf ist trotzdem erfolgreich):")
        for expected, actual in outcome.title_mismatches:
            lines.append(f"  erwartet {mask(expected)!r}, erhalten {mask(actual)!r}")

    message = EmailMessage()
    message["Subject"] = subject
    if from_address:
        message["From"] = from_address
    message["To"] = to_address
    message.set_content("\n".join(lines) + "\n")
    attach_html(
        message,
        "mail/meldung.html",
        {
            "subject": subject,
            "heading": f"{label}: {status}",
            "preheader": subject,
            "ok": outcome.success,
            "lines": lines,
        },
        ornament=None,
    )
    return message


def mask(value: str) -> str:
    """R24: Kapiteltitel stammen aus freier Nutzereingabe und werden nur maskiert dargestellt."""
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


def mask_tonie_id(tonie_id: str) -> str:
    """Tonie-IDs erscheinen in Mails nur mit den letzten vier Zeichen."""
    return "••••" + tonie_id[-4:]


RESULT_LABELS = {
    "erfolg": "Erfolg",
    "ersatzbeitrag": "Erfolg mit Ersatzbeitrag",
    "uebersprungen": "übersprungen (manueller Lauf bleibt)",
    "fehlschlag": "Fehlschlag",
    "abgebrochen": "abgebrochen",
}


@dataclass(frozen=True)
class SummaryRow:
    """Eine Zeile der Sammelmeldung; `result` None heisst "kein Ergebnis"."""

    calendar: str
    tonie_name: str
    tonie_id: str
    result: str | None
    target_day: int | None
    reason: str | None = None

    @property
    def success(self) -> bool:
        return self.result in ("erfolg", "ersatzbeitrag", "uebersprungen")


def build_summary_message(
    *,
    to_address: str,
    evening: date,
    run_type: str,
    rows: list[SummaryRow],
    from_address: str | None = None,
) -> EmailMessage:
    """R4/KTD11: Sammelmeldung eines Abends, eine Zeile je Tonie (Kalender,
    Tonie, Ergebnis, Tag). Geht an den Admin: Kalender- und Tonie-Namen
    duerfen stehen, Tonie-IDs nur maskiert."""
    label = RUN_TYPE_LABELS.get(run_type, run_type)
    count = f"{len(rows)} Tonie" if len(rows) == 1 else f"{len(rows)} Tonies"
    status = "Erfolg" if all(row.success for row in rows) else "nicht alles erfolgreich"
    subject = f"Vorlesezeit: Abendmeldung {evening:%d.%m.%Y} - {label} - {count} - {status}"

    lines = [f"{label} am {evening:%d.%m.%Y}", ""]
    for row in rows:
        result = RESULT_LABELS.get(row.result, row.result) if row.result else "kein Ergebnis"
        parts = [row.calendar, f"{row.tonie_name or 'Tonie'} ({mask_tonie_id(row.tonie_id)})"]
        parts.append(result)
        if row.target_day is not None:
            parts.append(f"Türchen {row.target_day}")
        lines.append(" · ".join(parts))
        if row.reason:
            lines.append(f"  Ursache/Hinweis: {row.reason}")

    message = EmailMessage()
    message["Subject"] = subject
    if from_address:
        message["From"] = from_address
    message["To"] = to_address
    message.set_content("\n".join(lines) + "\n")
    ok = all(row.success for row in rows)
    attach_html(
        message,
        "mail/meldung.html",
        {
            "subject": subject,
            "heading": f"Abendmeldung {evening.day}. {MONTHS_DE[evening.month - 1]}",
            "preheader": subject,
            "ok": ok,
            "lines": lines,
        },
        ornament=None,
    )
    return message


def send_report_mail(
    config: Config, session_factory: sessionmaker[Session], outcome: DeliveryOutcome
) -> None:
    """Mehrkalender U5: eigene kurze Sitzung fuer Zugang und `mail_failed_at`
    -- der Rueckruf laeuft auch im Hintergrund-Thread, ohne Anfragesitzung.
    Geht an den Admin selbst und bleibt deshalb neutral (R26)."""
    if not should_send_report(outcome):
        return

    message = build_report_message(to_address=config.admin_email, outcome=outcome)
    with session_factory() as session:
        send_message(config, session, message)
