"""Job 040: verpassten Tag nachholen.

Ist an Tag N der Ersatzbeitrag eingesprungen und wird die echte Geschichte von
Tag N danach freigegeben, legt der Lauf fuer Tag N+1 sie als zweites
App-Kapitel hinter die Tagesgeschichte. Der Lauf danach raeumt sie ab.

Kein Test liest die echte Uhr: der Ersatz-Lauf fuer Tag 5 steht mit festem
`started_at` im Verlauf, die Freigabe hat einen festen Zeitpunkt davor oder
danach. Echtes ffmpeg, Toniecloud als Zustandsersatz (tests/test_delivery_rahmen.py).
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.delivery.chapters import AppChapter, dump_app_chapters, load_app_chapters
from app.delivery.job import run_delivery
from app.delivery.rahmen import MELODIE_KEY, melodie
from app.mail.report import SummaryRow, build_summary_message
from app.models import Beitrag, Campaign, CreativeTonie, DeliveryRun, Person, Slot
from app.recording.normalize import normalize_recording
from app.settings import get_einstellungen
from tests.fixtures.audio import make_tone_mp3
from tests.mailutil import plain_text
from tests.test_delivery import STOCK, make_calendar_tonie, make_client, make_person_slot_beitrag
from tests.test_delivery_rahmen import ABSPANN, ABSPANN_KEY, FakeCloud, RahmenStorage

GESCHICHTE_5 = normalize_recording(make_tone_mp3(2.0, 440))
GESCHICHTE_6 = normalize_recording(make_tone_mp3(1.5, 495))
GESCHICHTE_7 = normalize_recording(make_tone_mp3(1.2, 523))
ERSATZ = normalize_recording(make_tone_mp3(1.0, 550))

ERSATZ_LAUF_AM = datetime(2026, 12, 4, 16, 0)
FREIGABE_DANACH = datetime(2026, 12, 4, 20, 0)
FREIGABE_DAVOR = datetime(2026, 12, 4, 15, 0)
ERSATZ_KAPITEL = {"id": "ersatz-kapitel", "title": "Ersatzgeschichte", "file": "ersatz-kapitel"}

NACHGEHOLT = (
    "Türchen 5 nachgeholt: Die Geschichte steht als zweites Kapitel hinter der Tagesgeschichte."
)
NICHT_NACHGEHOLT_PLATZ = (
    "Türchen 5 nicht nachgeholt: Für beide Geschichten fehlt Platz auf dem Tonie."
)
NICHT_NACHGEHOLT_ABGELEHNT = (
    "Türchen 5 nicht nachgeholt: Die Toniecloud hat das Kapitel-Paar abgelehnt ("
)
NICHT_NACHGEHOLT_LADEN = "Türchen 5 nicht nachgeholt: Die Geschichte ließ sich nicht laden ("


@pytest.fixture(scope="module")
def melodie_bytes() -> bytes:
    return melodie(RahmenStorage({}))


def _kalender(db: Session, *, rahmen_aus: bool = True, **felder) -> tuple[Campaign, CreativeTonie]:
    campaign = Campaign(name="Familie", **felder)
    db.add(campaign)
    db.commit()
    get_einstellungen(db).rahmen_aus = rahmen_aus
    db.commit()
    return make_calendar_tonie(db, campaign=campaign)


def _ersatz(db: Session, campaign: Campaign) -> Beitrag:
    person = Person(email="ersatz@example.test", display_name="Ersatz")
    db.add(person)
    db.flush()
    beitrag = Beitrag(
        person_id=person.id,
        title="Ersatzgeschichte",
        audio_object_key="audio/ersatz.mp3",
        approved_at=datetime(2026, 11, 1),
    )
    db.add(beitrag)
    db.flush()
    campaign.replacement_beitrag_id = beitrag.id
    db.commit()
    return beitrag


def _tag5_mit_ersatz(
    db: Session,
    campaign: Campaign,
    tonie: CreativeTonie,
    cloud: FakeCloud,
    *,
    freigabe: datetime | None = FREIGABE_DANACH,
    outcome: str = "ersatzbeitrag",
    run_type: str = "vorabend",
    lauf_tonie_id: str | None = None,
) -> tuple[Beitrag, Beitrag]:
    """Tag 5: der Vorabend-Lauf hat den Ersatzbeitrag aufgespielt (fester
    Zeitpunkt), die echte Geschichte von Tag 5 hat den Zeitpunkt `freigabe`."""
    ersatz = _ersatz(db, campaign)
    _slot5, beitrag5 = make_person_slot_beitrag(db, campaign, day=5, approved=False)
    beitrag5.approved_at = freigabe
    db.add(
        DeliveryRun(
            campaign_id=campaign.id,
            tonie_id=lauf_tonie_id or tonie.tonie_id,
            run_type=run_type,
            target_day=5,
            started_at=ERSATZ_LAUF_AM,
            outcome=outcome,
            beitrag_ids=str(ersatz.id),
        )
    )
    tonie.app_chapters = dump_app_chapters([AppChapter(ERSATZ_KAPITEL["id"], 1.0)])
    tonie.verified_beitrag_id = ersatz.id
    tonie.verified_chapter_id = ERSATZ_KAPITEL["id"]
    tonie.verified_for_day = 5
    db.commit()
    cloud.chapters = [dict(ERSATZ_KAPITEL), *(dict(c) for c in STOCK)]
    cloud.seconds[ERSATZ_KAPITEL["id"]] = 1.0
    return ersatz, beitrag5


def _storage(**extra: bytes) -> RahmenStorage:
    objects = {
        "audio/ersatz.mp3": ERSATZ,
        "audio/beitrag-5.mp3": GESCHICHTE_5,
        "audio/beitrag-6.mp3": GESCHICHTE_6,
        "audio/beitrag-7.mp3": GESCHICHTE_7,
    }
    objects.update(extra)
    return RahmenStorage(objects)


def _lauf(db, cloud, storage, tonie, *, day=6, run_type="vorabend"):
    client = make_client(cloud.handler)
    return run_delivery(db, client, storage, tonie, run_type=run_type, target_day=day)


def _letzter_eintrag(db: Session, tonie: CreativeTonie, day: int) -> DeliveryRun:
    return (
        db.execute(
            select(DeliveryRun)
            .where(DeliveryRun.tonie_id == tonie.tonie_id, DeliveryRun.target_day == day)
            .order_by(DeliveryRun.id.desc())
        )
        .scalars()
        .first()
    )


def _titel(cloud: FakeCloud) -> list[str]:
    return [c["title"] for c in cloud.chapters]


STOCK_TITEL = [c["title"] for c in STOCK]


# --- Nachholen ----------------------------------------------------------------


def test_vorabend_legt_verpassten_tag_als_zweites_kapitel_hinter_die_tagesgeschichte(
    db_session,
):
    campaign, tonie = _kalender(db_session)
    cloud = FakeCloud()
    _ersatz_beitrag, beitrag5 = _tag5_mit_ersatz(db_session, campaign, tonie, cloud)
    _slot6, beitrag6 = make_person_slot_beitrag(db_session, campaign, day=6)

    outcome = _lauf(db_session, cloud, _storage(), tonie)

    assert outcome.success is True
    assert outcome.used_beitrag_id == beitrag6.id
    assert len(cloud.uploads) == 2
    assert cloud.uploads[0] == GESCHICHTE_6
    assert cloud.uploads[1] == GESCHICHTE_5
    assert len(cloud.patches) == 1
    assert cloud.patches[0]["chapters"] == [
        {"title": "Tuerchen 6 Titel", "file": "file-1"},
        {"title": "Tuerchen 5 Titel", "file": "file-2"},
        *STOCK,
    ]
    assert _titel(cloud) == ["Tuerchen 6 Titel", "Tuerchen 5 Titel", *STOCK_TITEL]
    entry = _letzter_eintrag(db_session, tonie, 6)
    assert entry.outcome == "erfolg"
    assert entry.reason == NACHGEHOLT
    assert entry.beitrag_ids == f"{beitrag6.id},{beitrag5.id}"
    db_session.refresh(tonie)
    app = load_app_chapters(tonie.app_chapters)
    assert [c.id for c in app] == [cloud.chapters[0]["id"], cloud.chapters[1]["id"]]
    assert app[1].seconds == pytest.approx(cloud.upload_seconds()[1], abs=0.01)
    assert tonie.verified_beitrag_id == beitrag6.id
    assert tonie.verified_chapter_id == cloud.chapters[0]["id"]
    assert tonie.verified_for_day == 6


def test_naechster_lauf_raeumt_die_nachgeholte_geschichte_ab(db_session):
    campaign, tonie = _kalender(db_session)
    cloud = FakeCloud()
    _tag5_mit_ersatz(db_session, campaign, tonie, cloud)
    make_person_slot_beitrag(db_session, campaign, day=6)
    make_person_slot_beitrag(db_session, campaign, day=7)
    storage = _storage()
    _lauf(db_session, cloud, storage, tonie, day=6)

    outcome = _lauf(db_session, cloud, storage, tonie, day=7)

    assert outcome.success is True
    assert len(cloud.uploads) == 3
    assert cloud.patches[-1]["chapters"] == [
        {"title": "Tuerchen 7 Titel", "file": "file-3"},
        *STOCK,
    ]
    assert _titel(cloud) == ["Tuerchen 7 Titel", *STOCK_TITEL]
    assert not _letzter_eintrag(db_session, tonie, 7).reason
    db_session.refresh(tonie)
    assert [c.id for c in load_app_chapters(tonie.app_chapters)] == [cloud.chapters[0]["id"]]


def test_kontrolllauf_nach_dem_nachholen_aendert_nichts(db_session):
    campaign, tonie = _kalender(db_session)
    cloud = FakeCloud()
    _tag5_mit_ersatz(db_session, campaign, tonie, cloud)
    make_person_slot_beitrag(db_session, campaign, day=6)
    storage = _storage()
    _lauf(db_session, cloud, storage, tonie)

    outcome = _lauf(db_session, cloud, storage, tonie, run_type="kontrolllauf")

    assert outcome.success is True
    assert len(cloud.patches) == 1
    assert len(cloud.uploads) == 2
    assert _titel(cloud) == ["Tuerchen 6 Titel", "Tuerchen 5 Titel", *STOCK_TITEL]
    db_session.refresh(tonie)
    assert len(load_app_chapters(tonie.app_chapters)) == 2


def test_nachholen_auch_hinter_dem_ersatzbeitrag_von_tag_n_plus_1(db_session):
    campaign, tonie = _kalender(db_session)
    cloud = FakeCloud()
    ersatz, beitrag5 = _tag5_mit_ersatz(db_session, campaign, tonie, cloud)
    db_session.add(Slot(campaign_id=campaign.id, day=6))
    db_session.commit()

    outcome = _lauf(db_session, cloud, _storage(), tonie)

    assert outcome.success is True
    assert outcome.used_beitrag_id == ersatz.id
    assert cloud.uploads == [ERSATZ, GESCHICHTE_5]
    assert _titel(cloud) == ["Ersatzgeschichte", "Tuerchen 5 Titel", *STOCK_TITEL]
    entry = _letzter_eintrag(db_session, tonie, 6)
    assert entry.outcome == "ersatzbeitrag"
    assert entry.reason == f"Kein freigegebener Beitrag fuer Tag 6. · {NACHGEHOLT}"
    assert entry.beitrag_ids == f"{ersatz.id},{beitrag5.id}"


def test_nachgeholte_geschichte_geht_ohne_rahmen(db_session, melodie_bytes):
    campaign, tonie = _kalender(db_session, rahmen_aus=False, abspann_key=ABSPANN_KEY)
    cloud = FakeCloud()
    _tag5_mit_ersatz(db_session, campaign, tonie, cloud)
    make_person_slot_beitrag(db_session, campaign, day=6)
    storage = _storage(**{MELODIE_KEY: melodie_bytes, ABSPANN_KEY: ABSPANN})

    _lauf(db_session, cloud, storage, tonie)

    assert len(cloud.uploads) == 2
    assert cloud.upload_seconds()[0] > cloud.upload_seconds()[1] + 5
    assert cloud.uploads[1] == GESCHICHTE_5
    assert _letzter_eintrag(db_session, tonie, 6).reason == NACHGEHOLT


# --- Kein Nachholen -------------------------------------------------------------


def test_ohne_ersatzbeitrag_an_tag_n_kein_nachholen(db_session):
    campaign, tonie = _kalender(db_session)
    cloud = FakeCloud()
    _tag5_mit_ersatz(db_session, campaign, tonie, cloud, outcome="erfolg")
    make_person_slot_beitrag(db_session, campaign, day=6)

    _lauf(db_session, cloud, _storage(), tonie)

    assert cloud.uploads == [GESCHICHTE_6]
    assert _titel(cloud) == ["Tuerchen 6 Titel", *STOCK_TITEL]
    assert not _letzter_eintrag(db_session, tonie, 6).reason


def test_vor_dem_ersatzlauf_freigegeben_kein_nachholen(db_session):
    campaign, tonie = _kalender(db_session)
    cloud = FakeCloud()
    _tag5_mit_ersatz(db_session, campaign, tonie, cloud, freigabe=FREIGABE_DAVOR)
    make_person_slot_beitrag(db_session, campaign, day=6)

    _lauf(db_session, cloud, _storage(), tonie)

    assert cloud.uploads == [GESCHICHTE_6]
    assert _titel(cloud) == ["Tuerchen 6 Titel", *STOCK_TITEL]


def test_nicht_freigegeben_kein_nachholen(db_session):
    campaign, tonie = _kalender(db_session)
    cloud = FakeCloud()
    _tag5_mit_ersatz(db_session, campaign, tonie, cloud, freigabe=None)
    make_person_slot_beitrag(db_session, campaign, day=6)

    _lauf(db_session, cloud, _storage(), tonie)

    assert cloud.uploads == [GESCHICHTE_6]


def test_anstoss_fuer_tag_n_nach_der_freigabe_verhindert_nachholen(db_session):
    campaign, tonie = _kalender(db_session)
    cloud = FakeCloud()
    _ersatz_beitrag, beitrag5 = _tag5_mit_ersatz(db_session, campaign, tonie, cloud)
    db_session.add(
        DeliveryRun(
            campaign_id=campaign.id,
            tonie_id=tonie.tonie_id,
            run_type="anstoss",
            target_day=5,
            started_at=datetime(2026, 12, 4, 21, 0),
            outcome="erfolg",
            beitrag_ids=str(beitrag5.id),
        )
    )
    db_session.commit()
    make_person_slot_beitrag(db_session, campaign, day=6)

    _lauf(db_session, cloud, _storage(), tonie)

    assert cloud.uploads == [GESCHICHTE_6]


def test_ersatz_auf_anderem_tonie_kein_nachholen(db_session):
    campaign, tonie = _kalender(db_session)
    cloud = FakeCloud()
    _tag5_mit_ersatz(db_session, campaign, tonie, cloud, lauf_tonie_id="anderer-tonie")
    make_person_slot_beitrag(db_session, campaign, day=6)

    _lauf(db_session, cloud, _storage(), tonie)

    assert cloud.uploads == [GESCHICHTE_6]


# --- Platz, Ablehnung, Laden ------------------------------------------------------


def test_platz_fuer_beide_fehlt_liefert_nur_die_tagesgeschichte(db_session):
    campaign, tonie = _kalender(db_session)
    # Bestand 3 s: Tag 6 allein (1,5 s) passt in 5,5 s, mit Tag 5 (2 s) nicht.
    cloud = FakeCloud(max_seconds=5.5)
    _tag5_mit_ersatz(db_session, campaign, tonie, cloud)
    make_person_slot_beitrag(db_session, campaign, day=6)

    outcome = _lauf(db_session, cloud, _storage(), tonie)

    assert outcome.success is True
    assert cloud.uploads == [GESCHICHTE_6]
    assert _titel(cloud) == ["Tuerchen 6 Titel", *STOCK_TITEL]
    entry = _letzter_eintrag(db_session, tonie, 6)
    assert entry.outcome == "erfolg"
    assert entry.reason == NICHT_NACHGEHOLT_PLATZ
    db_session.refresh(tonie)
    assert len(load_app_chapters(tonie.app_chapters)) == 1


def test_abgelehntes_kapitel_paar_laedt_tagesgeschichte_allein_neu(db_session):
    campaign, tonie = _kalender(db_session)
    cloud = FakeCloud(reject={2})
    _tag5_mit_ersatz(db_session, campaign, tonie, cloud)
    _slot6, beitrag6 = make_person_slot_beitrag(db_session, campaign, day=6)

    outcome = _lauf(db_session, cloud, _storage(), tonie)

    assert outcome.success is True
    assert outcome.used_beitrag_id == beitrag6.id
    assert cloud.uploads == [GESCHICHTE_6, GESCHICHTE_5, GESCHICHTE_6]
    assert cloud.patches[-1]["chapters"] == [
        {"title": "Tuerchen 6 Titel", "file": "file-3"},
        *STOCK,
    ]
    assert _titel(cloud) == ["Tuerchen 6 Titel", *STOCK_TITEL]
    entry = _letzter_eintrag(db_session, tonie, 6)
    assert entry.outcome == "erfolg"
    assert entry.reason.startswith(NICHT_NACHGEHOLT_ABGELEHNT)
    assert entry.beitrag_ids == str(beitrag6.id)


def test_nicht_ladbare_geschichte_liefert_nur_die_tagesgeschichte(db_session):
    campaign, tonie = _kalender(db_session)
    cloud = FakeCloud()
    _tag5_mit_ersatz(db_session, campaign, tonie, cloud)
    make_person_slot_beitrag(db_session, campaign, day=6)
    storage = _storage()
    del storage.objects["audio/beitrag-5.mp3"]

    outcome = _lauf(db_session, cloud, storage, tonie)

    assert outcome.success is True
    assert cloud.uploads == [GESCHICHTE_6]
    assert _letzter_eintrag(db_session, tonie, 6).reason.startswith(NICHT_NACHGEHOLT_LADEN)


# --- Abendmeldung ---------------------------------------------------------------


def test_abendmeldung_nennt_das_nachholen(db_session):
    campaign, tonie = _kalender(db_session)
    cloud = FakeCloud()
    _tag5_mit_ersatz(db_session, campaign, tonie, cloud)
    make_person_slot_beitrag(db_session, campaign, day=6)

    outcome = _lauf(db_session, cloud, _storage(), tonie)
    entry = _letzter_eintrag(db_session, tonie, 6)
    message = build_summary_message(
        to_address="admin@example.test",
        evening=datetime(2026, 12, 5).date(),
        run_type="vorabend",
        rows=[
            SummaryRow(
                calendar=campaign.name,
                tonie_name="Tonie",
                tonie_id=tonie.tonie_id,
                result=entry.outcome,
                target_day=entry.target_day,
                reason=entry.reason,
            )
        ],
    )

    assert outcome.reason == NACHGEHOLT
    assert NACHGEHOLT in plain_text(message)
