"""Mehrkalender U3: Einstellungsdienst und Startwert-Uebernahme (R17-R43, KTD4-6, KTD16).

Alle Zugangsdaten sind erfundene Platzhalter. Kein Test gibt einen Klartext
aus; geprueft wird immer nur, dass er irgendwo *nicht* steht.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app import create_app
from app import settings as st
from app.config import ConfigError, load_config
from app.db import init_db
from app.models import CreativeTonie, Einstellungen, TonieKonto
from tests.conftest import REQUIRED_ENV

BERLIN = ZoneInfo("Europe/Berlin")
KEY_A = base64.urlsafe_b64encode(b"0" * 32).decode()
KEY_B = base64.urlsafe_b64encode(b"1" * 32).decode()
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=BERLIN)

SMTP_PW = REQUIRED_ENV["SMTPPW"]
TONIE_PW = REQUIRED_ENV["TONIE_PASSWORD"]


def _env(tmp_path, **overrides) -> dict[str, str]:
    env = dict(REQUIRED_ENV)
    env["DATABASE_PATH"] = str(tmp_path / "s.db")
    env["CREDENTIALS_KEY"] = KEY_A
    for name, value in overrides.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'settings.db'}")
    init_db(engine)
    return engine


def _start(engine, env) -> None:
    """Ein "Start": Konfiguration laden, Startwerte uebernehmen, Tokens pruefen."""
    config = load_config(env)
    with Session(engine) as session:
        st.seed_from_env(session, config, now=NOW)
        st.check_stored_credentials(session, config.credentials_key)


# --- load_config (R27/R29) ----------------------------------------------------


def test_config_starts_without_smtp_and_tonie_vars(tmp_path):
    env = _env(
        tmp_path,
        SMTP_HOST=None,
        SMTP_PORT=None,
        SMTPUSER=None,
        SMTPPW=None,
        SMTP_FROM_ADDRESS=None,
        TONIE_USERNAME=None,
        TONIE_PASSWORD=None,
        TONIE_DELIVERY_TIME=None,
        MAGIC_LINK_VALID_UNTIL=None,
        CREDENTIALS_KEY=None,
    )
    config = load_config(env)
    assert config.smtp_user is None
    assert config.tonie_password is None
    assert config.credentials_key is None
    # Uebergang: bestehende Leser bekommen weiter die bisherigen Vorgaben.
    assert config.tonie_delivery_time == time(20, 0)
    assert config.magic_link_valid_until == date(2026, 12, 31)


def test_config_without_admin_email_still_aborts(tmp_path):
    with pytest.raises(ConfigError, match="ADMIN_EMAIL"):
        load_config(_env(tmp_path, ADMIN_EMAIL=None))


@pytest.mark.parametrize("value", ["16:59", "23:01", "keine-zeit"])
def test_config_rejects_delivery_time_outside_window(tmp_path, value):
    with pytest.raises(ConfigError, match="TONIE_DELIVERY_TIME"):
        load_config(_env(tmp_path, TONIE_DELIVERY_TIME=value))


def test_config_repr_hides_credentials_key(tmp_path):
    assert KEY_A not in repr(load_config(_env(tmp_path)))


# --- Startwert-Uebernahme (R28/R34/R41/R42) --------------------------------


def test_ae14_seeded_values_readable_immediately_and_unchecked(engine, tmp_path):
    _start(engine, _env(tmp_path, INVITATION_DATE="2026-11-01", ADMIN_DISPLAY_NAME="Luca"))
    with Session(engine) as s:
        smtp = st.smtp_zugang(s, KEY_A)
        assert smtp is not None
        assert (smtp.host, smtp.port, smtp.user) == ("smtp-relay.brevo.com", 587, "test-smtp-user")
        assert smtp.password == SMTP_PW
        assert smtp.checked is False
        assert st.delivery_time_for(s, date(2026, 12, 1)) == time(20, 0)
        assert st.invitation_date(s) == date(2026, 11, 1)
        assert st.admin_display_name(s) == "Luca"
        assert st.magic_link_valid_until(s) == date(2026, 12, 31)
        herkunft = st.herkunft(s)
        assert herkunft["smtp_password"]["quelle"] == "umgebung"
        assert herkunft["smtp_password"]["ungeprueft"] is True
        assert herkunft["delivery_time"]["ungeprueft"] is True
        konten = s.scalars(select(TonieKonto)).all()
        assert [k.username for k in konten] == ["test-tonie-user"]
        assert konten[0].checked_at is None
        assert st.konto_zugang(s, KEY_A, konten[0].id).password == TONIE_PW


def test_ae4_seed_once_service_value_survives_restart_and_cleared_stays_empty(engine, tmp_path):
    env = _env(tmp_path)
    _start(engine, env)
    with Session(engine) as s:
        st.set_value(s, "admin_display_name", "Vom Setup", now=NOW)
        st.set_delivery_time(s, time(18, 30), now=NOW)
    env["ADMIN_DISPLAY_NAME"] = "Aus der Umgebung"
    _start(engine, env)
    with Session(engine) as s:
        assert st.admin_display_name(s) == "Vom Setup"
        assert st.delivery_time_for(s, date(2026, 12, 1)) == time(18, 30)
        st.set_value(s, "admin_display_name", None, now=NOW)
        st.set_value(s, "kind_name", "Emma", now=NOW)
        assert st.kind_name(s) == "Emma"
        st.set_value(s, "kind_name", None, now=NOW)
        assert st.kind_name(s) is None
        st.set_value(s, "invitation_date", None, now=NOW)
    env["INVITATION_DATE"] = "2026-11-01"
    _start(engine, env)
    with Session(engine) as s:
        assert st.admin_display_name(s) is None
        assert st.invitation_date(s) is None


def test_deleted_konto_is_not_recreated_on_restart(engine, tmp_path):
    env = _env(tmp_path)
    _start(engine, env)
    with Session(engine) as s:
        s.execute(text("DELETE FROM tonie_konten"))
        s.commit()
    _start(engine, env)
    with Session(engine) as s:
        assert s.scalars(select(TonieKonto)).all() == []


def test_without_key_passwords_not_seeded_other_values_are(engine, tmp_path):
    _start(engine, _env(tmp_path, CREDENTIALS_KEY=None))
    with Session(engine) as s:
        row = s.get(Einstellungen, 1)
        assert row.smtp_password is None
        assert row.smtp_user == "test-smtp-user"
        assert row.smtp_host == "smtp-relay.brevo.com"
        assert st.smtp_zugang(s, None) is None
        # Konto braucht das Passwort -- ohne Schluessel kein Konto, auch der
        # Benutzername wartet (beide werden spaeter gemeinsam uebernommen).
        assert s.scalars(select(TonieKonto)).all() == []
        herkunft = st.herkunft(s)
        assert "smtp_password" not in herkunft
        assert "tonie_username" not in herkunft
    # Spaeterer Start mit Schluessel holt die Passwoerter nach.
    _start(engine, _env(tmp_path))
    with Session(engine) as s:
        assert st.smtp_zugang(s, KEY_A).password == SMTP_PW
        assert len(s.scalars(select(TonieKonto)).all()) == 1


def test_env_differing_later_sets_hint_and_keeps_value(engine, tmp_path):
    env = _env(tmp_path)
    _start(engine, env)
    env["SMTP_HOST"] = "smtp.anders.example"
    env["SMTPPW"] = "anderes-platzhalter-passwort"
    _start(engine, env)
    with Session(engine) as s:
        smtp = st.smtp_zugang(s, KEY_A)
        assert smtp.host == "smtp-relay.brevo.com"
        assert smtp.password == SMTP_PW
        herkunft = st.herkunft(s)
        assert herkunft["smtp_host"]["abweichung"] is True
        assert herkunft["smtp_password"]["abweichung"] is True
        assert herkunft["smtp_user"]["abweichung"] is False
    # Zurueck auf den alten Wert: Hinweis verschwindet.
    env["SMTP_HOST"] = "smtp-relay.brevo.com"
    _start(engine, env)
    with Session(engine) as s:
        assert st.herkunft(s)["smtp_host"]["abweichung"] is False


def test_herkunft_holds_no_unkeyed_password_hash(engine, tmp_path):
    _start(engine, _env(tmp_path))
    with Session(engine) as s:
        raw = s.get(Einstellungen, 1).herkunft
    for plain in (SMTP_PW, TONIE_PW):
        assert plain not in raw
        assert hashlib.sha256(plain.encode()).hexdigest() not in raw
        assert hashlib.md5(plain.encode()).hexdigest() not in raw
    assert json.loads(raw)["smtp_password"]["fingerabdruck"]


# --- Schluessel fehlt oder passt nicht (R20/R21, AE5) -----------------------


def test_ae5_other_key_marks_reentry_without_error_and_never_shows_plaintext(
    engine, tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)
    _start(engine, _env(tmp_path))
    _start(engine, _env(tmp_path, CREDENTIALS_KEY=KEY_B))
    with Session(engine) as s:
        row = s.get(Einstellungen, 1)
        konto = s.scalars(select(TonieKonto)).one()
        assert row.smtp_needs_reentry is True
        assert konto.needs_reentry is True
        assert st.smtp_zugang(s, KEY_B) is None
        assert st.konto_zugang(s, KEY_B, konto.id) is None
        assert st.needs_reentry(s) == ["smtp_password", f"tonie_konto:{konto.id}"]
        snap = st.snapshot(s, KEY_B, evening=date(2026, 12, 1))
        assert snap.smtp is None and snap.konten == ()
        visible = [repr(row), repr(konto), row.smtp_password, konto.password, row.herkunft]
    visible.append(caplog.text)
    for plain in (SMTP_PW, TONIE_PW):
        assert all(plain not in v for v in visible)


def test_reentry_flag_clears_when_key_matches_again(engine, tmp_path):
    _start(engine, _env(tmp_path))
    _start(engine, _env(tmp_path, CREDENTIALS_KEY=None))
    with Session(engine) as s:
        assert st.needs_reentry(s) == ["smtp_password", "tonie_konto:1"]
    _start(engine, _env(tmp_path))
    with Session(engine) as s:
        assert st.needs_reentry(s) == []


def test_status_names_affected_fields_without_values(configured_env, monkeypatch):
    with TestClient(create_app()):
        pass
    monkeypatch.setenv("CREDENTIALS_KEY", KEY_B)
    with TestClient(create_app()) as client:
        response = client.get("/status")
    body = response.json()
    assert body["timezone"] == "Europe/Berlin"
    assert body["neu_eingeben"] == ["smtp_password", "tonie_konto:1"]
    for secret in (SMTP_PW, TONIE_PW, KEY_A, KEY_B):
        assert secret not in response.text


def test_app_starts_without_smtp_tonie_and_key(configured_env, monkeypatch):
    for name in ("SMTP_HOST", "SMTP_PORT", "SMTPUSER", "SMTPPW", "SMTP_FROM_ADDRESS"):
        monkeypatch.delenv(name)
    for name in ("TONIE_USERNAME", "TONIE_PASSWORD", "CREDENTIALS_KEY"):
        monkeypatch.delenv(name)
    with TestClient(create_app()) as client:
        body = client.get("/status").json()
    assert body["status"] == "ok"
    assert body["neu_eingeben"] == []


# --- repr ---------------------------------------------------------------------


def test_repr_of_konto_and_einstellungen_has_no_password(engine, tmp_path):
    _start(engine, _env(tmp_path))
    with Session(engine) as s:
        konto = s.scalars(select(TonieKonto)).one()
        row = s.get(Einstellungen, 1)
        smtp = st.smtp_zugang(s, KEY_A)
        zugang = st.konto_zugang(s, KEY_A, konto.id)
        snap = st.snapshot(s, KEY_A, evening=date(2026, 12, 1))
        for obj in (konto, row, smtp, zugang, snap):
            text_ = repr(obj)
            assert SMTP_PW not in text_ and TONIE_PW not in text_
            assert "gAAAA" not in text_  # kein Fernet-Token


# --- Schreibfunktionen ------------------------------------------------------


def test_set_smtp_after_check_stores_encrypted_and_checked(engine):
    with Session(engine) as s:
        st.set_smtp(
            s,
            KEY_A,
            host="smtp.example.test",
            port=465,
            user="neu@example.test",
            password="neues-platzhalter-pw",
            from_address="neu@example.test",
            now=NOW,
        )
        row = s.get(Einstellungen, 1)
        assert "neues-platzhalter-pw" not in row.smtp_password
        smtp = st.smtp_zugang(s, KEY_A)
        assert smtp.password == "neues-platzhalter-pw"
        assert smtp.checked is True
        assert st.herkunft(s)["smtp_password"]["quelle"] == "setup"


def test_set_smtp_without_password_keeps_stored_one(engine, tmp_path):
    _start(engine, _env(tmp_path))
    with Session(engine) as s:
        st.set_smtp(
            s,
            KEY_A,
            host="smtp.example.test",
            port=587,
            user="test-smtp-user",
            password=None,
            from_address="a@example.test",
            now=NOW,
        )
        assert st.smtp_zugang(s, KEY_A).password == SMTP_PW


def test_konto_create_and_password_reset(engine):
    with Session(engine) as s:
        konto = st.create_konto(
            s, KEY_A, username="k@example.test", password="pw-eins", label="Oma", now=NOW
        )
        konto.needs_reentry = True
        s.commit()
        st.set_konto_password(s, KEY_A, konto.id, "pw-zwei", now=NOW)
        zugang = st.konto_zugang(s, KEY_A, konto.id)
        assert zugang.password == "pw-zwei"
        assert s.get(TonieKonto, konto.id).needs_reentry is False
        assert s.get(TonieKonto, konto.id).checked_at is not None


def test_write_without_key_is_refused(engine):
    with Session(engine) as s, pytest.raises(st.SettingsError):
        st.create_konto(s, None, username="k@example.test", password="pw", now=NOW)


def test_mark_checked_clears_unchecked(engine, tmp_path):
    _start(engine, _env(tmp_path))
    with Session(engine) as s:
        konto = s.scalars(select(TonieKonto)).one()
        st.mark_smtp_checked(s, now=NOW)
        st.mark_konto_checked(s, konto.id, now=NOW)
        assert st.smtp_zugang(s, KEY_A).checked is True
        assert s.get(TonieKonto, konto.id).checked_at is not None
        herkunft = st.herkunft(s)
        assert herkunft["smtp_password"]["ungeprueft"] is False
        assert herkunft["tonie_password"]["ungeprueft"] is False


def test_set_value_rejects_unknown_field(engine):
    with Session(engine) as s, pytest.raises(st.SettingsError):
        st.set_value(s, "smtp_password", "x", now=NOW)


# --- Lieferzeit (R22/R43, KTD16) ------------------------------------------------


@pytest.mark.parametrize("value", [time(16, 59), time(23, 1)])
def test_service_rejects_delivery_time_outside_window(engine, value):
    with Session(engine) as s, pytest.raises(st.SettingsError):
        st.set_delivery_time(s, value, now=NOW)


def test_r43_change_during_open_window_applies_from_next_evening(engine):
    open_window = datetime(2026, 12, 3, 20, 30, tzinfo=BERLIN)  # 20:00 + 2:45 offen
    with Session(engine) as s:
        st.set_delivery_time(s, time(19, 0), now=open_window)
        assert st.delivery_time_for(s, date(2026, 12, 3)) == time(20, 0)
        assert st.delivery_time_for(s, date(2026, 12, 4)) == time(19, 0)
        assert st.delivery_time_for(s, date(2026, 12, 10)) == time(19, 0)


def test_r43_change_after_midnight_in_open_window_applies_from_tonight(engine):
    with Session(engine) as s:
        st.set_delivery_time(s, time(22, 0), now=NOW)  # Basis 22:00
        after_midnight = datetime(2026, 12, 4, 0, 30, tzinfo=BERLIN)  # Abend 3.12. offen
        st.set_delivery_time(s, time(18, 0), now=after_midnight)
        assert st.delivery_time_for(s, date(2026, 12, 3)) == time(22, 0)
        assert st.delivery_time_for(s, date(2026, 12, 4)) == time(18, 0)


def test_r43_change_outside_window_applies_immediately(engine):
    before_window = datetime(2026, 12, 3, 19, 59, tzinfo=BERLIN)
    with Session(engine) as s:
        st.set_delivery_time(s, time(21, 0), now=before_window)
        assert st.delivery_time_for(s, date(2026, 12, 3)) == time(21, 0)
        row = s.get(Einstellungen, 1)
        assert row.delivery_time_pending is None


def test_pending_change_is_folded_in_when_set_again(engine):
    with Session(engine) as s:
        st.set_delivery_time(s, time(19, 0), now=datetime(2026, 12, 3, 20, 30, tzinfo=BERLIN))
        st.set_delivery_time(s, time(21, 0), now=datetime(2026, 12, 5, 12, 0, tzinfo=BERLIN))
        assert st.delivery_time_for(s, date(2026, 12, 3)) == time(21, 0)
        assert st.delivery_time_for(s, date(2026, 12, 5)) == time(21, 0)


# --- Schnappschuss (R24, KTD6) --------------------------------------------------


def test_snapshot_is_unchanged_by_later_service_changes(engine, tmp_path):
    _start(engine, _env(tmp_path))
    with Session(engine) as s:
        snap = st.snapshot(s, KEY_A, evening=date(2026, 12, 1))
        st.set_delivery_time(s, time(18, 0), now=NOW)
        st.set_konto_password(s, KEY_A, snap.konten[0].id, "spaeteres-pw", now=NOW)
        st.set_smtp(
            s,
            KEY_A,
            host="smtp.example.test",
            port=465,
            user="x@example.test",
            password="spaeteres-smtp-pw",
            from_address="x@example.test",
            now=NOW,
        )
    assert snap.delivery_time == time(20, 0)
    assert snap.konten[0].password == TONIE_PW
    assert snap.smtp.password == SMTP_PW
    assert snap.smtp.host == "smtp-relay.brevo.com"
    with pytest.raises(AttributeError):
        snap.delivery_time = time(18, 0)


# --- Verbindung mit dem migrierten Tonie (R34) ---------------------------------


def test_seeded_konto_is_linked_to_migrated_tonie(configured_env, tmp_path, monkeypatch):
    from tests.test_migration import _old_db

    engine = _old_db(tmp_path)
    engine.dispose()
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "alt.db"))
    with TestClient(create_app()) as client:
        assert client.get("/status").json()["neu_eingeben"] == []
    with Session(create_engine(f"sqlite:///{tmp_path / 'alt.db'}")) as s:
        tonie = s.scalars(select(CreativeTonie)).one()
        konto = s.scalars(select(TonieKonto)).one()
        assert tonie.tonie_id == "TONIE-ALT"
        assert tonie.konto_id == konto.id
        assert konto.username == "test-tonie-user"


def test_seeded_konto_not_linked_when_several_tonies_without_konto(engine, tmp_path):
    with Session(engine) as s:
        s.add_all([CreativeTonie(tonie_id="T1"), CreativeTonie(tonie_id="T2")])
        s.commit()
    _start(engine, _env(tmp_path))
    with Session(engine) as s:
        assert [t.konto_id for t in s.scalars(select(CreativeTonie))] == [None, None]
        assert len(s.scalars(select(TonieKonto)).all()) == 1
