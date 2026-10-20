"""Die sieben Bildschirme des Assistenten (Mockup docs/design/one-klick-assistent-mockup.html).

Bis „Installieren“ landet jede Antwort nur in `app.state.antworten`. Jeder
Bildschirm schickt auf den ersten offenen zurück, wenn ein früherer fehlt.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app import __version__
from app.einladungscode import EinladungscodeError, decode, gueltiger_host
from app.mail.smtp import (
    ANMELDUNG_FEHLGESCHLAGEN,
    VERSAND_FEHLGESCHLAGEN,
)
from app.templating import templates as app_templates
from installer.antworten import Antworten, Mail, Sicherung, erster_offener
from installer.docker import SICHERUNGSORDNER
from installer.geraet import MIN_FREI_BYTES, MIN_RAM_BYTES, architektur
from installer.installation import (
    LOKALER_SICHERUNGSORDNER,
    SCHRITTE,
    Lauf,
    env_tauglich,
    lies_env,
    lies_installation,
    schreibe_dateien,
    setze_abgeschlossen,
)
from installer.notfallblatt import notfallblatt_pdf

templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")
templates.env.globals["asset_v"] = app_templates.env.globals["asset_v"]

# Der Ort, an dem die Startdatei die Laufwerke einbindet; daran hängt die
# Zuordnung zum Pfad auf dem Gerät (`docker inspect` des eigenen Containers).
LAUFWERKE_ZIEL = "/laufwerke"
SYSTEMLAUFWERK = {"mac": "Macintosh HD", "windows": "Laufwerk C:", "linux": "Systemlaufwerk"}
_MAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]{2,}$")
UNLESBAR = "Ein \\ am Ende oder vor einem ' kann Vorlesezeit nicht speichern."


class Umleitung(Exception):
    def __init__(self, pfad: str) -> None:
        self.pfad = pfad


def abgeschlossen(request: Request) -> bool:
    installation = lies_installation(request.app.state.umgebung.install_dir)
    return bool(installation and installation.get("abgeschlossen"))


def nicht_abgeschlossen(request: Request) -> None:
    """Nach dem Abschluss ändert die Einrichtung nichts mehr; weiter geht es in der Wartung."""
    if abgeschlossen(request):
        raise Umleitung("/wartung")


router = APIRouter(dependencies=[Depends(nicht_abgeschlossen)])


def setze_fehlerseiten(app: FastAPI) -> None:
    @app.exception_handler(Umleitung)
    def _umleiten(request: Request, exc: Umleitung) -> RedirectResponse:
        return _zu(exc.pfad)


# --- Hilfen ------------------------------------------------------------------------


def _a(request: Request) -> Antworten:
    return request.app.state.antworten


def _zu(pfad: str) -> RedirectResponse:
    return RedirectResponse(pfad, status_code=303)


def _seite(request: Request, name: str, kontext: dict, status: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(request, name, kontext, status_code=status)


def _gb(n: int) -> str:
    return f"{n // 10**9} GB"


# --- Bildschirm 1: Gerät -------------------------------------------------------------


@dataclass(frozen=True)
class Punkt:
    id: str
    titel: str
    meta: str
    bild: str


CHECKLISTE = {
    "mac": [
        Punkt(
            "ruhezustand",
            "Ruhezustand aus",
            "Der Rechner schläft nie ein, auch nachts nicht.",
            "Systemeinstellungen, Energie, „Ruhezustand verhindern“ eingeschaltet",
        ),
        Punkt(
            "docker",
            "Docker startet von selbst",
            "Docker Desktop öffnet sich bei der Anmeldung.",
            "Docker Desktop, Einstellungen, „Start Docker Desktop when you sign in“",
        ),
        Punkt(
            "anmeldung",
            "Automatische Anmeldung",
            "Nach einem Neustart meldet sich der Rechner ohne Passwort an.",
            "Systemeinstellungen, Benutzer:innen & Gruppen, „Automatisch anmelden als“",
        ),
        Punkt(
            "filevault",
            "FileVault aus",
            "Sonst wartet der Mac nach einem Stromausfall auf dein Passwort.",
            "Systemeinstellungen, Datenschutz & Sicherheit, FileVault",
        ),
        Punkt(
            "strom",
            "Nach Stromausfall einschalten",
            "Der Rechner startet von selbst, wenn der Strom wiederkommt.",
            "Systemeinstellungen, Energie, „Nach Stromausfall automatisch starten“",
        ),
    ],
    "windows": [
        Punkt(
            "ruhezustand",
            "Energiesparmodus aus",
            "Der PC geht nie in den Standby.",
            "Einstellungen, System, Netzbetrieb",
        ),
        Punkt(
            "docker",
            "Docker startet von selbst",
            "Docker Desktop öffnet sich bei der Anmeldung.",
            "Docker Desktop, Settings, „Start Docker Desktop when you sign in“",
        ),
        Punkt(
            "anmeldung",
            "Automatische Anmeldung",
            "Nach einem Neustart meldet sich der PC ohne Passwort an.",
            "Einstellungen, Konten, Anmeldeoptionen",
        ),
        Punkt(
            "strom",
            "Nach Stromausfall einschalten",
            "Steht im BIOS, oft „Restore on AC Power Loss“.",
            "BIOS, Power, „Restore on AC Power Loss“ auf „Power On“",
        ),
    ],
    "linux": [
        Punkt(
            "ruhezustand",
            "Ruhezustand aus",
            "Das Gerät schläft nie ein, auch nachts nicht.",
            "Einstellungen, Energie, Bereitschaft „Nie“",
        ),
        Punkt(
            "docker",
            "Docker startet von selbst",
            "Der Docker-Dienst startet beim Hochfahren (systemctl enable docker).",
            "Terminal: sudo systemctl enable docker",
        ),
        Punkt(
            "strom",
            "Nach Stromausfall einschalten",
            "Steht im BIOS, oft „Restore on AC Power Loss“.",
            "BIOS, Power, „Restore on AC Power Loss“ auf „Power On“",
        ),
    ],
}


def _pruefpunkte(a: Antworten, host_os: str) -> tuple[list[dict], bool]:
    """Zeilen der automatischen Prüfung und ob das Gerät hart ungeeignet ist."""
    m, tls = a.messung, a.tls
    arch = architektur(m.maschine)
    prozessor = "Apple-Chip" if host_os == "mac" and arch == "arm64" else f"{arch}"
    zeilen = [
        {
            "ok": arch is not None,
            "titel": "Prozessor passt" if arch else "Prozessor passt nicht",
            "meta": prozessor if arch else f"{m.maschine}, nötig ist amd64 oder arm64",
        },
        {
            "ok": m.frei_bytes >= MIN_FREI_BYTES,
            "titel": "Genug Platz" if m.frei_bytes >= MIN_FREI_BYTES else "Zu wenig Platz",
            "meta": f"{_gb(m.frei_bytes)} frei, nötig sind {_gb(MIN_FREI_BYTES)}",
        },
        {
            "ok": m.ram_bytes >= MIN_RAM_BYTES,
            "titel": "Genug Arbeitsspeicher"
            if m.ram_bytes >= MIN_RAM_BYTES
            else "Zu wenig Arbeitsspeicher",
            "meta": _gb(m.ram_bytes)
            if m.ram_bytes >= MIN_RAM_BYTES
            else f"{_gb(m.ram_bytes)}, nötig sind {_gb(MIN_RAM_BYTES)}",
        },
    ]
    if tls.art == "ok":
        zeilen.append(
            {
                "ok": True,
                "titel": "Verbindung ins Internet ist privat",
                "meta": "Niemand liest unterwegs mit",
            }
        )
    elif tls.art == "proxy":
        zeilen.append(
            {"ok": False, "titel": "Verbindung ins Internet", "meta": "Ein Firmen-Proxy liest mit"}
        )
    else:
        zeilen.append(
            {
                "ok": False,
                "titel": "Keine Verbindung ins Internet",
                "meta": "login.tonies.com ist nicht erreichbar",
            }
        )
    hart = not all(z["ok"] for z in zeilen[:3]) or tls.art == "offline"
    return zeilen, hart


def _messen(request: Request) -> None:
    a, u, w = _a(request), request.app.state.umgebung, request.app.state.werkzeuge
    a.messung = w.messung(u.install_dir)
    a.tls = w.tls()


def _geraet_seite(request: Request) -> HTMLResponse:
    a, u = _a(request), request.app.state.umgebung
    zeilen, hart = _pruefpunkte(a, u.host_os)
    return _seite(
        request,
        "geraet.html",
        {
            "zeilen": zeilen,
            "hart": hart,
            "proxy": a.tls.art == "proxy",
            "aussteller": a.tls.aussteller or "unbekannt",
            "offline": a.tls.art == "offline",
            "punkte": CHECKLISTE[u.host_os],
            "host_os": u.host_os,
            "erledigt": a.erledigt,
        },
    )


@router.get("/geraet", response_class=HTMLResponse)
def geraet(request: Request):
    _messen(request)
    return _geraet_seite(request)


@router.post("/geraet")
def geraet_weiter(request: Request, erledigt: list[str] = Form([]), aktion: str = Form("weiter")):
    a, u = _a(request), request.app.state.umgebung
    a.erledigt = set(erledigt) & {p.id for p in CHECKLISTE[u.host_os]}
    if a.messung is None:
        _messen(request)
    _, hart = _pruefpunkte(a, u.host_os)
    if hart or (a.tls.art == "proxy" and aktion != "trotzdem"):
        return _geraet_seite(request)
    a.geraet_ok = True
    return _zu("/adresse")


# --- Bildschirm 2: Adresse -------------------------------------------------------------


def _code_status(code: str) -> dict:
    try:
        einladung = decode(code)
    except EinladungscodeError as exc:
        kompakt = re.sub(r"\s+", "", code).upper()
        version = re.match(r"^(VZ\d+)-", kompakt)
        if version and version.group(1) != "VZ1":
            technik = (
                f"Code-Version {version.group(1)}, dieser Assistent kennt VZ1 "
                f"(Vorlesezeit {__version__})"
            )
        elif version:
            technik = f"{len(kompakt)} Zeichen gelesen, die Daten darin sind nicht lesbar"
        else:
            technik = None
        return {"ok": False, "meldung": str(exc), "technik": technik}
    s = einladung.smtp
    werte = [einladung.tunnel_token] + ([s.host, s.user, s.key, s.absender] if s else [])
    if not all(env_tauglich(w) for w in werte):
        meldung = (
            "In diesem Code steht ein Zeichen, das Vorlesezeit nicht speichern kann. "
            "Bitte die Person, die dich eingeladen hat, um einen neuen Code."
        )
        return {"ok": False, "meldung": meldung, "technik": None}
    return {"ok": True, "host": einladung.host, "einladung": einladung}


def _adresse_seite(request: Request, **kontext) -> HTMLResponse:
    a = _a(request)
    art = request.query_params.get("art", kontext.pop("art", a.art))
    basis = {
        "art": art if art in ("code", "domain") else "code",
        "status": None,
        "code": "",
        "host": a.host if a.art == "domain" else "",
        "fehler": {},
    }
    if art == "code" and a.art == "code" and a.host:
        basis["status"] = {"ok": True, "host": a.host}
    return _seite(request, "adresse.html", basis | kontext)


@router.get("/adresse", response_class=HTMLResponse)
def adresse(request: Request):
    if offen := erster_offener(_a(request), vor="adresse"):
        return _zu(offen)
    return _adresse_seite(request)


@router.post("/adresse/pruefen", response_class=HTMLResponse)
def adresse_pruefen(request: Request, code: str = Form("")):
    status = _code_status(code)
    status.pop("einladung", None)
    return _seite(request, "_code_status.html", {"status": status})


@router.post("/adresse")
def adresse_weiter(
    request: Request,
    art: str = Form("code"),
    code: str = Form(""),
    host: str = Form(""),
    tunnel_token: str = Form(""),
):
    a = _a(request)
    if offen := erster_offener(a, vor="adresse"):
        return _zu(offen)
    if art == "domain":
        host = host.strip().lower()
        fehler = {}
        if not gueltiger_host(host):
            fehler["host"] = (
                "Das ist keine vollständige Adresse, zum Beispiel advent.familie-berger.de."
            )
        if not tunnel_token.strip():
            fehler["tunnel_token"] = "Trag bitte das Tunnel-Token ein."
        elif not env_tauglich(tunnel_token.strip()):
            fehler["tunnel_token"] = (
                "Dieses Tunnel-Token kann Vorlesezeit nicht speichern. "
                "Kopier es bitte noch einmal ganz."
            )
        if fehler:
            return _adresse_seite(request, art="domain", host=host, fehler=fehler)
        a.art, a.host, a.tunnel_token, a.code_smtp = "domain", host, tunnel_token.strip(), None
    else:
        status = _code_status(code)
        if not status["ok"]:
            return _adresse_seite(request, art="code", status=status, code=code)
        einladung = status["einladung"]
        s = einladung.smtp
        a.art, a.host, a.tunnel_token = "code", einladung.host, einladung.tunnel_token
        a.code_smtp = Mail(s.host, s.port, s.user, s.key, s.absender, aus_code=True) if s else None
    if a.mail is not None and (a.mail.aus_code or a.code_smtp is not None):
        a.mail = None
    return _zu("/du")


# --- Bildschirm 3: Du ---------------------------------------------------------------


@router.get("/du", response_class=HTMLResponse)
def du(request: Request):
    a = _a(request)
    if offen := erster_offener(a, vor="du"):
        return _zu(offen)
    kontext = {"email": a.admin_email or "", "name": a.anzeigename, "fehler": None}
    return _seite(request, "du.html", kontext | {"name_fehler": None})


@router.post("/du")
def du_weiter(request: Request, admin_email: str = Form(""), anzeigename: str = Form("")):
    a = _a(request)
    if offen := erster_offener(a, vor="du"):
        return _zu(offen)
    email, name = admin_email.strip(), anzeigename.strip()
    fehler = None
    if not env_tauglich(email):
        fehler = f"{UNLESBAR} Lass es bitte weg."
    elif not _MAIL.match(email):
        fehler = "Hier fehlt noch etwas, zum Beispiel „.de“ am Ende."
    name_fehler = None if env_tauglich(name) else f"{UNLESBAR} Lass es bitte weg."
    if fehler or name_fehler:
        kontext = {"email": email, "name": name, "fehler": fehler, "name_fehler": name_fehler}
        return _seite(request, "du.html", kontext)
    a.admin_email, a.anzeigename = email, name
    return _zu("/mail")


# --- Bildschirm 4: Mail -------------------------------------------------------------


@dataclass(frozen=True)
class Anbieter:
    name: str
    host: str
    login: str
    passwort: str
    hinweis: str
    abgelehnt: str
    eigener_absender: bool = False


ANBIETER = {
    "gmail": Anbieter(
        "Gmail",
        "smtp.gmail.com",
        "Gmail-Adresse",
        "App-Passwort",
        "Gmail braucht dafür ein eigenes App-Passwort, nicht dein normales.",
        "Gmail hat die Anmeldung abgelehnt. "
        "Trag bitte ein App-Passwort ein, nicht dein normales Passwort.",
    ),
    "gmx": Anbieter(
        "GMX",
        "mail.gmx.net",
        "GMX-Adresse",
        "Passwort",
        "In GMX muss unter E-Mail, Einstellungen „POP3/IMAP Abruf“ erlaubt sein.",
        "GMX hat die Anmeldung abgelehnt. Prüf Adresse und Passwort und ob POP3/IMAP erlaubt ist.",
    ),
    "webde": Anbieter(
        "web.de",
        "smtp.web.de",
        "WEB.DE-Adresse",
        "Passwort",
        "In WEB.DE muss unter E-Mail, Einstellungen „POP3/IMAP Abruf“ erlaubt sein.",
        "WEB.DE hat die Anmeldung abgelehnt. "
        "Prüf Adresse und Passwort und ob POP3/IMAP erlaubt ist.",
    ),
    "brevo": Anbieter(
        "Brevo",
        "smtp-relay.brevo.com",
        "SMTP-Login",
        "SMTP-Schlüssel",
        "Login und Schlüssel stehen in Brevo unter SMTP & API, SMTP.",
        "Brevo hat die Anmeldung abgelehnt. Prüf SMTP-Login und SMTP-Schlüssel.",
        eigener_absender=True,
    ),
}
SMTP_PORT = 587


def _mail_seite(
    request: Request,
    *,
    anbieter: str | None = None,
    adresse: str | None = None,
    absender: str | None = None,
    meldung: dict | None = None,
) -> HTMLResponse:
    a = _a(request)
    if a.code_smtp is not None:
        return _seite(
            request,
            "mail_code.html",
            {
                "name": a.anzeigename,
                "absender": a.code_smtp.absender,
                "admin": a.admin_email,
            },
        )
    gespeichert = a.mail if a.mail and not a.mail.aus_code else None
    anbieter = (
        anbieter
        or request.query_params.get("anbieter")
        or (gespeichert.anbieter if gespeichert else "gmail")
    )
    if anbieter not in ANBIETER:
        anbieter = "gmail"
    if adresse is None:
        adresse = gespeichert.user if gespeichert and gespeichert.anbieter == anbieter else ""
    if absender is None:
        absender = gespeichert.absender if gespeichert and gespeichert.anbieter == anbieter else ""
    return _seite(
        request,
        "mail.html",
        {
            "anbieter": anbieter,
            "alle": ANBIETER,
            "a": ANBIETER[anbieter],
            "adresse": adresse,
            "absender": absender,
            "port": SMTP_PORT,
            "meldung": meldung,
            "geprueft": gespeichert is not None and gespeichert.anbieter == anbieter,
        },
    )


@router.get("/mail", response_class=HTMLResponse)
def mail(request: Request):
    if offen := erster_offener(_a(request), vor="mail"):
        return _zu(offen)
    return _mail_seite(request)


def _testmail_meldung(grund: str | None, anbieter: Anbieter, admin: str) -> dict:
    if grund is None:
        return {"ok": True, "satz": f"Die Testmail ist raus. Schau in dein Postfach {admin}."}
    if grund == ANMELDUNG_FEHLGESCHLAGEN:
        satz = anbieter.abgelehnt
    elif grund == VERSAND_FEHLGESCHLAGEN:
        satz = (
            "Die Anmeldung hat geklappt, die Testmail ging aber nicht raus. "
            "Prüf die Absenderadresse."
        )
    else:
        satz = (
            f"Der Mailserver von {anbieter.name} antwortet nicht. "
            "Prüf die Internetverbindung und versuch es noch einmal."
        )
    return {"ok": False, "satz": satz, "technik": f"{anbieter.host}:{SMTP_PORT}: {grund}"}


@router.post("/mail")
def mail_weiter(
    request: Request,
    aktion: str = Form("weiter"),
    anbieter: str = Form("gmail"),
    adresse: str = Form(""),
    passwort: str = Form(""),
    absender: str = Form(""),
):
    a, w = _a(request), request.app.state.werkzeuge
    if offen := erster_offener(a, vor="mail"):
        return _zu(offen)
    if a.code_smtp is not None:
        a.mail = a.code_smtp
        return _zu("/sicherung")
    anbieter = anbieter if anbieter in ANBIETER else "gmail"
    vorlage = ANBIETER[anbieter]
    adresse, absender = adresse.strip(), absender.strip()
    von = absender if vorlage.eigener_absender else adresse
    seite = {"anbieter": anbieter, "adresse": adresse, "absender": absender}

    if aktion == "test":
        if not adresse or not passwort or not von:
            meldung = {"ok": False, "satz": "Trag bitte alle Felder ein."}
            return _mail_seite(request, **seite, meldung=meldung)
        if not all(env_tauglich(w) for w in (adresse, passwort, von)):
            satz = (
                f"{UNLESBAR} Lass das \\ bitte weg. "
                "Steht es so in deinem Passwort, leg bitte ein anderes an."
            )
            return _mail_seite(request, **seite, meldung={"ok": False, "satz": satz})
        ergebnis = w.smtp_pruefung(
            host=vorlage.host,
            port=SMTP_PORT,
            user=adresse,
            password=passwort,
            from_address=von,
            to_address=a.admin_email,
        )
        a.mail = (
            Mail(vorlage.host, SMTP_PORT, adresse, passwort, von, anbieter=anbieter)
            if ergebnis.ok
            else None
        )
        meldung = _testmail_meldung(None if ergebnis.ok else ergebnis.grund, vorlage, a.admin_email)
        return _mail_seite(request, **seite, meldung=meldung)

    m = a.mail
    unveraendert = (
        m is not None
        and not m.aus_code
        and m.anbieter == anbieter
        and m.user == adresse
        and m.absender == von
        and passwort in ("", m.passwort)
    )
    if unveraendert:
        return _zu("/sicherung")
    meldung = {
        "ok": False,
        "satz": "Schick bitte zuerst eine Testmail. Erst wenn sie ankommt, geht es weiter.",
    }
    return _mail_seite(request, **seite, meldung=meldung)


# --- Bildschirm 5: Sicherung -----------------------------------------------------------


@dataclass(frozen=True)
class Laufwerk:
    id: str
    name: str
    frei: str
    system: bool = False


def _laufwerke(request: Request) -> list[Laufwerk]:
    u = request.app.state.umgebung
    gefunden = []
    if u.laufwerke_dir.is_dir():
        for pfad in sorted(u.laufwerke_dir.iterdir()):
            # Symlinks wie „Macintosh HD“ -> / zeigen im Container ins eigene System.
            if pfad.is_dir() and not pfad.is_symlink():
                frei = _gb(shutil.disk_usage(pfad).free)
                gefunden.append(Laufwerk(pfad.name, pfad.name, frei))
    frei = _gb(shutil.disk_usage(u.install_dir).free)
    gefunden.append(Laufwerk("", SYSTEMLAUFWERK[u.host_os], frei, system=True))
    return gefunden


def _lokal(request: Request, name: str) -> Sicherung:
    u = request.app.state.umgebung
    trenner = "\\" if u.host_os == "windows" else "/"
    pfad = u.host_install_dir.rstrip(trenner) + trenner + LOKALER_SICHERUNGSORDNER
    return Sicherung(pfad=pfad, name=name, lokal=True)


def _sicherung_seite(request: Request, **kontext) -> HTMLResponse:
    a = _a(request)
    gewaehlt = None
    if a.sicherung is not None and not a.sicherung.lokal:
        gewaehlt = a.sicherung.name
    basis = {
        "laufwerke": _laufwerke(request),
        "gewaehlt": gewaehlt,
        "fehler_id": None,
        "meldung": None,
    }
    return _seite(request, "sicherung.html", basis | kontext)


@router.get("/sicherung", response_class=HTMLResponse)
def sicherung(request: Request):
    if offen := erster_offener(_a(request), vor="sicherung"):
        return _zu(offen)
    return _sicherung_seite(request)


@router.post("/sicherung")
def sicherung_weiter(
    request: Request, aktion: str = Form("suchen"), laufwerk: str | None = Form(None)
):
    a, u, w = _a(request), request.app.state.umgebung, request.app.state.werkzeuge
    if offen := erster_offener(a, vor="sicherung"):
        return _zu(offen)
    if aktion == "spaeter":
        a.sicherung = _lokal(request, "Später, bis dahin auf diesem Rechner")
        return _zu("/zusammenfassung")
    if aktion != "sichern" or laufwerk is None:
        return _sicherung_seite(request)
    if laufwerk == "":
        a.sicherung = _lokal(request, f"{SYSTEMLAUFWERK[u.host_os]}, dieser Rechner")
        return _zu("/zusammenfassung")
    if laufwerk not in {lw.id for lw in _laufwerke(request) if not lw.system}:
        return _sicherung_seite(request)
    container_pfad = f"{LAUFWERKE_ZIEL}/{laufwerk}"
    host = w.docker.host_pfad(container_pfad)
    ergebnis = w.docker.ordner_anlegen(u.installer_image, host) if host else None
    if ergebnis is None or ergebnis.code != 0:
        grund = (
            ergebnis.fehler.strip().splitlines()[-1]
            if ergebnis and ergebnis.fehler.strip()
            else "Laufwerk nicht gefunden"
        )
        return _sicherung_seite(
            request,
            gewaehlt=laufwerk,
            fehler_id=laufwerk,
            meldung={
                "ok": False,
                "satz": "Auf dieses Laufwerk kann Vorlesezeit nicht schreiben. "
                "Wähle bitte ein anderes Laufwerk.",
                "technik": f"{container_pfad}: {grund}",
            },
        )
    a.sicherung = Sicherung(pfad=f"{host}/{SICHERUNGSORDNER}", name=laufwerk)
    return _sicherung_seite(
        request,
        gewaehlt=laufwerk,
        meldung={
            "ok": True,
            "satz": f"Fertig. Ab heute Nacht landet jede Sicherung im Ordner "
            f"„{SICHERUNGSORDNER}“ auf {laufwerk}.",
        },
    )


# --- Bildschirm 6: Zusammenfassung und Installieren ----------------------------------------


def _geraet_zeile(request: Request) -> str:
    a, u = _a(request), request.app.state.umgebung
    offen = len(CHECKLISTE[u.host_os]) - len(a.erledigt)
    basis = "Firmen-Proxy liest mit" if a.tls and a.tls.art == "proxy" else "passt"
    if offen == 0:
        return f"{basis}, alle Einstellungen erledigt"
    return f"{basis}, {offen} Einstellung{'en' if offen > 1 else ''} noch offen"


@router.get("/zusammenfassung", response_class=HTMLResponse)
def zusammenfassung(request: Request):
    a = _a(request)
    if offen := erster_offener(a):
        return _zu(offen)
    return _seite(request, "zusammenfassung.html", {"a": a, "geraet": _geraet_zeile(request)})


def _starte_lauf(request: Request) -> None:
    app, u, w = request.app, request.app.state.umgebung, request.app.state.werkzeuge
    if app.state.lauf is not None and app.state.lauf.laeuft:
        return
    m = _a(request).messung or w.messung(u.install_dir)
    app.state.lauf = Lauf(
        u.install_dir,
        w.docker,
        w.http,
        uhr=w.uhr,
        schlafen=w.schlafen,
        architektur=architektur(m.maschine) or m.maschine,
    )
    w.starte(app.state.lauf.ausfuehren)


@router.post("/installieren")
def installieren(request: Request):
    a = _a(request)
    if offen := erster_offener(a):
        return _zu(offen)
    lauf = request.app.state.lauf
    if lauf is not None and lauf.laeuft:  # der Lauf liest gerade die Dateien
        return _zu("/installieren")
    schreibe_dateien(request.app.state.umgebung.install_dir, a)
    _starte_lauf(request)
    return _zu("/installieren")


@router.post("/installieren/wiederholen")
def wiederholen(request: Request):
    if (request.app.state.umgebung.install_dir / ".env").exists():
        _starte_lauf(request)
    return _zu("/installieren")


TEXTE = {
    "einstellungen": {"fehler": "Einstellungen speichern"},
    "laden": {"laeuft": "Programm wird geladen", "ok": "Programm geladen"},
    "start": {"ok": "Vorlesezeit läuft"},
    "tunnel": {"ok": "Verbindung ins Internet steht"},
    "aussen": {},
}
GRUNDTEXT = {
    "einstellungen": "Einstellungen gespeichert",
    "laden": "Programm laden",
    "start": "Vorlesezeit startet",
    "tunnel": "Verbindung ins Internet",
    "aussen": "Von außen erreichbar",
}


@router.get("/installieren", response_class=HTMLResponse)
def installieren_stand(request: Request):
    u = request.app.state.umgebung
    lauf = request.app.state.lauf
    if lauf is None:
        if not (u.install_dir / ".env").exists():
            return _zu(erster_offener(_a(request)) or "/zusammenfassung")
        # Neustart der Startdatei mitten in der Installation: hier geht es weiter.
        _starte_lauf(request)
        lauf = request.app.state.lauf
    if lauf.fertig:
        return _zu("/fertig")
    zeilen = []
    for name in SCHRITTE:
        zustand = lauf.schritte[name]
        if name == "einstellungen" and zustand == "wartet":
            zustand = "ok"  # die Dateien schreibt schon „Installieren“
        meta = None
        if (
            name == "aussen"
            and zustand == "fehler"
            and lauf.fehler.knopf == "Erneut prüfen"
            and "antwortet von außen" in lauf.fehler.satz
        ):
            meta = "seit 5 Minuten keine Antwort"
        zeilen.append(
            {"zustand": zustand, "meta": meta, "text": TEXTE[name].get(zustand, GRUNDTEXT[name])}
        )
    return _seite(request, "installieren.html", {"zeilen": zeilen, "fehler": lauf.fehler})


# --- Bildschirm 7: Fertig -----------------------------------------------------------------


def _fertig_kontext(request: Request, fehler: str | None = None) -> dict:
    installation = lies_installation(request.app.state.umgebung.install_dir)
    return {
        "adresse": installation["adresse"],
        "admin": installation["admin_email"],
        "datei": _dateiname(installation["adresse"]),
        "fehler": fehler,
        "geladen": request.app.state.notfallblatt_geladen,
    }


def _dateiname(adresse: str) -> str:
    return f"Vorlesezeit-Notfallblatt-{adresse.split('.')[0]}.pdf"


def _fertig_noetig(request: Request) -> RedirectResponse | None:
    lauf = request.app.state.lauf
    return None if lauf is not None and lauf.fertig else _zu("/installieren")


@router.get("/fertig", response_class=HTMLResponse)
def fertig(request: Request):
    if umleitung := _fertig_noetig(request):
        return umleitung
    return _seite(request, "fertig.html", _fertig_kontext(request))


@router.get("/notfallblatt")
def notfallblatt(request: Request):
    if umleitung := _fertig_noetig(request):
        return umleitung
    install_dir = request.app.state.umgebung.install_dir
    installation = lies_installation(install_dir)
    key = lies_env(install_dir / ".env")["CREDENTIALS_KEY"]
    pdf = notfallblatt_pdf(installation["adresse"], installation["version"], key)
    request.app.state.notfallblatt_geladen = True
    datei = _dateiname(installation["adresse"])
    return Response(
        pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{datei}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/abschliessen", response_class=HTMLResponse)
def abschliessen(request: Request, gespeichert: str | None = Form(None)):
    if umleitung := _fertig_noetig(request):
        return umleitung
    if not gespeichert:
        fehler = "Hak bitte zuerst an, dass du das Notfallblatt gespeichert hast."
        return _seite(request, "fertig.html", _fertig_kontext(request, fehler))
    setze_abgeschlossen(request.app.state.umgebung.install_dir)
    request.app.state.werkzeuge.beenden()
    return _seite(request, "geschafft.html", {})
