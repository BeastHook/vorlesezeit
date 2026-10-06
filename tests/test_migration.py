"""U2 (Mehrkalender-Plan): additive Migration einer Datenbank im Schema vor U2.

Die Fixture-Datenbank wird aus der DDL des Altstands angelegt (erzeugt mit
dem unveraenderten `app/models.py` von `d239414` und `sqlite3 .schema`), nicht
aus den aktuellen Modellen -- sonst bewiese der Test nichts ueber Altbestaende.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.db as db_module
from app.db import init_db
from app.models import Auftrag, Beitrag, Campaign, CreativeTonie, Slot

OLD_SCHEMA = """
CREATE TABLE persons (
    id INTEGER NOT NULL,
    email VARCHAR NOT NULL,
    display_name VARCHAR NOT NULL,
    is_admin BOOLEAN NOT NULL,
    access_version INTEGER NOT NULL,
    PRIMARY KEY (id)
);
CREATE UNIQUE INDEX ix_persons_email ON persons (email);
CREATE TABLE campaigns (
    id INTEGER NOT NULL,
    creative_tonie_id VARCHAR,
    replacement_beitrag_id INTEGER,
    verified_beitrag_id INTEGER,
    verified_chapter_id VARCHAR,
    app_chapters VARCHAR,
    verified_for_day INTEGER,
    PRIMARY KEY (id),
    FOREIGN KEY(replacement_beitrag_id) REFERENCES beitraege (id),
    FOREIGN KEY(verified_beitrag_id) REFERENCES beitraege (id)
);
CREATE TABLE slots (
    id INTEGER NOT NULL,
    campaign_id INTEGER NOT NULL,
    day INTEGER NOT NULL,
    title VARCHAR,
    vorlesetext VARCHAR,
    assigned_person_id INTEGER,
    invitation_ready BOOLEAN NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_slot_campaign_day UNIQUE (campaign_id, day),
    FOREIGN KEY(campaign_id) REFERENCES campaigns (id),
    FOREIGN KEY(assigned_person_id) REFERENCES persons (id)
);
CREATE TABLE beitraege (
    id INTEGER NOT NULL,
    person_id INTEGER NOT NULL,
    slot_id INTEGER,
    title VARCHAR,
    audio_object_key VARCHAR,
    approved_at DATETIME,
    rejected_at DATETIME,
    detached_at DATETIME,
    cut_start_seconds FLOAT,
    cut_end_seconds FLOAT,
    chapter_title VARCHAR,
    PRIMARY KEY (id),
    FOREIGN KEY(person_id) REFERENCES persons (id),
    FOREIGN KEY(slot_id) REFERENCES slots (id)
);
CREATE TABLE delivery_runs (
    id INTEGER NOT NULL,
    campaign_id INTEGER NOT NULL,
    run_type VARCHAR NOT NULL,
    target_day INTEGER,
    evening DATE,
    started_at DATETIME NOT NULL,
    outcome VARCHAR NOT NULL,
    reason VARCHAR,
    beitrag_ids VARCHAR,
    PRIMARY KEY (id),
    FOREIGN KEY(campaign_id) REFERENCES campaigns (id)
);
"""

APP_CHAPTERS = json.dumps([{"id": "kapitel-7", "seconds": 312.5}])

# Slots: 1 vergeben (Person+Titel+Text, Beitraege), 2 leer, 3 Titel+Text ohne
# Person, 4 ohne Person mit Admin-Upload, 5 leer mit geloestem Beitrag (der
# zaehlt nicht, slot_id NULL), 6-24 leer.
OLD_DATA = [
    "INSERT INTO persons VALUES (1, 'admin@example.test', 'Admin', 1, 0)",
    "INSERT INTO persons VALUES (2, 'oma@example.test', 'Oma', 0, 2)",
    "INSERT INTO campaigns (id, creative_tonie_id, replacement_beitrag_id, verified_beitrag_id,"
    " verified_chapter_id, app_chapters, verified_for_day)"
    f" VALUES (1, 'TONIE-ALT', 6, 1, 'kapitel-7', '{APP_CHAPTERS}', 7)",
    "INSERT INTO slots VALUES (1, 1, 1, 'Der Stern', 'Es war einmal', 2, 1)",
    "INSERT INTO slots VALUES (2, 1, 2, NULL, NULL, NULL, 0)",
    "INSERT INTO slots VALUES (3, 1, 3, 'Schneemann', 'Text drei', NULL, 0)",
    "INSERT INTO slots VALUES (4, 1, 4, NULL, NULL, NULL, 0)",
    "INSERT INTO slots VALUES (5, 1, 5, NULL, NULL, NULL, 0)",
    *[f"INSERT INTO slots VALUES ({d}, 1, {d}, NULL, NULL, NULL, 0)" for d in range(6, 25)],
    # 1: freigegeben mit Zuschnitt und Kapitelname am Slot 1
    "INSERT INTO beitraege VALUES (1, 2, 1, 'Stern', 'k/1.mp3', '2026-09-20 10:00:00.000000',"
    " NULL, NULL, 1.5, 40.25, 'Der Stern von Oma')",
    # 2: abgelehnt am Slot 1
    "INSERT INTO beitraege VALUES (2, 2, 1, 'Stern alt', 'k/2.mp3', NULL,"
    " '2026-09-19 10:00:00.000000', NULL, NULL, NULL, NULL)",
    # 3: Admin-Upload an Slot 4 ohne Person
    "INSERT INTO beitraege VALUES (3, 1, 4, 'Upload', 'k/3.mp3', NULL, NULL, NULL,"
    " NULL, NULL, NULL)",
    # 4: geloest (R35)
    "INSERT INTO beitraege VALUES (4, 2, NULL, 'Frueher', 'k/4.mp3', NULL, NULL,"
    " '2026-09-18 10:00:00.000000', NULL, NULL, NULL)",
    # 5: freie Nachricht
    "INSERT INTO beitraege VALUES (5, 2, NULL, 'Gruss', 'k/5.mp3', NULL, NULL, NULL,"
    " NULL, NULL, NULL)",
    # 6: Ersatzbeitrag (freigegeben, ohne Slot)
    "INSERT INTO beitraege VALUES (6, 1, NULL, 'Ersatz', 'k/6.mp3', '2026-09-20 10:00:00.000000',"
    " NULL, NULL, NULL, NULL, NULL)",
    "INSERT INTO delivery_runs VALUES (1, 1, 'anstoss', 2, NULL, '2026-10-01 12:52:00.000000',"
    " 'erfolg', NULL, '1')",
    "INSERT INTO delivery_runs VALUES (2, 1, 'aufraeumen', NULL, NULL,"
    " '2026-10-01 13:00:00.000000', 'erfolg', NULL, NULL)",
]

OLD_TABLES = ["persons", "campaigns", "slots", "beitraege", "delivery_runs"]


def _old_db(tmp_path, *, data=OLD_DATA, schema=OLD_SCHEMA):
    engine = create_engine(f"sqlite:///{tmp_path / 'alt.db'}")
    with engine.begin() as conn:
        for statement in schema.split(";"):
            if statement.strip():
                conn.exec_driver_sql(statement)
        for statement in data:
            conn.exec_driver_sql(statement)
    return engine


def _dump(engine) -> dict[str, list[tuple]]:
    tables = inspect(engine).get_table_names()
    with engine.connect() as conn:
        return {t: conn.execute(text(f"SELECT * FROM {t} ORDER BY 1")).all() for t in tables}


def _columns(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


@pytest.fixture
def migrated(tmp_path):
    engine = _old_db(tmp_path)
    before = _dump(engine)
    init_db(engine)
    return engine, before


def test_ae9_one_calendar_named_familie_and_one_tonie_with_taken_over_state(migrated):
    engine, _ = migrated
    with Session(engine) as s:
        campaigns = s.scalars(select(Campaign)).all()
        assert [c.name for c in campaigns] == ["Familie"]
        tonies = s.scalars(select(CreativeTonie)).all()
        assert len(tonies) == 1
        tonie = tonies[0]
        assert tonie.tonie_id == "TONIE-ALT"
        assert tonie.campaign_id == 1
        assert tonie.konto_id is None
        assert tonie.app_chapters == APP_CHAPTERS
        assert tonie.verified_beitrag_id == 1
        assert tonie.verified_chapter_id == "kapitel-7"
        assert tonie.verified_for_day == 7
        assert tonie.abraeumen_offen is False
        assert [t.tonie_id for t in campaigns[0].tonies] == ["TONIE-ALT"]


def test_assigned_slot_becomes_auftrag_and_beitraege_point_to_it(migrated):
    engine, _ = migrated
    with Session(engine) as s:
        slot = s.get(Slot, 1)
        auftrag = slot.auftrag
        assert auftrag is not None
        assert (auftrag.person_id, auftrag.title, auftrag.vorlesetext) == (
            2,
            "Der Stern",
            "Es war einmal",
        )
        approved, rejected = s.get(Beitrag, 1), s.get(Beitrag, 2)
        assert approved.auftrag_id == auftrag.id
        assert rejected.auftrag_id == auftrag.id
        assert (approved.cut_start_seconds, approved.cut_end_seconds) == (1.5, 40.25)
        assert approved.chapter_title == "Der Stern von Oma"
        assert approved.slot_id == 1
        assert sorted(b.id for b in auftrag.beitraege) == [1, 2]
        assert [sl.id for sl in auftrag.slots] == [1]


def test_empty_slot_gets_no_auftrag(migrated):
    engine, _ = migrated
    with Session(engine) as s:
        assert s.get(Slot, 2).auftrag_id is None
        assert s.get(Slot, 5).auftrag_id is None
        # Slots 1, 3, 4 -- sonst keiner.
        assert sorted(s.scalars(select(Slot.id).where(Slot.auftrag_id.is_not(None)))) == [1, 3, 4]
        assert len(s.scalars(select(Auftrag)).all()) == 3


def test_slot_with_texts_but_no_person_becomes_auftrag_without_person(migrated):
    engine, _ = migrated
    with Session(engine) as s:
        auftrag = s.get(Slot, 3).auftrag
        assert auftrag.person_id is None
        assert (auftrag.title, auftrag.vorlesetext) == ("Schneemann", "Text drei")


def test_admin_upload_without_person_becomes_auftrag_without_person(migrated):
    engine, _ = migrated
    with Session(engine) as s:
        auftrag = s.get(Slot, 4).auftrag
        assert auftrag is not None
        assert (auftrag.person_id, auftrag.title, auftrag.vorlesetext) == (None, None, None)
        assert s.get(Beitrag, 3).auftrag_id == auftrag.id


def test_detached_and_free_beitraege_stay_without_auftrag(migrated):
    engine, _ = migrated
    with Session(engine) as s:
        for beitrag_id in (4, 5, 6):
            beitrag = s.get(Beitrag, beitrag_id)
            assert beitrag.auftrag_id is None
            assert beitrag.slot_id is None
        assert s.get(Beitrag, 4).detached_at is not None


def test_replacement_beitrag_stays_on_calendar(migrated):
    engine, _ = migrated
    with Session(engine) as s:
        assert s.get(Campaign, 1).replacement_beitrag_id == 6


def test_delivery_runs_carry_previous_tonie_id(migrated):
    engine, _ = migrated
    with engine.connect() as conn:
        assert conn.execute(text("SELECT tonie_id FROM delivery_runs")).scalars().all() == [
            "TONIE-ALT",
            "TONIE-ALT",
        ]


def test_old_tables_keep_row_counts_and_old_columns(migrated):
    engine, before = migrated
    after = _dump(engine)
    for table in OLD_TABLES:
        assert len(after[table]) == len(before[table]), table
    # Altspalten unveraendert: die ersten Spalten jeder Zeile sind die alten.
    for table in OLD_TABLES:
        width = len(before[table][0])
        assert [row[:width] for row in after[table]] == before[table], table


def test_new_columns_and_tables_exist(migrated):
    engine, _ = migrated
    assert "name" in _columns(engine, "campaigns")
    assert "auftrag_id" in _columns(engine, "slots")
    assert "auftrag_id" in _columns(engine, "beitraege")
    assert "tonie_id" in _columns(engine, "delivery_runs")
    assert "invited_at" in _columns(engine, "persons")
    tables = set(inspect(engine).get_table_names())
    assert {
        "tonie_konten",
        "creative_tonies",
        "auftraege",
        "einstellungen",
        "einrichtungslinks",
        "abendmeldungen",
    } <= tables


def test_einstellungen_without_kind_name_gets_column(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ohne-kind.db'}")
    init_db(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE einstellungen DROP COLUMN kind_name")
    assert "kind_name" not in _columns(engine, "einstellungen")

    init_db(engine)

    assert "kind_name" in _columns(engine, "einstellungen")


def test_second_init_db_changes_nothing(migrated):
    engine, _ = migrated
    first = _dump(engine)
    init_db(engine)
    assert _dump(engine) == first


def test_campaign_without_tonie_gets_no_creative_tonie(tmp_path):
    data = [
        "INSERT INTO persons VALUES (1, 'admin@example.test', 'Admin', 1, 0)",
        "INSERT INTO campaigns (id) VALUES (1)",
        "INSERT INTO slots VALUES (1, 1, 1, 'Titel', NULL, 1, 0)",
        "INSERT INTO delivery_runs VALUES (1, 1, 'trockenlauf', 1, NULL,"
        " '2026-10-01 12:00:00.000000', 'fehlschlag', NULL, NULL)",
    ]
    engine = _old_db(tmp_path, data=data)
    init_db(engine)
    with Session(engine) as s:
        assert s.scalars(select(CreativeTonie)).all() == []
        assert s.get(Campaign, 1).name == "Familie"
        assert s.get(Slot, 1).auftrag.person_id == 1
    with engine.connect() as conn:
        assert conn.execute(text("SELECT tonie_id FROM delivery_runs")).scalar_one() is None


def test_db_before_u16_without_app_chapters_migrates_in_order(tmp_path):
    schema = OLD_SCHEMA.replace("    app_chapters VARCHAR,\n", "")
    data = [
        "INSERT INTO campaigns (id, creative_tonie_id, verified_chapter_id)"
        " VALUES (1, 'TONIE-ALT', 'kapitel-9')",
    ]
    engine = _old_db(tmp_path, schema=schema, data=data)
    init_db(engine)
    with Session(engine) as s:
        tonie = s.scalars(select(CreativeTonie)).one()
        assert json.loads(tonie.app_chapters) == [{"id": "kapitel-9", "seconds": None}]


def test_app_chapters_backfill_is_atomic(tmp_path, monkeypatch):
    """Spiegelt test_mapping_is_atomic fuer _add_app_chapters_column: ein Fehlschlag
    mitten in der Abbildung darf die Spalte nicht stehen lassen, sonst ueberspringt
    der naechste init_db die Funktion komplett (Spalten-Check) und holt nichts nach."""
    schema = OLD_SCHEMA.replace("    app_chapters VARCHAR,\n", "")
    data = [
        "INSERT INTO campaigns (id, creative_tonie_id, verified_chapter_id)"
        " VALUES (1, 'TONIE-ALT-1', 'kapitel-9')",
        "INSERT INTO campaigns (id, creative_tonie_id, verified_chapter_id)"
        " VALUES (2, 'TONIE-ALT-2', 'kapitel-11')",
    ]
    engine = _old_db(tmp_path, schema=schema, data=data)
    before = _dump(engine)
    real_dumps = json.dumps
    calls = []

    def failing_dumps(*args, **kwargs):
        calls.append(args)
        if len(calls) > 1:
            raise RuntimeError("Abbruch mitten im Backfill")
        return real_dumps(*args, **kwargs)

    monkeypatch.setattr(db_module.json, "dumps", failing_dumps)
    with pytest.raises(RuntimeError):
        init_db(engine)

    assert "app_chapters" not in _columns(engine, "campaigns")
    after = _dump(engine)
    for table in ["persons", "campaigns", "slots", "beitraege", "delivery_runs"]:
        assert after[table] == before[table], table

    # Der naechste Start holt den Backfill vollstaendig nach.
    monkeypatch.setattr(db_module.json, "dumps", real_dumps)
    init_db(engine)
    with Session(engine) as s:
        tonies = {t.tonie_id: t for t in s.scalars(select(CreativeTonie)).all()}
        assert json.loads(tonies["TONIE-ALT-1"].app_chapters) == [
            {"id": "kapitel-9", "seconds": None}
        ]
        assert json.loads(tonies["TONIE-ALT-2"].app_chapters) == [
            {"id": "kapitel-11", "seconds": None}
        ]


def test_fresh_db_with_calendar_via_orm_never_triggers_mapping(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'neu.db'}")
    init_db(engine)
    with Session(engine) as s:
        campaign = Campaign()
        s.add(campaign)
        s.flush()
        s.add_all(Slot(campaign_id=campaign.id, day=d, title=f"T{d}") for d in range(1, 25))
        s.commit()
        assert campaign.name == "Familie"

    init_db(engine)

    with Session(engine) as s:
        assert s.scalars(select(CreativeTonie)).all() == []
        assert s.scalars(select(Auftrag)).all() == []
        assert all(slot.auftrag_id is None for slot in s.scalars(select(Slot)))


def test_empty_db_gets_tables_but_no_campaign(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'leer.db'}")
    init_db(engine)
    assert "auftraege" in inspect(engine).get_table_names()
    with Session(engine) as s:
        assert s.scalars(select(Campaign)).all() == []


def test_mapping_is_atomic(tmp_path, monkeypatch):
    engine = _old_db(tmp_path)
    before = _dump(engine)
    real = db_module._map_existing_data

    def failing(conn):
        real(conn)
        raise RuntimeError("Abbruch mitten in der Abbildung")

    monkeypatch.setattr(db_module, "_map_existing_data", failing)
    with pytest.raises(RuntimeError):
        init_db(engine)

    assert "auftrag_id" not in _columns(engine, "slots")
    assert "auftrag_id" not in _columns(engine, "beitraege")
    assert "name" not in _columns(engine, "campaigns")
    assert "tonie_id" not in _columns(engine, "delivery_runs")
    after = _dump(engine)
    for table in OLD_TABLES:
        assert after[table] == before[table], table
    assert after["auftraege"] == []
    assert after["creative_tonies"] == []

    # Der naechste Start holt die Abbildung vollstaendig nach.
    monkeypatch.setattr(db_module, "_map_existing_data", real)
    init_db(engine)
    with Session(engine) as s:
        assert len(s.scalars(select(Auftrag)).all()) == 3
        assert s.scalars(select(CreativeTonie.tonie_id)).all() == ["TONIE-ALT"]


def test_two_slots_on_same_calendar_day_are_rejected(tmp_path):
    """R9: ein Kalendertag traegt genau einen Slot und damit hoechstens einen Auftrag."""
    engine = create_engine(f"sqlite:///{tmp_path / 'r9.db'}")
    init_db(engine)
    with Session(engine) as s:
        campaign = Campaign()
        s.add(campaign)
        s.flush()
        s.add(Slot(campaign_id=campaign.id, day=3, auftrag=Auftrag(title="A")))
        s.commit()
        s.add(Slot(campaign_id=campaign.id, day=3, auftrag=Auftrag(title="B")))
        with pytest.raises(IntegrityError):
            s.commit()


def test_new_person_columns_added_to_existing_db(tmp_path):
    """Personen-Status/Loeschen (2026-10-04): additive Spalten."""
    engine = create_engine(f"sqlite:///{tmp_path / 'ohne-status.db'}")
    init_db(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE persons DROP COLUMN reminded_at")
        conn.exec_driver_sql("ALTER TABLE einstellungen DROP COLUMN hoechste_person_id")

    init_db(engine)

    assert "reminded_at" in _columns(engine, "persons")
    assert "hoechste_person_id" in _columns(engine, "einstellungen")
