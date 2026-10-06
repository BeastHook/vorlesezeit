"""U7/Mehrkalender U12: Einstiegslogik der Aufnahmeflaeche (R3-R5, R25/AE9,
R24, R36) -- reine Logik, ohne HTTP-Unterbau. Die Familie adressiert den
Auftrag (KTD14); die Reihenfolge folgt seinem fruehesten Kalendertag."""

from __future__ import annotations

from datetime import datetime

from app.models import Auftrag, Beitrag, Campaign, Person, Slot
from app.recording.routing import (
    admin_names,
    earliest_open_auftrag,
    is_slot_open,
    person_auftraege_overview,
    sanitize_title,
)


def make_campaign(db_session, name="Familie"):
    campaign = Campaign(name=name)
    db_session.add(campaign)
    db_session.commit()
    return campaign


def make_person(db_session, email="verwandte@example.test"):
    person = Person(email=email, display_name="Verwandte")
    db_session.add(person)
    db_session.commit()
    return person


def make_auftrag(db_session, person, *days, submitted=False, rejected=False):
    """`days` sind (Kalender, Tag); ohne Tage ist der Auftrag ein Entwurf."""
    auftrag = Auftrag(person_id=person.id if person else None, title="Sterne zählen")
    db_session.add(auftrag)
    for campaign, day in days:
        db_session.add(Slot(campaign_id=campaign.id, day=day, auftrag=auftrag))
    if submitted:
        db_session.add(
            Beitrag(
                person_id=person.id,
                auftrag=auftrag,
                audio_object_key=f"k-{id(auftrag)}",
                rejected_at=datetime(2026, 10, 2) if rejected else None,
            )
        )
    db_session.commit()
    return auftrag


def test_earliest_open_auftrag_follows_earliest_calendar_day_ae9(db_session):
    a, b = make_campaign(db_session), make_campaign(db_session, "Patenkinder")
    person = make_person(db_session)
    make_auftrag(db_session, person, (a, 9))
    earlier = make_auftrag(db_session, person, (a, 12), (b, 3))

    result = earliest_open_auftrag(db_session, person)

    assert result is not None
    assert result.id == earlier.id


def test_earliest_open_auftrag_skips_submitted_but_not_rejected(db_session):
    campaign = make_campaign(db_session)
    person = make_person(db_session)
    make_auftrag(db_session, person, (campaign, 3), submitted=True)
    rejected = make_auftrag(db_session, person, (campaign, 6), submitted=True, rejected=True)
    make_auftrag(db_session, person, (campaign, 9))

    assert earliest_open_auftrag(db_session, person).id == rejected.id


def test_earliest_open_auftrag_none_when_all_submitted_ae2(db_session):
    campaign = make_campaign(db_session)
    person = make_person(db_session)
    make_auftrag(db_session, person, (campaign, 3), submitted=True)

    assert earliest_open_auftrag(db_session, person) is None


def test_draft_and_foreign_auftrag_are_never_open_for_person_r36(db_session):
    campaign = make_campaign(db_session)
    person = make_person(db_session)
    other = make_person(db_session, email="andere@example.test")
    make_auftrag(db_session, person)  # Entwurf ohne Kalendertag
    make_auftrag(db_session, other, (campaign, 3))
    make_auftrag(db_session, None, (campaign, 4))

    assert earliest_open_auftrag(db_session, person) is None
    assert person_auftraege_overview(db_session, person) == []


def test_overview_lists_each_auftrag_once_with_status(db_session):
    a, b = make_campaign(db_session), make_campaign(db_session, "Patenkinder")
    person = make_person(db_session)
    submitted = make_auftrag(db_session, person, (a, 3), (b, 12), submitted=True)
    open_ = make_auftrag(db_session, person, (a, 9))

    overview = person_auftraege_overview(db_session, person)

    assert [(row.auftrag.id, row.status) for row in overview] == [
        (submitted.id, "eingereicht"),
        (open_.id, "offen"),
    ]


def test_is_slot_open_follows_auftrag(db_session):
    campaign = make_campaign(db_session)
    person = make_person(db_session)
    open_slot = make_auftrag(db_session, person, (campaign, 3)).slots[0]
    done_slot = make_auftrag(db_session, person, (campaign, 4), submitted=True).slots[0]

    assert is_slot_open(db_session, open_slot) is True
    assert is_slot_open(db_session, done_slot) is False


def test_admin_names_use_display_name_r26(db_session):
    from app import settings

    assert admin_names(db_session).nom == "der Admin"
    settings.set_value(db_session, "admin_display_name", "Luca", now=datetime(2026, 10, 3))

    names = admin_names(db_session)
    assert (names.nom, names.Nom, names.dat, names.acc) == ("Luca",) * 4


def test_sanitize_title_strips_control_chars_and_newlines():
    assert sanitize_title("Der\nMond \tzählt  mit\x00") == "Der Mond zählt mit"


def test_sanitize_title_truncates_to_max_length():
    long_title = "x" * 150
    result = sanitize_title(long_title)
    assert result is not None
    assert len(result) == 100


def test_sanitize_title_empty_becomes_none_ae25():
    assert sanitize_title("") is None
    assert sanitize_title("   \n\t  ") is None
