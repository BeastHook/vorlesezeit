"""Die Bildschirme der Wartung (Mockup docs/design/wartung-mockup.html).

Eine Übersicht mit Status und Aufgaben, jede Aufgabe ein eigener Bildschirm.
Höchstens ein Vorgang läuft zugleich (`app.state.vorgang`); jeder POST einer
Aufgabe schickt währenddessen auf dessen Fortschrittsseite.
"""

from __future__ import annotations

import re
import threading
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.update_hinweis import RELEASES_URL, is_newer
from installer.installation import _letzte_zeile, lies_env, lies_installation
from installer.notfallblatt import notfallblatt_pdf
from installer.views import Umleitung, _dateiname, _seite, _zu, abgeschlossen
from installer.wartung import (
    Abfrage,
    Aktualisierung,
    Deinstallation,
    Neustart,
    Zeile,
    berlin,
    finde_ziel,
    im_advent,
    laufende_version,
    protokoll,
    release_abfragen,
    stand_zeit,
    status,
    wann,
)

STAND = {
    "aktualisieren": "/wartung/aktualisieren/stand",
    "neustart": "/wartung/neustart/stand",
    "deinstallieren": "/wartung/deinstallieren/stand",
}
HINWEIS_ABEND = "Am besten tagsüber, abends ab 17 Uhr kommt die Geschichte auf den Tonie."
_LINK = re.compile(r"/einrichtung/[A-Za-z0-9_-]+")
_VORSCHAU_ZEILEN = 12
_sperre = threading.Lock()


def _laufender(request: Request):
    vorgang = request.app.state.vorgang
    return vorgang if vorgang is not None and vorgang.laeuft else None


def nur_nach_abschluss(request: Request) -> None:
    """Die Wartung gibt es erst nach dem Abschluss; die Ergebnisseite des Löschens auch danach."""
    vorgang = request.app.state.vorgang
    if vorgang is not None and request.url.path == STAND[vorgang.art]:
        return
    if not abgeschlossen(request):
        raise Umleitung("/")


def keine_zweite_aufgabe(request: Request) -> None:
    if request.method == "POST" and (laufend := _laufender(request)):
        raise Umleitung(STAND[laufend.art])


router = APIRouter(
    prefix="/wartung", dependencies=[Depends(nur_nach_abschluss), Depends(keine_zweite_aufgabe)]
)
einstieg = APIRouter()


# --- Hilfen ------------------------------------------------------------------------------


def _env(request: Request) -> dict[str, str]:
    return lies_env(request.app.state.umgebung.install_dir / ".env")


def _adresse(request: Request) -> str:
    return lies_installation(request.app.state.umgebung.install_dir)["adresse"]


def _ziel(request: Request, env: dict[str, str]):
    u, w = request.app.state.umgebung, request.app.state.werkzeuge
    return finde_ziel(u.install_dir, u.laufwerke_dir, u.host_install_dir, w.docker, env)


def _uhr(request: Request) -> dict:
    w = request.app.state.werkzeuge
    return {"uhr": w.uhr, "schlafen": w.schlafen}


def _starte(request: Request, vorgang) -> RedirectResponse:
    with _sperre:
        if laufend := _laufender(request):
            return _zu(STAND[laufend.art])
        vorgang.laeuft = True
        request.app.state.vorgang = vorgang
    request.app.state.werkzeuge.starte(vorgang.ausfuehren)
    return _zu(STAND[vorgang.art])


def _neustart(request: Request) -> Neustart:
    u, w = request.app.state.umgebung, request.app.state.werkzeuge
    return Neustart(u.install_dir, w.docker, w.http, **_uhr(request))


def _aktualisierung(request: Request, stand: dict) -> Aktualisierung:
    u, w = request.app.state.umgebung, request.app.state.werkzeuge
    return Aktualisierung(
        u.install_dir,
        w.docker,
        w.http,
        stand=stand,
        finde=lambda env: _ziel(request, env),
        jetzt=w.jetzt,
        **_uhr(request),
    )


def _vorgang_seite(request: Request, name: str, ueberschrift: str, zeilen: list, **kontext):
    basis = {
        "name": name,
        "ueberschrift": ueberschrift,
        "zeilen": zeilen,
        "laeuft": request.app.state.vorgang.laeuft,
        "meldung": None,
        "technik": None,
        "hinweis": None,
        "knopf": None,
    }
    return _seite(request, "wartung_vorgang.html", basis | kontext)


PROTOKOLL_KNOPF = {"text": "Protokoll exportieren", "href": "/wartung/protokoll"}
UEBERSICHT_KNOPF = {"text": "Zur Übersicht", "href": "/wartung"}


# --- Einstieg ------------------------------------------------------------------------------


@einstieg.get("/")
def start(request: Request):
    u = request.app.state.umgebung
    installation = lies_installation(u.install_dir)
    if installation and installation.get("deinstalliert"):
        # Nach dem Deinstallieren ohne Löschen: der nächste Start läuft weiter.
        return _starte(request, _neustart(request))
    if installation and installation.get("abgeschlossen"):
        return _zu("/wartung")
    if (u.install_dir / ".env").exists():
        return _zu("/installieren")
    return _zu("/geraet")


# --- Übersicht -----------------------------------------------------------------------------

UEBERSCHRIFT = {
    0: "Alles in Ordnung",
    1: "Ein Ding braucht dich",
    2: "Zwei Dinge brauchen dich",
    3: "Drei Dinge brauchen dich",
}


def _neuer(abfrage: Abfrage, version: str):
    r = abfrage.release
    return r if r is not None and is_newer(r.version, version) else None


@router.get("", response_class=HTMLResponse)
def uebersicht(request: Request):
    u, w = request.app.state.umgebung, request.app.state.werkzeuge
    if laufend := _laufender(request):
        return _zu(STAND[laufend.art])
    if (stand := Aktualisierung.lies_stand(u.install_dir)) is not None:
        # Ein unfertiges Update von einem früheren Start: hier geht es weiter.
        return _starte(request, _aktualisierung(request, stand))
    env, adresse = _env(request), _adresse(request)
    version = laufende_version(env)
    abfrage = release_abfragen(w.http, w.jetzt())
    neu = _neuer(abfrage, version)
    s = status(
        install_dir=u.install_dir,
        ziel=_ziel(request, env),
        docker=w.docker,
        http=w.http,
        adresse=adresse,
        version=version,
        die_neueste=abfrage.release is not None and neu is None,
        jetzt=w.jetzt(),
    )
    ueberschrift = UEBERSCHRIFT[s.probleme] if s.gesund else "Vorlesezeit läuft gerade nicht"
    kontext = {
        "ueberschrift": ueberschrift,
        "status": s,
        "adresse": adresse,
        "neu": neu,
        "abfrage": abfrage,
    }
    return _seite(request, "wartung.html", kontext)


# --- Aktualisieren -------------------------------------------------------------------------


def _angebot(request: Request) -> dict:
    w = request.app.state.werkzeuge
    alt = laufende_version(_env(request))
    jetzt = w.jetzt()
    abfrage = release_abfragen(w.http, jetzt)
    neu = _neuer(abfrage, alt)
    advent = im_advent(jetzt)
    return {
        "alt": alt,
        "abfrage": abfrage,
        "neu": neu,
        "advent": advent,
        "gesperrt": neu is not None and advent and not neu.dringend,
        "zeit": wann(abfrage.zeit, jetzt),
        "releases_url": RELEASES_URL,
        "hinweis": HINWEIS_ABEND,
    }


@router.get("/aktualisieren", response_class=HTMLResponse)
def aktualisieren(request: Request):
    return _seite(request, "wartung_aktualisieren.html", _angebot(request))


@router.post("/aktualisieren")
def aktualisieren_starten(request: Request):
    angebot = _angebot(request)
    if angebot["neu"] is None or angebot["gesperrt"]:
        return _zu("/wartung/aktualisieren")
    stand = Aktualisierung.neuer_stand(angebot["alt"], angebot["neu"].version)
    return _starte(request, _aktualisierung(request, stand))


def _minuten(dauer: timedelta) -> str:
    sekunden = max(int(dauer.total_seconds()), 0)
    return f"{sekunden // 60}:{sekunden % 60:02d}"


@router.get("/aktualisieren/stand", response_class=HTMLResponse)
def aktualisieren_stand(request: Request):
    v = request.app.state.vorgang
    if not isinstance(v, Aktualisierung):
        return _zu("/wartung/aktualisieren")
    alt, neu, s = v.stand["alt"], v.stand["neu"], v.schritte
    zeit = stand_zeit(v.stand["rueckfallstand"] or "")
    uhrzeit = f"{berlin(zeit):%H:%M} Uhr" if zeit else None
    gesichert = ", ".join(t for t in (v.ziel_name and f"auf {v.ziel_name}", uhrzeit) if t)
    pruefen_meta = None
    if s["pruefen"] == "laeuft" and (seit := v.seit_pruefen()) is not None:
        teil = "Läuft ✓, von außen noch nicht. " if v.zuletzt["gesund"] else ""
        pruefen_meta = f"{teil}Seit {_minuten(seit)} Minuten."
    elif s["pruefen"] == "fehler":
        pruefen_meta = "nach 3 Minuten keine Antwort"
    zeilen = [
        Zeile(
            s["sichern"],
            {"ok": "Gesichert", "laeuft": "Wird gesichert"}.get(s["sichern"], "Sichern"),
            gesichert if s["sichern"] == "ok" and gesichert else None,
            zeichen="✗",
        ),
        Zeile(
            s["laden"],
            {
                "ok": f"Version {neu} geladen und gestartet",
                "laeuft": "Neue Version wird geladen",
            }.get(s["laden"], "Neue Version laden und starten"),
            zeichen="✗",
        ),
        Zeile(s["pruefen"], "Läuft und ist von außen erreichbar", pruefen_meta),
    ]
    zurueck = f"Zurück auf {alt}" + (f", Daten aus der Sicherung von {uhrzeit}" if uhrzeit else "")

    if v.ergebnis is None or (v.laeuft and v.ergebnis == "rueckfall"):
        if s["rueckfall"] == "laeuft":
            zeilen.append(Zeile("laeuft", zurueck))
            hinweis = "Lass den Rechner an, das dauert etwa eine Minute."
            return _vorgang_seite(
                request, "Aktualisieren", "Zurück auf die vorige Version", zeilen, hinweis=hinweis
            )
        if s["pruefen"] == "laeuft":
            hinweis = (
                f"Antwortet Vorlesezeit nach 3 Minuten nicht, geht es von selbst auf {alt} "
                "zurück. Du musst nichts tun."
            )
        else:
            hinweis = (
                "Lass den Rechner an. Fenster versehentlich geschlossen? "
                "Öffne die Startdatei noch einmal, es geht hier weiter."
            )
        return _vorgang_seite(
            request, "Aktualisieren", "Vorlesezeit wird aktualisiert", zeilen, hinweis=hinweis
        )
    if v.ergebnis == "ok":
        meldung = {
            "ok": True,
            "satz": "Fertig. Die Familie merkt davon nichts, alle Links gelten weiter.",
        }
        return _vorgang_seite(
            request,
            "Aktualisieren",
            f"Vorlesezeit {neu} läuft",
            zeilen,
            meldung=meldung,
            knopf=UEBERSICHT_KNOPF,
        )
    if v.ergebnis == "abbruch":
        return _vorgang_seite(
            request,
            "Aktualisieren",
            "Vorlesezeit wird aktualisiert",
            zeilen,
            meldung={"ok": False, "satz": v.fehler.satz},
            technik=v.fehler.technik,
            knopf={"text": "Noch einmal versuchen", "post": "/wartung/aktualisieren"},
        )
    # Rückfall
    technik = "\n".join(
        t
        for t in (
            v.fehler.technik if v.fehler else None,
            f"Rückfall: Compose auf {alt}, DB aus {v.stand['rueckfallstand']}",
            v.rueckfall_fehler.technik if v.rueckfall_fehler else None,
        )
        if t
    )
    if v.rueckfall_fehler is not None:
        zeilen.append(Zeile("fehler", zurueck, zeichen="✗"))
        satz = (
            f"Die neue Version ist nicht richtig gestartet, und der Weg zurück auf {alt} hat "
            "nicht geklappt. Öffne die Startdatei noch einmal, dann versucht es Vorlesezeit "
            "erneut. Schick das Protokoll an die Person, die dich eingeladen hat."
        )
    else:
        gesund, aussen = v.zuletzt["gesund"], v.zuletzt["aussen"]
        zeilen = [
            Zeile("ok", f"Vorlesezeit {alt} läuft wieder")
            if gesund
            else Zeile("fehler", f"Vorlesezeit {alt} läuft noch nicht", zeichen="✗"),
            Zeile("ok", "Daten wie vor dem Update", f"aus der Sicherung von {uhrzeit}"),
            Zeile("ok", "Von außen erreichbar")
            if aussen
            else Zeile("fehler", "Von außen nicht erreichbar", zeichen="✗"),
        ]
        if gesund and aussen:
            satz = (
                "Die neue Version ist nicht richtig gestartet, alles läuft wieder wie vorher. "
                "Schick das Protokoll an die Person, die dich eingeladen hat."
            )
        else:
            satz = (
                "Die neue Version ist nicht richtig gestartet, und die vorige läuft noch nicht "
                "richtig. Schick das Protokoll an die Person, die dich eingeladen hat."
            )
    return _vorgang_seite(
        request,
        "Aktualisieren",
        "Das Update hat nicht geklappt",
        zeilen,
        meldung={"ok": False, "satz": satz},
        technik=technik,
        knopf=PROTOKOLL_KNOPF,
    )


# --- Neu starten ---------------------------------------------------------------------------


@router.get("/neustart", response_class=HTMLResponse)
def neustart(request: Request):
    return _seite(request, "wartung_neustart.html", {})


@router.post("/neustart")
def neustart_starten(request: Request):
    return _starte(request, _neustart(request))


NEUSTART_TEXTE = {
    "anhalten": {},
    "start": {"ok": "Vorlesezeit läuft"},
    "tunnel": {"ok": "Verbindung ins Internet steht"},
    "aussen": {},
}
NEUSTART_GRUNDTEXT = {
    "anhalten": "Angehalten",
    "start": "Vorlesezeit startet",
    "tunnel": "Verbindung ins Internet",
    "aussen": "Von außen erreichbar",
}


@router.get("/neustart/stand", response_class=HTMLResponse)
def neustart_stand(request: Request):
    v = request.app.state.vorgang
    if not isinstance(v, Neustart):
        return _zu("/wartung/neustart")
    zeilen = [
        Zeile(z, NEUSTART_TEXTE[n].get(z, NEUSTART_GRUNDTEXT[n]), zeichen="✗")
        for n, z in v.schritte.items()
    ]
    if v.fehler is not None:
        return _vorgang_seite(
            request,
            "Neu starten",
            "Vorlesezeit startet neu",
            zeilen,
            meldung={"ok": False, "satz": v.fehler.satz},
            technik=v.fehler.technik,
            knopf=PROTOKOLL_KNOPF,
        )
    if v.fertig:
        return _vorgang_seite(
            request, "Neu starten", "Vorlesezeit läuft wieder", zeilen, knopf=UEBERSICHT_KNOPF
        )
    return _vorgang_seite(request, "Neu starten", "Vorlesezeit startet neu", zeilen)


# --- Anmeldelink ---------------------------------------------------------------------------


@router.get("/anmeldelink", response_class=HTMLResponse)
def anmeldelink(request: Request):
    return _seite(request, "wartung_anmeldelink.html", {"link": None, "meldung": None})


@router.post("/anmeldelink", response_class=HTMLResponse)
def anmeldelink_erzeugen(request: Request):
    w = request.app.state.werkzeuge
    if not w.docker.app_gesund():
        meldung = {
            "satz": "Vorlesezeit läuft gerade nicht, deshalb gibt es keinen Anmeldelink. "
            "Starte es zuerst neu.",
            "technik": None,
            "knopf": {"text": "Neu starten", "href": "/wartung/neustart"},
        }
        return _seite(request, "wartung_anmeldelink.html", {"link": None, "meldung": meldung})
    r = w.docker.anmeldelink()
    pfad = r.ausgabe.strip()
    if r.code != 0 or not _LINK.fullmatch(pfad):
        meldung = {
            "satz": "Der Anmeldelink ließ sich nicht erzeugen. Aktualisiere Vorlesezeit "
            "oder schick das Protokoll an die Person, die dich eingeladen hat.",
            "technik": f"app.auth.anmeldelink: Exit {r.code}, {_letzte_zeile(r.fehler)}",
            "knopf": PROTOKOLL_KNOPF,
        }
        return _seite(request, "wartung_anmeldelink.html", {"link": None, "meldung": meldung})
    jetzt = w.jetzt()
    kontext = {
        "link": f"https://{_adresse(request)}{pfad}",
        "bis": wann(jetzt + timedelta(hours=24), jetzt),
        "meldung": None,
    }
    antwort = _seite(request, "wartung_anmeldelink.html", kontext)
    antwort.headers["Cache-Control"] = "no-store"
    return antwort


# --- Protokoll -----------------------------------------------------------------------------


def _protokoll_datei(request: Request) -> str:
    datum = berlin(request.app.state.werkzeuge.jetzt()).strftime("%Y-%m-%d")
    return f"vorlesezeit-protokoll-{_adresse(request).split('.')[0]}-{datum}.txt"


@router.get("/protokoll", response_class=HTMLResponse)
def protokoll_seite(request: Request):
    kontext = {"datei": _protokoll_datei(request), "geladen": request.app.state.protokoll}
    return _seite(request, "wartung_protokoll.html", kontext)


@router.get("/protokoll/herunterladen")
def protokoll_herunterladen(request: Request):
    w = request.app.state.werkzeuge
    datei = _protokoll_datei(request)
    text = protokoll(_env(request), _adresse(request), w.docker.logs(), w.jetzt())
    vorschau = "\n".join(text.splitlines()[:_VORSCHAU_ZEILEN])
    request.app.state.protokoll = {"datei": datei, "vorschau": vorschau}
    return Response(
        text,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{datei}"',
            "Cache-Control": "no-store",
        },
    )


# --- Notfallblatt --------------------------------------------------------------------------


@router.get("/notfallblatt", response_class=HTMLResponse)
def notfallblatt(request: Request):
    kontext = {
        "datei": _dateiname(_adresse(request)),
        "geladen": request.app.state.notfallblatt_geladen,
    }
    return _seite(request, "wartung_notfallblatt.html", kontext)


@router.get("/notfallblatt/herunterladen")
def notfallblatt_herunterladen(request: Request):
    env, adresse = _env(request), _adresse(request)
    pdf = notfallblatt_pdf(adresse, laufende_version(env), env["CREDENTIALS_KEY"])
    request.app.state.notfallblatt_geladen = True
    return Response(
        pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{_dateiname(adresse)}"',
            "Cache-Control": "no-store",
        },
    )


# --- Deinstallieren ------------------------------------------------------------------------


def _deinstallieren_seite(request: Request, **kontext) -> HTMLResponse:
    adresse = _adresse(request)
    basis = {
        "adresse": adresse,
        "instanz": adresse.split(".")[0],
        "ziel": _ziel(request, _env(request)),
        "loeschen": False,
        "name": "",
        "fehler": None,
    }
    return _seite(request, "wartung_deinstallieren.html", basis | kontext)


@router.get("/deinstallieren", response_class=HTMLResponse)
def deinstallieren(request: Request):
    return _deinstallieren_seite(request)


@router.post("/deinstallieren")
def deinstallieren_starten(
    request: Request, loeschen: str | None = Form(None), name: str = Form("")
):
    u = request.app.state.umgebung
    instanz = _adresse(request).split(".")[0]
    if loeschen and name != instanz:
        fehler = f"Das passt noch nicht. Tippe genau „{instanz}“, klein und ohne Leerzeichen."
        return _deinstallieren_seite(request, loeschen=True, name=name, fehler=fehler)
    vorgang = Deinstallation(
        u.install_dir,
        request.app.state.werkzeuge.docker,
        ziel=_ziel(request, _env(request)),
        image=u.installer_image,
        loeschen=bool(loeschen),
        **_uhr(request),
    )
    return _starte(request, vorgang)


@router.get("/deinstallieren/stand", response_class=HTMLResponse)
def deinstallieren_stand(request: Request):
    v = request.app.state.vorgang
    if not isinstance(v, Deinstallation):
        return _zu("/wartung/deinstallieren")
    if v.fertig:
        # Die Seite geht noch hinaus, dann beendet sich der Assistent.
        request.app.state.werkzeuge.beenden()
        return _seite(
            request, "wartung_entfernt.html", {"loeschen": v.loeschen, "adresse": v.adresse}
        )
    s = v.schritte
    zeilen = [
        Zeile(s["anhalten"], "Angehalten", zeichen="✗"),
        Zeile(s["entfernen"], "Programm entfernen", zeichen="✗"),
    ]
    if v.loeschen:
        zeilen.append(Zeile(s["entfernen"], "Aufnahmen und Datenbank löschen", zeichen="✗"))
        zeilen.append(
            Zeile(s["sicherungen"], f"Sicherungen auf {v.ziel.name} löschen", zeichen="✗")
        )
    if v.fehler is not None:
        return _vorgang_seite(
            request,
            "Deinstallieren",
            "Vorlesezeit wird entfernt",
            zeilen,
            meldung={"ok": False, "satz": v.fehler.satz},
            technik=v.fehler.technik,
            knopf=PROTOKOLL_KNOPF
            if v.fehler.knopf == "Protokoll exportieren"
            else {"text": v.fehler.knopf, "href": "/wartung/deinstallieren"},
        )
    return _vorgang_seite(request, "Deinstallieren", "Vorlesezeit wird entfernt", zeilen)
