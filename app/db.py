"""Datenbankzugriff: SQLite-Datei in einem eingebundenen Verzeichnis (KTD12).

Zugriff ausschliesslich ueber SQLAlchemy als duenne Schicht ohne
herstellerspezifisches SQL -- der spaetere Wechsel auf eine verwaltete
Datenbank ist damit eine Konfigurationsaenderung plus Datenmigration, kein
Umbau (KTD12).
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import Config


class Base(DeclarativeBase):
    pass


def create_db_engine(config: Config):
    # KTD16: 30 s Wartefrist auf jeder Verbindung (statt der 5 s des
    # Treibers). Die Transaktionsbehandlung des Treibers bleibt unveraendert
    # -- kein BEGIN IMMEDIATE, sonst waere jede Anfrage serialisiert.
    return create_engine(
        f"sqlite:///{config.database_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )


def init_db(engine) -> None:
    """Legt fehlende Tabellen an und ergaenzt fehlende Spalten additiv (U16).

    KTD16: WAL ist eine Einstellung der Datenbankdatei und bleibt dort
    bestehen -- Leser und ein Schreiber blockieren sich damit nicht. Kein
    herstellerspezifisches SQL in Abfragen (KTD12), nur hier beim Einrichten."""
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(engine)
    _add_app_chapters_column(engine)
    _migrate_mehrkalender(engine)


def _add_app_chapters_column(engine) -> None:
    """U16: `create_all` legt keine Spalten auf bestehenden Tabellen an, und die
    Produktionsdatenbank traegt echte Aufnahmen (kein `down -v`). Einmalig
    anlegen und einen vorhandenen Verifiziert-Zustand uebernehmen; die Dauer
    ist dafuer unbekannt (None)."""
    columns = {c["name"] for c in inspect(engine).get_columns("campaigns")}
    if "app_chapters" in columns:
        return
    with engine.begin() as conn:
        # Der Treiber oeffnet vor DDL keine Transaktion von selbst (pysqlite:
        # nur vor DML) -- deshalb das ausdruckliche BEGIN, sonst bliebe das
        # ALTER nach einem Fehler im Backfill stehen.
        conn.exec_driver_sql("BEGIN")
        conn.execute(text("ALTER TABLE campaigns ADD COLUMN app_chapters VARCHAR"))
        rows = conn.execute(
            text(
                "SELECT id, verified_chapter_id FROM campaigns "
                "WHERE verified_chapter_id IS NOT NULL"
            )
        ).all()
        for campaign_id, chapter_id in rows:
            conn.execute(
                text("UPDATE campaigns SET app_chapters = :value WHERE id = :id"),
                {"value": json.dumps([{"id": chapter_id, "seconds": None}]), "id": campaign_id},
            )


# Mehrkalender U2: neue Spalten auf Bestandstabellen, die `create_all` nicht
# anlegt. SQLite erlaubt REFERENCES bei ADD COLUMN, solange die Vorgabe NULL ist.
_MEHRKALENDER_COLUMNS = [
    ("campaigns", "name", "VARCHAR"),
    ("slots", "auftrag_id", "INTEGER REFERENCES auftraege (id)"),
    ("beitraege", "auftrag_id", "INTEGER REFERENCES auftraege (id)"),
    ("delivery_runs", "tonie_id", "VARCHAR"),
    ("persons", "invited_at", "DATETIME"),
    ("einstellungen", "kind_name", "VARCHAR"),
    ("persons", "reminded_at", "DATETIME"),
    ("einstellungen", "hoechste_person_id", "INTEGER"),
]


def _migrate_mehrkalender(engine) -> None:
    """Mehrkalender U2: fehlende Spalten ergaenzen und, nur wenn
    `slots.auftrag_id` vor dem Start fehlte, die Altdaten abbilden (R33).

    Ausloeser ist allein der Schemazustand, nie der Zeileninhalt: eine frische
    Instanz bekommt `slots.auftrag_id` schon von `create_all` und bildet nie
    ab. Spalten und Abbildung laufen in einer Transaktion. Der Treiber oeffnet
    vor DDL keine Transaktion von selbst (pysqlite: nur vor DML) -- deshalb
    das ausdrueckliche BEGIN, sonst bliebe ein ALTER nach einem Fehler stehen."""
    inspector = inspect(engine)
    existing = {
        table: {c["name"] for c in inspector.get_columns(table)}
        for table, _, _ in _MEHRKALENDER_COLUMNS
    }
    missing = [(t, c, ddl) for t, c, ddl in _MEHRKALENDER_COLUMNS if c not in existing[t]]
    if not missing:
        return
    with engine.begin() as conn:
        conn.exec_driver_sql("BEGIN")
        for table, column, ddl in missing:
            conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        if ("slots", "auftrag_id") in {(t, c) for t, c, _ in missing}:
            _map_existing_data(conn)


def _map_existing_data(conn) -> None:
    """R33: Abbildung einer Datenbank im Schema vor dem Mehrkalender-Umbau.
    Altspalten bleiben unveraendert stehen."""
    conn.execute(text("UPDATE campaigns SET name = 'Familie' WHERE name IS NULL"))
    conn.execute(
        text(
            "INSERT INTO creative_tonies (tonie_id, konto_id, campaign_id, name, app_chapters, "
            "verified_beitrag_id, verified_chapter_id, verified_for_day, abraeumen_offen) "
            "SELECT creative_tonie_id, NULL, id, '', app_chapters, verified_beitrag_id, "
            "verified_chapter_id, verified_for_day, 0 FROM campaigns "
            "WHERE creative_tonie_id IS NOT NULL"
        )
    )
    # Ein Tuerchen wird Auftrag, sobald es Person, Titel, Text oder einen nicht
    # geloesten Beitrag traegt (geloeste haben slot_id NULL).
    slots = conn.execute(
        text(
            "SELECT id, assigned_person_id, title, vorlesetext FROM slots "
            "WHERE assigned_person_id IS NOT NULL OR title IS NOT NULL "
            "OR vorlesetext IS NOT NULL "
            "OR EXISTS (SELECT 1 FROM beitraege WHERE beitraege.slot_id = slots.id) "
            "ORDER BY id"
        )
    ).all()
    for slot_id, person_id, title, vorlesetext in slots:
        auftrag_id = conn.execute(
            text(
                "INSERT INTO auftraege (person_id, title, vorlesetext) "
                "VALUES (:person_id, :title, :vorlesetext)"
            ),
            {"person_id": person_id, "title": title, "vorlesetext": vorlesetext},
        ).lastrowid
        conn.execute(
            text("UPDATE slots SET auftrag_id = :auftrag_id WHERE id = :id"),
            {"auftrag_id": auftrag_id, "id": slot_id},
        )
    conn.execute(
        text(
            "UPDATE beitraege SET auftrag_id = "
            "(SELECT auftrag_id FROM slots WHERE slots.id = beitraege.slot_id) "
            "WHERE slot_id IS NOT NULL"
        )
    )
    conn.execute(
        text(
            "UPDATE delivery_runs SET tonie_id = "
            "(SELECT creative_tonie_id FROM campaigns "
            "WHERE campaigns.id = delivery_runs.campaign_id)"
        )
    )


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """FastAPI-Dependency: eine Session pro Request, geschlossen am Ende."""
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
