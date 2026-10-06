"""U14: Nächtliche Sicherung (deploy/backup/backup.py).

Das Skript liest seine Konfiguration beim Import aus Umgebungsvariablen; jeder
Test lädt es deshalb frisch mit eigenem Ziel, eigener Datenbank und einem
eigenen Bucket. Läuft gegen echtes MinIO (kein Mock) -- vorher starten:

    docker compose up -d minio createbuckets
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import uuid
from pathlib import Path

import boto3
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "backup" / "backup.py"

ENDPOINT = os.environ.get("STORAGE_ENDPOINT_URL", "http://localhost:9000")
ACCESS_KEY = os.environ.get("STORAGE_ACCESS_KEY", "vorlesezeit")
SECRET_KEY = os.environ.get("STORAGE_SECRET_KEY", "vorlesezeit-dev-secret")
REGION = os.environ.get("STORAGE_REGION", "us-east-1")


@pytest.fixture
def s3():
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name=REGION,
    )


@pytest.fixture
def bucket(s3):
    # Eigener Bucket je Test: die Spiegelung liest immer den ganzen Bucket, ein
    # geteilter Test-Bucket wäre durch parallele Tests nicht vorhersagbar.
    name = f"backup-test-{uuid.uuid4().hex[:20]}"
    s3.create_bucket(Bucket=name)
    yield name
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=name):
        for obj in page.get("Contents", []):
            s3.delete_object(Bucket=name, Key=obj["Key"])
    s3.delete_bucket(Bucket=name)


@pytest.fixture
def backup(tmp_path, bucket, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "data" / "vorlesezeit.db"))
    monkeypatch.setenv("BACKUP_TARGET_DIR", str(tmp_path / "backup"))
    monkeypatch.setenv("BACKUP_MARKER_PATH", str(tmp_path / "marker" / "last-success"))
    monkeypatch.setenv("STORAGE_BUCKET", bucket)
    monkeypatch.setenv("STORAGE_ENDPOINT_URL", ENDPOINT)
    monkeypatch.setenv("STORAGE_ACCESS_KEY", ACCESS_KEY)
    monkeypatch.setenv("STORAGE_SECRET_KEY", SECRET_KEY)
    monkeypatch.setenv("STORAGE_REGION", REGION)
    spec = importlib.util.spec_from_file_location(f"backup_{uuid.uuid4().hex}", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_wal_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE beitrag (id INTEGER PRIMARY KEY, titel TEXT)")
    conn.execute("INSERT INTO beitrag (titel) VALUES ('Sterne zählen')")
    conn.commit()
    conn.close()


def _mirror_files(backup) -> dict[str, bytes]:
    root = backup.TARGET / "objects"
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


# --- Datenbank ---------------------------------------------------------------


def test_backup_database_writes_readable_single_file_copy(backup):
    _make_wal_database(backup.DATABASE_PATH)

    copy = backup.backup_database()

    assert copy.exists()
    assert not copy.with_name(copy.name + "-wal").exists()
    conn = sqlite3.connect(copy)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT titel FROM beitrag").fetchall() == [("Sterne zählen",)]
    finally:
        conn.close()


def test_backup_database_keeps_only_last_seven_states(backup):
    _make_wal_database(backup.DATABASE_PATH)
    db_dir = backup.TARGET / "db"
    db_dir.mkdir(parents=True)
    # Ältere Stände vorbelegen statt achtmal zu sichern: der Dateiname hat nur
    # Sekundenauflösung.
    older = [db_dir / f"vorlesezeit-2026010{day}T000000Z.db" for day in range(1, 9)]
    for path in older:
        path.write_bytes(b"alt")

    newest = backup.backup_database()

    remaining = sorted(db_dir.glob("vorlesezeit-*.db"))
    assert len(remaining) == backup.KEEP_DB_STATES == 7
    assert newest in remaining
    assert remaining == [*older[-6:], newest]


def test_backup_rotation_counts_states_from_before_the_rename(backup):
    """Stände vor der Umbenennung heißen toniapply-*.db; die Rotation zählt
    beide Präfixe und behält die jüngsten nach Zeitstempel, nicht nach Name."""
    _make_wal_database(backup.DATABASE_PATH)
    db_dir = backup.TARGET / "db"
    db_dir.mkdir(parents=True)
    oldest = db_dir / "vorlesezeit-20260101T000000Z.db"
    renamed = [db_dir / f"toniapply-2026010{day}T000000Z.db" for day in range(2, 10)]
    for path in [oldest, *renamed]:
        path.write_bytes(b"alt")

    newest = backup.backup_database()

    remaining = sorted(p for p in db_dir.iterdir() if p.suffix == ".db")
    assert len(remaining) == backup.KEEP_DB_STATES
    assert set(remaining) == {*renamed[-6:], newest}


# --- Bucket-Spiegelung -------------------------------------------------------


def test_mirror_downloads_new_objects(backup, s3, bucket):
    s3.put_object(Bucket=bucket, Key="beitraege/a.mp3", Body=b"aaa")
    s3.put_object(Bucket=bucket, Key="beitraege/b.mp3", Body=b"bbbb")

    backup.mirror_bucket()

    assert _mirror_files(backup) == {"beitraege/a.mp3": b"aaa", "beitraege/b.mp3": b"bbbb"}


def test_mirror_redownloads_changed_objects(backup, s3, bucket):
    s3.put_object(Bucket=bucket, Key="beitraege/a.mp3", Body=b"alt")
    backup.mirror_bucket()

    s3.put_object(Bucket=bucket, Key="beitraege/a.mp3", Body=b"neuer Inhalt")
    backup.mirror_bucket()

    assert _mirror_files(backup) == {"beitraege/a.mp3": b"neuer Inhalt"}


def test_mirror_removes_locally_what_was_deleted_remotely(backup, s3, bucket):
    s3.put_object(Bucket=bucket, Key="beitraege/bleibt.mp3", Body=b"x")
    s3.put_object(Bucket=bucket, Key="geloescht/weg.mp3", Body=b"y")
    backup.mirror_bucket()

    s3.delete_object(Bucket=bucket, Key="geloescht/weg.mp3")
    backup.mirror_bucket()

    assert _mirror_files(backup) == {"beitraege/bleibt.mp3": b"x"}
    # Leer gewordene Verzeichnisse verschwinden mit (R40).
    assert not (backup.TARGET / "objects" / "geloescht").exists()


def test_mirror_refuses_empty_listing_when_mirror_holds_files(backup, s3, bucket):
    s3.put_object(Bucket=bucket, Key="beitraege/a.mp3", Body=b"aaa")
    backup.mirror_bucket()
    s3.delete_object(Bucket=bucket, Key="beitraege/a.mp3")

    with pytest.raises(RuntimeError, match="leer"):
        backup.mirror_bucket()

    assert _mirror_files(backup) == {"beitraege/a.mp3": b"aaa"}


def test_mirror_accepts_empty_listing_when_mirror_is_empty(backup):
    backup.mirror_bucket()

    assert _mirror_files(backup) == {}


# --- Rücksicherung -----------------------------------------------------------


def test_restore_refuses_when_database_exists(backup):
    _make_wal_database(backup.DATABASE_PATH)
    name = backup.backup_database().name
    before = backup.DATABASE_PATH.read_bytes()

    assert backup.restore(name) == 1
    assert backup.DATABASE_PATH.read_bytes() == before


def test_restore_restores_database_and_reuploads_mirror(backup, s3, bucket):
    _make_wal_database(backup.DATABASE_PATH)
    s3.put_object(Bucket=bucket, Key="beitraege/a.mp3", Body=b"aaa")
    name = backup.backup_database().name
    backup.mirror_bucket()
    # Leere Instanz simulieren: Datenbank weg, Bucket leer.
    for suffix in ("", "-wal", "-shm"):
        Path(f"{backup.DATABASE_PATH}{suffix}").unlink(missing_ok=True)
    s3.delete_object(Bucket=bucket, Key="beitraege/a.mp3")

    assert backup.restore(name) == 0

    conn = sqlite3.connect(backup.DATABASE_PATH)
    try:
        assert conn.execute("SELECT titel FROM beitrag").fetchall() == [("Sterne zählen",)]
    finally:
        conn.close()
    body = s3.get_object(Bucket=bucket, Key="beitraege/a.mp3")["Body"].read()
    assert body == b"aaa"


# --- Gesamtlauf --------------------------------------------------------------


def test_run_once_writes_marker_after_both_steps(backup, s3, bucket):
    _make_wal_database(backup.DATABASE_PATH)
    s3.put_object(Bucket=bucket, Key="beitraege/a.mp3", Body=b"aaa")

    assert backup.run_once() is True

    assert backup.MARKER.exists()
    assert list((backup.TARGET / "db").glob("vorlesezeit-*.db"))
    assert _mirror_files(backup) == {"beitraege/a.mp3": b"aaa"}


def test_run_once_writes_no_marker_when_mirroring_fails(backup, s3, bucket):
    _make_wal_database(backup.DATABASE_PATH)
    s3.put_object(Bucket=bucket, Key="beitraege/a.mp3", Body=b"aaa")
    backup.mirror_bucket()
    s3.delete_object(Bucket=bucket, Key="beitraege/a.mp3")

    assert backup.run_once() is False

    assert not backup.MARKER.exists()
    assert _mirror_files(backup) == {"beitraege/a.mp3": b"aaa"}


# --- Zeitpunkt und Nachholen (Ruhezustand/Neustart) --------------------------

from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

BERLIN = ZoneInfo("Europe/Berlin")


def _berlin(day: int, hour: int, minute: int) -> datetime:
    return datetime(2026, 12, day, hour, minute, tzinfo=BERLIN)


def test_backup_runs_at_half_past_midnight(backup):
    assert (backup.RUN_AT.hour, backup.RUN_AT.minute) == (0, 30)


def test_due_without_any_previous_backup(backup):
    assert backup.is_due(_berlin(5, 9, 0), None)


def test_not_due_after_tonights_backup(backup):
    assert not backup.is_due(_berlin(5, 9, 0), _berlin(5, 0, 31))


def test_not_due_before_tonights_time_when_last_night_ran(backup):
    assert not backup.is_due(_berlin(5, 0, 20), _berlin(4, 0, 31))


def test_missed_backup_is_caught_up_after_waking(backup):
    """Mac schlief um 00:30: beim Aufwachen am Morgen ist die Sicherung
    faellig, statt bis zur naechsten Nacht zu warten."""
    assert backup.is_due(_berlin(5, 9, 0), _berlin(4, 0, 31))


def test_marker_round_trip(backup):
    assert backup.read_marker() is None
    backup.write_marker()
    assert backup.read_marker() is not None
    assert not backup.is_due(datetime.now(BERLIN), backup.read_marker())
