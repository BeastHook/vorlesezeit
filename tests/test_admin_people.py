"""U8: Admin-Reiter Personen (R43, R39, AE14, AE20, F6).

Test scenarios aus dem Plan:
- AE20: Erinnerung geht an eine Person mit offenem Tuerchen (Tagesnummern +
  frischer Link) und ist fuer eine Person ohne offenes Tuerchen nicht
  ausloesbar (Knopf deaktiviert, serverseitig abgewiesen, keine Mail).
- "ueberfaellig" erscheint erst nach der Aufnahme-Deadline.
- R39/AE14: Ein Widerruf entwertet Links und Sitzungen der betroffenen
  Person und laesst die der anderen unberuehrt.
- Einladung verschickt den Magic-Link an die richtige Adresse.
- Nicht-Admin bekommt 403.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.admin.setup import add_calendar_day, create_auftrag
from app.auth.tokens import create_magic_link_token
from app.config import Config
from app.models import Beitrag, Campaign, Person, Slot
from tests.mailutil import plain_text

EARLY = datetime(2026, 10, 1, 12, tzinfo=ZoneInfo("Europe/Berlin"))


def _person(db: Session, email: str, name: str) -> Person:
    person = Person(email=email, display_name=name)
    db.add(person)
    db.commit()
    db.refresh(person)
    return person


def _campaign(db: Session) -> Campaign:
    campaign = Campaign()
    db.add(campaign)
    db.flush()
    for day in range(1, 25):
        db.add(Slot(campaign_id=campaign.id, day=day))
    db.commit()
    return campaign


def _assign(db: Session, campaign: Campaign, person: Person, *days: int) -> None:
    """Mehrkalender U9: je Tag ein eigener Auftrag der Person."""
    for slot in campaign.slots:
        if slot.day in days:
            auftrag = create_auftrag(db, person_id=person.id)
            add_calendar_day(db, auftrag.id, slot.id, now=EARLY, delivery_time=time(20))
    db.commit()


def _submit(db: Session, campaign: Campaign, person: Person, day: int) -> None:
    slot = next(s for s in campaign.slots if s.day == day)
    db.add(
        Beitrag(
            person_id=person.id,
            auftrag=slot.auftrag,
            audio_object_key=f"k/{day}.mp3",
        )
    )
    db.commit()


@pytest.fixture
def mails(monkeypatch) -> dict[str, list[dict]]:
    sent: dict[str, list[dict]] = {"reminder": [], "invite": []}

    # Mehrkalender U5: die Mailfunktionen bekommen die DB-Sitzung (Zugang aus
    # den Einstellungen); aufgezeichnet wird nur der Mailinhalt.
    def fake_reminder(config, *, session, **kwargs):
        sent["reminder"].append(kwargs)

    def fake_invite(config, *, session, **kwargs):
        sent["invite"].append(kwargs)

    monkeypatch.setattr("app.admin.people.send_reminder_mail", fake_reminder)
    monkeypatch.setattr("app.admin.people.send_invitation_mail", fake_invite)
    return sent


def _freeze_today(monkeypatch, year: int, month: int, day: int) -> None:
    monkeypatch.setattr(
        "app.admin.people.berlin_now",
        lambda request: datetime(year, month, day, 12, tzinfo=ZoneInfo("Europe/Berlin")),
    )


def test_non_admin_gets_403(person_client: TestClient, db_session: Session):
    other = _person(db_session, "a@example.test", "A")
    assert person_client.get("/admin/personen").status_code == 403
    assert person_client.post(f"/admin/personen/{other.id}/erinnern").status_code == 403
    assert person_client.post(f"/admin/personen/{other.id}/einladen").status_code == 403
    assert person_client.post(f"/admin/personen/{other.id}/widerrufen").status_code == 403


def test_overview_without_campaign(admin_client: TestClient, db_session: Session):
    _person(db_session, "klaus@example.test", "Klaus")

    response = admin_client.get("/admin/personen")

    assert response.status_code == 200
    assert "Klaus" in response.text
    assert "klaus@example.test" in response.text
    assert "kein Türchen" in response.text
    assert "Person hinzufügen" in response.text
    assert 'action="/admin/persons"' in response.text
    # Der Admin selbst steht nicht in der Verwandten-Liste.
    assert "admin@example.test" not in response.text


def test_overview_states_and_overdue(admin_client: TestClient, db_session: Session, monkeypatch):
    campaign = _campaign(db_session)
    klaus = _person(db_session, "klaus@example.test", "Klaus")
    bea = _person(db_session, "bea@example.test", "Bea")
    _assign(db_session, campaign, klaus, 14, 15)
    _assign(db_session, campaign, bea, 3, 7)
    _submit(db_session, campaign, bea, 3)
    _submit(db_session, campaign, bea, 7)

    _freeze_today(monkeypatch, 2026, 11, 1)
    before = admin_client.get("/admin/personen").text
    # Mehrkalender U10 (R15): je Auftrag "Kalender · Tag".
    assert '<span class="cal-chip">Familie · Tag 14</span>' in before
    assert '<span class="cal-chip">Familie · Tag 15</span>' in before
    assert "2 offen" in before
    assert "überfällig" not in before
    assert "alles abgegeben" in before
    assert "Keine offenen Türchen" in before  # Bea: Erinnern deaktiviert (AE20)

    _freeze_today(monkeypatch, 2026, 11, 25)  # Deadline 24.11. ueberschritten
    after = admin_client.get("/admin/personen").text
    assert "2 offen · überfällig" in after
    assert "tag-warn" in after


def test_reminder_goes_to_person_with_open_slot(
    admin_client: TestClient, db_session: Session, mails
):
    campaign = _campaign(db_session)
    klaus = _person(db_session, "klaus@example.test", "Klaus")
    _assign(db_session, campaign, klaus, 15, 14)
    _submit(db_session, campaign, klaus, 14)

    response = admin_client.post(f"/admin/personen/{klaus.id}/erinnern")

    assert response.status_code == 200  # nach Redirect auf /admin/personen
    assert "Erinnerung an Klaus verschickt." in response.text
    assert len(mails["reminder"]) == 1
    sent = mails["reminder"][0]
    assert sent["to_address"] == "klaus@example.test"
    assert sent["open_days"] == [15]
    assert "/login/confirm?token=" in sent["login_url"]


def test_reminder_refused_without_open_slot(admin_client: TestClient, db_session: Session, mails):
    campaign = _campaign(db_session)
    bea = _person(db_session, "bea@example.test", "Bea")
    nobody = _person(db_session, "nobody@example.test", "Nobody")
    _assign(db_session, campaign, bea, 3)
    _submit(db_session, campaign, bea, 3)

    for person in (bea, nobody):
        response = admin_client.post(f"/admin/personen/{person.id}/erinnern")
        assert response.status_code == 200
        assert "flash err" in response.text

    assert mails["reminder"] == []


def test_reminder_mail_failure_shows_error(
    admin_client: TestClient, db_session: Session, monkeypatch
):
    campaign = _campaign(db_session)
    klaus = _person(db_session, "klaus@example.test", "Klaus")
    _assign(db_session, campaign, klaus, 14)

    def boom(config, **kwargs):
        raise OSError("smtp down")

    monkeypatch.setattr("app.admin.people.send_reminder_mail", boom)
    response = admin_client.post(f"/admin/personen/{klaus.id}/erinnern")

    assert "flash err" in response.text


def test_invite_sends_magic_link(admin_client: TestClient, db_session: Session, mails):
    klaus = _person(db_session, "klaus@example.test", "Klaus")

    response = admin_client.post(f"/admin/personen/{klaus.id}/einladen")

    assert "Einladung an Klaus verschickt." in response.text
    assert mails["invite"] == [
        {"to_address": "klaus@example.test", "login_url": mails["invite"][0]["login_url"]}
    ]
    assert "/login/confirm?token=" in mails["invite"][0]["login_url"]


def test_invite_sets_invited_at_once_r23(
    admin_client: TestClient, db_session: Session, mails, monkeypatch
):
    klaus = _person(db_session, "klaus@example.test", "Klaus")
    assert klaus.invited_at is None
    _freeze_today(monkeypatch, 2026, 10, 5)

    admin_client.post(f"/admin/personen/{klaus.id}/einladen")
    db_session.expire_all()
    first = db_session.get(Person, klaus.id).invited_at
    assert first is not None and first.date().isoformat() == "2026-10-05"

    _freeze_today(monkeypatch, 2026, 10, 9)
    admin_client.post(f"/admin/personen/{klaus.id}/einladen")
    db_session.expire_all()

    assert len(mails["invite"]) == 2
    assert db_session.get(Person, klaus.id).invited_at == first


def test_failed_invite_leaves_invited_at_empty(
    admin_client: TestClient, db_session: Session, monkeypatch
):
    def boom(config, **kwargs):
        raise OSError("smtp down")

    monkeypatch.setattr("app.admin.people.send_invitation_mail", boom)
    klaus = _person(db_session, "klaus@example.test", "Klaus")

    response = admin_client.post(f"/admin/personen/{klaus.id}/einladen")

    assert "flash err" in response.text
    db_session.expire_all()
    assert db_session.get(Person, klaus.id).invited_at is None


def test_reminder_builds_plain_text_message():
    """Textteil bleibt Kern, HTML ist Alternative (Advent-Design)."""
    from app.mail.reminder import build_reminder_message

    message = build_reminder_message(
        to_address="klaus@example.test",
        display_name="<b>Klaus</b>",
        open_days=[14, 15],
        login_url="https://example.test/login/confirm?token=abc",
        from_address="vorlesezeit@example.test",
    )

    assert message["To"] == "klaus@example.test"
    assert message.get_content_type() == "multipart/alternative"
    body = plain_text(message)
    assert "14, 15" in body
    assert "https://example.test/login/confirm?token=abc" in body


def test_revoke_confirmation_page(admin_client: TestClient, db_session: Session):
    klaus = _person(db_session, "klaus@example.test", "Klaus")

    response = admin_client.get(f"/admin/personen/{klaus.id}/widerrufen")

    assert response.status_code == 200
    assert "Klaus" in response.text
    assert f'action="/admin/personen/{klaus.id}/widerrufen"' in response.text


def test_revoke_refused_for_admin(admin_client: TestClient, db_session: Session):
    from sqlalchemy import select

    admin = db_session.execute(select(Person).where(Person.is_admin.is_(True))).scalar_one()
    version = admin.access_version

    response = admin_client.post(f"/admin/personen/{admin.id}/widerrufen", follow_redirects=False)

    assert response.status_code == 404
    db_session.refresh(admin)
    assert admin.access_version == version


def _login(app, config: Config, person: Person) -> TestClient:
    other = TestClient(app)
    other.post("/login/confirm", data={"token": create_magic_link_token(config, person)})
    return other


def test_revoke_invalidates_only_affected_person(
    admin_client: TestClient, config: Config, db_session: Session
):
    """R39/AE14."""
    a = _person(db_session, "a@example.test", "A")
    b = _person(db_session, "b@example.test", "B")
    client_a = _login(admin_client.app, config, a)
    client_b = _login(admin_client.app, config, b)
    token_a = create_magic_link_token(config, a)
    token_b = create_magic_link_token(config, b)
    assert client_a.get("/", follow_redirects=False).status_code == 200
    assert client_b.get("/", follow_redirects=False).status_code == 200

    response = admin_client.post(f"/admin/personen/{a.id}/widerrufen")
    assert "flash ok" in response.text

    home_a = client_a.get("/", follow_redirects=False)
    assert home_a.status_code == 303
    assert home_a.headers["location"] == "/login"
    assert client_a.post("/login/confirm", data={"token": token_a}).status_code == 400

    assert client_b.get("/", follow_redirects=False).status_code == 200
    fresh_b = TestClient(admin_client.app)
    confirm_b = fresh_b.post("/login/confirm", data={"token": token_b}, follow_redirects=False)
    assert confirm_b.status_code == 303


def test_free_submissions_panel(admin_client: TestClient, db_session: Session):
    lena = _person(db_session, "lena@example.test", "Lena")
    assert "/admin/aufnahmen" not in admin_client.get("/admin/personen").text.split("</nav>")[1]

    db_session.add(Beitrag(person_id=lena.id, audio_object_key="k/frei1.mp3"))
    db_session.add(Beitrag(person_id=lena.id, audio_object_key="k/frei2.mp3"))
    db_session.add(Beitrag(person_id=lena.id, audio_object_key=None))  # nie hochgeladen
    db_session.commit()

    page = admin_client.get("/admin/personen").text.split("</nav>")[1]
    assert "Eingang ohne Türchen" in page
    assert "2 freie" in page
    assert 'href="/admin/aufnahmen"' in page


# --- Mehrkalender U10: Einladungshinweis (R23) und Kalender · Tag je Auftrag ---


def _hint(html: str) -> str | None:
    """Der Einladungshinweis oberhalb der Liste, sonst None."""
    page = html.split("</nav>", 1)[1]
    if "Einladungstermin erreicht" not in page:
        return None
    return page.split("Einladungstermin erreicht", 1)[1].split("<h3>Verwandte", 1)[0]


def test_invitation_hint_from_date_on_lists_exactly_uninvited_r23(
    admin_client: TestClient, db_session: Session, monkeypatch
):
    from datetime import date

    from app import settings

    campaign = _campaign(db_session)
    klaus = _person(db_session, "klaus@example.test", "Opa Klaus")
    ruth = _person(db_session, "ruth@example.test", "Ruth")
    miri = _person(db_session, "miri@example.test", "Miri")  # schon eingeladen
    gerda = _person(db_session, "gerda@example.test", "Tante Gerda")  # nur Entwurf
    willi = _person(db_session, "willi@example.test", "Willi")  # widerrufen
    _person(db_session, "ohne@example.test", "Ohne Auftrag")
    _assign(db_session, campaign, klaus, 4)
    _assign(db_session, campaign, ruth, 3)
    _assign(db_session, campaign, miri, 5)
    _assign(db_session, campaign, willi, 6)
    create_auftrag(db_session, person_id=gerda.id, title="Weihnachtsmarkt")
    miri.invited_at = datetime(2026, 10, 20, 12)
    willi.access_version = 1
    settings.set_value(db_session, "invitation_date", date(2026, 11, 1), now=EARLY)
    db_session.commit()

    _freeze_today(monkeypatch, 2026, 10, 31)
    assert _hint(admin_client.get("/admin/personen").text) is None

    _freeze_today(monkeypatch, 2026, 11, 1)
    html = admin_client.get("/admin/personen").text
    hint = _hint(html)
    assert hint is not None
    assert "Opa Klaus und Ruth" in hint
    for name in ("Miri", "Tante Gerda", "Willi", "Ohne Auftrag"):
        assert name not in hint
    assert "nur Entwurf" in html
    assert "Entwurf: Weihnachtsmarkt" in html


def test_no_invitation_hint_without_date(
    admin_client: TestClient, db_session: Session, monkeypatch
):
    campaign = _campaign(db_session)
    _assign(db_session, campaign, _person(db_session, "k@example.test", "Klaus"), 4)
    _freeze_today(monkeypatch, 2026, 12, 1)
    assert _hint(admin_client.get("/admin/personen").text) is None


def test_person_row_names_calendar_and_day_per_auftrag_r15(
    admin_client: TestClient, db_session: Session
):
    a = _campaign(db_session)
    b = _campaign(db_session)
    b.name = "Familie Berger"
    db_session.commit()
    miri = _person(db_session, "miri@example.test", "Miri")
    auftrag = create_auftrag(db_session, person_id=miri.id)
    for calendar, day in ((a, 5), (b, 12)):
        slot = next(s for s in calendar.slots if s.day == day)
        add_calendar_day(db_session, auftrag.id, slot.id, now=EARLY, delivery_time=time(20))
    db_session.commit()

    html = admin_client.get("/admin/personen").text

    assert "Familie · Tag 5" in html
    assert "Familie Berger · Tag 12" in html
    assert "1 offen" in html


# --- Status Einladung/Erinnerung und Loeschen (2026-10-04) ---------------------


def _row(html: str, name: str) -> str:
    """Die Personenzeile mit diesem Namen."""
    return html.split(f"<strong>{name}</strong>", 1)[1].split('<div class="person-row">', 1)[0]


def test_reminder_sets_reminded_at_to_last_send(
    admin_client: TestClient, db_session: Session, mails, monkeypatch
):
    campaign = _campaign(db_session)
    klaus = _person(db_session, "klaus@example.test", "Klaus")
    _assign(db_session, campaign, klaus, 14)
    assert klaus.reminded_at is None

    _freeze_today(monkeypatch, 2026, 11, 2)
    admin_client.post(f"/admin/personen/{klaus.id}/erinnern")
    _freeze_today(monkeypatch, 2026, 11, 12)
    admin_client.post(f"/admin/personen/{klaus.id}/erinnern")

    db_session.expire_all()
    assert db_session.get(Person, klaus.id).reminded_at.date().isoformat() == "2026-11-12"


def test_failed_reminder_leaves_reminded_at_empty(
    admin_client: TestClient, db_session: Session, monkeypatch
):
    campaign = _campaign(db_session)
    klaus = _person(db_session, "klaus@example.test", "Klaus")
    _assign(db_session, campaign, klaus, 14)

    def boom(config, **kwargs):
        raise OSError("smtp down")

    monkeypatch.setattr("app.admin.people.send_reminder_mail", boom)
    admin_client.post(f"/admin/personen/{klaus.id}/erinnern")

    db_session.expire_all()
    assert db_session.get(Person, klaus.id).reminded_at is None


def test_status_line_shows_invitation_and_reminder(admin_client: TestClient, db_session: Session):
    gisela = _person(db_session, "gisela@example.test", "Gisela")
    ben = _person(db_session, "ben@example.test", "Ben")
    _person(db_session, "mia@example.test", "Mia")
    gisela.invited_at = datetime(2026, 10, 4, 21, 5)
    gisela.reminded_at = datetime(2026, 11, 12, 9, 0)
    ben.invited_at = datetime(2026, 10, 4, 21, 6)
    db_session.commit()

    html = admin_client.get("/admin/personen").text

    assert "Eingeladen am 4. Okt." in _row(html, "Gisela")
    assert "Erinnert am 12. Nov." in _row(html, "Gisela")
    assert "Eingeladen am 4. Okt." in _row(html, "Ben")
    assert "Noch nicht erinnert" in _row(html, "Ben")
    assert "Noch nicht eingeladen" in _row(html, "Mia")
    assert "Erinnert" not in _row(html, "Mia")
    # Umbruch nur am Trenner, nie mitten im Datum.
    assert '<span class="person-status-part">Eingeladen am 4. Okt.</span>' in html


def test_delete_link_in_each_row(admin_client: TestClient, db_session: Session):
    klaus = _person(db_session, "klaus@example.test", "Klaus")
    html = admin_client.get("/admin/personen").text
    assert f'href="/admin/personen/{klaus.id}/loeschen"' in html


def test_delete_confirmation_lists_stories_to_be_freed(
    admin_client: TestClient, db_session: Session
):
    campaign = _campaign(db_session)
    gisela = _person(db_session, "gisela@example.test", "Gisela")
    _assign(db_session, campaign, gisela, 3)
    next(s for s in campaign.slots if s.day == 3).auftrag.title = "Der kleine Tannenbaum"
    create_auftrag(db_session, person_id=gisela.id, title="Sterne zählen")
    db_session.commit()

    response = admin_client.get(f"/admin/personen/{gisela.id}/loeschen")

    assert response.status_code == 200
    assert "Gisela löschen?" in response.text
    assert "„Der kleine Tannenbaum“ (Familie · Tag 3)" in response.text
    assert "„Sterne zählen“" in response.text
    assert f'action="/admin/personen/{gisela.id}/loeschen"' in response.text


def test_delete_frees_auftraege_and_removes_person(admin_client: TestClient, db_session: Session):
    from app.models import Auftrag

    campaign = _campaign(db_session)
    gisela = _person(db_session, "gisela@example.test", "Gisela")
    _assign(db_session, campaign, gisela, 3, 17)
    gisela_id = gisela.id

    response = admin_client.post(f"/admin/personen/{gisela_id}/loeschen")

    assert "Gisela gelöscht." in response.text
    db_session.expire_all()
    assert db_session.get(Person, gisela_id) is None
    auftraege = db_session.query(Auftrag).all()
    assert len(auftraege) == 2
    assert all(a.person_id is None for a in auftraege)
    # Die Tage haengen weiter an ihren Auftraegen.
    assert sorted(s.day for a in auftraege for s in a.slots) == [3, 17]


def test_delete_refused_with_any_beitrag(admin_client: TestClient, db_session: Session):
    campaign = _campaign(db_session)
    ben = _person(db_session, "ben@example.test", "Ben")
    _assign(db_session, campaign, ben, 8)
    _submit(db_session, campaign, ben, 8)
    beitrag = db_session.query(Beitrag).one()
    beitrag.rejected_at = datetime(2026, 11, 1, 12)  # auch abgelehnte zaehlen
    db_session.commit()

    page = admin_client.get(f"/admin/personen/{ben.id}/loeschen")
    assert "hat schon Aufnahmen eingereicht" in page.text
    assert f'action="/admin/personen/{ben.id}/loeschen"' not in page.text

    response = admin_client.post(f"/admin/personen/{ben.id}/loeschen")
    assert "flash err" in response.text
    db_session.expire_all()
    assert db_session.get(Person, ben.id) is not None
    assert db_session.query(Beitrag).one().auftrag.person_id == ben.id


def test_delete_refused_for_admin(admin_client: TestClient, db_session: Session):
    from sqlalchemy import select

    admin = db_session.execute(select(Person).where(Person.is_admin.is_(True))).scalar_one()
    assert admin_client.get(f"/admin/personen/{admin.id}/loeschen").status_code == 404
    assert admin_client.post(f"/admin/personen/{admin.id}/loeschen").status_code == 404


def test_delete_forbidden_for_non_admin(person_client: TestClient, db_session: Session):
    other = _person(db_session, "a@example.test", "A")
    assert person_client.post(f"/admin/personen/{other.id}/loeschen").status_code == 403
    assert db_session.get(Person, other.id) is not None


def test_deleted_persons_link_and_session_never_reach_a_new_person(
    admin_client: TestClient, config: Config, db_session: Session
):
    """SQLite vergibt die hoechste id nach dem Loeschen neu. Der Link und die
    Sitzung der geloeschten Person duerfen die neu angelegte nicht oeffnen."""
    wrong = _person(db_session, "falsch@example.test", "Falsch")
    stale_client = _login(admin_client.app, config, wrong)
    stale_token = create_magic_link_token(config, wrong)
    wrong_id = wrong.id
    assert stale_client.get("/", follow_redirects=False).status_code == 200

    admin_client.post(f"/admin/personen/{wrong_id}/loeschen")
    admin_client.post(
        "/admin/persons", data={"email": "richtig@example.test", "display_name": "Richtig"}
    )
    db_session.expire_all()
    right = db_session.query(Person).filter_by(email="richtig@example.test").one()

    assert right.id != wrong_id
    assert stale_client.get("/", follow_redirects=False).headers["location"] == "/login"
    fresh = TestClient(admin_client.app)
    assert fresh.post("/login/confirm", data={"token": stale_token}).status_code == 400
