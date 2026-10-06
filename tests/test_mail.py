"""Mehrkalender U5: Mail aus den Einstellungen (R19, R20, R26, R30; KTD7, KTD17).

Test scenarios aus dem Plan:
- Erfolgreicher Versand loescht "letzte Mail scheiterte", Anmeldefehler setzt ihn.
- Magic-Link-Mail laeuft ueber den gemeinsamen Helfer mit Zeitgrenze.
- AE3: Pruefung mit falschem Passwort -> "Anmeldung fehlgeschlagen",
  gespeicherte Werte unveraendert.
- Pruefung mit Zeitueberschreitung -> eigener Fehlergrund.
- Erinnerungs- und Ablehnungsmail nennen den Anzeigenamen, kein "Admin".
- Kein Mailtext und kein Log enthaelt ein Passwort.
- Linkgueltigkeit kommt aus den Einstellungen (ohne Neustart).
"""

from __future__ import annotations

import logging
import smtplib
import socket
import ssl
from datetime import date, datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from app import settings
from app.config import Config
from app.mail import smtp as smtp_module
from app.mail.magic_link import send_invitation_mail, send_magic_link_mail
from app.mail.rejection import send_rejection_mail
from app.mail.reminder import send_reminder_mail
from app.mail.smtp import SmtpNichtEingerichtet, check_smtp, send_message
from tests.conftest import REQUIRED_ENV
from tests.mailutil import html_text, plain_text

NOW = datetime(2026, 10, 2, 12, tzinfo=ZoneInfo("Europe/Berlin"))
SMTP_PW = REQUIRED_ENV["SMTPPW"]
WRONG_PW = "erfundenes-falsches-passwort"
REAL_SMTP = smtplib.SMTP


class FakeSMTP:
    """Zeichnet Verbindungen auf; `fail_login` simuliert einen Anmeldefehler."""

    instances: list[FakeSMTP] = []
    fail_login = False

    def __init__(self, host, port, timeout=None, **kwargs) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.starttls_context = None
        self.logins: list[tuple[str, str]] = []
        self.sent: list[EmailMessage] = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def starttls(self, context=None) -> None:
        self.starttls_context = context

    def login(self, user, password) -> None:
        if FakeSMTP.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 authentication failed")
        self.logins.append((user, password))

    def send_message(self, message) -> None:
        self.sent.append(message)


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail_login = False
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    return FakeSMTP


@pytest.fixture
def seeded(db_session: Session, config: Config) -> Session:
    """SMTP-Zugang wie nach dem Start: aus der Umgebung uebernommen."""
    settings.seed_from_env(db_session, config, now=NOW)
    return db_session


def _message() -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = "Test"
    message["To"] = "klaus@example.test"
    message.set_content("Hallo")
    return message


def _failed_at(db: Session):
    db.expire_all()
    return settings.get_einstellungen(db).mail_failed_at


# --- send_message ------------------------------------------------------------


def test_send_uses_settings_with_timeout_and_verified_tls(seeded, config, fake_smtp):
    send_message(config, seeded, _message())

    [conn] = fake_smtp.instances
    assert (conn.host, conn.port) == ("smtp-relay.brevo.com", 587)
    assert conn.timeout == smtp_module.SMTP_TIMEOUT_SECONDS
    assert isinstance(conn.starttls_context, ssl.SSLContext)
    assert conn.starttls_context.verify_mode == ssl.CERT_REQUIRED
    assert conn.logins == [("test-smtp-user", SMTP_PW)]
    assert conn.sent[0]["From"] == "vorlesezeit@example.test"


def test_send_reads_changed_settings_without_restart(seeded, config, fake_smtp):
    settings.set_smtp(
        seeded,
        config.credentials_key,
        host="smtp.anders.example",
        port=2525,
        user="neu",
        password="erfundenes-neues-passwort",
        from_address="neu@example.test",
        now=NOW,
    )

    send_message(config, seeded, _message())

    [conn] = fake_smtp.instances
    assert (conn.host, conn.port, conn.logins) == (
        "smtp.anders.example",
        2525,
        [("neu", "erfundenes-neues-passwort")],
    )


def test_success_clears_failed_marker(seeded, config, fake_smtp):
    settings.get_einstellungen(seeded).mail_failed_at = NOW
    seeded.commit()

    send_message(config, seeded, _message())

    assert _failed_at(seeded) is None


def test_auth_error_sets_failed_marker_and_reraises(seeded, config, fake_smtp):
    fake_smtp.fail_login = True

    with pytest.raises(smtplib.SMTPAuthenticationError):
        send_message(config, seeded, _message())

    assert _failed_at(seeded) is not None


def test_connection_error_sets_failed_marker(seeded, config, monkeypatch):
    def refuse(*args, **kwargs):
        raise ConnectionRefusedError("verbindung abgelehnt")

    monkeypatch.setattr("smtplib.SMTP", refuse)

    with pytest.raises(OSError):
        send_message(config, seeded, _message())

    assert _failed_at(seeded) is not None


def test_without_usable_smtp_named_error_and_no_connection(db_session, config, fake_smtp):
    # Keine Startwerte uebernommen: nichts eingetragen.
    with pytest.raises(SmtpNichtEingerichtet):
        send_message(config, db_session, _message())

    assert fake_smtp.instances == []


def test_from_carries_admin_display_name(seeded, config, fake_smtp):
    settings.set_value(seeded, "admin_display_name", "Oma Inge", now=NOW)

    send_message(config, seeded, _message())

    assert fake_smtp.instances[0].sent[0]["From"] == "Oma Inge <vorlesezeit@example.test>"


# --- Mailtexte (R26) ---------------------------------------------------------


def test_magic_link_mail_uses_shared_helper_and_separates_validity_from_deadline(
    seeded, config, fake_smtp
):
    settings.set_value(seeded, "magic_link_valid_until", date(2026, 12, 20), now=NOW)
    settings.set_value(seeded, "recording_deadline", date(2026, 11, 24), now=NOW)
    settings.set_value(seeded, "admin_display_name", "Oma Inge", now=NOW)

    send_magic_link_mail(
        config, session=seeded, to_address="klaus@example.test", login_url="https://x/l?token=t"
    )

    [conn] = fake_smtp.instances
    assert conn.timeout == smtp_module.SMTP_TIMEOUT_SECONDS
    message = conn.sent[0]
    body = plain_text(message)
    assert "https://x/l?token=t" in body
    assert "20. Dezember" in body  # Linkgueltigkeit
    assert "24. November" in body  # Aufnahmefrist, getrennt genannt
    assert "Oma Inge" in body
    assert message["From"] == "Oma Inge <vorlesezeit@example.test>"
    assert "Admin" not in body


def test_reminder_and_rejection_name_display_name_not_admin(seeded, config, fake_smtp):
    settings.set_value(seeded, "admin_display_name", "Oma Inge", now=NOW)

    send_reminder_mail(
        config,
        session=seeded,
        to_address="klaus@example.test",
        display_name="Klaus",
        open_days=[3],
        login_url="https://x/l",
    )
    send_rejection_mail(
        config,
        session=seeded,
        to_address="klaus@example.test",
        display_name="Klaus",
        title="Sterne zählen",
        comment="",
        login_url="https://x/l",
    )

    for conn in fake_smtp.instances:
        message = conn.sent[0]
        assert "Oma Inge" in plain_text(message)
        assert "Admin" not in plain_text(message)
        assert "Admin" not in message["From"]


def test_without_display_name_mails_stay_neutral(seeded, config, fake_smtp):
    send_reminder_mail(
        config,
        session=seeded,
        to_address="klaus@example.test",
        display_name="Klaus",
        open_days=[3],
        login_url="https://x/l",
    )

    message = fake_smtp.instances[0].sent[0]
    assert "Admin" not in plain_text(message)
    assert message["From"] == "vorlesezeit@example.test"


# --- Pruefung neuer SMTP-Werte (R19, AE3) -----------------------------------


def _check(**overrides):
    values = {
        "host": "smtp.anders.example",
        "port": 587,
        "user": "neu",
        "password": WRONG_PW,
        "from_address": "neu@example.test",
        "to_address": "admin@example.test",
    }
    return check_smtp(**(values | overrides))


def test_check_with_wrong_password_names_login_failure_and_stores_nothing_ae3(
    seeded, config, fake_smtp
):
    before = settings.smtp_zugang(seeded, config.credentials_key)
    fake_smtp.fail_login = True

    result = _check()

    assert result.ok is False
    assert result.grund == "Anmeldung fehlgeschlagen"
    assert WRONG_PW not in result.grund
    seeded.expire_all()
    assert settings.smtp_zugang(seeded, config.credentials_key) == before
    assert _failed_at(seeded) is None


def test_check_success_sends_test_mail_to_admin(fake_smtp):
    result = _check(password="erfundenes-richtiges-passwort")

    assert result.ok is True and result.grund is None
    [conn] = fake_smtp.instances
    assert conn.sent[0]["To"] == "admin@example.test"
    assert isinstance(conn.starttls_context, ssl.SSLContext)


def test_check_connection_refused_names_connection(monkeypatch):
    def refuse(*args, **kwargs):
        raise ConnectionRefusedError("verbindung abgelehnt")

    monkeypatch.setattr("smtplib.SMTP", refuse)

    assert _check().grund == "Verbindung fehlgeschlagen"


def test_check_timeout_has_own_reason(monkeypatch):
    """Echtes smtplib gegen einen stummen lokalen Socket."""
    monkeypatch.setattr("smtplib.SMTP", REAL_SMTP)
    monkeypatch.setattr("app.mail.smtp.SMTP_TIMEOUT_SECONDS", 0.3)
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(1)
    try:
        result = _check(host="127.0.0.1", port=silent.getsockname()[1])
    finally:
        silent.close()

    assert result.ok is False
    assert result.grund == "Zeitüberschreitung"


def test_no_password_in_mail_texts_or_logs(seeded, config, fake_smtp, caplog):
    settings.set_value(seeded, "admin_display_name", "Oma Inge", now=NOW)
    with caplog.at_level(logging.DEBUG):
        send_magic_link_mail(
            config, session=seeded, to_address="k@example.test", login_url="https://x/l"
        )
        fake_smtp.fail_login = True
        with pytest.raises(smtplib.SMTPAuthenticationError) as excinfo:
            send_message(config, seeded, _message())
        result = _check()

    sent = fake_smtp.instances[0].sent[0]
    for text in (sent.as_string(), str(excinfo.value), result.grund, caplog.text):
        assert SMTP_PW not in text
        assert WRONG_PW not in text


# --- Advent-Design: neuer Wortlaut + HTML-Fassung (Task 8) ------------------


def test_magic_link_mail_new_wording_and_html(seeded, config, fake_smtp):
    settings.set_value(seeded, "admin_display_name", "Oma Inge", now=NOW)
    send_magic_link_mail(
        config, session=seeded, to_address="k@example.test", login_url="https://x/l?token=t"
    )
    message = fake_smtp.instances[0].sent[0]
    assert message["Subject"] == "Dein Schlüssel zum Adventskalender"
    text = plain_text(message)
    assert "hier ist dein persönlicher Schlüssel zum Familien-Adventskalender" in text
    assert text.rstrip().endswith("Herzliche Grüße\nOma Inge")
    html = html_text(message)
    assert "Schön, dass du dabei bist" in html
    assert 'href="https://x/l?token=t"' in html
    assert "Zum Adventskalender" in html


def test_reminder_and_rejection_html_escape_user_input():
    from app.mail.rejection import build_rejection_message
    from app.mail.reminder import build_reminder_message

    rej = build_rejection_message(
        to_address="t@example.test",
        display_name="<b>Ruth</b>",
        title="<i>Stern</i>",
        comment="<script>x</script>",
        login_url="https://e/l",
    )
    assert rej["Subject"] == "Magst du deine Geschichte noch einmal erzählen?"
    assert "<script>x</script>" in plain_text(rej)
    assert "<script>" not in html_text(rej) and "&lt;script&gt;" in html_text(rej)
    assert "Türchen" not in plain_text(rej) and "Türchen" not in html_text(rej)

    rem = build_reminder_message(
        to_address="k@example.test",
        display_name="<b>Klaus</b>",
        open_days=[14, 15],
        login_url="https://e/l",
    )
    assert rem["Subject"] == "Ein Türchen wartet noch auf deine Stimme"
    assert "hinter Türchen 14, 15 fehlt noch deine Geschichte" in plain_text(rem)
    assert "Hinter Türchen 14, 15 ist es noch still" in html_text(rem)
    assert "&lt;b&gt;Klaus&lt;/b&gt;" in html_text(rem)


def test_family_mails_text_part_stands_alone_without_images():
    from app.mail.reminder import build_reminder_message

    rem = build_reminder_message(
        to_address="k@e.t", display_name="Klaus", open_days=[3], login_url="https://e/l"
    )
    text = plain_text(rem)
    assert "https://e/l" in text and "Herzliche Grüße" in text
    assert 'alt=""' in html_text(rem)


def test_reminder_html_greeting_has_no_stray_blank_line():
    """Fix round 1: greeting.rstrip in der HTML-Fassung, Textteil unberuehrt."""
    from app.mail.reminder import build_reminder_message

    rem = build_reminder_message(
        to_address="k@example.test", display_name="Klaus", open_days=[3], login_url="https://e/l"
    )
    html = html_text(rem)
    assert "Hallo Klaus,\n" not in html

    anon = build_reminder_message(
        to_address="k@example.test", display_name="", open_days=[3], login_url="https://e/l"
    )
    assert "Hallo,\n" not in html_text(anon)


def test_reminder_html_continues_greeting_in_lowercase():
    from app.mail.reminder import build_reminder_message

    rem = build_reminder_message(
        to_address="k@e.t", display_name="Klaus", open_days=[6], login_url="https://e/l"
    )
    assert "hinter Türchen 6 fehlt noch deine Geschichte" in html_text(rem)


# --- Einladung mit Kindernamen ---------------------------------------------


def test_invitation_mail_names_children_in_number_neutral_sentences(seeded, config, fake_smtp):
    settings.set_value(seeded, "kind_name", "Emma & Lukas", now=NOW)
    settings.set_value(seeded, "admin_display_name", "Luca", now=NOW)
    settings.set_value(seeded, "magic_link_valid_until", date(2026, 12, 31), now=NOW)
    settings.set_value(seeded, "recording_deadline", date(2026, 11, 24), now=NOW)

    send_invitation_mail(
        config, session=seeded, to_address="k@example.test", login_url="https://x/l?token=t"
    )

    message = fake_smtp.instances[0].sent[0]
    assert message["Subject"] == "Ein Adventskalender für Emma & Lukas, und du liest vor"
    text = plain_text(message)
    assert "dieses Jahr gibt es für Emma & Lukas einen besonderen Adventskalender" in text
    assert "landet sie auf dem Tonie für Emma & Lukas." in text
    assert "eine kleine Nachricht für Emma & Lukas einsprechen" in text
    assert "Das wird Emma & Lukas riesig freuen!" in text
    assert "Aufnehmen kannst du bis zum 24. November." in text
    assert "Dein Link gilt bis zum 31. Dezember." in text
    assert "https://x/l?token=t" in text
    assert text.rstrip().endswith("Luca")
    html = html_text(message)
    assert "Ein Adventskalender für Emma &amp; Lukas" in html
    assert 'href="https://x/l?token=t"' in html
    assert "Zum Adventskalender" in html


def test_invitation_mail_without_child_name_falls_back_to_family(seeded, config, fake_smtp):
    send_invitation_mail(
        config, session=seeded, to_address="k@example.test", login_url="https://x/l"
    )

    message = fake_smtp.instances[0].sent[0]
    assert message["Subject"] == "Ein Adventskalender für die Familie, und du liest vor"
    text = plain_text(message)
    assert "dieses Jahr gibt es einen besonderen Familien-Adventskalender" in text
    assert "landet sie auf dem Tonie." in text
    assert "eine kleine Nachricht einsprechen" in text
    assert "Das wird eine Freude!" in text
    assert "None" not in text and "für ," not in text


def test_login_mail_stays_short_with_child_name(seeded, config, fake_smtp):
    settings.set_value(seeded, "kind_name", "Emma", now=NOW)
    send_magic_link_mail(
        config, session=seeded, to_address="k@example.test", login_url="https://x/l"
    )

    message = fake_smtp.instances[0].sent[0]
    assert message["Subject"] == "Dein Schlüssel zum Adventskalender"
    assert "So geht" not in plain_text(message)


def test_reminder_thanks_for_reading_to_child(seeded, config, fake_smtp):
    settings.set_value(seeded, "kind_name", "Emma", now=NOW)
    send_reminder_mail(
        config,
        session=seeded,
        to_address="k@example.test",
        display_name="Klaus",
        open_days=[3],
        login_url="https://x/l",
    )

    message = fake_smtp.instances[0].sent[0]
    assert "Danke, dass du für Emma vorliest!" in plain_text(message)
    assert "Danke, dass du für Emma vorliest!" in html_text(message)
