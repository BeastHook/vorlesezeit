"""Mehrkalender U10: Tonie-Umschalter im Admin-Bereich (R14, R16, R40; KTD12)."""

from __future__ import annotations

import re
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Campaign, CreativeTonie, DeliveryRun, Slot


def _calendar(db: Session, name: str, *tonies: tuple[str, str]) -> Campaign:
    calendar = Campaign(name=name)
    db.add(calendar)
    db.flush()
    for day in range(1, 25):
        db.add(Slot(campaign_id=calendar.id, day=day))
    for tonie_id, tonie_name in tonies:
        db.add(CreativeTonie(tonie_id=tonie_id, name=tonie_name, campaign_id=calendar.id))
    db.commit()
    return calendar


def _tonie(db: Session, tonie_id: str) -> CreativeTonie:
    return db.query(CreativeTonie).filter_by(tonie_id=tonie_id).one()


def _run(db: Session, calendar: Campaign, tonie_id: str, day: int) -> None:
    db.add(
        DeliveryRun(
            campaign_id=calendar.id,
            tonie_id=tonie_id,
            run_type="vorabend",
            target_day=day,
            started_at=datetime(2026, 12, day - 1, 19, 0),
            outcome="erfolg",
        )
    )
    db.commit()


OPTION = r'<option value="([^"]+)"( selected)?>([^<]*)</option>'


def _options(html: str) -> dict[str, tuple[str, bool]]:
    """value -> (Beschriftung, ausgewaehlt) des Umschalters."""
    select = re.search(r'<select[^>]*name="auswahl".*?</select>', html, re.S)
    assert select, "Umschalter fehlt"
    return {
        m.group(1): (m.group(3).strip(), bool(m.group(2)))
        for m in re.finditer(OPTION, select.group(0))
    }


def _selected(html: str) -> str:
    return next(label for label, sel in _options(html).values() if sel)


def _choose(client: TestClient, label: str, back: str = "/admin/kalender"):
    value = next(v for v, (lbl, _) in _options(client.get(back).text).items() if lbl == label)
    return client.post("/admin/tonie", data={"auswahl": value, "zurueck": back})


@pytest.fixture
def two_calendars(db_session: Session):
    a = _calendar(db_session, "Familie Sommer", ("T-KINDER", "Kinderzimmer"))
    b = _calendar(db_session, "Familie Berger", ("T-BERGER", "Bergers Tonie"))
    return a, b


def test_switcher_groups_tonies_by_calendar_and_defaults_to_first(admin_client, two_calendars):
    html = admin_client.get("/admin/kalender").text

    assert '<optgroup label="Familie Sommer">' in html
    assert '<optgroup label="Familie Berger">' in html
    assert _selected(html) == "Kinderzimmer · Familie Sommer"
    assert "Familie Sommer" in html.split('class="scope-note', 1)[1].split("</div>", 1)[0]


def test_switch_to_tonie_of_calendar_b_follows_all_tabs_r16(
    admin_client, db_session, two_calendars
):
    a, b = two_calendars
    _run(db_session, a, "T-KINDER", 6)
    _run(db_session, b, "T-BERGER", 7)

    response = _choose(admin_client, "Bergers Tonie · Familie Berger", back="/admin/geschichten")
    assert str(response.url).endswith("/admin/geschichten")

    kalender = admin_client.get("/admin/kalender").text
    scope = kalender.split('class="scope-note', 1)[1].split("</div>", 1)[0]
    assert "Familie Berger" in scope and "Familie Sommer" not in scope

    auslieferung = admin_client.get("/admin/auslieferung").text
    assert "Vorabend Tag 7" in auslieferung
    assert "Vorabend Tag 6" not in auslieferung
    assert _selected(auslieferung) == "Bergers Tonie · Familie Berger"
    for tab in ("geschichten", "aufnahmen", "personen"):
        assert _selected(admin_client.get(f"/admin/{tab}").text) == (
            "Bergers Tonie · Familie Berger"
        )


def test_detached_or_deleted_tonie_falls_back_to_default(admin_client, db_session, two_calendars):
    _choose(admin_client, "Bergers Tonie · Familie Berger")
    berger = _tonie(db_session, "T-BERGER")
    berger.campaign_id = None  # getrennt
    db_session.commit()

    html = admin_client.get("/admin/kalender")
    assert html.status_code == 200
    assert _selected(html.text) == "Kinderzimmer · Familie Sommer"

    _choose(admin_client, "Bergers Tonie")  # jetzt ohne Kalender waehlbar
    db_session.delete(_tonie(db_session, "T-BERGER"))
    db_session.commit()
    html = admin_client.get("/admin/auslieferung")
    assert html.status_code == 200
    assert _selected(html.text) == "Kinderzimmer · Familie Sommer"


def test_unknown_choice_falls_back_to_default(admin_client, two_calendars):
    _choose(admin_client, "Bergers Tonie · Familie Berger")

    response = admin_client.post(
        "/admin/tonie", data={"auswahl": "t:999:1", "zurueck": "/admin/kalender"}
    )

    assert response.status_code == 200
    assert _selected(response.text) == "Kinderzimmer · Familie Sommer"


def test_switch_redirect_stays_inside_admin(admin_client, two_calendars):
    response = admin_client.post(
        "/admin/tonie",
        data={"auswahl": "garbage", "zurueck": "https://evil.example/admin/x"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/kalender"


def test_switch_forbidden_for_ordinary_person(person_client, two_calendars):
    response = person_client.post("/admin/tonie", data={"auswahl": "t:1:1"})
    assert response.status_code == 403


def test_without_tonies_first_calendar_and_setup_hint(admin_client, db_session):
    _calendar(db_session, "Familie Sommer")
    _calendar(db_session, "Patenkinder")

    kalender = admin_client.get("/admin/kalender").text
    assert _selected(kalender) == "Familie Sommer (kein Tonie)"
    assert kalender.count('data-day="') == 24

    auslieferung = admin_client.get("/admin/auslieferung").text
    assert "bespielt keinen Tonie" in auslieferung
    assert 'href="/admin/setup"' in auslieferung.split("</nav>", 1)[1]
    assert "Jetzt aufspielen" not in auslieferung


def test_calendar_without_tonie_selectable(admin_client, db_session, two_calendars):
    _calendar(db_session, "Patenkinder")

    html = admin_client.get("/admin/kalender").text
    assert "Patenkinder (kein Tonie)" in [label for label, _ in _options(html).values()]

    response = _choose(admin_client, "Patenkinder (kein Tonie)")
    scope = response.text.split('class="scope-note', 1)[1].split("</div>", 1)[0]
    assert "Patenkinder" in scope
    assert "bespielt keinen Tonie" in admin_client.get("/admin/auslieferung").text


def test_tonie_without_calendar_shows_hint_and_only_manual_run(
    admin_client, db_session, two_calendars
):
    db_session.add(CreativeTonie(tonie_id="T-WEIHNACHT", name="Weihnachts-Tonie"))
    db_session.commit()

    response = _choose(admin_client, "Weihnachts-Tonie")
    assert "Weihnachts-Tonie gehört zu keinem Kalender" in response.text
    assert "gehört zu keinem Kalender" in admin_client.get("/admin/geschichten").text

    auslieferung = admin_client.get("/admin/auslieferung").text
    assert "Manueller Lauf" in auslieferung
    assert "Ersatzbeitrag (R21)" not in auslieferung
    assert "Nächster Lauf" not in auslieferung


def test_mirrored_calendar_names_other_tonie_r40(admin_client, db_session):
    _calendar(db_session, "Familie Sommer", ("T-KINDER", "Kinderzimmer"), ("T-WOHN", "Wohnzimmer"))

    for tab in ("kalender", "geschichten"):
        page = admin_client.get(f"/admin/{tab}").text
        scope = page.split('class="scope-note', 1)[1].split("</div>")[0]
        assert "<strong>Kinderzimmer</strong> und <strong>Wohnzimmer</strong>" in scope

    auslieferung = admin_client.get("/admin/auslieferung").text
    assert "Nur Kinderzimmer" in auslieferung
    assert "Wohnzimmer hat einen eigenen Lauf" in auslieferung


def test_setup_tab_present(admin_client, two_calendars):
    nav = admin_client.get("/admin/kalender").text.split('class="admin-tabs"', 1)[1]
    assert 'href="/admin/setup"' in nav.split("</nav>", 1)[0]


def test_calendar_tile_follows_selected_mirrored_tonie_r14(admin_client, db_session):
    """R14/KTD10: gespiegelte Tonies, Vorabend-Laeufe mit demselben
    Startzeitpunkt -- die Kachel zeigt den Lauf des gewaehlten Tonies."""
    calendar = _calendar(
        db_session, "Familie Sommer", ("T-KINDER", "Kinderzimmer"), ("T-OMA", "Omas Tonie")
    )
    started = datetime(2026, 12, 4, 19, 0)
    for tonie_id, outcome, reason in (
        ("T-KINDER", "fehlschlag", "Anmeldung abgelehnt"),
        ("T-OMA", "erfolg", None),
    ):
        db_session.add(
            DeliveryRun(
                campaign_id=calendar.id,
                tonie_id=tonie_id,
                run_type="vorabend",
                target_day=5,
                started_at=started,
                outcome=outcome,
                reason=reason,
            )
        )
    db_session.commit()

    assert _selected(admin_client.get("/admin/kalender").text) == "Kinderzimmer · Familie Sommer"
    assert "Anmeldung abgelehnt" in admin_client.get("/admin/kalender").text

    _choose(admin_client, "Omas Tonie · Familie Sommer")
    assert "Anmeldung abgelehnt" not in admin_client.get("/admin/kalender").text
