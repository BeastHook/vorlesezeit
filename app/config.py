"""Konfiguration aus der Umgebung (KTD11; Mehrkalender R27/R28).

Fehlt eine Pflichtvariable, bricht der Start mit einer klaren Meldung ab,
statt mit einem spaeteren, schwer zuzuordnenden Fehler zu scheitern. Was das
Setup pflegt (SMTP, tonies-Konto, Termine), ist hier nur noch optionaler
Startwert fuer `app/settings.py::seed_from_env` (R28/R29).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, time

# R27: nur diese Werte bleiben dauerhaft in der Umgebung (CREDENTIALS_KEY ist
# optional, R21).
REQUIRED_VARS = (
    "APP_TIMEZONE",
    "STORAGE_ENDPOINT_URL",
    "STORAGE_ACCESS_KEY",
    "STORAGE_SECRET_KEY",
    "STORAGE_BUCKET",
    "DATABASE_PATH",
    "SESSION_SECRET_KEY",
    "ADMIN_EMAIL",
    "TRIGGER_SECRET",
)

# R28: optionale Startwerte; nur gesetzte landen in `Config.startwert_vars`.
STARTWERT_VARS = (
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTPUSER",
    "SMTPPW",
    "SMTP_FROM_ADDRESS",
    "TONIE_USERNAME",
    "TONIE_PASSWORD",
    "TONIE_DELIVERY_TIME",
    "RECORDING_DEADLINE",
    "INVITATION_DATE",
    "MAGIC_LINK_VALID_UNTIL",
    "ADMIN_DISPLAY_NAME",
)

# R46: Bereich, in dem die Vorabend-Auslieferung liegen darf.
DELIVERY_TIME_MIN = time(17, 0)
DELIVERY_TIME_MAX = time(23, 0)
DEFAULT_DELIVERY_TIME = time(20, 0)

# R43: Aufnahme-Deadline, ab der offene Slots als ueberfaellig gelten (Plan,
# "Dependencies / Assumptions": 24.11.).
DEFAULT_RECORDING_DEADLINE = date(2026, 11, 24)
DEFAULT_MAGIC_LINK_VALID_UNTIL = date(2026, 12, 31)

# KTD19: Zeitmarke der naechtlichen Sicherung, auf einem Volume, das die
# Sicherung schreibt und die App liest (docker-compose.heimserver.yml).
DEFAULT_BACKUP_MARKER_PATH = "/backup-marker/last-success"


class ConfigError(RuntimeError):
    """Wird beim Start geworfen, wenn Pflichtkonfiguration fehlt."""


@dataclass(frozen=True)
class Config:
    timezone: str
    storage_endpoint_url: str
    storage_access_key: str
    storage_secret_key: str
    storage_bucket: str
    storage_region: str
    database_path: str
    session_secret_key: str
    admin_email: str
    # KTD3: langes Zufallsgeheimnis fuer den Auslöse-Endpunkt, konstantzeitig verglichen.
    trigger_secret: str
    # KTD4: Fernet-Schluessel fuer die Passwoerter in der DB. Optional (R21):
    # ohne ihn startet die App, betroffene Zugangsdaten sind "neu eingeben".
    credentials_key: str | None = field(default=None, repr=False)
    # --- Nur Startwert fuer settings.seed_from_env; Leser wechseln auf
    # app/settings.py (Mehrkalender U5/U6/U7/U9/U10/U12). Die Vorgaben der
    # Zeit-/Datumsfelder halten bis dahin die bestehenden Leser am Leben.
    magic_link_valid_until: date = DEFAULT_MAGIC_LINK_VALID_UNTIL
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = field(default=None, repr=False)
    smtp_from_address: str | None = None
    tonie_username: str | None = None
    tonie_password: str | None = field(default=None, repr=False)
    tonie_delivery_time: time = DEFAULT_DELIVERY_TIME
    recording_deadline: date = DEFAULT_RECORDING_DEADLINE
    invitation_date: date | None = None
    admin_display_name: str | None = None
    # Welche STARTWERT_VARS gesetzt waren -- eine Vorgabe ist kein Startwert.
    startwert_vars: frozenset[str] = frozenset()
    backup_marker_path: str = DEFAULT_BACKUP_MARKER_PATH


def check_delivery_time(value: time) -> time:
    """R46/R22: die Lieferzeit bleibt im Fenster 17:00-23:00."""
    if not (DELIVERY_TIME_MIN <= value <= DELIVERY_TIME_MAX):
        raise ValueError(
            f"Lieferzeit muss zwischen {DELIVERY_TIME_MIN:%H:%M} und "
            f"{DELIVERY_TIME_MAX:%H:%M} liegen (R46), war: {value:%H:%M}"
        )
    return value


def _optional(source, name: str, parse, description: str):
    if not source.get(name):
        return None
    try:
        return parse(source[name])
    except ValueError as exc:
        raise ConfigError(f"{name} muss {description} sein, war: {source[name]!r}") from exc


def load_config(env: dict[str, str] | None = None) -> Config:
    """Liest die Konfiguration aus der Umgebung (oder einem uebergebenen Mapping fuer Tests)."""
    source = env if env is not None else os.environ

    missing = [name for name in REQUIRED_VARS if not source.get(name)]
    if missing:
        raise ConfigError("Fehlende Pflicht-Umgebungsvariablen: " + ", ".join(missing))

    iso = "ein ISO-Datum (JJJJ-MM-TT)"
    magic_link_valid_until = _optional(source, "MAGIC_LINK_VALID_UNTIL", date.fromisoformat, iso)
    recording_deadline = _optional(source, "RECORDING_DEADLINE", date.fromisoformat, iso)
    invitation_date = _optional(source, "INVITATION_DATE", date.fromisoformat, iso)
    smtp_port = _optional(source, "SMTP_PORT", int, "eine Zahl")
    delivery_time = _optional(
        source,
        "TONIE_DELIVERY_TIME",
        lambda value: check_delivery_time(time.fromisoformat(value)),
        "eine Uhrzeit (SS:MM) zwischen 17:00 und 23:00",
    )

    return Config(
        timezone=source["APP_TIMEZONE"],
        storage_endpoint_url=source["STORAGE_ENDPOINT_URL"],
        storage_access_key=source["STORAGE_ACCESS_KEY"],
        storage_secret_key=source["STORAGE_SECRET_KEY"],
        storage_bucket=source["STORAGE_BUCKET"],
        storage_region=source.get("STORAGE_REGION", "us-east-1"),
        database_path=source["DATABASE_PATH"],
        session_secret_key=source["SESSION_SECRET_KEY"],
        admin_email=source["ADMIN_EMAIL"],
        trigger_secret=source["TRIGGER_SECRET"],
        credentials_key=source.get("CREDENTIALS_KEY") or None,
        magic_link_valid_until=magic_link_valid_until or DEFAULT_MAGIC_LINK_VALID_UNTIL,
        smtp_host=source.get("SMTP_HOST") or None,
        smtp_port=smtp_port,
        smtp_user=source.get("SMTPUSER") or None,
        smtp_password=source.get("SMTPPW") or None,
        smtp_from_address=source.get("SMTP_FROM_ADDRESS") or None,
        tonie_username=source.get("TONIE_USERNAME") or None,
        tonie_password=source.get("TONIE_PASSWORD") or None,
        tonie_delivery_time=delivery_time or DEFAULT_DELIVERY_TIME,
        recording_deadline=recording_deadline or DEFAULT_RECORDING_DEADLINE,
        invitation_date=invitation_date,
        admin_display_name=source.get("ADMIN_DISPLAY_NAME") or None,
        startwert_vars=frozenset(name for name in STARTWERT_VARS if source.get(name)),
        backup_marker_path=source.get("BACKUP_MARKER_PATH") or DEFAULT_BACKUP_MARKER_PATH,
    )
