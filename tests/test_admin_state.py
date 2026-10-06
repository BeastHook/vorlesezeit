"""U8: Zustandsableitung fuer Slots und Beitraege (R12, R30, R42).

Gemeinsame Grundlage aller Admin-Reiter -- deshalb eigene Testdatei,
unabhaengig von den HTTP-Routen.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin.setup import add_calendar_day, create_auftrag, create_campaign, create_person
from app.admin.state import (
    beitrag_state,
    chapter_title_open,
    delivery_has_run,
    slot_state,
)
from app.models import Beitrag, DeliveryRun, Slot

BERLIN = ZoneInfo("Europe/Berlin")
DELIVERY = time(20, 0)


def _slot(db: Session, day: int) -> Slot:
    return db.execute(select(Slot).where(Slot.day == day)).scalar_one()


def _setup(db: Session):
    campaign = create_campaign(db)
    person = create_person(db, email="tante@example.test", display_name="Tante")
    return campaign, person


def assign_slot(db: Session, slot_id: int, person_id: int | None):
    """Mehrkalender U9: ein Auftrag fuer die Person an diesem Tag."""
    auftrag = create_auftrag(db, person_id=person_id)
    add_calendar_day(db, auftrag.id, slot_id, now=EARLY, delivery_time=DELIVERY)
    db.commit()
    return auftrag


def _beitrag(db: Session, person, slot, **kw) -> Beitrag:
    auftrag = None
    if slot is not None:
        auftrag = slot.auftrag or assign_slot(db, slot.id, None)
    b = Beitrag(person_id=person.id, auftrag=auftrag, audio_object_key="k", **kw)
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


def _run(db: Session, campaign, day: int, outcome: str, reason: str | None = None, rt="vorabend"):
    db.add(
        DeliveryRun(
            campaign_id=campaign.id,
            run_type=rt,
            target_day=day,
            started_at=datetime(2026, 11, 30, 19, 0) + timedelta(days=day - 1),
            outcome=outcome,
            reason=reason,
        )
    )
    db.commit()


EARLY = datetime(2026, 10, 1, 12, 0, tzinfo=BERLIN)


def test_unassigned_slot_is_frei_and_missing(db_session: Session):
    _setup(db_session)
    state = slot_state(db_session, _slot(db_session, 5), now=EARLY, delivery_time=DELIVERY)
    assert state.key == "frei"
    assert state.missing is True


def test_assigned_without_beitrag_is_offen(db_session: Session):
    _, person = _setup(db_session)
    slot = _slot(db_session, 5)
    assign_slot(db_session, slot.id, person.id)
    state = slot_state(db_session, slot, now=EARLY, delivery_time=DELIVERY)
    assert state.key == "offen"
    assert state.missing is True


def test_submitted_then_approved(db_session: Session):
    _, person = _setup(db_session)
    slot = _slot(db_session, 5)
    assign_slot(db_session, slot.id, person.id)
    b = _beitrag(db_session, person, slot)
    assert slot_state(db_session, slot, now=EARLY, delivery_time=DELIVERY).key == "eingereicht"

    b.approved_at = datetime.now(UTC).replace(tzinfo=None)
    db_session.commit()
    state = slot_state(db_session, slot, now=EARLY, delivery_time=DELIVERY)
    assert state.key == "freigegeben"
    assert state.missing is False
    assert state.beitrag is not None and state.beitrag.id == b.id


def test_rejected_beitrag_leaves_slot_open(db_session: Session):
    """R13: nach einer Ablehnung gilt der Slot wieder als offen."""
    _, person = _setup(db_session)
    slot = _slot(db_session, 5)
    assign_slot(db_session, slot.id, person.id)
    _beitrag(db_session, person, slot, rejected_at=datetime(2026, 10, 2))
    state = slot_state(db_session, slot, now=EARLY, delivery_time=DELIVERY)
    assert state.key == "offen"


def test_delivery_outcomes_map_to_states_with_reason(db_session: Session):
    """R12: ausgeliefert, mit Ersatzbeitrag versorgt, fehlgeschlagen mit Ursache."""
    campaign, _ = _setup(db_session)
    _run(db_session, campaign, 3, "erfolg")
    _run(db_session, campaign, 4, "ersatzbeitrag", "Kein freigegebener Beitrag fuer Tag 4.")
    _run(db_session, campaign, 5, "fehlschlag", "Anmeldung abgelehnt")
    now = datetime(2026, 12, 6, 9, 0, tzinfo=BERLIN)

    assert slot_state(db_session, _slot(db_session, 3), now=now, delivery_time=DELIVERY).key == (
        "ausgeliefert"
    )
    assert slot_state(db_session, _slot(db_session, 4), now=now, delivery_time=DELIVERY).key == (
        "ersatz"
    )
    failed = slot_state(db_session, _slot(db_session, 5), now=now, delivery_time=DELIVERY)
    assert failed.key == "fehlschlag"
    assert failed.detail == "Anmeldung abgelehnt"
    assert failed.missing is False


def test_latest_automatic_run_wins(db_session: Session):
    campaign, _ = _setup(db_session)
    _run(db_session, campaign, 5, "fehlschlag", "Zeitueberschreitung")
    db_session.add(
        DeliveryRun(
            campaign_id=campaign.id,
            run_type="kontrolllauf",
            target_day=5,
            started_at=datetime(2026, 12, 4, 21, 0),
            outcome="erfolg",
        )
    )
    db_session.commit()
    now = datetime(2026, 12, 5, 9, 0, tzinfo=BERLIN)
    assert slot_state(db_session, _slot(db_session, 5), now=now, delivery_time=DELIVERY).key == (
        "ausgeliefert"
    )


def test_skipped_kontrolllauf_keeps_vorabend_state(db_session: Session):
    """Ein uebersprungener Kontrolllauf (manueller Lauf dazwischen) verdraengt
    nicht das Ergebnis der Vorabend-Auslieferung."""
    campaign, _ = _setup(db_session)
    _run(db_session, campaign, 5, "ersatzbeitrag", "Kein freigegebener Beitrag")
    db_session.add(
        DeliveryRun(
            campaign_id=campaign.id,
            run_type="kontrolllauf",
            target_day=5,
            started_at=datetime(2026, 12, 4, 21, 0),
            outcome="uebersprungen",
        )
    )
    db_session.commit()
    now = datetime(2026, 12, 5, 9, 0, tzinfo=BERLIN)
    assert slot_state(db_session, _slot(db_session, 5), now=now, delivery_time=DELIVERY).key == (
        "ersatz"
    )


def test_dry_run_does_not_count_as_delivery(db_session: Session):
    campaign, _ = _setup(db_session)
    _run(db_session, campaign, 5, "erfolg", rt="trockenlauf")
    slot = _slot(db_session, 5)
    assert slot_state(db_session, slot, now=EARLY, delivery_time=DELIVERY).key == "frei"
    assert delivery_has_run(db_session, slot, now=EARLY, delivery_time=DELIVERY) is False


def test_delivery_has_run_by_clock_or_record(db_session: Session):
    """R30: Bezug ist die Vorabend-Auslieferung, nicht der Adventstag."""
    campaign, _ = _setup(db_session)
    slot = _slot(db_session, 6)
    before = datetime(2026, 12, 5, 19, 59, tzinfo=BERLIN)
    after = datetime(2026, 12, 5, 20, 0, tzinfo=BERLIN)
    assert delivery_has_run(db_session, slot, now=before, delivery_time=DELIVERY) is False
    assert delivery_has_run(db_session, slot, now=after, delivery_time=DELIVERY) is True

    _run(db_session, campaign, 6, "erfolg")
    assert delivery_has_run(db_session, slot, now=EARLY, delivery_time=DELIVERY) is True


def test_day_one_delivers_on_november_30(db_session: Session):
    _setup(db_session)
    slot = _slot(db_session, 1)
    assert delivery_has_run(
        db_session, slot, now=datetime(2026, 11, 30, 20, 0, tzinfo=BERLIN), delivery_time=DELIVERY
    )


def test_chapter_title_open_only_without_any_title(db_session: Session):
    """R42/AE25: ohne Auftragstitel, eigenen Titel und Kapitelnamen gilt der
    Kapitelname als offen."""
    _, person = _setup(db_session)
    slot = _slot(db_session, 7)
    b = _beitrag(db_session, person, slot)
    assert chapter_title_open(b) is True
    b.title = "Mein Titel"
    assert chapter_title_open(b) is False
    b.title = None
    slot.auftrag.title = "Auftragstitel"
    assert chapter_title_open(b) is False
    free = _beitrag(db_session, person, None)
    assert chapter_title_open(free) is True
    free.chapter_title = "Hallo"
    assert chapter_title_open(free) is False


def test_beitrag_state_labels(db_session: Session):
    _, person = _setup(db_session)
    b = _beitrag(db_session, person, None)
    assert beitrag_state(b).key == "eingereicht"
    b.approved_at = datetime(2026, 10, 3)
    assert beitrag_state(b).key == "freigegeben"
    b.approved_at = None
    b.rejected_at = datetime(2026, 10, 3)
    assert beitrag_state(b).key == "abgelehnt"


# --- Mehrkalender U7: Festwerden je Kalender (KTD10) ------------------------------


def test_moved_tonie_does_not_fix_days_in_its_new_calendar(db_session: Session):
    """Regression: ein von A nach B umgehaengter Tonie bringt seine
    Vorabend-Laeufe aus A mit -- sie machen denselben Tag in B nicht fest."""
    from app.admin.state import is_slot_delivered, is_slot_fixed
    from app.models import Campaign, CreativeTonie

    a, b = Campaign(name="A"), Campaign(name="B")
    db_session.add_all([a, b])
    db_session.flush()
    slot_b = Slot(campaign_id=b.id, day=5)
    db_session.add(slot_b)
    tonie = CreativeTonie(tonie_id="TONIE-WANDERT", campaign_id=a.id)
    db_session.add(tonie)
    db_session.flush()
    person = create_person(db_session, email="tante@example.test")
    auftrag = assign_slot(db_session, slot_b.id, person.id)
    beitrag = Beitrag(person_id=person.id, auftrag=auftrag, audio_object_key="k")
    db_session.add(beitrag)
    db_session.flush()
    db_session.add(
        DeliveryRun(
            campaign_id=a.id,
            tonie_id="TONIE-WANDERT",
            run_type="vorabend",
            target_day=5,
            started_at=datetime(2026, 12, 4, 19, 0),
            outcome="erfolg",
            beitrag_ids=str(beitrag.id),
        )
    )
    tonie.campaign_id = b.id
    db_session.commit()
    db_session.expire_all()  # B.tonies frisch laden (expire_on_commit=False)
    assert [t.tonie_id for t in slot_b.campaign.tonies] == ["TONIE-WANDERT"]

    assert is_slot_fixed(db_session, slot_b, now=EARLY, delivery_time=DELIVERY) is False
    assert is_slot_delivered(db_session, slot_b, auftrag) is False


def test_current_tonie_prefers_first_calendar_and_falls_back_to_unlinked(db_session: Session):
    from app.admin.state import current_tonie, linked_tonies
    from app.models import Campaign, CreativeTonie

    assert current_tonie(db_session) is None
    loose = CreativeTonie(tonie_id="LOSE")
    db_session.add(loose)
    db_session.commit()
    assert current_tonie(db_session).tonie_id == "LOSE"

    first, second = Campaign(name="Eins"), Campaign(name="Zwei")
    db_session.add_all([first, second])
    db_session.flush()
    db_session.add_all(
        [
            CreativeTonie(tonie_id="ZWEI", campaign_id=second.id),
            CreativeTonie(tonie_id="EINS", campaign_id=first.id),
        ]
    )
    db_session.commit()

    assert current_tonie(db_session).tonie_id == "EINS"
    assert [t.tonie_id for t in linked_tonies(db_session)] == ["EINS", "ZWEI", "LOSE"]


def test_slot_state_follows_selected_tonie_of_mirrored_calendar(db_session: Session):
    """R14/KTD10: zwei gespiegelte Tonies, ein Vorabend-Lauf je Tonie mit
    demselben Startzeitpunkt -- der Zustand folgt dem gewaehlten Tonie, nicht
    dem Lauf mit der hoechsten ID. Ohne Tonie bleibt die Kalendersicht."""
    from app.models import CreativeTonie

    campaign, _ = _setup(db_session)
    db_session.add_all(
        [
            CreativeTonie(tonie_id="T-A", campaign_id=campaign.id),
            CreativeTonie(tonie_id="T-B", campaign_id=campaign.id),
        ]
    )
    started = datetime(2026, 12, 4, 19, 0)
    for tonie_id, outcome, reason in (
        ("T-A", "fehlschlag", "Anmeldung abgelehnt"),
        ("T-B", "erfolg", None),
    ):
        db_session.add(
            DeliveryRun(
                campaign_id=campaign.id,
                tonie_id=tonie_id,
                run_type="vorabend",
                target_day=5,
                started_at=started,
                outcome=outcome,
                reason=reason,
            )
        )
    db_session.commit()
    slot = _slot(db_session, 5)
    now = datetime(2026, 12, 5, 9, 0, tzinfo=BERLIN)

    state_a = slot_state(db_session, slot, now=now, delivery_time=DELIVERY, tonie_id="T-A")
    assert (state_a.key, state_a.detail) == ("fehlschlag", "Anmeldung abgelehnt")
    state_b = slot_state(db_session, slot, now=now, delivery_time=DELIVERY, tonie_id="T-B")
    assert state_b.key == "ausgeliefert"
