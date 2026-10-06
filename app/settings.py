"""Mehrkalender U3: Einstellungsdienst (R17-R43, KTD4-KTD6, KTD16).

Betriebswerte liegen in der Tabelle `einstellungen` (genau eine Zeile, id=1),
tonies-Konten in `tonie_konten`. Gelesen wird je Anfrage und je Lauf direkt aus
der DB, ohne Cache (R24); ein Lauf bekommt mit `snapshot` einen
unveraenderlichen Stand seines Starts.

Passwoerter stehen als Fernet-Token in normalen String-Spalten (kein
TypeDecorator): ein TypeDecorator kann beim Lesen weder das "neu eingeben"-Flag
einer anderen Spalte setzen noch ohne Ausnahme mit einem fehlenden Schluessel
umgehen. Entschluesselt wird nur hier, nur fuer den internen Gebrauch; der
Klartext steht ausschliesslich in Feldern mit `repr=False` (R20).

`herkunft` (JSON) haelt je Feld: quelle ("umgebung" | "setup"), uebernommen_am,
ungeprueft (R41), fingerabdruck der Umgebung und abweichung (R42). Ein Feld mit
Eintrag wird nie wieder aus der Umgebung uebernommen (R28) -- auch nicht nach
Leeren oder Loeschen. Fuer Passwoerter ist der Fingerabdruck ein HMAC mit
CREDENTIALS_KEY, ohne Schluessel gibt es keinen (KTD5).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import (
    DEFAULT_DELIVERY_TIME,
    DEFAULT_MAGIC_LINK_VALID_UNTIL,
    DEFAULT_RECORDING_DEADLINE,
    Config,
    check_delivery_time,
)
from app.crypto import CredentialsUnavailable, decrypt, encrypt, fingerprint, key_usable
from app.models import CreativeTonie, Einstellungen, TonieKonto

logger = logging.getLogger(__name__)


class SettingsError(ValueError):
    """Ungueltiger Wert oder Schreiben ohne brauchbaren CREDENTIALS_KEY."""


@dataclass(frozen=True)
class _Feld:
    env: str  # Name der Umgebungsvariable (Startwert, R28)
    attr: str  # Attribut auf Config
    secret: bool = False


# Felder der Zeile `einstellungen`, die aus der Umgebung starten duerfen.
_EINSTELLUNGEN_FELDER = {
    "smtp_host": _Feld("SMTP_HOST", "smtp_host"),
    "smtp_port": _Feld("SMTP_PORT", "smtp_port"),
    "smtp_user": _Feld("SMTPUSER", "smtp_user"),
    "smtp_password": _Feld("SMTPPW", "smtp_password", secret=True),
    "smtp_from_address": _Feld("SMTP_FROM_ADDRESS", "smtp_from_address"),
    "delivery_time": _Feld("TONIE_DELIVERY_TIME", "tonie_delivery_time"),
    "recording_deadline": _Feld("RECORDING_DEADLINE", "recording_deadline"),
    "invitation_date": _Feld("INVITATION_DATE", "invitation_date"),
    "magic_link_valid_until": _Feld("MAGIC_LINK_VALID_UNTIL", "magic_link_valid_until"),
    "admin_display_name": _Feld("ADMIN_DISPLAY_NAME", "admin_display_name"),
}
# Das tonies-Konto startet nur als Paar (ein Konto braucht beides).
_KONTO_FELDER = {
    "tonie_username": _Feld("TONIE_USERNAME", "tonie_username"),
    "tonie_password": _Feld("TONIE_PASSWORD", "tonie_password", secret=True),
}
_SMTP_FELDER = ("smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_from_address")
# Per `set_value` frei setzbar; Zugangsdaten und Lieferzeit haben eigene Wege.
_SIMPLE_FELDER = (
    "recording_deadline",
    "invitation_date",
    "magic_link_valid_until",
    "admin_display_name",
    "kind_name",
)


@dataclass(frozen=True)
class SmtpZugang:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    from_address: str
    checked: bool  # R41: False = "ungeprueft"


@dataclass(frozen=True)
class KontoZugang:
    id: int
    label: str
    username: str
    password: str = field(repr=False)
    checked: bool


@dataclass(frozen=True)
class LaufSchnappschuss:
    """KTD6/R24: Stand beim Start eines Laufs; spaetere Aenderungen erreichen
    ihn nicht. `konten` enthaelt nur Konten mit lesbarem Passwort."""

    evening: date
    delivery_time: time
    smtp: SmtpZugang | None
    konten: tuple[KontoZugang, ...]


# --- Grundlagen -------------------------------------------------------------


def get_einstellungen(session: Session) -> Einstellungen:
    row = session.get(Einstellungen, 1)
    if row is None:
        row = Einstellungen(id=1)
        session.add(row)
        session.flush()
    return row


def herkunft(session: Session) -> dict[str, dict[str, Any]]:
    return json.loads(get_einstellungen(session).herkunft or "{}")


def _save_herkunft(row: Einstellungen, data: dict) -> None:
    row.herkunft = json.dumps(data, sort_keys=True)


def _text(value: Any) -> str:
    if isinstance(value, time):
        return value.strftime("%H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _fingerprint(feld: _Feld, key: str | None, value: Any) -> str | None:
    if feld.secret:
        return fingerprint(key, value)
    return hashlib.sha256(_text(value).encode()).hexdigest()


def _decryptable(key: str | None, token: str) -> bool:
    try:
        decrypt(key, token)
    except CredentialsUnavailable:
        return False
    return True


def _encrypt(key: str | None, plaintext: str) -> str:
    try:
        return encrypt(key, plaintext)
    except CredentialsUnavailable as exc:
        raise SettingsError(str(exc)) from None


def _mark_setup(data: dict, name: str, now: datetime) -> None:
    entry = data.setdefault(name, {})
    entry.update(quelle="setup", geaendert_am=now.isoformat(), ungeprueft=False)


# --- Startwerte (R28/R34/R41/R42) ------------------------------------------


def seed_from_env(session: Session, config: Config, *, now: datetime) -> list[str]:
    """Uebernimmt jedes gesetzte Startwert-Feld genau einmal. Passwoerter nur
    mit brauchbarem Schluessel (R42). Fuer schon uebernommene Felder nur den
    Abweichungshinweis aktualisieren. Liefert die Namen der neu uebernommenen
    Felder (nie Werte)."""
    row = get_einstellungen(session)
    data = json.loads(row.herkunft or "{}")
    key = config.credentials_key
    taken: list[str] = []

    def seed_entry(feld: _Feld, value: Any) -> dict:
        return {
            "quelle": "umgebung",
            "uebernommen_am": now.isoformat(),
            "ungeprueft": True,
            "fingerabdruck": _fingerprint(feld, key, value),
            "abweichung": False,
        }

    def update_hint(name: str, feld: _Feld) -> None:
        entry = data[name]
        if not entry.get("fingerabdruck"):
            return
        if feld.env not in config.startwert_vars:
            entry["abweichung"] = False
            return
        current = _fingerprint(feld, key, getattr(config, feld.attr))
        if current is not None:
            entry["abweichung"] = current != entry["fingerabdruck"]

    for name, feld in _EINSTELLUNGEN_FELDER.items():
        if name in data:
            update_hint(name, feld)
            continue
        if feld.env not in config.startwert_vars:
            continue
        if feld.secret and not key_usable(key):
            continue
        value = getattr(config, feld.attr)
        if feld.secret:
            stored: Any = encrypt(key, value)
        elif name == "delivery_time":
            stored = _text(value)
        else:
            stored = value
        setattr(row, name, stored)
        data[name] = seed_entry(feld, value)
        taken.append(name)

    if "tonie_username" in data:
        for name, feld in _KONTO_FELDER.items():
            if name in data:
                update_hint(name, feld)
    elif all(f.env in config.startwert_vars for f in _KONTO_FELDER.values()) and key_usable(key):
        konto = session.scalars(
            select(TonieKonto).where(TonieKonto.username == config.tonie_username)
        ).first()
        if konto is None:
            konto = TonieKonto(
                username=config.tonie_username, password=encrypt(key, config.tonie_password)
            )
            session.add(konto)
            session.flush()
            _link_single_tonie(session, konto)
        for name, feld in _KONTO_FELDER.items():
            data[name] = seed_entry(feld, getattr(config, feld.attr)) | {"konto_id": konto.id}
            taken.append(name)

    _save_herkunft(row, data)
    session.commit()
    if taken:
        logger.info("Startwerte aus der Umgebung uebernommen: %s", ", ".join(taken))
    return taken


def _link_single_tonie(session: Session, konto: TonieKonto) -> None:
    """R34: der in U2 migrierte Tonie gehoert zum Konto aus der Umgebung --
    aber nur, wenn genau ein Tonie ohne Konto existiert (sonst ist die
    Zuordnung geraten und bleibt dem Setup ueberlassen)."""
    orphans = session.scalars(select(CreativeTonie).where(CreativeTonie.konto_id.is_(None))).all()
    if len(orphans) == 1:
        orphans[0].konto_id = konto.id


# --- Schluessel fehlt oder passt nicht (R20/R21) ------------------------------


def check_stored_credentials(session: Session, key: str | None) -> list[str]:
    """Beim Start: jedes gespeicherte Token einmal pruefen und das Flag
    "neu eingeben" danach setzen (oder, wenn der Schluessel wieder passt,
    zuruecknehmen). Liefert `needs_reentry`."""
    row = get_einstellungen(session)
    if row.smtp_password is not None:
        row.smtp_needs_reentry = not _decryptable(key, row.smtp_password)
    for konto in session.scalars(select(TonieKonto)):
        if konto.password is not None:
            konto.needs_reentry = not _decryptable(key, konto.password)
    session.commit()
    names = needs_reentry(session)
    if names:
        logger.warning("Zugangsdaten neu eingeben: %s", ", ".join(names))
    return names


def needs_reentry(session: Session) -> list[str]:
    """Namen der Zugangsdaten, die neu einzugeben sind -- nie Werte (R21)."""
    names = ["smtp_password"] if get_einstellungen(session).smtp_needs_reentry else []
    konten = session.scalars(
        select(TonieKonto.id).where(TonieKonto.needs_reentry.is_(True)).order_by(TonieKonto.id)
    )
    return names + [f"tonie_konto:{konto_id}" for konto_id in konten]


# --- Lesen ------------------------------------------------------------------


def delivery_time_for(session: Session, evening: date) -> time:
    """KTD16: die fuer den Abend `evening` (Berliner Datum) gueltige Lieferzeit."""
    row = get_einstellungen(session)
    if (
        row.delivery_time_pending
        and row.delivery_time_pending_from
        and evening >= row.delivery_time_pending_from
    ):
        return time.fromisoformat(row.delivery_time_pending)
    return time.fromisoformat(row.delivery_time) if row.delivery_time else DEFAULT_DELIVERY_TIME


def recording_deadline(session: Session) -> date:
    return get_einstellungen(session).recording_deadline or DEFAULT_RECORDING_DEADLINE


def invitation_date(session: Session) -> date | None:
    return get_einstellungen(session).invitation_date


def magic_link_valid_until(session: Session) -> date:
    return get_einstellungen(session).magic_link_valid_until or DEFAULT_MAGIC_LINK_VALID_UNTIL


def admin_display_name(session: Session) -> str | None:
    return get_einstellungen(session).admin_display_name


def kind_name(session: Session) -> str | None:
    return get_einstellungen(session).kind_name


def smtp_zugang(session: Session, key: str | None) -> SmtpZugang | None:
    """Vollstaendiger SMTP-Zugang mit Klartext-Passwort, nur fuer den Versand.
    None, wenn unvollstaendig oder "neu eingeben" (dann ohne Anmeldeversuch, R21)."""
    row = get_einstellungen(session)
    parts = (row.smtp_host, row.smtp_port, row.smtp_user, row.smtp_password)
    if any(p is None for p in parts) or not row.smtp_from_address or row.smtp_needs_reentry:
        return None
    try:
        password = decrypt(key, row.smtp_password)
    except CredentialsUnavailable:
        row.smtp_needs_reentry = True
        session.commit()
        return None
    return SmtpZugang(
        host=row.smtp_host,
        port=row.smtp_port,
        user=row.smtp_user,
        password=password,
        from_address=row.smtp_from_address,
        checked=row.smtp_checked_at is not None,
    )


def _zugang(session: Session, key: str | None, konto: TonieKonto) -> KontoZugang | None:
    if konto.password is None or konto.needs_reentry:
        return None
    try:
        password = decrypt(key, konto.password)
    except CredentialsUnavailable:
        konto.needs_reentry = True
        session.commit()
        return None
    return KontoZugang(
        id=konto.id,
        label=konto.label,
        username=konto.username,
        password=password,
        checked=konto.checked_at is not None,
    )


def konto_zugang(session: Session, key: str | None, konto_id: int) -> KontoZugang | None:
    konto = session.get(TonieKonto, konto_id)
    return None if konto is None else _zugang(session, key, konto)


def snapshot(session: Session, key: str | None, *, evening: date) -> LaufSchnappschuss:
    konten = session.scalars(select(TonieKonto).order_by(TonieKonto.id)).all()
    zugaenge = (_zugang(session, key, konto) for konto in konten)
    return LaufSchnappschuss(
        evening=evening,
        delivery_time=delivery_time_for(session, evening),
        smtp=smtp_zugang(session, key),
        konten=tuple(z for z in zugaenge if z is not None),
    )


# --- Schreiben (nach bestandener Pruefung, U5/U6/U11) -------------------------


def set_value(session: Session, name: str, value: Any, *, now: datetime) -> None:
    """Aufnahmefrist, Einladungstermin, Linkgueltigkeit, Anzeigename, Kindername (R17/R22).
    `None` leert das Feld; die Umgebung holt es nicht zurueck (R28)."""
    if name not in _SIMPLE_FELDER:
        raise SettingsError(f"Feld {name!r} ist hier nicht setzbar")
    row = get_einstellungen(session)
    setattr(row, name, value)
    data = herkunft(session)
    _mark_setup(data, name, now)
    _save_herkunft(row, data)
    session.commit()


def _window_open(session: Session, now: datetime) -> date | None:
    """Liefert den Abend, dessen Lieferfenster `now` gerade offen ist."""
    # Spaeter Import: der Ausloeser wird selbst den Einstellungsdienst lesen.
    from app.delivery.trigger import WINDOW_AFTER_DELIVERY, WINDOW_START

    evening = now.date() if now.time() >= WINDOW_START else now.date() - timedelta(days=1)
    start = datetime.combine(evening, delivery_time_for(session, evening), tzinfo=now.tzinfo)
    return evening if start <= now < start + WINDOW_AFTER_DELIVERY else None


def set_delivery_time(session: Session, value: time, *, now: datetime) -> None:
    """R22/R43/KTD16: `now` in Europe/Berlin. Ist das Lieferfenster des
    laufenden Abends offen, gilt der neue Wert erst ab dem naechsten Abend."""
    try:
        check_delivery_time(value)
    except ValueError as exc:
        raise SettingsError(str(exc)) from None
    row = get_einstellungen(session)
    open_evening = _window_open(session, now)
    if open_evening is not None:
        row.delivery_time = _text(delivery_time_for(session, open_evening))
        row.delivery_time_pending = _text(value)
        row.delivery_time_pending_from = open_evening + timedelta(days=1)
    else:
        row.delivery_time = _text(value)
        row.delivery_time_pending = None
        row.delivery_time_pending_from = None
    data = herkunft(session)
    _mark_setup(data, "delivery_time", now)
    _save_herkunft(row, data)
    session.commit()


def set_smtp(
    session: Session,
    key: str | None,
    *,
    host: str,
    port: int,
    user: str,
    password: str | None,
    from_address: str,
    now: datetime,
) -> None:
    """Nach bestandener Pruefung (U5). `password=None` behaelt das
    gespeicherte, solange es lesbar ist."""
    row = get_einstellungen(session)
    if password is None and (row.smtp_password is None or row.smtp_needs_reentry):
        raise SettingsError("SMTP-Passwort muss neu eingegeben werden")
    if password is not None:
        row.smtp_password = _encrypt(key, password)
        row.smtp_needs_reentry = False
    row.smtp_host, row.smtp_port, row.smtp_user = host, port, user
    row.smtp_from_address = from_address
    row.smtp_checked_at = now
    data = herkunft(session)
    for name in _SMTP_FELDER:
        if name != "smtp_password" or password is not None:
            _mark_setup(data, name, now)
        elif name in data:
            data[name]["ungeprueft"] = False
    _save_herkunft(row, data)
    session.commit()


def mark_smtp_checked(session: Session, *, now: datetime) -> None:
    """R41: eine Pruefung des (z. B. uebernommenen) SMTP-Zugangs gelang."""
    row = get_einstellungen(session)
    row.smtp_checked_at = now
    data = herkunft(session)
    for name in _SMTP_FELDER:
        if name in data:
            data[name]["ungeprueft"] = False
    _save_herkunft(row, data)
    session.commit()


def create_konto(
    session: Session,
    key: str | None,
    *,
    username: str,
    password: str,
    label: str = "",
    now: datetime,
) -> TonieKonto:
    """Nach bestandener Pruefung (U6/U11)."""
    konto = TonieKonto(
        label=label, username=username, password=_encrypt(key, password), checked_at=now
    )
    session.add(konto)
    session.commit()
    return konto


def _konto_herkunft_checked(
    session: Session, konto_id: int, *, password_set_at: datetime | None
) -> None:
    """Herkunft der Startwert-Felder nachziehen, falls sie zu diesem Konto gehoeren."""
    row = get_einstellungen(session)
    data = herkunft(session)
    for name in _KONTO_FELDER:
        entry = data.get(name)
        if entry is not None and entry.get("konto_id") == konto_id:
            entry["ungeprueft"] = False
            if name == "tonie_password" and password_set_at is not None:
                _mark_setup(data, name, password_set_at)
    _save_herkunft(row, data)


def set_konto_password(
    session: Session,
    key: str | None,
    konto_id: int,
    password: str,
    *,
    now: datetime,
    username: str | None = None,
) -> None:
    """Nach bestandener Pruefung (U6/U11); hebt "neu eingeben" auf."""
    konto = session.get(TonieKonto, konto_id)
    if konto is None:
        raise SettingsError(f"Konto {konto_id} existiert nicht")
    konto.password = _encrypt(key, password)
    if username is not None:
        konto.username = username
    konto.needs_reentry = False
    konto.checked_at = now
    _konto_herkunft_checked(session, konto_id, password_set_at=now)
    session.commit()


def delete_konto(session: Session, konto: TonieKonto) -> None:
    """Konto samt seiner Tonies loeschen; ob das erlaubt ist, prueft das Setup.
    Die Herkunft der Startwerte bleibt (R28: kein Neu-Seed aus der Umgebung),
    verliert aber den Kontobezug -- eine wiederverwendete ID gehoert sonst
    einem fremden Konto."""
    row = get_einstellungen(session)
    data = herkunft(session)
    for name in _KONTO_FELDER:
        entry = data.get(name)
        if entry is not None and entry.get("konto_id") == konto.id:
            entry["konto_id"] = None
    _save_herkunft(row, data)
    for tonie in konto.tonies:
        session.delete(tonie)
    session.delete(konto)
    session.commit()


def mark_konto_checked(session: Session, konto_id: int, *, now: datetime) -> None:
    """R41: eine Anmeldung mit dem gespeicherten Konto gelang."""
    konto = session.get(TonieKonto, konto_id)
    if konto is None:
        raise SettingsError(f"Konto {konto_id} existiert nicht")
    konto.checked_at = now
    _konto_herkunft_checked(session, konto_id, password_set_at=None)
    session.commit()
