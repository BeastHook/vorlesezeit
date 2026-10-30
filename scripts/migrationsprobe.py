"""Migrationsprobe: `init_db` auf einer Kopie einer Datenbank, vor jeder
Bereitstellung mit Schemaaenderung (docs/betrieb.md).

    python -m scripts.migrationsprobe <pfad-zur-datenbank>

Das Skript kopiert die Datei in ein temporaeres Verzeichnis und arbeitet nur
auf der Kopie. Es prueft Zeilenzahlen, Altwerte, neue Spalten, einen harmlosen
zweiten Start und `integrity_check`. Danach listet es die Personen, die die
Migration als widerrufen markiert hat (Advent-Anreicherung KTD8), mit ID und
Anzeigename zur Bestaetigung durch den Admin. Keine E-Mail-Adressen, keine
Vorlesetexte, keine Zugangsdaten. Exit-Code 0 nur, wenn alle Pruefungen
bestehen.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from app.db import _MEHRKALENDER_COLUMNS, init_db


def _snapshot(engine) -> dict[str, tuple[list[str], list[tuple]]]:
    inspector = inspect(engine)
    snapshot = {}
    with engine.connect() as conn:
        for table in inspector.get_table_names():
            columns = [c["name"] for c in inspector.get_columns(table)]
            rows = conn.execute(text(f'SELECT * FROM "{table}" ORDER BY rowid')).all()
            snapshot[table] = (columns, [tuple(row) for row in rows])
    return snapshot


def _old_values_unchanged(before, after) -> bool:
    for table, (columns, rows) in before.items():
        if table not in after:
            return False
        after_columns, after_rows = after[table]
        if after_columns[: len(columns)] != columns or len(after_rows) != len(rows):
            return False
        if [row[: len(columns)] for row in after_rows] != rows:
            return False
    return True


def _check(label: str, passed: bool) -> bool:
    print(f"{label}: {'ok' if passed else 'FEHLER'}")
    return passed


def probe(original: Path) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "probe.db"
        shutil.copyfile(original, copy)
        engine = create_engine(f"sqlite:///{copy}")
        try:
            return _probe_copy(engine)
        finally:
            engine.dispose()


def _probe_copy(engine) -> bool:
    before = _snapshot(engine)
    init_db(engine)
    after = _snapshot(engine)

    print("Zeilen je Tabelle (vor -> nach erstem init_db):")
    for table in sorted(after):
        count_before = len(before[table][1]) if table in before else "-"
        print(f"  {table}: {count_before} -> {len(after[table][1])}")

    results = [_check("Alte Spaltenwerte unveraendert", _old_values_unchanged(before, after))]
    expected = {(t, c) for t, c, _ in _MEHRKALENDER_COLUMNS}
    present = {(t, c) for t, (columns, _) in after.items() for c in columns}
    results.append(
        _check(
            "Neue Spalten und Tabelle morgenmails vorhanden",
            expected <= present and "morgenmails" in after,
        )
    )

    init_db(engine)
    results.append(_check("Zweiter init_db ohne Aenderung", _snapshot(engine) == after))

    with engine.connect() as conn:
        integrity = conn.exec_driver_sql("PRAGMA integrity_check").scalars().all()
        print(f"integrity_check: {', '.join(integrity)}")
        results.append(integrity == ["ok"])

        revoked = conn.execute(
            text("SELECT id, display_name FROM persons WHERE revoked_at IS NOT NULL ORDER BY id")
        ).all()
    print("Als widerrufen markierte Personen (bitte bestaetigen):")
    for person_id, display_name in revoked:
        print(f"  {person_id} {display_name}")
    if not revoked:
        print("  keine")

    passed = all(results)
    print(f"Ergebnis: {'bestanden' if passed else 'nicht bestanden'}")
    return passed


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("Aufruf: python -m scripts.migrationsprobe <pfad-zur-datenbank>", file=sys.stderr)
        return 1
    original = Path(args[0])
    if not original.is_file():
        print(f"Datei nicht gefunden: {original}", file=sys.stderr)
        return 1
    return 0 if probe(original) else 1


if __name__ == "__main__":
    sys.exit(main())
