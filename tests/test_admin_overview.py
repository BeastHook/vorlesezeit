"""U8 Schritt 1 und 11: Reiter Kalender (R12) und Geschichten (R24)."""

from __future__ import annotations

import re
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin.setup import add_calendar_day, create_auftrag, create_campaign, create_person
from app.models import Beitrag, DeliveryRun, Slot

# Fester Zeitpunkt vor dem Advent: jeder Tag liegt noch in der Zukunft.
BEFORE_ADVENT = datetime(2026, 10, 1, 12, 0, tzinfo=ZoneInfo("Europe/Berlin"))


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr("app.admin.overview.berlin_now", lambda request: BEFORE_ADVENT)


@pytest.fixture
def campaign(db_session: Session):
    return create_campaign(db_session)


def _slot(db: Session, day: int) -> Slot:
    """Der Tag im ersten Kalender (dem aus der Fixture `campaign`)."""
    return db.execute(
        select(Slot).where(Slot.day == day).order_by(Slot.campaign_id).limit(1)
    ).scalar_one()


def _assign(db: Session, slot: Slot, person_id: int) -> None:
    """Mehrkalender U9: ein Auftrag fuer die Person an diesem Tag."""
    auftrag = create_auftrag(db, person_id=person_id)
    add_calendar_day(db, auftrag.id, slot.id, now=BEFORE_ADVENT, delivery_time=time(20))
    db.commit()


def _beitrag(db: Session, person_id: int, slot: Slot | None, **kw) -> Beitrag:
    beitrag = Beitrag(
        person_id=person_id,
        auftrag=slot.auftrag if slot else None,
        audio_object_key="key.mp3",
        **kw,
    )
    db.add(beitrag)
    db.commit()
    return beitrag


def _tile(html: str, day: int) -> str:
    match = re.search(rf'<a [^>]*data-day="{day}"[^>]*>.*?</a>', html, re.S)
    assert match, f"Kachel fuer Tag {day} fehlt"
    return match.group(0)


def test_admin_home_redirects_to_kalender_which_renders(admin_client: TestClient, campaign):
    response = admin_client.get("/admin")
    assert response.status_code == 200
    assert str(response.url).endswith("/admin/kalender")
    assert "Adventskalender 2026" in response.text
    assert response.text.count('data-day="') == 24


def test_kalender_forbidden_for_non_admin(person_client: TestClient, campaign):
    assert person_client.get("/admin/kalender").status_code == 403
    assert person_client.get("/admin/geschichten").status_code == 403


def test_kalender_without_campaign_redirects_to_admin(admin_client: TestClient):
    response = admin_client.get("/admin/kalender", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"


def test_day_without_approved_beitrag_is_highlighted(
    admin_client: TestClient, db_session: Session, campaign
):
    """Die Übersicht hebt einen Tag ohne freigegebenen Beitrag hervor."""
    klaus = create_person(db_session, "klaus@example.test", "Klaus")
    approved_slot = _slot(db_session, 7)
    _assign(db_session, approved_slot, klaus.id)
    _beitrag(db_session, klaus.id, approved_slot, approved_at=datetime(2026, 10, 1))

    html = admin_client.get("/admin/kalender").text

    # Kraeftig markiert werden nur die naechsten drei Auslieferungen
    # (Designabgleich 2026-09-29); "Was fehlt" zaehlt weiter alle Tage.
    for day in (1, 2, 3):
        assert "st-missing" in _tile(html, day)
    assert "st-missing" not in _tile(html, 5)
    assert "23 Türchen ohne freigegebenen Beitrag" in html
    tile7 = _tile(html, 7)
    assert "st-freigegeben" in tile7
    assert "st-missing" not in tile7
    assert "Klaus · freigegeben" in tile7


def test_missing_highlight_window_follows_next_delivery(
    admin_client: TestClient, monkeypatch, campaign
):
    # 9.12., 21:00: die Vorabend-Auslieferung fuer Tag 10 (20:00) ist vorbei,
    # als naechstes stehen Tag 11, 12 und 13 an.
    mid_advent = datetime(2026, 12, 9, 21, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    monkeypatch.setattr("app.admin.overview.berlin_now", lambda request: mid_advent)

    html = admin_client.get("/admin/kalender").text

    for day in (11, 12, 13):
        assert "st-missing" in _tile(html, day)
    for day in (10, 14, 24):
        assert "st-missing" not in _tile(html, day)


def test_tile_shows_failure_with_named_cause_and_ersatz(
    admin_client: TestClient, db_session: Session, campaign
):
    for day, outcome, reason in (
        (4, "fehlschlag", "Anmeldung abgelehnt"),
        (3, "ersatzbeitrag", "kein freigegebener Beitrag"),
    ):
        db_session.add(
            DeliveryRun(
                campaign_id=campaign.id,
                run_type="vorabend",
                target_day=day,
                started_at=datetime(2026, 12, day - 1, 20, 0),
                outcome=outcome,
                reason=reason,
            )
        )
    db_session.commit()

    html = admin_client.get("/admin/kalender").text

    tile4 = _tile(html, 4)
    assert "st-fehlschlag" in tile4
    assert "Auslieferung fehlgeschlagen" in tile4
    assert "Anmeldung abgelehnt" in tile4
    tile3 = _tile(html, 3)
    assert "st-ersatz" in tile3
    assert "mit Ersatzbeitrag" in tile3
    assert "kein freigegebener Beitrag" in tile3


def test_free_submission_never_appears_in_calendar(
    admin_client: TestClient, db_session: Session, campaign
):
    """Freie Einreichungen erscheinen im eigenen Eingang und nicht in der Tagesübersicht."""
    lena = create_person(db_session, "lena@example.test", "Lena")
    _beitrag(db_session, lena.id, None, title="Hallo aus Leipzig")

    html = admin_client.get("/admin/kalender").text

    assert "Hallo aus Leipzig" not in html
    assert "Lena" not in html


def test_tile_marks_open_chapter_title_and_links_to_beitrag(
    admin_client: TestClient, db_session: Session, campaign
):
    pia = create_person(db_session, "pia@example.test", "Pia")
    slot = _slot(db_session, 11)
    _assign(db_session, slot, pia.id)
    beitrag = _beitrag(db_session, pia.id, slot)

    html = admin_client.get("/admin/kalender").text

    tile = _tile(html, 11)
    assert "Kapitelname offen" in tile
    assert f'href="/admin/aufnahmen?beitrag_id={beitrag.id}"' in tile
    assert "Kapitelname offen" not in _tile(html, 12)
    assert f'href="/admin/geschichten?slot_id={_slot(db_session, 12).id}"' in _tile(html, 12)


def test_calendar_tiles_are_still_doors_with_seal_when_delivered(
    admin_client: TestClient, db_session: Session, campaign
):
    """Advent-Fassung (Task 6): Kacheln bleiben Tuerchen, Admin oeffnet nie
    ganz, ein ausgelieferter Tag traegt ein Siegel."""
    mira = create_person(db_session, "mira@example.test", "Mira")
    slot = _slot(db_session, 10)
    _assign(db_session, slot, mira.id)
    beitrag = _beitrag(db_session, mira.id, slot, approved_at=datetime(2026, 10, 1))
    db_session.add(
        DeliveryRun(
            campaign_id=campaign.id,
            run_type="vorabend",
            target_day=10,
            started_at=datetime(2026, 12, 9, 20, 0),
            outcome="erfolg",
            beitrag_ids=str(beitrag.id),
        )
    )
    db_session.commit()

    html = admin_client.get("/admin/kalender").text

    assert 'class="tuerchen is-zu is-still' in html
    tile10 = _tile(html, 10)
    assert 'class="siegel"' in tile10
    assert "is-offen" not in html


def test_calendar_tile_announces_day_for_screen_readers(
    admin_client: TestClient, db_session: Session, campaign
):
    """Final-Review-Fund (Task 4): die Kalenderkachel verraet den Tag nur per
    sichtbarer Ziffer; eine Screenreader-Nutzerin bekommt ohne diesen Text
    nur "(ohne Titel)" zu hoeren. Im Adminbereich ist die Tuerchennummer kein
    Geheimnis (anders als R10 fuer die Familie), darum wird sie hier
    angesagt."""
    html = admin_client.get("/admin/kalender").text

    tile1 = _tile(html, 1)
    assert '<span class="visually-hidden">Türchen 1</span>' in tile1
    assert 'class="ziffer" aria-hidden="true">1<' in tile1


def test_was_fehlt_names_unassigned_and_waiting_days(
    admin_client: TestClient, db_session: Session, campaign
):
    lena = create_person(db_session, "lena@example.test", "Lena")
    slot = _slot(db_session, 9)
    _assign(db_session, slot, lena.id)
    _beitrag(db_session, lena.id, slot)

    html = admin_client.get("/admin/kalender").text

    assert "24 Türchen ohne freigegebenen Beitrag" in html
    assert "Tag 9 wartet auf deine Freigabe" in html
    assert 'href="/admin/personen"' in html


def test_geschichten_lists_24_slots_and_selects_day_one(
    admin_client: TestClient, db_session: Session, campaign
):
    html = admin_client.get("/admin/geschichten").text
    assert html.count('class="rec-row') == 24
    assert "Türchen 1 · 1. Dezember" in html
    assert ">noch niemand</option>" in html
    assert "Einladung" not in html


def test_geschichten_saves_title_and_text(admin_client: TestClient, db_session: Session, campaign):
    klaus = create_person(db_session, "klaus@example.test", "Klaus")
    slot = _slot(db_session, 15)
    response = admin_client.post(
        f"/admin/geschichten/{slot.id}",
        data={
            "person_id": str(klaus.id),
            "title": "Der Pfefferkuchenmann",
            "vorlesetext": "Es war einmal",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/geschichten?slot_id={slot.id}"

    db_session.expire_all()
    slot = _slot(db_session, 15)
    assert slot.auftrag.title == "Der Pfefferkuchenmann"
    assert slot.auftrag.vorlesetext == "Es war einmal"

    html = admin_client.get(f"/admin/geschichten?slot_id={slot.id}").text
    # Mehrkalender U10: die Maske zeigt den Auftrag; die Zeile links den Tag.
    assert '<div class="panel-kicker">Auftrag</div>' in html
    assert re.search(r'class="rec-row sel"[^>]*>.*?Der Pfefferkuchenmann.*?>Klaus<', html, re.S)
    assert "Es war einmal</textarea>" in html

    admin_client.post(
        f"/admin/geschichten/{slot.id}",
        data={"person_id": str(klaus.id), "title": "", "vorlesetext": ""},
    )
    db_session.expire_all()
    slot = _slot(db_session, 15)
    assert slot.auftrag.title is None
    assert slot.auftrag.vorlesetext is None


def test_geschichten_assign_reassign_and_unassign(
    admin_client: TestClient, db_session: Session, campaign
):
    klaus = create_person(db_session, "klaus@example.test", "Klaus")
    ruth = create_person(db_session, "ruth@example.test", "Ruth")
    slot_id = _slot(db_session, 14).id

    def post(person_id):
        return admin_client.post(
            f"/admin/geschichten/{slot_id}",
            data={"person_id": str(person_id), "title": "", "vorlesetext": ""},
        )

    post(klaus.id)
    db_session.expire_all()
    auftrag_id = _slot(db_session, 14).auftrag_id
    assert _slot(db_session, 14).auftrag.person_id == klaus.id

    beitrag = _beitrag(db_session, klaus.id, _slot(db_session, 14))
    html = post(klaus.id).text  # unveraenderte Person: kein Loesen
    db_session.expire_all()
    assert db_session.get(Beitrag, beitrag.id).auftrag_id == auftrag_id
    assert "Aufnahme dazu ansehen" in html

    html = post(ruth.id).text
    db_session.expire_all()
    assert _slot(db_session, 14).auftrag.person_id == ruth.id
    assert db_session.get(Beitrag, beitrag.id).auftrag_id is None
    assert "Beitrag hat sich vom Türchen gelöst" in html

    second = _beitrag(db_session, ruth.id, _slot(db_session, 14))
    html = post("").text
    db_session.expire_all()
    slot = _slot(db_session, 14)
    assert slot.auftrag.person_id is None
    detached = db_session.get(Beitrag, second.id)
    assert detached.auftrag_id is None
    assert detached.detached_at is not None
    assert detached.person_id == ruth.id
    assert "Beitrag hat sich vom Türchen gelöst" in html


# --- Mehrkalender U10: Auftragsvergabe ueber mehrere Kalender -----------------


def _second_calendar(db: Session, name: str = "Familie Berger"):
    from app.models import Campaign, CreativeTonie

    calendar = Campaign(name=name)
    db.add(calendar)
    db.flush()
    for day in range(1, 25):
        db.add(Slot(campaign_id=calendar.id, day=day))
    db.add(CreativeTonie(tonie_id=f"T-{name}", name=f"Tonie {name}", campaign_id=calendar.id))
    db.commit()
    return calendar


def _cal_slot(db: Session, calendar, day: int) -> Slot:
    return db.execute(
        select(Slot).where(Slot.campaign_id == calendar.id, Slot.day == day)
    ).scalar_one()


def _flash(html: str) -> str:
    match = re.search(r'<div class="flash (\w+)"[^>]*>(.*?)</div>', html, re.S)
    assert match, "Hinweisbalken fehlt"
    return f"{match.group(1)}: {match.group(2)}"


def test_auftrag_in_two_calendars_via_mask_and_remove_day(
    admin_client: TestClient, db_session: Session, campaign
):
    miri = create_person(db_session, "miri@example.test", "Miri")
    berger = _second_calendar(db_session)
    a5, b12 = _slot(db_session, 5), _cal_slot(db_session, berger, 12)

    admin_client.post(
        f"/admin/geschichten/{a5.id}",
        data={"person_id": str(miri.id), "title": "Sterne zählen", "vorlesetext": ""},
    )
    db_session.expire_all()
    auftrag_id = _slot(db_session, 5).auftrag_id
    assert auftrag_id is not None

    page = admin_client.get(f"/admin/geschichten?auftrag_id={auftrag_id}").text
    # Die Auswahl bietet nur Kalender ohne diesen Auftrag und nur freie Tage.
    add_select = page.split('name="slot_id"', 1)[1].split("</select>", 1)[0]
    assert f'value="{b12.id}"' in add_select
    assert f'value="{a5.id}"' not in add_select
    assert f'value="{_slot(db_session, 6).id}"' not in add_select

    response = admin_client.post(
        f"/admin/auftraege/{auftrag_id}/tage", data={"slot_id": str(b12.id)}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/geschichten?auftrag_id={auftrag_id}"
    page = admin_client.get(response.headers["location"]).text
    days = page.split('class="day-list"', 1)[1].split("</ul>", 1)[0]
    assert "Familie" in days and "Tag 5" in days
    assert "Familie Berger" in days and "Tag 12" in days
    # Linke Liste (Vorgabe: Kalender des ersten Tonie, hier Berger) nennt
    # den anderen Kalender des Auftrags.
    assert "Miri · auch Familie · Tag 5" in page

    admin_client.post(f"/admin/auftraege/{auftrag_id}/tage/{b12.id}/entfernen")
    db_session.expire_all()
    assert _cal_slot(db_session, berger, 12).auftrag_id is None
    assert _slot(db_session, 5).auftrag_id == auftrag_id


def test_mask_shows_named_errors_for_taken_day_and_same_calendar(
    admin_client: TestClient, db_session: Session, campaign
):
    klaus = create_person(db_session, "klaus@example.test", "Klaus")
    berger = _second_calendar(db_session)
    _assign(db_session, _slot(db_session, 4), klaus.id)
    taken = _cal_slot(db_session, berger, 12)
    other = create_auftrag(db_session, person_id=klaus.id)
    add_calendar_day(db_session, other.id, taken.id, now=BEFORE_ADVENT, delivery_time=time(20))
    db_session.commit()
    auftrag_id = _slot(db_session, 4).auftrag_id

    html = admin_client.post(
        f"/admin/auftraege/{auftrag_id}/tage", data={"slot_id": str(taken.id)}
    ).text
    assert _flash(html).startswith("err: Kalendertag schon belegt")

    html = admin_client.post(
        f"/admin/auftraege/{auftrag_id}/tage", data={"slot_id": str(_slot(db_session, 9).id)}
    ).text
    assert _flash(html).startswith("err: Auftrag liegt schon in diesem Kalender")


def test_fixed_day_has_no_remove_button(admin_client: TestClient, db_session: Session, campaign):
    klaus = create_person(db_session, "klaus@example.test", "Klaus")
    berger = _second_calendar(db_session)
    slot = _cal_slot(db_session, berger, 3)
    _assign(db_session, slot, klaus.id)
    auftrag_id = _cal_slot(db_session, berger, 3).auftrag_id
    db_session.add(
        DeliveryRun(
            campaign_id=berger.id,
            tonie_id="T-Familie Berger",
            run_type="vorabend",
            target_day=3,
            started_at=datetime(2026, 12, 2, 20, 0),
            outcome="erfolg",
        )
    )
    db_session.commit()

    page = admin_client.get(f"/admin/geschichten?auftrag_id={auftrag_id}").text

    assert f"/tage/{slot.id}/entfernen" not in page
    assert "fest: Vorabend gelaufen" in page
    assert "lock-note" in page
    # Neuvergabe gesperrt: Auswahl gesperrt, Person geht trotzdem mit.
    assert re.search(r'<select class="input" id="person_id"[^>]*disabled', page)

    response = admin_client.post(f"/admin/auftraege/{auftrag_id}/tage/{slot.id}/entfernen")
    assert _flash(response.text).startswith("err: Türchen 3")
    db_session.expire_all()
    assert _cal_slot(db_session, berger, 3).auftrag_id == auftrag_id

    # Titel bleibt nach der Auslieferung aenderbar (Abnahme 2026-10-02).
    admin_client.post(
        f"/admin/auftraege/{auftrag_id}",
        data={"person_id": str(klaus.id), "title": "Neuer Titel", "vorlesetext": ""},
    )
    db_session.expire_all()
    assert _cal_slot(db_session, berger, 3).auftrag.title == "Neuer Titel"


def test_new_draft_listed_without_calendar_day(
    admin_client: TestClient, db_session: Session, campaign
):
    ruth = create_person(db_session, "ruth@example.test", "Ruth")
    assert "Neuer Auftrag" in admin_client.get("/admin/geschichten").text
    assert 'action="/admin/auftraege"' in admin_client.get("/admin/geschichten?neu=1").text

    response = admin_client.post(
        "/admin/auftraege",
        data={"person_id": str(ruth.id), "title": "Das Lebkuchenhaus", "vorlesetext": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303
    page = admin_client.get(response.headers["location"]).text
    drafts = page.split("Ohne Kalendertag", 1)[1]
    assert "Das Lebkuchenhaus" in drafts
    assert "Entwurf" in drafts
    assert "Noch kein Kalendertag." in page


def test_calendar_legend_shows_sample_tiles(admin_client: TestClient, campaign):
    """Legende A (Nutzerentscheidung 2026-10-04, docs/design/legende-name-mockup.html):
    fuenf Muster-Kacheln mit Zustand und Bedeutung statt winziger Zeichen."""
    html = admin_client.get("/admin/kalender").text
    legend = html.split('class="legende-muster"')[1].split("</div>\n{% endblock")[0]

    assert legend.count("<figure>") == 5
    for text in (
        "noch offen",
        "eingereicht oder freigegeben",
        "ausgeliefert",
        "fehlt",
        "nächster Liefertag",
    ):
        assert text in legend
    assert 'class="muster fehlt"' in legend
    assert 'class="muster glimmt"' in legend
    assert 'class="siegel"' in legend
