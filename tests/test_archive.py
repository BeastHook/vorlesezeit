"""U9: Familienarchiv -- tagweise Sichtbarkeit, serverseitig gefiltert.
Gegen echtes MinIO (Audio ueber die App mit Bereichsanfragen, Loeschen
nach R10).

- Covers AE8, AE11, R22, R23, R28, R34, R37, R40 (Archivseite).
- Verification-Zeile aus U9: derselbe Beitrag wird mit verschobener
  Zeitbasis vor und nach seiner Tagesgrenze abgerufen.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin.setup import create_campaign, create_person
from app.auth.tokens import create_magic_link_token
from app.config import Config
from app.models import Auftrag, Beitrag, Campaign, Person, Slot
from tests.fixtures.audio import make_tone_mp3, make_tone_webm

BERLIN = ZoneInfo("Europe/Berlin")


def _at(month: int, day: int, hour: int = 12, minute: int = 0, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=BERLIN)


def _freeze(monkeypatch, moment: datetime) -> None:
    monkeypatch.setattr("app.archive.views.berlin_now", lambda request: moment)


def _slot(db: Session, day: int, calendar: str = "Familie") -> Slot:
    return db.execute(
        select(Slot).join(Campaign).where(Slot.day == day, Campaign.name == calendar)
    ).scalar_one()


def _calendar(db: Session, name: str) -> Campaign:
    """Ein weiterer Kalender mit 24 Tagen (create_campaign kennt nur einen)."""
    campaign = Campaign(name=name)
    db.add(campaign)
    db.flush()
    db.add_all(Slot(campaign_id=campaign.id, day=day) for day in range(1, 25))
    db.commit()
    return campaign


def _auftrag_at(db: Session, person: Person, *slots: Slot, title: str | None = None) -> Auftrag:
    auftrag = Auftrag(person_id=person.id, title=title)
    db.add(auftrag)
    for slot in slots:
        slot.auftrag = auftrag
    db.commit()
    return auftrag


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _beitrag(client: TestClient, db: Session, person: Person, slot: Slot | None, **kw) -> Beitrag:
    """Mit `slot` haengt der Beitrag am Auftrag dieses Kalendertags (angelegt
    fuer `person`, falls der Tag noch frei ist)."""
    key = f"beitraege/{person.id}/{uuid.uuid4().hex}.mp3"
    client.app.state.storage.put(key, make_tone_mp3(1.0), content_type="audio/mpeg")
    auftrag = None
    if slot is not None:
        auftrag = slot.auftrag or _auftrag_at(db, person, slot)
    b = Beitrag(person_id=person.id, auftrag=auftrag, audio_object_key=key, **kw)
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


def _login(client: TestClient, config: Config, person: Person) -> TestClient:
    other = TestClient(client.app)
    other.post("/login/confirm", data={"token": create_magic_link_token(config, person)})
    return other


@pytest.fixture
def family(client: TestClient, db_session: Session):
    create_campaign(db_session)
    ruth = create_person(db_session, email="ruth@example.test", display_name="Ruth")
    miri = create_person(db_session, email="miri@example.test", display_name="Miri")
    # R32: Ruth gehoert ueber einen eigenen Auftrag zum Kalender "Familie".
    _auftrag_at(db_session, ruth, _slot(db_session, 24))
    return ruth, miri


def test_foreign_beitrag_hidden_until_midnight_after_its_day_ae8(
    client: TestClient, config: Config, db_session: Session, family, monkeypatch
):
    """AE8 + Verification: am 5.12. um 23:59 ist Miris Geschichte zum 5.
    noch verschlossen, Ruths eigene (spaetere) Aufnahme aber sofort da."""
    ruth, miri = family
    foreign = _beitrag(
        client,
        db_session,
        miri,
        _slot(db_session, 5),
        title="Sterne zählen",
        approved_at=_utcnow(),
    )
    _beitrag(client, db_session, ruth, _slot(db_session, 17), title="Die Wichtelwerkstatt")
    ruth_client = _login(client, config, ruth)
    _freeze(monkeypatch, _at(12, 5, 23, 59))

    page = ruth_client.get("/archiv").text

    assert "Die Wichtelwerkstatt" in page
    assert "Sterne zählen" not in page
    assert "Miri" not in page
    assert "öffnet sich am 6. Dezember" in page
    audio = ruth_client.get(f"/archiv/{foreign.id}/audio", follow_redirects=False)
    assert audio.status_code == 404
    # AE11: kein Byte Audio, auch nicht ueber eine Bereichsanfrage.
    payload = client.app.state.storage.get(foreign.audio_object_key)
    assert payload[:16] not in audio.content
    ranged = ruth_client.get(f"/archiv/{foreign.id}/audio", headers={"Range": "bytes=0-1"})
    assert ranged.status_code == 404
    assert "content-range" not in ranged.headers


def test_same_foreign_beitrag_visible_from_midnight(
    client: TestClient, config: Config, db_session: Session, family, monkeypatch
):
    """R22/R23 + Verification: ab 6.12., 0:00 ist sie hoer- und abspielbar."""
    ruth, miri = family
    foreign = _beitrag(
        client,
        db_session,
        miri,
        _slot(db_session, 5),
        title="Sterne zählen",
        approved_at=_utcnow(),
    )
    ruth_client = _login(client, config, ruth)
    _freeze(monkeypatch, _at(12, 6, 0, 0))

    page = ruth_client.get("/archiv").text

    assert "Sterne zählen" in page
    assert "Gesprochen von Miri" in page
    assert f'src="/archiv/{foreign.id}/audio"' in page
    audio = ruth_client.get(f"/archiv/{foreign.id}/audio", follow_redirects=False)
    assert audio.status_code == 200
    assert audio.content == client.app.state.storage.get(foreign.audio_object_key)


def test_free_submission_only_visible_to_its_author(
    client: TestClient, config: Config, db_session: Session, family, monkeypatch
):
    ruth, miri = family
    free = _beitrag(client, db_session, miri, None, title="Grüße an Paul")
    _freeze(monkeypatch, _at(1, 10, year=2027))

    ruth_page = _login(client, config, ruth).get("/archiv").text
    assert "Grüße an Paul" not in ruth_page
    ruth_audio = _login(client, config, ruth).get(
        f"/archiv/{free.id}/audio", follow_redirects=False
    )
    assert ruth_audio.status_code == 404

    miri_client = _login(client, config, miri)
    assert "Grüße an Paul" in miri_client.get("/archiv").text
    assert miri_client.get(f"/archiv/{free.id}/audio", follow_redirects=False).status_code == 200


def test_foreign_unapproved_rejected_or_detached_never_visible(
    client: TestClient, config: Config, db_session: Session, family, monkeypatch
):
    ruth, miri = family
    pending = _beitrag(client, db_session, miri, _slot(db_session, 1), title="Noch offen")
    rejected = _beitrag(
        client,
        db_session,
        miri,
        _slot(db_session, 2),
        title="Abgelehnt",
        approved_at=None,
        rejected_at=_utcnow(),
    )
    detached = _beitrag(
        client,
        db_session,
        miri,
        None,
        title="Gelöst",
        approved_at=_utcnow(),
        detached_at=_utcnow(),
    )
    _freeze(monkeypatch, _at(1, 10, year=2027))
    ruth_client = _login(client, config, ruth)

    page = ruth_client.get("/archiv").text

    for beitrag in (pending, rejected, detached):
        assert beitrag.title not in page
        response = ruth_client.get(f"/archiv/{beitrag.id}/audio", follow_redirects=False)
        assert response.status_code == 404


def test_admin_sees_same_family_view(
    admin_client: TestClient, db_session: Session, family, monkeypatch
):
    """Serverseitig gefiltert, auch fuer den Admin -- er hoert alles in der
    Admin-Flaeche, das Archiv ist die Familienansicht."""
    _, miri = family
    _beitrag(
        admin_client,
        db_session,
        miri,
        _slot(db_session, 5),
        title="Sterne zählen",
        approved_at=_utcnow(),
    )
    _freeze(monkeypatch, _at(12, 5, 20, 0))

    assert "Sterne zählen" not in admin_client.get("/archiv").text


def test_own_rejected_recording_offers_rerecording_without_player(
    client: TestClient, config: Config, db_session: Session, family, monkeypatch
):
    ruth, _ = family
    slot = _slot(db_session, 6)
    rejected = _beitrag(client, db_session, ruth, slot, title="Der Nikolaus", rejected_at=_utcnow())
    _freeze(monkeypatch, _at(11, 20))

    page = _login(client, config, ruth).get("/archiv").text

    assert "Der Nikolaus" in page
    assert "abgelehnt, bitte neu aufnehmen" in page
    assert f'href="/record/auftrag/{slot.auftrag_id}"' in page
    # R10: keine Tuerchennummer bei den eigenen Aufnahmen.
    assert "6. Dezember" not in page
    assert f'src="/archiv/{rejected.id}/audio"' not in page


def test_own_detached_recording_stays_with_its_author_r35(
    client: TestClient, config: Config, db_session: Session, family, monkeypatch
):
    ruth, _ = family
    detached = _beitrag(
        client, db_session, ruth, None, title="Früher Versuch", detached_at=_utcnow()
    )
    _freeze(monkeypatch, _at(11, 20))
    ruth_client = _login(client, config, ruth)

    page = ruth_client.get("/archiv").text

    assert "Früher Versuch" in page
    assert "Deine Geschichte wurde neu vergeben" in page
    assert (
        ruth_client.get(f"/archiv/{detached.id}/audio", follow_redirects=False).status_code == 200
    )


def test_before_advent_family_section_names_first_opening(
    client: TestClient, config: Config, db_session: Session, family, monkeypatch
):
    ruth, _ = family
    _freeze(monkeypatch, _at(11, 20))

    page = _login(client, config, ruth).get("/archiv").text

    assert "Die erste Geschichte öffnet sich am 2. Dezember." in page


def test_replaced_recording_gone_from_archive_and_storage_r10(
    person_client: TestClient, db_session: Session, monkeypatch
):
    """U9-Testzeile: eine nach R10 ersetzte Aufnahme ist weder ueber die
    Audio-Route noch im Archiv erreichbar."""
    import botocore.exceptions

    create_campaign(db_session)
    person = db_session.execute(
        select(Person).where(Person.email == "verwandte@example.test")
    ).scalar_one()
    auftrag = _auftrag_at(db_session, person, _slot(db_session, 3))
    _freeze(monkeypatch, _at(11, 20))

    for title in ("Erster Versuch", "Zweiter Versuch"):
        response = person_client.post(
            f"/record/auftrag/{auftrag.id}",
            files={"audio": ("a.webm", make_tone_webm(seconds=1.0), "audio/webm")},
            data={"title": title, "reported_type": "audio/webm"},
        )
        assert response.status_code == 200
        if title == "Erster Versuch":
            db_session.expire_all()
            old_key = db_session.execute(
                select(Beitrag.audio_object_key).where(Beitrag.auftrag_id == auftrag.id)
            ).scalar_one()

    db_session.expire_all()
    beitrag = db_session.execute(
        select(Beitrag).where(Beitrag.auftrag_id == auftrag.id)
    ).scalar_one()
    page = person_client.get("/archiv").text
    assert "Zweiter Versuch" in page
    assert "Erster Versuch" not in page
    audio = person_client.get(f"/archiv/{beitrag.id}/audio", follow_redirects=False)
    assert audio.status_code == 200
    assert audio.content == person_client.app.state.storage.get(beitrag.audio_object_key)
    with pytest.raises(botocore.exceptions.ClientError):
        person_client.app.state.storage.get(old_key)


def test_archive_requires_login(client: TestClient):
    response = client.get("/archiv", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("/login")


def test_navigation_reaches_all_three_areas_and_marks_current_r34(
    person_client: TestClient, monkeypatch
):
    _freeze(monkeypatch, _at(11, 20))
    for path, current in (
        ("/archiv", "Archiv"),
        ("/record/free", "Freie Nachricht"),
        ("/record/campaign", "Meine Geschichten"),
    ):
        html = person_client.get(path).text
        for href in ('href="/record/campaign"', 'href="/record/free"', 'href="/archiv"'):
            assert href in html, (path, href)
        assert f'aria-current="page">{current}</a>' in html, path


def test_confirmation_leads_to_archive_r37(
    person_client: TestClient, db_session: Session, monkeypatch
):
    """R37: nach der Einreichung weiter zum naechsten offenen Tuerchen oder,
    wenn keins offen ist, ins Archiv."""
    create_campaign(db_session)
    person = db_session.execute(
        select(Person).where(Person.email == "verwandte@example.test")
    ).scalar_one()
    first = _auftrag_at(db_session, person, _slot(db_session, 3))
    second = _auftrag_at(db_session, person, _slot(db_session, 9))

    def submit(auftrag: Auftrag) -> str:
        person_client.post(
            f"/record/auftrag/{auftrag.id}",
            files={"audio": ("a.webm", make_tone_webm(seconds=1.0), "audio/webm")},
            data={"title": "Probe", "reported_type": "audio/webm"},
        )
        return person_client.get(f"/record/auftrag/{auftrag.id}/confirmed").text

    with_next = submit(first)
    assert f'href="/record/auftrag/{second.id}"' in with_next
    assert 'href="/archiv">Später, ins Archiv</a>' in with_next

    last = submit(second)
    assert 'href="/archiv">Ins Archiv</a>' in last


def test_list_and_audio_route_agree_for_every_state(
    client: TestClient, config: Config, db_session: Session, family, monkeypatch
):
    """Review-Befund U9 #2: die Regel steht an einer Stelle -- Liste und
    Audio-Route kommen fuer jede Kombination zum selben Ergebnis.
    Mehrkalender U12: zusaetzlich Ruths Mitgliedschaft im Kalender (R32)."""
    import itertools

    ruth, miri = family
    _calendar(db_session, "Patenkinder")  # Ruth hat dort keinen Auftrag
    next_day = {"Familie": 1, "Patenkinder": 1}
    cases = []
    for n, (approved, rejected, detached, in_slot, member) in enumerate(
        itertools.product((True, False), repeat=5), start=1
    ):
        slot = None
        if in_slot:
            calendar = "Familie" if member else "Patenkinder"
            slot = _slot(db_session, next_day[calendar], calendar)
            next_day[calendar] += 1
        beitrag = _beitrag(
            client,
            db_session,
            miri,
            slot,
            title=f"Fall {n:02d}",
            approved_at=_utcnow() if approved else None,
            rejected_at=_utcnow() if rejected else None,
            detached_at=_utcnow() if detached else None,
        )
        expected = (
            approved and not rejected and not detached and in_slot and member and slot.day < 9
        )
        cases.append((beitrag, expected))
    _freeze(monkeypatch, _at(12, 9))  # Tage 1-8 offen, ab 9 noch zu
    ruth_client = _login(client, config, ruth)

    page = ruth_client.get("/archiv").text
    for b, expected in cases:
        listed = b.title in page
        audio = ruth_client.get(f"/archiv/{b.id}/audio", follow_redirects=False).status_code
        assert listed == (audio == 200), (b.title, listed, audio)
        assert listed == expected, (b.title, listed)
    assert sum(expected for _, expected in cases) == 1


# --- Mehrkalender U12: Sichtbarkeit je Kalender (R32) ----------------------


def test_grandmother_only_in_a_sees_a_but_nothing_only_in_b_ae7(
    client: TestClient, config: Config, db_session: Session, family, monkeypatch
):
    ruth, miri = family
    _calendar(db_session, "Patenkinder")
    in_a = _beitrag(
        client, db_session, miri, _slot(db_session, 5), title="Aus A", approved_at=_utcnow()
    )
    only_b = _beitrag(
        client,
        db_session,
        miri,
        _slot(db_session, 3, "Patenkinder"),
        title="Nur aus B",
        approved_at=_utcnow(),
    )
    _freeze(monkeypatch, _at(12, 7))
    ruth_client = _login(client, config, ruth)

    page = ruth_client.get("/archiv").text

    assert "Aus A" in page
    assert "Nur aus B" not in page
    assert ruth_client.get(f"/archiv/{in_a.id}/audio").status_code == 200
    assert ruth_client.get(f"/archiv/{only_b.id}/audio").status_code == 404


def test_story_in_two_calendars_appears_once_from_earliest_day(
    client: TestClient, config: Config, db_session: Session, family, monkeypatch
):
    ruth, miri = family
    _calendar(db_session, "Patenkinder")
    _auftrag_at(db_session, ruth, _slot(db_session, 20, "Patenkinder"))
    shared = _auftrag_at(
        db_session,
        miri,
        _slot(db_session, 12, "Familie"),
        _slot(db_session, 5, "Patenkinder"),
        title="Sterne zählen",
    )
    beitrag = Beitrag(
        person_id=miri.id, auftrag=shared, audio_object_key="x", approved_at=_utcnow()
    )
    db_session.add(beitrag)
    db_session.commit()
    ruth_client = _login(client, config, ruth)

    _freeze(monkeypatch, _at(12, 5, 23, 59))
    assert "Sterne zählen" not in ruth_client.get("/archiv").text

    _freeze(monkeypatch, _at(12, 6, 0, 0))
    page = ruth_client.get("/archiv").text
    assert page.count("Sterne zählen") == 1
    assert '<span class="ziffer">5</span>' in page


def test_shared_story_opens_per_own_calendars_day(
    client: TestClient, config: Config, db_session: Session, family, monkeypatch
):
    """Wer nur im Kalender mit dem spaeteren Tag ist, hoert sie erst danach."""
    ruth, miri = family  # Ruth nur in "Familie"
    _calendar(db_session, "Patenkinder")
    shared = _auftrag_at(
        db_session,
        miri,
        _slot(db_session, 12, "Familie"),
        _slot(db_session, 5, "Patenkinder"),
        title="Sterne zählen",
    )
    beitrag = _beitrag(client, db_session, miri, None, approved_at=_utcnow())
    beitrag.auftrag = shared
    db_session.commit()
    ruth_client = _login(client, config, ruth)

    _freeze(monkeypatch, _at(12, 12, 23, 0))
    assert "Sterne zählen" not in ruth_client.get("/archiv").text
    assert ruth_client.get(f"/archiv/{beitrag.id}/audio").status_code == 404

    _freeze(monkeypatch, _at(12, 13, 0, 0))
    assert "Sterne zählen" in ruth_client.get("/archiv").text
    assert ruth_client.get(f"/archiv/{beitrag.id}/audio").status_code == 200


def test_reassignment_ends_calendar_membership(
    client: TestClient, config: Config, db_session: Session, family, monkeypatch
):
    from datetime import time

    from app.admin.setup import reassign_auftrag

    ruth, miri = family
    foreign = _beitrag(
        client, db_session, miri, _slot(db_session, 5), title="Aus A", approved_at=_utcnow()
    )
    ruth_client = _login(client, config, ruth)
    _freeze(monkeypatch, _at(12, 7))
    assert "Aus A" in ruth_client.get("/archiv").text

    ruths = _slot(db_session, 24).auftrag
    reassign_auftrag(db_session, ruths.id, miri.id, now=_at(11, 20), delivery_time=time(20, 0))
    db_session.commit()

    assert "Aus A" not in ruth_client.get("/archiv").text
    assert ruth_client.get(f"/archiv/{foreign.id}/audio").status_code == 404


def test_draft_auftrag_gives_no_membership_r36(
    client: TestClient, config: Config, db_session: Session, family, monkeypatch
):
    ruth, miri = family
    _calendar(db_session, "Patenkinder")
    _auftrag_at(db_session, ruth)  # Entwurf ohne Kalendertag
    _beitrag(
        client,
        db_session,
        miri,
        _slot(db_session, 3, "Patenkinder"),
        title="Nur aus B",
        approved_at=_utcnow(),
    )
    _freeze(monkeypatch, _at(12, 7))

    assert "Nur aus B" not in _login(client, config, ruth).get("/archiv").text


def test_admin_sees_all_calendars_r32(
    admin_client: TestClient, db_session: Session, family, monkeypatch
):
    _, miri = family
    _calendar(db_session, "Patenkinder")
    b = _beitrag(
        admin_client,
        db_session,
        miri,
        _slot(db_session, 3, "Patenkinder"),
        title="Nur aus B",
        approved_at=_utcnow(),
    )
    _freeze(monkeypatch, _at(12, 7))

    assert "Nur aus B" in admin_client.get("/archiv").text
    assert admin_client.get(f"/archiv/{b.id}/audio").status_code == 200


def test_fallback_title_has_no_door_number(
    client: TestClient, config: Config, db_session: Session, family, monkeypatch
):
    ruth, miri = family
    _beitrag(client, db_session, miri, _slot(db_session, 5), approved_at=_utcnow())
    _freeze(monkeypatch, _at(12, 7))

    page = _login(client, config, ruth).get("/archiv").text

    assert "Gesprochen von Miri" in page
    assert "Türchen 5" not in page


def test_archive_uses_numbered_doors_and_own_seal(
    client: TestClient, config: Config, db_session: Session, family, monkeypatch
):
    ruth, miri = family
    _beitrag(
        client,
        db_session,
        miri,
        _slot(db_session, 5),
        title="Sterne zählen",
        approved_at=_utcnow(),
    )
    _beitrag(client, db_session, ruth, _slot(db_session, 4), title="Eigene")
    _freeze(monkeypatch, _at(12, 7))

    html = _login(client, config, ruth).get("/archiv").text

    assert 'class="tuerchen is-offen' in html
    assert '<span class="ziffer">5</span>' in html
    assert 'class="siegel"' in html


# --- U13: Audio ueber die App (R28, KTD17) ---------------------------------


@pytest.fixture
def own_audio(client: TestClient, config: Config, db_session: Session, family, monkeypatch):
    ruth, _ = family
    beitrag = _beitrag(client, db_session, ruth, _slot(db_session, 4), title="Eigene")
    _freeze(monkeypatch, _at(11, 20))
    payload = client.app.state.storage.get(beitrag.audio_object_key)
    return _login(client, config, ruth), f"/archiv/{beitrag.id}/audio", payload


def test_own_audio_without_range_is_full_mp3(own_audio):
    ruth_client, url, payload = own_audio

    response = ruth_client.get(url, follow_redirects=False)

    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cache-control"].startswith("private")


def test_range_first_two_bytes_is_partial(own_audio):
    ruth_client, url, payload = own_audio

    response = ruth_client.get(url, headers={"Range": "bytes=0-1"})

    assert response.status_code == 206
    assert response.content == payload[:2]
    assert response.headers["content-range"] == f"bytes 0-1/{len(payload)}"
    assert response.headers["accept-ranges"] == "bytes"


def test_open_range_runs_to_end_of_file(own_audio):
    ruth_client, url, payload = own_audio
    assert len(payload) > 1000

    response = ruth_client.get(url, headers={"Range": "bytes=1000-"})

    assert response.status_code == 206
    assert response.content == payload[1000:]
    assert response.headers["content-range"] == f"bytes 1000-{len(payload) - 1}/{len(payload)}"


def test_range_beyond_end_is_416(own_audio):
    ruth_client, url, payload = own_audio

    response = ruth_client.get(url, headers={"Range": f"bytes={len(payload) + 10}-"})

    assert response.status_code == 416
    assert response.content == b""


def test_beitrag_without_stored_file_is_404(
    client: TestClient, config: Config, db_session: Session, family, monkeypatch
):
    ruth, _ = family
    no_key = Beitrag(person_id=ruth.id, title="Ohne Datei")
    db_session.add(no_key)
    db_session.commit()
    gone = _beitrag(client, db_session, ruth, _slot(db_session, 4), title="Datei weg")
    client.app.state.storage.delete(gone.audio_object_key)
    _freeze(monkeypatch, _at(11, 20))
    ruth_client = _login(client, config, ruth)

    assert ruth_client.get(f"/archiv/{no_key.id}/audio", follow_redirects=False).status_code == 404
    assert ruth_client.get(f"/archiv/{gone.id}/audio", follow_redirects=False).status_code == 404
