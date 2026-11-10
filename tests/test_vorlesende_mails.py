"""Advent-Anreicherung U7: Morgen-Mails an die Vorlesenden (R12-R14, R16, R20,
KTD6, KTD7, KTD9).

Test-first. Der Verlauf (`DeliveryRun`) wird direkt angelegt, die Uhr per
`get_now` gestellt, SMTP durch einen Fake ersetzt. Der Morgen-Zweig von
`/delivery/trigger` darf weder die Toniecloud noch einen Lauf anfassen.
"""

from __future__ import annotations

import logging
import re
import smtplib
from datetime import UTC, date, datetime
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from app import settings
from app.delivery.trigger import get_now, get_run_starter, get_toniecloud_factory
from app.models import (
    Auftrag,
    Beitrag,
    Campaign,
    CreativeTonie,
    DeliveryRun,
    Morgenmail,
    Person,
    Slot,
)
from tests.mailutil import html_text, plain_text
from tests.test_delivery import FixedFactory, make_client

BERLIN = ZoneInfo("Europe/Berlin")
SECRET = {"X-Trigger-Secret": "test-trigger-secret", "X-Trigger-Source": "zeitplan"}
ADRESSE = "https://advent.vorlesezeit.app"
OMA = "oma@example.test"
TITEL = "Der Stern über dem Stall"
LINK = re.compile(r"https://advent\.vorlesezeit\.app/login/confirm\?\S+")


def berlin(*args) -> datetime:
    return datetime(*args, tzinfo=BERLIN)


def utc_naive(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(tzinfo=None)


class FakeSMTP:
    sent: list = []
    fail_once_for: set[str] = set()

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def starttls(self, **kwargs) -> None:
        pass

    def login(self, *args) -> None:
        pass

    def send_message(self, message) -> None:
        to = message["To"]
        if to in FakeSMTP.fail_once_for:
            FakeSMTP.fail_once_for.discard(to)
            raise smtplib.SMTPServerDisconnected("Verbindung weg")
        FakeSMTP.sent.append(message)


def no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"Unerwarteter Toniecloud-Aufruf: {request.method} {request.url}")


def no_run(fn) -> None:
    raise AssertionError("Der Morgen-Zweig darf keinen Lauf starten.")


@pytest.fixture
def morgen(client, monkeypatch):
    FakeSMTP.sent = []
    FakeSMTP.fail_once_for = set()
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    client.app.dependency_overrides[get_toniecloud_factory] = lambda: FixedFactory(
        make_client(no_network)
    )
    client.app.dependency_overrides[get_run_starter] = lambda: no_run

    def call(at: datetime):
        client.app.dependency_overrides[get_now] = lambda: at
        return client.post("/delivery/trigger", headers=SECRET)

    yield call
    client.app.dependency_overrides.clear()


def mails_to(address: str) -> list:
    return [m for m in FakeSMTP.sent if m["To"] == address]


def family_mails() -> list:
    """Alle Mails ausser der Sammel-Abendmeldung an den Admin."""
    return [m for m in FakeSMTP.sent if m["To"] != "admin@example.test"]


def set_address(db, value: str | None = ADRESSE) -> None:
    settings.get_einstellungen(db).oeffentliche_adresse = value
    db.commit()


def make_calendar(db, name: str, kind: str | None) -> Campaign:
    campaign = Campaign(name=name, kind_name=kind)
    db.add(campaign)
    db.commit()
    return campaign


def make_tonie(db, campaign: Campaign, tonie_id: str) -> CreativeTonie:
    tonie = CreativeTonie(tonie_id=tonie_id, campaign_id=campaign.id)
    db.add(tonie)
    db.commit()
    return tonie


def make_person(db, email: str = OMA, name: str | None = "Oma Gerda") -> Person:
    person = Person(email=email, display_name=name)
    db.add(person)
    db.commit()
    return person


def make_story(
    db,
    person: Person,
    days: list[tuple[Campaign, int]],
    *,
    title: str | None = TITEL,
    uploader: Person | None = None,
) -> Beitrag:
    auftrag = Auftrag(person_id=person.id, title=title)
    db.add(auftrag)
    db.flush()
    for campaign, day in days:
        db.add(Slot(campaign_id=campaign.id, day=day, auftrag=auftrag))
    db.flush()
    beitrag = Beitrag(
        person_id=(uploader or person).id,
        auftrag=auftrag,
        title="Einreichungstitel",
        audio_object_key=f"audio/beitrag-{auftrag.id}.mp3",
        approved_at=datetime(2026, 11, 20),
    )
    db.add(beitrag)
    db.commit()
    return beitrag


def make_replacement(db, person: Person) -> Beitrag:
    beitrag = Beitrag(
        person_id=person.id,
        title="Ersatz",
        audio_object_key="audio/ersatz.mp3",
        approved_at=datetime(2026, 11, 20),
    )
    db.add(beitrag)
    db.commit()
    return beitrag


def add_run(
    db,
    tonie: CreativeTonie,
    *,
    day: int,
    outcome: str,
    beitrag: Beitrag | None,
    started: datetime,
    run_type: str = "vorabend",
) -> DeliveryRun:
    evening = started.date() if run_type in ("vorabend", "kontrolllauf") else None
    run = DeliveryRun(
        campaign_id=tonie.campaign_id,
        tonie_id=tonie.tonie_id,
        run_type=run_type,
        target_day=day,
        evening=evening,
        started_at=utc_naive(started),
        outcome=outcome,
        beitrag_ids=str(beitrag.id) if beitrag is not None else None,
    )
    db.add(run)
    db.commit()
    return run


def delivered(db, *, day: int = 5, kind: str | None = "Mila", title: str | None = TITEL):
    """Kalender mit einem Tonie, Oma Gerdas Geschichte am Tag `day`,
    erfolgreicher Vorabend-Lauf am Vorabend um 20:05."""
    campaign = make_calendar(db, "Familie", kind)
    tonie = make_tonie(db, campaign, "tonie-a")
    oma = make_person(db)
    beitrag = make_story(db, oma, [(campaign, day)], title=title)
    add_run(
        db,
        tonie,
        day=day,
        outcome="erfolg",
        beitrag=beitrag,
        started=berlin(2026, 12, day - 1, 20, 5),
    )
    return campaign, tonie, oma, beitrag


# --- Tages-Mail (R12, R16) ---------------------------------------------------


def test_heute_mail_after_successful_vorabend(morgen, db_session):
    delivered(db_session)

    response = morgen(berlin(2026, 12, 5, 7, 0))

    assert response.status_code == 200
    assert response.json() == {"gesendet": 1}
    [mail] = mails_to(OMA)
    assert mail["Subject"] == "Heute ist deine Geschichte dran"
    text = plain_text(mail)
    assert "Hallo Oma Gerda," in text
    assert (
        "heute Morgen wartet auf dem Tonie für Mila deine Geschichte „Der Stern über dem Stall“."
        in text
    )
    assert "Danke, dass du vorgelesen hast, das wird Mila freuen!" in text
    assert (
        "Ab morgen früh kannst du sie im Archiv nachhören, "
        "zusammen mit den Geschichten der anderen."
    ) in text
    assert "http" not in text
    html = html_text(mail)
    assert "Heute ist deine Geschichte dran" in html
    assert "cid:" in html


def test_heute_mail_without_kind_name_and_title(morgen, db_session):
    delivered(db_session, kind=None, title=None)

    morgen(berlin(2026, 12, 5, 7, 0))

    text = plain_text(mails_to(OMA)[0])
    assert "heute Morgen wartet auf dem Tonie deine Geschichte." in text
    assert "Danke, dass du vorgelesen hast, das wird eine Freude!" in text
    assert "für" not in text.split("Danke")[0]


def test_heute_mail_ignores_instance_kind_name_when_calendar_has_none(morgen, db_session):
    settings.get_einstellungen(db_session).kind_name = "Emma"
    db_session.commit()
    delivered(db_session, kind=None, title=None)

    morgen(berlin(2026, 12, 5, 7, 0))

    text = plain_text(mails_to(OMA)[0])
    assert "heute Morgen wartet auf dem Tonie deine Geschichte." in text
    assert "Emma" not in text


def test_mirrored_tonies_and_two_calendars_send_exactly_one_mail_ae8(morgen, db_session):
    mila = make_calendar(db_session, "Familie", "Mila")
    emma = make_calendar(db_session, "Großeltern", "Emma")
    t1 = make_tonie(db_session, mila, "tonie-a")
    t2 = make_tonie(db_session, mila, "tonie-b")
    t3 = make_tonie(db_session, emma, "tonie-c")
    oma = make_person(db_session)
    beitrag = make_story(db_session, oma, [(mila, 12), (emma, 12)])
    for tonie in (t1, t2, t3):
        add_run(
            db_session,
            tonie,
            day=12,
            outcome="erfolg",
            beitrag=beitrag,
            started=berlin(2026, 12, 11, 20, 5),
        )

    response = morgen(berlin(2026, 12, 12, 7, 0))

    assert response.json() == {"gesendet": 1}
    [mail] = mails_to(OMA)
    assert "auf dem Tonie für Mila und Emma deine Geschichte" in plain_text(mail)


def test_kind_names_only_from_calendars_with_success(morgen, db_session):
    mila = make_calendar(db_session, "Familie", "Mila")
    emma = make_calendar(db_session, "Großeltern", "Emma")
    t1 = make_tonie(db_session, mila, "tonie-a")
    t2 = make_tonie(db_session, emma, "tonie-b")
    oma = make_person(db_session)
    beitrag = make_story(db_session, oma, [(mila, 12), (emma, 12)])
    ersatz = make_replacement(db_session, oma)
    add_run(
        db_session,
        t1,
        day=12,
        outcome="erfolg",
        beitrag=beitrag,
        started=berlin(2026, 12, 11, 20, 5),
    )
    add_run(
        db_session,
        t2,
        day=12,
        outcome="ersatzbeitrag",
        beitrag=ersatz,
        started=berlin(2026, 12, 11, 20, 5),
    )

    morgen(berlin(2026, 12, 12, 7, 0))

    text = plain_text(mails_to(OMA)[0])
    assert "auf dem Tonie für Mila deine Geschichte" in text
    assert "Emma" not in text


def test_revoked_person_gets_no_mail_ae8(morgen, db_session):
    _, _, oma, _ = delivered(db_session)
    oma.revoked_at = datetime(2026, 11, 30, 12, 0)
    db_session.commit()
    set_address(db_session)

    first = morgen(berlin(2026, 12, 5, 7, 0))
    morgen(berlin(2026, 12, 6, 7, 0))

    assert first.status_code == 204
    assert mails_to(OMA) == []


def test_replacement_without_later_success_sends_no_mail_ae9(morgen, db_session):
    campaign = make_calendar(db_session, "Familie", "Mila")
    tonie = make_tonie(db_session, campaign, "tonie-a")
    opa = make_person(db_session, "opa@example.test", "Opa")
    make_story(db_session, opa, [(campaign, 15)])
    ersatz = make_replacement(db_session, opa)
    add_run(
        db_session,
        tonie,
        day=15,
        outcome="ersatzbeitrag",
        beitrag=ersatz,
        started=berlin(2026, 12, 14, 20, 5),
    )
    set_address(db_session)

    morgen(berlin(2026, 12, 15, 7, 0))
    morgen(berlin(2026, 12, 16, 7, 0))

    assert mails_to("opa@example.test") == []


def test_control_run_repairing_day_sends_heute_mail(morgen, db_session):
    campaign = make_calendar(db_session, "Familie", "Mila")
    tonie = make_tonie(db_session, campaign, "tonie-a")
    oma = make_person(db_session)
    beitrag = make_story(db_session, oma, [(campaign, 15)])
    ersatz = make_replacement(db_session, oma)
    add_run(
        db_session,
        tonie,
        day=15,
        outcome="ersatzbeitrag",
        beitrag=ersatz,
        started=berlin(2026, 12, 14, 20, 5),
    )
    add_run(
        db_session,
        tonie,
        day=15,
        outcome="erfolg",
        beitrag=beitrag,
        started=berlin(2026, 12, 14, 22, 5),
        run_type="kontrolllauf",
    )

    morgen(berlin(2026, 12, 15, 7, 0))

    assert len(mails_to(OMA)) == 1


def test_later_replacement_cancels_heute_mail(morgen, db_session):
    _, tonie, oma, _ = delivered(db_session, day=15)
    ersatz = make_replacement(db_session, oma)
    add_run(
        db_session,
        tonie,
        day=15,
        outcome="ersatzbeitrag",
        beitrag=ersatz,
        started=berlin(2026, 12, 14, 22, 5),
        run_type="kontrolllauf",
    )

    morgen(berlin(2026, 12, 15, 7, 0))

    assert mails_to(OMA) == []


def test_skipped_run_does_not_count_as_last_run(morgen, db_session):
    _, tonie, oma, _ = delivered(db_session, day=15)
    add_run(
        db_session,
        tonie,
        day=15,
        outcome="uebersprungen",
        beitrag=None,
        started=berlin(2026, 12, 14, 22, 5),
        run_type="kontrolllauf",
    )

    morgen(berlin(2026, 12, 15, 7, 0))

    assert len(mails_to(OMA)) == 1


def test_admin_upload_mails_the_assigned_person(morgen, db_session):
    admin = db_session.execute(select(Person).where(Person.is_admin.is_(True))).scalar_one()
    campaign = make_calendar(db_session, "Familie", "Mila")
    tonie = make_tonie(db_session, campaign, "tonie-a")
    oma = make_person(db_session)
    beitrag = make_story(db_session, oma, [(campaign, 5)], uploader=admin)
    add_run(
        db_session,
        tonie,
        day=5,
        outcome="erfolg",
        beitrag=beitrag,
        started=berlin(2026, 12, 4, 20, 5),
    )

    morgen(berlin(2026, 12, 5, 7, 0))

    assert len(mails_to(OMA)) == 1
    assert family_mails() == mails_to(OMA)


def test_anstoss_days_ahead_sends_no_mail(morgen, db_session):
    campaign = make_calendar(db_session, "Familie", "Mila")
    tonie = make_tonie(db_session, campaign, "tonie-a")
    oma = make_person(db_session)
    beitrag = make_story(db_session, oma, [(campaign, 8)])
    add_run(
        db_session,
        tonie,
        day=8,
        outcome="erfolg",
        beitrag=beitrag,
        started=berlin(2026, 12, 3, 9, 0),
        run_type="anstoss",
    )

    morgen(berlin(2026, 12, 3, 10, 0))
    morgen(berlin(2026, 12, 8, 7, 0))

    assert mails_to(OMA) == []


def test_anstoss_on_the_evening_before_counts(morgen, db_session):
    campaign = make_calendar(db_session, "Familie", "Mila")
    tonie = make_tonie(db_session, campaign, "tonie-a")
    oma = make_person(db_session)
    beitrag = make_story(db_session, oma, [(campaign, 8)])
    add_run(
        db_session,
        tonie,
        day=8,
        outcome="erfolg",
        beitrag=beitrag,
        started=berlin(2026, 12, 7, 18, 0),
        run_type="anstoss",
    )

    morgen(berlin(2026, 12, 8, 7, 0))

    assert len(mails_to(OMA)) == 1


# --- Archiv-Mail (R13, R20, KTD9) --------------------------------------------


def test_archiv_mail_next_morning_with_login_link_to_archive(morgen, client, config, db_session):
    _, _, oma, _ = delivered(db_session)
    set_address(db_session)
    morgen(berlin(2026, 12, 5, 7, 0))
    FakeSMTP.sent = []

    response = morgen(berlin(2026, 12, 6, 7, 0))

    assert response.status_code == 200
    [mail] = mails_to(OMA)
    assert mail["Subject"] == "Deine Geschichte ist jetzt im Archiv"
    text = plain_text(mail)
    assert "Hallo Oma Gerda," in text
    assert (
        "gestern war deine Geschichte „Der Stern über dem Stall“ auf dem Tonie für Mila. "
        "Ab heute kannst du sie im Archiv nachhören, zusammen mit allen Geschichten, "
        "die schon dran waren."
    ) in text
    assert "Über diesen Link kommst du direkt ins Archiv:" in text
    assert "Danke, dass du dabei bist!" in text
    html = html_text(mail)
    assert "Jetzt im Archiv" in html
    assert "Zum Archiv" in html

    link = LINK.search(text).group(0)
    query = parse_qs(urlsplit(link).query)
    assert urlsplit(link).path == "/login/confirm"
    assert query["next"] == ["/archiv"]
    login = client.post(
        "/login/confirm",
        data={"token": query["token"][0], "next": query["next"][0]},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert login.headers["location"] == "/archiv"
    assert client.get("/archiv").status_code == 200


def test_archiv_mail_only_on_first_archive_day(morgen, db_session):
    mila = make_calendar(db_session, "Familie", "Mila")
    emma = make_calendar(db_session, "Großeltern", "Emma")
    t1 = make_tonie(db_session, mila, "tonie-a")
    t2 = make_tonie(db_session, emma, "tonie-b")
    oma = make_person(db_session)
    beitrag = make_story(db_session, oma, [(mila, 5), (emma, 12)])
    add_run(
        db_session, t1, day=5, outcome="erfolg", beitrag=beitrag, started=berlin(2026, 12, 4, 20, 5)
    )
    add_run(
        db_session,
        t2,
        day=12,
        outcome="erfolg",
        beitrag=beitrag,
        started=berlin(2026, 12, 11, 20, 5),
    )
    set_address(db_session)

    morgen(berlin(2026, 12, 6, 7, 0))
    archiv_6 = [m for m in mails_to(OMA) if "Archiv" in m["Subject"]]
    FakeSMTP.sent = []
    morgen(berlin(2026, 12, 12, 7, 0))
    heute_12 = mails_to(OMA)
    FakeSMTP.sent = []
    morgen(berlin(2026, 12, 13, 7, 0))

    assert len(archiv_6) == 1
    assert [m["Subject"] for m in heute_12] == ["Heute ist deine Geschichte dran"]
    assert mails_to(OMA) == []


def test_christmas_morning_sends_archiv_for_day_24_only(morgen, db_session):
    delivered(db_session, day=24)
    set_address(db_session)

    morgen(berlin(2026, 12, 25, 7, 0))

    assert [m["Subject"] for m in mails_to(OMA)] == ["Deine Geschichte ist jetzt im Archiv"]


def test_without_public_address_no_archiv_mail_but_heute_mail(morgen, db_session, caplog):
    campaign, tonie, oma, _ = delivered(db_session, day=5)
    set_address(db_session, None)
    opa = make_person(db_session, "opa@example.test", "Opa")
    beitrag_opa = make_story(db_session, opa, [(campaign, 6)])
    add_run(
        db_session,
        tonie,
        day=6,
        outcome="erfolg",
        beitrag=beitrag_opa,
        started=berlin(2026, 12, 5, 20, 5),
    )
    caplog.set_level(logging.INFO)

    morgen(berlin(2026, 12, 6, 7, 0))

    assert mails_to(OMA) == []
    assert [m["Subject"] for m in mails_to("opa@example.test")] == [
        "Heute ist deine Geschichte dran"
    ]
    lines = [r.getMessage() for r in caplog.records if "Archiv-Mail entfällt" in r.getMessage()]
    assert lines
    assert any(f"person={oma.id}" in line for line in lines)
    assert all("@" not in line and "http" not in line for line in lines)


# --- Zeitfenster, Einmaligkeit, Fehler (KTD6, R14) ---------------------------


@pytest.mark.parametrize(
    ("at", "sends"),
    [
        (berlin(2026, 12, 5, 6, 55), False),
        (berlin(2026, 12, 5, 7, 0), True),
        (berlin(2026, 12, 5, 10, 55), True),
        (berlin(2026, 12, 5, 11, 5), False),
    ],
)
def test_morning_window(morgen, db_session, at, sends):
    delivered(db_session)

    morgen(at)

    assert (len(mails_to(OMA)) == 1) is sends


def test_november_sends_nothing(morgen, db_session):
    campaign = make_calendar(db_session, "Familie", "Mila")
    tonie = make_tonie(db_session, campaign, "tonie-a")
    oma = make_person(db_session)
    beitrag = make_story(db_session, oma, [(campaign, 20)])
    add_run(
        db_session,
        tonie,
        day=20,
        outcome="erfolg",
        beitrag=beitrag,
        started=berlin(2026, 11, 19, 20, 5),
        run_type="anstoss",
    )
    set_address(db_session)

    response = morgen(berlin(2026, 11, 20, 7, 0))

    assert response.status_code == 204
    assert family_mails() == []


def test_second_tick_sends_no_duplicate(morgen, db_session):
    delivered(db_session)

    morgen(berlin(2026, 12, 5, 7, 0))
    second = morgen(berlin(2026, 12, 5, 7, 5))

    assert second.status_code == 204
    assert len(mails_to(OMA)) == 1


def test_existing_marker_from_parallel_tick_blocks_mail(morgen, db_session):
    _, _, oma, _ = delivered(db_session)
    db_session.add(
        Morgenmail(
            person_id=oma.id,
            mailart="heute",
            tag=date(2026, 12, 5),
            sent_at=datetime(2026, 12, 5, 6, 0),
        )
    )
    db_session.commit()

    morgen(berlin(2026, 12, 5, 7, 0))

    assert mails_to(OMA) == []


def test_marker_records_person_mailart_and_advent_date(morgen, db_session):
    _, _, oma, _ = delivered(db_session)
    set_address(db_session)

    morgen(berlin(2026, 12, 5, 7, 0))
    morgen(berlin(2026, 12, 6, 7, 0))

    db_session.expire_all()
    rows = db_session.execute(
        select(Morgenmail.person_id, Morgenmail.mailart, Morgenmail.tag).order_by(Morgenmail.id)
    ).all()
    assert [tuple(r) for r in rows] == [
        (oma.id, "heute", date(2026, 12, 5)),
        (oma.id, "archiv", date(2026, 12, 5)),
    ]


def test_smtp_failure_removes_marker_and_next_tick_retries(morgen, db_session, caplog):
    _, _, oma, _ = delivered(db_session)
    FakeSMTP.fail_once_for = {OMA}
    caplog.set_level(logging.INFO)

    first = morgen(berlin(2026, 12, 5, 7, 0))

    assert first.status_code == 204
    assert mails_to(OMA) == []
    db_session.expire_all()
    assert db_session.execute(select(Morgenmail)).first() is None
    lines = [
        r.getMessage() for r in caplog.records if "Morgen-Mail fehlgeschlagen" in r.getMessage()
    ]
    assert lines == [
        f"Morgen-Mail fehlgeschlagen: person={oma.id} mailart=heute fehler=SMTPServerDisconnected"
    ]

    second = morgen(berlin(2026, 12, 5, 7, 5))
    third = morgen(berlin(2026, 12, 5, 7, 10))

    assert second.status_code == 200
    assert third.status_code == 204
    assert len(mails_to(OMA)) == 1


# --- Anmeldeziel /archiv (app/auth/routes.py) --------------------------------


@pytest.mark.parametrize(
    ("next_path", "expected"),
    [
        ("/archiv", "/archiv"),
        ("/archiv/", "/"),
        ("/archiv/x", "/"),
        ("/archiv?x=1", "/"),
        ("//evil.example/archiv", "/"),
        ("https://evil.example/archiv", "/"),
    ],
)
def test_login_next_allows_exactly_archiv(client, config, db_session, next_path, expected):
    from app.auth.tokens import create_magic_link_token

    person = make_person(db_session)
    token = create_magic_link_token(config, person)

    page = client.get("/login/confirm", params={"token": token, "next": next_path})
    assert page.status_code == 200
    assert ('name="next" value="/archiv"' in page.text) is (expected == "/archiv")
    response = client.post(
        "/login/confirm", data={"token": token, "next": next_path}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == expected
