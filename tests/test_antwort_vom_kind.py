"""Job 042: Antwort vom Kind (Neuanlage von Job 041).

Der Admin legt im Reiter Aufnahmen je vorlesender Person eine Antwort-Aufnahme
ab: per Aufnahme im Browser oder per Datei, beides ueber recorder.js an
POST /admin/aufnahmen/antwort (normalisiert wie ein Admin-Upload, nie ein
Beitrag, nie auf den Tonie). Die Person hoert sie im Archiv unter "Antwort für
dich" und bekommt eine Mail mit Anmeldelink auf /archiv. Fremde Antworten sieht
niemand. Mockup: docs/design/antwort-vom-kind-mockup.html (Fassung 2).

Gegen echten S3-Speicher und echtes ffmpeg.

Kein Test liest ein ORM-Objekt, dessen Zeile der gepruefte Vorgang geloescht
hat (sonst ObjectDeletedError): IDs und Schluessel stehen vorher in Variablen.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin.setup import create_campaign, create_person
from app.auth.tokens import create_magic_link_token
from app.config import Config
from app.models import Auftrag, Beitrag, Campaign, Person, Slot
from app.storage import ObjectMissing
from tests.fixtures.audio import make_tone_mp3, make_wrong_format_mp3
from tests.mailutil import html_text, plain_text

KURZ = "Jan. Feb. März Apr. Mai Juni Juli Aug. Sept. Okt. Nov. Dez.".split()
LANG = "Januar Februar März April Mai Juni Juli August September Oktober November Dezember".split()


def _antwort_model():
    from app.models import Antwort

    return Antwort


def _slot(db: Session, day: int) -> Slot:
    return db.execute(select(Slot).where(Slot.day == day)).scalar_one()


def _login(client: TestClient, config: Config, person: Person) -> TestClient:
    other = TestClient(client.app)
    other.post("/login/confirm", data={"token": create_magic_link_token(config, person)})
    return other


@pytest.fixture
def family(db_session: Session):
    """Ruth und Miri lesen vor (je ein Auftrag), Ben hat keinen Auftrag."""
    create_campaign(db_session)
    ruth = create_person(db_session, email="ruth@example.test", display_name="Ruth")
    miri = create_person(db_session, email="miri@example.test", display_name="Miri")
    ben = create_person(db_session, email="ben@example.test", display_name="Ben")
    for person, day in ((ruth, 3), (miri, 4)):
        auftrag = Auftrag(person_id=person.id, title=f"Geschichte {person.display_name}")
        db_session.add(auftrag)
        _slot(db_session, day).auftrag = auftrag
    db_session.commit()
    return ruth, miri, ben


@pytest.fixture
def mails(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(
        "app.admin.review.send_antwort_mail",
        lambda config, **kw: sent.append(kw),
        raising=False,
    )
    return sent


def _upload(client: TestClient, person_id: int, data: bytes | None = None, **kw):
    """Genau die Felder, die recorder.js schickt, plus person_id aus der Auswahl."""
    return client.post(
        "/admin/aufnahmen/antwort",
        data={"person_id": str(person_id), "reported_type": "audio/webm", "title": ""},
        files={
            "audio": ("aufnahme.webm", make_tone_mp3(1.5) if data is None else data, "audio/webm")
        },
        **kw,
    )


def _antworten(db: Session) -> list:
    db.expire_all()
    return db.execute(select(_antwort_model())).scalars().all()


def _ok(response) -> None:
    assert response.status_code == 200
    assert response.json() == {"ok": True, "next_url": "/admin/aufnahmen"}


# --- Reiter Aufnahmen: Panel und Aufnahmeflaeche --------------------------------


def test_formular_bietet_nur_vorlesende_personen(admin_client: TestClient, family):
    ruth, miri, ben = family
    page = admin_client.get("/admin/aufnahmen").text

    assert 'name="person_id"' in page
    # Die Option nennt nur den Namen; die Tage-Auswahl "Einspringen" nennt mehr.
    assert f'<option value="{ruth.id}">Ruth</option>' in page
    assert f'<option value="{miri.id}">Miri</option>' in page
    assert ">Ben</option>" not in page
    assert ">admin@example.test</option>" not in page


def test_formular_ohne_widerrufene_person(admin_client: TestClient, db_session: Session, family):
    ruth, miri, _ = family
    miri.revoked_at = datetime.now(UTC).replace(tzinfo=None)
    db_session.commit()

    page = admin_client.get("/admin/aufnahmen").text

    assert f'<option value="{ruth.id}">Ruth</option>' in page
    assert ">Miri</option>" not in page


def test_panel_hat_die_aufnahmeflaeche_der_freien_nachricht(admin_client: TestClient, family):
    page = admin_client.get("/admin/aufnahmen").text

    assert "Antwort vom Kind" in page
    assert "Eine Antwort für eine Person aufnehmen oder hochladen" in page
    assert 'id="recorder-app"' in page
    assert 'data-submit-url="/admin/aufnahmen/antwort"' in page
    assert 'class="fixed-bottom im-panel"' in page
    assert 'data-role="record-btn"' in page
    assert "Sprich die Antwort in Ruhe ein. Du kannst sie dir anhören und neu aufnehmen." in page
    # Knopf "Datei" mit Upload wie bei der freien Nachricht.
    assert 'class="setup-details upload-details"' in page
    assert 'data-role="file-input"' in page
    assert "Hochladen und Mail schicken" in page
    # Angehoert: Speichern nennt die gewaehlte Person (zuerst die erste Option).
    assert "Speichern und Mail schicken" in page
    name = '<span data-role="person-name">Ruth</span>'
    assert f"{name} bekommt eine Mail mit Link ins Archiv." in page
    assert "Sie kommt nie auf den Tonie." in page
    assert "Mikrofonzugriff nötig" in page
    assert '<script src="/static/recorder.js?v=' in page
    # Kein Titelfeld, kein Lauftext: die Antwort hat keinen Vorlesetext.
    assert 'data-role="title-field"' not in page
    assert 'data-role="scroll-toggle"' not in page


def test_panel_ohne_vorlesende_person(admin_client: TestClient, db_session: Session):
    create_campaign(db_session)
    create_person(db_session, email="ben@example.test", display_name="Ben")

    page = admin_client.get("/admin/aufnahmen").text

    assert "Antwort vom Kind" in page
    assert "Noch liest niemand vor." in page
    assert 'id="recorder-app"' not in page
    assert 'name="person_id"' not in page


def test_recorder_js_schickt_die_gewaehlte_person_mit(client: TestClient):
    js = client.get("/static/recorder.js").text

    assert "person_id" in js
    assert "person-name" in js


def test_familienseite_bleibt_ohne_personenauswahl(
    admin_client: TestClient, config: Config, family
):
    _, miri, _ = family
    # Die Auswahl gibt es nur im Admin-Panel ...
    assert 'name="person_id"' in admin_client.get("/admin/aufnahmen").text

    # ... die freie Nachricht im Familienbereich bleibt wie heute.
    page = _login(admin_client, config, miri).get("/record/free").text

    assert 'id="recorder-app"' in page
    assert 'name="person_id"' not in page
    assert "im-panel" not in page


# --- Speichern ---------------------------------------------------------------


def test_upload_speichert_normalisierte_antwort_und_keinen_beitrag(
    admin_client: TestClient, db_session: Session, family, mails
):
    ruth, _, _ = family

    _ok(_upload(admin_client, ruth.id))

    rows = _antworten(db_session)
    assert len(rows) == 1
    assert rows[0].person_id == ruth.id
    assert rows[0].uploaded_at is not None
    key = rows[0].audio_object_key
    assert key.startswith(f"antworten/{ruth.id}/") and key.endswith(".mp3")
    stored = admin_client.app.state.storage.read(key)
    assert stored.content_type == "audio/mpeg"
    assert stored.data[:3] == b"ID3" or stored.data[0] == 0xFF
    # Nie ein Beitrag: damit nie im Eingang, nie freigebbar, nie auf dem Tonie.
    assert db_session.execute(select(Beitrag)).first() is None


def test_upload_schickt_mail_mit_link_aufs_archiv(
    admin_client: TestClient, db_session: Session, family, mails
):
    ruth, _, _ = family

    _ok(_upload(admin_client, ruth.id))
    page = admin_client.get("/admin/aufnahmen").text

    assert len(mails) == 1
    assert mails[0]["to_address"] == "ruth@example.test"
    assert mails[0]["display_name"] == "Ruth"
    assert mails[0]["person_id"] == ruth.id
    query = parse_qs(urlsplit(mails[0]["login_url"]).query)
    assert query["next"] == ["/archiv"]
    assert query["token"]
    assert 'data-result="ok"' in page
    assert "Antwort für Ruth gespeichert, Mail verschickt." in page


def test_zweiter_upload_ersetzt_antwort_und_loescht_alte_datei(
    admin_client: TestClient, db_session: Session, family, mails
):
    ruth, _, _ = family
    _ok(_upload(admin_client, ruth.id))
    old_key = _antworten(db_session)[0].audio_object_key

    _ok(_upload(admin_client, ruth.id, make_tone_mp3(2.0)))

    rows = _antworten(db_session)
    assert len(rows) == 1
    assert rows[0].audio_object_key != old_key
    with pytest.raises(ObjectMissing):
        admin_client.app.state.storage.read(old_key)
    assert len(mails) == 2


def test_unlesbare_datei_wird_abgewiesen(
    admin_client: TestClient, db_session: Session, family, mails
):
    ruth, _, _ = family

    response = _upload(admin_client, ruth.id, make_wrong_format_mp3())

    assert response.status_code == 422
    assert response.json() == {"error": "Die Datei lässt sich nicht als Audio lesen."}
    assert _antworten(db_session) == []
    assert mails == []
    # Die Fehlermeldung zeigt recorder.js; kein Hinweisbalken bleibt liegen.
    assert 'data-result="err"' not in admin_client.get("/admin/aufnahmen").text


def test_leere_datei_wird_abgewiesen(admin_client: TestClient, db_session: Session, family, mails):
    ruth, _, _ = family

    response = _upload(admin_client, ruth.id, b"")

    assert response.status_code == 422
    assert response.json() == {"error": "Die Datei ist leer."}
    assert _antworten(db_session) == []
    assert mails == []


def test_zu_grosse_datei_wird_abgewiesen(
    admin_client: TestClient, db_session: Session, family, mails, monkeypatch
):
    ruth, _, _ = family
    monkeypatch.setattr("app.admin.review.MAX_UPLOAD_BYTES", 10)

    response = _upload(admin_client, ruth.id)

    assert response.status_code == 413
    assert response.json() == {"error": "Die Datei ist zu groß."}
    assert _antworten(db_session) == []
    assert mails == []


@pytest.mark.parametrize("who", ["ohne_auftrag", "admin", "unbekannt"])
def test_nur_vorlesende_personen_bekommen_eine_antwort(
    admin_client: TestClient, db_session: Session, family, mails, who
):
    _, _, ben = family
    if who == "ohne_auftrag":
        person_id = ben.id
    elif who == "admin":
        person_id = db_session.execute(select(Person.id).where(Person.is_admin.is_(True))).scalar()
    else:
        person_id = 9999

    response = _upload(admin_client, person_id)

    assert response.status_code == 422
    assert response.json() == {"error": "Diese Person liest nicht vor."}
    assert _antworten(db_session) == []
    assert mails == []


def test_widerrufene_person_bekommt_keine_antwort(
    admin_client: TestClient, db_session: Session, family, mails
):
    ruth, _, _ = family
    ruth.revoked_at = datetime.now(UTC).replace(tzinfo=None)
    db_session.commit()

    response = _upload(admin_client, ruth.id)

    assert response.status_code == 422
    assert response.json() == {
        "error": "Der Zugang von Ruth ist widerrufen. Keine Antwort gespeichert."
    }
    assert _antworten(db_session) == []
    assert mails == []


def test_mailfehler_behaelt_antwort_und_loggt_ohne_details(
    admin_client: TestClient, db_session: Session, family, monkeypatch, caplog
):
    ruth, _, _ = family

    def boom(config, **kw):
        raise OSError("smtp down ruth@example.test")

    monkeypatch.setattr("app.admin.review.send_antwort_mail", boom, raising=False)
    caplog.set_level(logging.WARNING)

    _ok(_upload(admin_client, ruth.id))
    page = admin_client.get("/admin/aufnahmen").text

    assert len(_antworten(db_session)) == 1
    assert 'data-result="err"' in page
    assert "Antwort für Ruth gespeichert, aber die Mail ließ sich nicht verschicken." in page
    assert f"Antwort-Mail fehlgeschlagen: person={ruth.id} fehler=OSError" in caplog.text
    assert "smtp down" not in caplog.text
    assert "ruth@example.test" not in caplog.text
    assert "token=" not in caplog.text


# --- Liste, Anhoeren, Loeschen ----------------------------------------------------


def test_liste_zeigt_vorhandene_antwort_mit_player(
    admin_client: TestClient, db_session: Session, family, mails
):
    ruth, _, _ = family
    _ok(_upload(admin_client, ruth.id))
    antwort = _antworten(db_session)[0]
    when = antwort.uploaded_at

    page = admin_client.get("/admin/aufnahmen").text

    assert "Vorhandene Antworten" in page
    assert f"hochgeladen am {when.day}. {KURZ[when.month - 1]}" in page
    assert f'src="/admin/aufnahmen/antwort/{antwort.id}/audio"' in page
    assert f'action="/admin/aufnahmen/antwort/{antwort.id}/loeschen"' in page
    audio = admin_client.get(f"/admin/aufnahmen/antwort/{antwort.id}/audio")
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/mpeg"


def test_antwort_steht_nicht_bei_den_aufnahmen_und_nicht_im_zip(
    admin_client: TestClient, db_session: Session, family, mails
):
    ruth, _, _ = family
    _ok(_upload(admin_client, ruth.id))

    page = admin_client.get("/admin/aufnahmen").text

    assert "Noch keine Aufnahmen." in page
    assert "Keine freien Nachrichten." in page
    assert 'href="/admin/aufnahmen/download"' not in page


def test_admin_loescht_antwort_samt_datei(
    admin_client: TestClient, config: Config, db_session: Session, family, mails
):
    ruth, _, _ = family
    _ok(_upload(admin_client, ruth.id))
    antwort = _antworten(db_session)[0]
    # Vor dem Loeschen merken: die Zeile ist danach weg.
    aid, akey = antwort.id, antwort.audio_object_key

    page = admin_client.post(f"/admin/aufnahmen/antwort/{aid}/loeschen").text

    assert 'data-result="ok"' in page
    assert "Antwort gelöscht." in page
    assert _antworten(db_session) == []
    with pytest.raises(ObjectMissing):
        admin_client.app.state.storage.read(akey)
    assert _login(admin_client, config, ruth).get("/archiv/antwort").status_code == 404


def test_unbekannte_antwort_ist_404(admin_client: TestClient, db_session: Session, family, mails):
    ruth, _, _ = family
    _ok(_upload(admin_client, ruth.id))
    aid = _antworten(db_session)[0].id

    assert admin_client.get(f"/admin/aufnahmen/antwort/{aid}/audio").status_code == 200
    assert admin_client.get("/admin/aufnahmen/antwort/9999/audio").status_code == 404
    assert admin_client.post("/admin/aufnahmen/antwort/9999/loeschen").status_code == 404
    assert len(_antworten(db_session)) == 1


def test_familie_erreicht_die_admin_routen_nicht(
    admin_client: TestClient, config: Config, db_session: Session, family, mails
):
    ruth, miri, _ = family
    _ok(_upload(admin_client, ruth.id))
    antwort = _antworten(db_session)[0]
    miri_client = _login(admin_client, config, miri)

    assert _upload(miri_client, miri.id, follow_redirects=False).status_code == 403
    assert miri_client.get(f"/admin/aufnahmen/antwort/{antwort.id}/audio").status_code == 403
    assert miri_client.post(f"/admin/aufnahmen/antwort/{antwort.id}/loeschen").status_code == 403
    assert len(_antworten(db_session)) == 1


# --- Archiv --------------------------------------------------------------------


def test_archiv_zeigt_die_antwort_nur_ihrer_person(
    admin_client: TestClient, config: Config, db_session: Session, family, mails
):
    ruth, miri, _ = family
    _ok(_upload(admin_client, ruth.id))
    ruth_client = _login(admin_client, config, ruth)
    miri_client = _login(admin_client, config, miri)

    page = ruth_client.get("/archiv").text
    assert "<h3>Antwort für dich</h3>" in page
    assert page.index("Antwort für dich") < page.index("Aus der Familie")
    assert '<audio controls preload="none" src="/archiv/antwort"></audio>' in page
    audio = ruth_client.get("/archiv/antwort")
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/mpeg"

    other = miri_client.get("/archiv").text
    assert "Antwort für dich" not in other
    assert 'src="/archiv/antwort"' not in other
    assert miri_client.get("/archiv/antwort").status_code == 404
    assert admin_client.get("/archiv/antwort").status_code == 404


def test_archiv_nennt_kind_und_datum(
    admin_client: TestClient, config: Config, db_session: Session, family, mails
):
    ruth, _, _ = family
    db_session.execute(select(Campaign)).scalar_one().kind_name = "Emma"
    db_session.commit()
    _ok(_upload(admin_client, ruth.id))
    when = _antworten(db_session)[0].uploaded_at

    page = _login(admin_client, config, ruth).get("/archiv").text

    assert '<div class="row-title">Antwort von Emma</div>' in page
    assert f'<div class="row-sub">{when.day}. {LANG[when.month - 1]}</div>' in page


def test_archiv_ohne_kindername(
    admin_client: TestClient, config: Config, db_session: Session, family, mails
):
    ruth, _, _ = family
    _ok(_upload(admin_client, ruth.id))

    page = _login(admin_client, config, ruth).get("/archiv").text

    assert '<div class="row-title">Antwort</div>' in page


def test_archiv_ohne_antwort_hat_keinen_abschnitt(
    admin_client: TestClient, config: Config, family, mails
):
    ruth, miri, _ = family
    # Ruth hat eine Antwort, Miri nicht.
    _ok(_upload(admin_client, ruth.id))
    miri_client = _login(admin_client, config, miri)

    assert "Antwort für dich" not in miri_client.get("/archiv").text
    assert miri_client.get("/archiv/antwort").status_code == 404


def test_archiv_antwort_braucht_anmeldung(client: TestClient):
    response = client.get("/archiv/antwort", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- Person loeschen -----------------------------------------------------------


def test_person_loeschen_entfernt_ihre_antwort(
    admin_client: TestClient, db_session: Session, family, mails
):
    ruth, _, _ = family
    _ok(_upload(admin_client, ruth.id))
    key = _antworten(db_session)[0].audio_object_key
    # Vor dem Loeschen merken: die Zeile der Person ist danach weg.
    rid = ruth.id

    admin_client.post(f"/admin/personen/{rid}/loeschen")

    db_session.expire_all()
    assert db_session.get(Person, rid) is None
    assert _antworten(db_session) == []
    with pytest.raises(ObjectMissing):
        admin_client.app.state.storage.read(key)


# --- Mail ----------------------------------------------------------------------

LINK = "https://advent.example.test/login/confirm?token=abc"


def test_mail_mit_kindername():
    from app.mail.antwort import build_antwort_message

    message = build_antwort_message(
        to_address="ruth@example.test",
        display_name="Ruth",
        login_url=LINK,
        kind_name="Emma",
        sender_name="Luca",
    )

    assert message["Subject"] == "Emma hat dir geantwortet"
    assert message["To"] == "ruth@example.test"
    text = plain_text(message)
    assert text.startswith("Hallo Ruth,")
    assert "Emma hat dir auf deine Geschichte geantwortet." in text
    assert "Du findest die Antwort im Archiv unter „Antwort für dich“." in text
    assert "Über diesen Link kommst du direkt ins Archiv:" in text
    assert LINK in text
    assert "Viel Freude beim Anhören!" in text
    assert "Herzliche Grüße\nLuca" in text
    html = html_text(message)
    assert LINK in html
    assert "Zum Archiv" in html
    assert "Emma hat dir auf deine Geschichte geantwortet." in html


def test_mail_ohne_kindername_und_ohne_anzeigename():
    from app.mail.antwort import build_antwort_message

    message = build_antwort_message(to_address="ruth@example.test", display_name="", login_url=LINK)

    assert message["Subject"] == "Eine Antwort für dich"
    text = plain_text(message)
    assert text.startswith("Hallo,")
    assert "Es gibt eine Antwort auf deine Geschichte." in text
    assert "„Antwort für dich“" in text
    assert LINK in text
