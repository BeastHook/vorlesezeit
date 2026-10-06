"""Nächtliche Sicherung (U14, KTD19): Datenbank und Aufnahmen aufs zweite Medium.

Jede Nacht um 00:30 Europe/Berlin (verpasste Läufe, etwa im Ruhezustand des
Geräts, werden nach dem Aufwachen nachgeholt):
1. SQLite über die Online-Sicherung (sqlite3-Backup-API) nach
   <Ziel>/db/vorlesezeit-<UTC-Zeit>.db -- nie als rohe Dateikopie, solange WAL
   aktiv ist. Die letzten sieben Stände bleiben, ältere werden entfernt;
   Stände von vor der Umbenennung (toniapply-*.db) zählen mit.
2. Den Bucket nach <Ziel>/objects/ spiegeln, einschließlich Löschungen: was
   im Bucket fehlt, verschwindet auch aus der Spiegelung (R40).
3. Erst nach beidem die Zeitmarke schreiben, die /backup/check liest.

Aufruf:
  backup.py            Dauerbetrieb, nächtlich
  backup.py --once     sofort einmal sichern, Exit-Code 0 bei Erfolg
  backup.py restore <dateiname>
                       Rücksicherung in eine LEERE Instanz: der genannte
                       Datenbankstand aus <Ziel>/db/ wird zur Datenbank, die
                       Spiegelung wandert zurück in den Bucket. Bricht ab,
                       wenn schon eine Datenbank existiert.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import sys
import time
from datetime import UTC, datetime, timedelta
from datetime import time as clock
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3

BERLIN = ZoneInfo("Europe/Berlin")
RUN_AT = clock(0, 30)
# Kurzer Takt statt eines langen sleep bis zur Laufzeit: ein langer sleep
# zählt die Zeit im Ruhezustand nicht mit, ein verpasster Lauf fiele aus.
CHECK_EVERY_SECONDS = 60
RETRY_GAP = timedelta(minutes=30)
KEEP_DB_STATES = 7
# Stände von vor der Umbenennung (2026-10-06) heißen noch toniapply-*.db.
DB_PREFIXES = ("vorlesezeit", "toniapply")

DATABASE_PATH = Path(os.environ["DATABASE_PATH"])
TARGET = Path(os.environ.get("BACKUP_TARGET_DIR", "/backup"))
MARKER = Path(os.environ.get("BACKUP_MARKER_PATH", "/backup-marker/last-success"))
BUCKET = os.environ["STORAGE_BUCKET"]


def log(message: str) -> None:
    print(f"{datetime.now(BERLIN):%Y-%m-%d %H:%M:%S %Z} sicherung: {message}", flush=True)


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["STORAGE_ENDPOINT_URL"],
        aws_access_key_id=os.environ["STORAGE_ACCESS_KEY"],
        aws_secret_access_key=os.environ["STORAGE_SECRET_KEY"],
        region_name=os.environ.get("STORAGE_REGION", "us-east-1"),
    )


def backup_database() -> Path:
    db_dir = TARGET / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    final = db_dir / f"vorlesezeit-{stamp}.db"
    partial = final.with_suffix(".db.partial")
    # mode=rw: legt keine leere Datenbank an, falls die App noch nie lief.
    source = sqlite3.connect(f"file:{DATABASE_PATH}?mode=rw", uri=True, timeout=30)
    dest = sqlite3.connect(partial)
    try:
        source.backup(dest)
        # Die Kopie erbt WAL; als Sicherung soll sie eine einzelne Datei sein.
        dest.execute("PRAGMA journal_mode=DELETE")
    finally:
        dest.close()
        source.close()
    partial.rename(final)

    states = sorted(
        (p for prefix in DB_PREFIXES for p in db_dir.glob(f"{prefix}-*.db")),
        key=lambda p: p.stem.split("-", 1)[1],
    )
    for old in states[:-KEEP_DB_STATES]:
        old.unlink()
    log(f"Datenbank gesichert: {final.name}, {min(len(states), KEEP_DB_STATES)} Stände")
    return final


def mirror_bucket() -> None:
    s3 = s3_client()
    root = TARGET / "objects"
    root.mkdir(parents=True, exist_ok=True)

    remote = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
        for obj in page.get("Contents", []):
            remote[obj["Key"]] = obj

    copied = 0
    for key, obj in remote.items():
        local = root / key
        mtime = obj["LastModified"].timestamp()
        try:
            info = local.stat()
        except FileNotFoundError:
            info = None
        if (
            info is not None
            and stat.S_ISREG(info.st_mode)
            and info.st_size == obj["Size"]
            and info.st_mtime == mtime
        ):
            continue
        local.parent.mkdir(parents=True, exist_ok=True)
        partial = local.with_name(local.name + ".partial")
        s3.download_file(BUCKET, key, str(partial))
        os.utime(partial, (mtime, mtime))
        partial.rename(local)
        copied += 1

    # Ein leerer Bucket bei gefüllter Spiegelung ist eher ein Fehler (falscher
    # Bucket, frisch aufgesetztes MinIO) als 'alles gelöscht' -- dann nicht die
    # einzige Sicherung der Aufnahmen wegräumen, sondern ohne Zeitmarke abbrechen.
    if not remote and any(p.is_file() for p in root.rglob("*")):
        raise RuntimeError(
            f"Bucket {BUCKET} ist leer, die Spiegelung aber nicht -- "
            "Spiegelung bleibt unverändert, bitte Bucket prüfen"
        )

    removed = 0
    for local in sorted(root.rglob("*"), reverse=True):
        key = local.relative_to(root).as_posix()
        if local.is_file() and key not in remote:
            local.unlink()
            removed += 1
        elif local.is_dir() and not any(local.iterdir()):
            local.rmdir()
    log(f"Bucket gespiegelt: {len(remote)} Objekte, {copied} neu, {removed} entfernt")


def write_marker() -> None:
    MARKER.parent.mkdir(parents=True, exist_ok=True)
    partial = MARKER.with_name(MARKER.name + ".partial")
    partial.write_text(datetime.now(UTC).isoformat())
    partial.replace(MARKER)
    log("Zeitmarke geschrieben")


def run_once() -> bool:
    try:
        backup_database()
        mirror_bucket()
        write_marker()
    except Exception as exc:  # noqa: BLE001 -- eine Nacht darf den Dienst nicht beenden
        log(f"FEHLGESCHLAGEN, keine Zeitmarke: {exc!r}")
        return False
    return True


def restore(db_name: str) -> int:
    if DATABASE_PATH.exists():
        log(f"Abbruch: {DATABASE_PATH} existiert schon, Rücksicherung nur in eine leere Instanz")
        return 1
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TARGET / "db" / db_name, DATABASE_PATH)
    s3 = s3_client()
    root = TARGET / "objects"
    files = [p for p in root.rglob("*") if p.is_file()]
    for local in files:
        s3.upload_file(str(local), BUCKET, local.relative_to(root).as_posix())
    log(f"Rückgesichert: Datenbank {db_name}, {len(files)} Objekte")
    return 0


def read_marker() -> datetime | None:
    try:
        return datetime.fromisoformat(MARKER.read_text().strip())
    except (OSError, ValueError):
        return None


def last_scheduled(now: datetime) -> datetime:
    """Der jüngste planmäßige Zeitpunkt, der nicht in der Zukunft liegt."""
    run = datetime.combine(now.date(), RUN_AT, tzinfo=BERLIN)
    return run if run <= now else run - timedelta(days=1)


def is_due(now: datetime, last_success: datetime | None) -> bool:
    """Fällig, solange seit dem jüngsten planmäßigen Zeitpunkt keine
    Sicherung gelungen ist -- so holt das Skript einen verpassten Lauf nach."""
    return last_success is None or last_success < last_scheduled(now)


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["restore"] and len(args) == 2:
        return restore(args[1])
    if args == ["--once"]:
        return 0 if run_once() else 1
    if args:
        print(__doc__)
        return 2

    log(f"gestartet, täglich um {RUN_AT:%H:%M} Europe/Berlin, verpasste Läufe werden nachgeholt")
    last_attempt: datetime | None = None
    while True:
        now = datetime.now(BERLIN)
        retry_allowed = last_attempt is None or now - last_attempt >= RETRY_GAP
        if is_due(now, read_marker()) and retry_allowed:
            last_attempt = now
            run_once()
        time.sleep(CHECK_EVERY_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
