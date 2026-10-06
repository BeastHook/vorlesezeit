"""Mehrkalender U11: Reiter Setup (R17-R26, R37, R41-R43; KTD13, KTD15, KTD16).

Kalender, tonies-Konten mit ihren Tonies und deren Kalender-Zuordnung
(beide auch loeschbar, mit Rueckfrage),
Mailversand, Termine und Anzeigename. Geaenderte Zugangsdaten gelten erst
nach bestandener Pruefung (R19): Konto per Test-Anmeldung, die zugleich die
Tonies listet, SMTP per Testmail an den Admin. Gespeicherte Passwoerter
erscheinen nie im HTML (R20) -- das Feld bleibt leer, ein Etikett sagt
"gesetzt".

Alle Routen sind plain `def` (Known Pitfalls: Pruefungen blockieren). Jeder
POST prueft nach der Admin-Pruefung den `Origin`-Header (KTD13):

    Origin == "<Schema>://<Host>" der Anfrage, so wie Starlette sie sieht.

Hinter dem Tunnel setzt Uvicorn (`--proxy-headers --forwarded-allow-ips=*`,
Dockerfile) das Schema aus `X-Forwarded-Proto`; der Host kommt aus dem
`Host`-Header, den der Cloudflare Tunnel unveraendert weiterreicht. Fehlt der
Header, ist er "null" oder passt er nicht: 403. Browser senden `Origin` bei
jedem POST-Formular mit.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app import settings
from app.admin.common import flash, redirect, render
from app.admin.setup import new_campaign
from app.auth.dependencies import get_db, require_admin
from app.crypto import CredentialsUnavailable, decrypt
from app.delivery.chapters import load_app_chapters
from app.delivery.job import TonieBusyError, client_for, detach_tonie, tonie_run_active
from app.delivery.trigger import get_now, get_toniecloud_factory
from app.mail.magic_link import german_date
from app.mail.smtp import check_smtp
from app.models import Auftrag, Campaign, CreativeTonie, DeliveryRun, Person, Slot, TonieKonto
from app.toniecloud.client import (
    KontoPruefung,
    LoginTimeoutError,
    TonieCloudError,
    TonieCloudFactory,
    check_konto,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/setup")

PAGE = "/admin/setup"

KONTO_ZEITUEBERSCHREITUNG = "Zeitüberschreitung: Die Toniecloud hat nicht geantwortet."
KONTO_NICHT_ERREICHBAR = "Die Toniecloud ist gerade nicht erreichbar."
SMTP_PASSWORT_NEU = "Server geändert. Bitte das Passwort für den neuen Zugang eingeben."
LIEFERZEIT_FENSTER = "Die Lieferzeit muss zwischen 17:00 und 23:00 liegen."
ABRAEUMEN_OFFEN = "Das Kapitel konnte nicht entfernt werden. Die App versucht es am 25.12. erneut."

FELD_NAMEN = {
    "smtp_host": "Server",
    "smtp_port": "Port",
    "smtp_user": "Benutzer",
    "smtp_password": "Passwort",
    "smtp_from_address": "Absender",
    "delivery_time": "Lieferzeit",
    "recording_deadline": "Aufnahmefrist",
    "invitation_date": "Einladungstermin",
    "magic_link_valid_until": "Anmeldelinks gültig bis",
    "admin_display_name": "Name",
    "tonie_username": "E-Mail",
    "tonie_password": "Passwort",
}
SMTP_FELDER = ("smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_from_address")
TERMIN_FELDER = ("delivery_time", "recording_deadline", "invitation_date", "magic_link_valid_until")


# --- Abhaengigkeiten ------------------------------------------------------------


def get_konto_check() -> Callable[[str, str], KontoPruefung]:
    """Test-Anmeldung (U6). Tests ersetzen das mit einem MockTransport."""
    return check_konto


def setup_admin(request: Request, admin: Person = Depends(require_admin)) -> Person:
    """KTD13: erst die Admin-Pruefung (nicht angemeldet -> wie ueberall /login),
    dann der Origin-Vergleich."""
    own = f"{request.url.scheme}://{request.url.netloc}"
    if request.headers.get("origin") != own:
        raise HTTPException(status_code=403, detail="Formular kommt nicht von dieser Seite.")
    return admin


async def _form(request: Request) -> FormData:
    # Mehrfachfelder (Zuordnung) liest eine async-Dependency, die Route bleibt `def`.
    return await request.form()


# --- Anzeige ----------------------------------------------------------------------


def mask_email(value: str) -> str:
    """Konten erscheinen nur maskiert (Mockup): "o…@example.test"."""
    local, at, domain = value.partition("@")
    return f"{local[:1]}…@{domain}" if at else f"{value[:1]}…"


def short_id(tonie_id: str) -> str:
    return f"…{tonie_id[-4:]}"


def when(value: datetime | None) -> str:
    return "" if value is None else f"{german_date(value)}, {value:%H:%M}"


@dataclass
class Hinweise:
    """R41/R42 fuer einen Abschnitt: aus der Umgebung uebernommen, noch
    ungeprueft, und welche Felder in der Umgebung inzwischen anders stehen."""

    umgebung: bool = False
    ungeprueft: bool = False
    abweichend: list[str] = field(default_factory=list)


def _hinweise(data: dict, names, *, konto_id: int | None = None) -> Hinweise:
    result = Hinweise()
    for name in names:
        entry = data.get(name)
        if entry is None or (konto_id is not None and entry.get("konto_id") != konto_id):
            continue
        result.umgebung |= entry.get("quelle") == "umgebung"
        result.ungeprueft |= bool(entry.get("ungeprueft"))
        if entry.get("abweichung"):
            result.abweichend.append(FELD_NAMEN[name])
    return result


@dataclass
class TonieZeile:
    tonie: CreativeTonie
    busy: bool
    # Nur im Pruefergebnis: der Tonie haengt bisher an einem anderen Konto.
    anderes_konto: TonieKonto | None = None


@dataclass
class KontoPanel:
    konto: TonieKonto
    tonies: list[TonieZeile]
    hinweise: Hinweise
    geprueft: bool = False  # in dieser Antwort frisch geprueft
    fehler: str | None = None

    @property
    def busy(self) -> list[str]:
        return [z.tonie.name or short_id(z.tonie.tonie_id) for z in self.tonies if z.busy]


def _zeile(tonie: CreativeTonie, konto: TonieKonto | None = None) -> TonieZeile:
    other = tonie.konto if konto is not None and tonie.konto_id != konto.id else None
    return TonieZeile(tonie, tonie_run_active(tonie.tonie_id), other)


def _konto_panels(
    db: Session, data: dict, geprueft: dict[int, list[CreativeTonie]], fehler: dict
) -> list[KontoPanel]:
    panels = []
    for konto in db.scalars(select(TonieKonto).order_by(TonieKonto.id)):
        if konto.id in geprueft:
            zeilen = [_zeile(t, konto) for t in geprueft[konto.id]]
        else:
            zeilen = [_zeile(t) for t in sorted(konto.tonies, key=lambda t: t.id)]
        panels.append(
            KontoPanel(
                konto,
                zeilen,
                _hinweise(data, ("tonie_username", "tonie_password"), konto_id=konto.id),
                geprueft=konto.id in geprueft,
                fehler=fehler.get(f"konto_{konto.id}"),
            )
        )
    return panels


@dataclass
class KalenderPanel:
    calendar: Campaign
    tonies: list[CreativeTonie]
    vergeben: int


def _kalender_panels(db: Session) -> list[KalenderPanel]:
    vergeben = dict(
        db.execute(
            select(Slot.campaign_id, func.count(Slot.id))
            .where(Slot.auftrag_id.is_not(None))
            .group_by(Slot.campaign_id)
        ).all()
    )
    return [
        KalenderPanel(c, sorted(c.tonies, key=lambda t: t.id), vergeben.get(c.id, 0))
        for c in db.scalars(select(Campaign).order_by(Campaign.id))
    ]


def _time_text(row) -> str:
    return row.delivery_time_pending or row.delivery_time or ""


def _page(
    request: Request,
    db: Session,
    *,
    geprueft: dict[int, list[CreativeTonie]] | None = None,
    fehler: dict | None = None,
    eingaben: dict | None = None,
) -> HTMLResponse:
    fehler = fehler or {}
    row = settings.get_einstellungen(db)
    data = settings.herkunft(db)
    smtp = _hinweise(data, SMTP_FELDER)
    smtp_status = (
        "neu"
        if row.smtp_needs_reentry
        else "leer"
        if row.smtp_password is None
        else "ungeprueft"
        if row.smtp_checked_at is None or smtp.ungeprueft
        else "geprueft"
    )
    smtp_values = {
        "host": row.smtp_host or "",
        "port": row.smtp_port or "",
        "user": row.smtp_user or "",
        "from_address": row.smtp_from_address or "",
    }
    termine_values = {
        "delivery_time": _time_text(row),
        "recording_deadline": settings.recording_deadline(db).isoformat(),
        "invitation_date": row.invitation_date.isoformat() if row.invitation_date else "",
        "magic_link_valid_until": settings.magic_link_valid_until(db).isoformat(),
    }
    if eingaben and "smtp" in eingaben:
        smtp_values = eingaben["smtp"]
    if eingaben and "termine" in eingaben:
        termine_values = eingaben["termine"]
    return render(
        request,
        "admin/setup.html",
        {
            "kalender": _kalender_panels(db),
            "calendars": db.scalars(select(Campaign).order_by(Campaign.id)).all(),
            "konten": _konto_panels(db, data, geprueft or {}, fehler),
            "neu_eingeben": settings.needs_reentry(db),
            "smtp": smtp_values,
            "smtp_status": smtp_status,
            "smtp_hinweise": smtp,
            "smtp_checked": when(row.smtp_checked_at),
            "admin_email": mask_email(request.app.state.config.admin_email),
            "termine": termine_values,
            "termine_hinweise": _hinweise(data, TERMIN_FELDER),
            "delivery_pending": row.delivery_time_pending,
            "delivery_pending_from": row.delivery_time_pending_from,
            "current_delivery": row.delivery_time or "",
            "name_hinweise": _hinweise(data, ("admin_display_name",)),
            "display_name": row.admin_display_name or "",
            "kind_name": row.kind_name or "",
            "fehler": fehler,
            "eingaben": eingaben or {},
            "mask_email": mask_email,
            "short_id": short_id,
            "when": when,
            "german_date": german_date,
        },
        tab="setup",
    )


@router.get("", response_class=HTMLResponse)
def setup_page(request: Request, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    return _page(request, db)


@router.get("/status")
def setup_status(db: Session = Depends(get_db), _admin=Depends(require_admin)):
    """Live-Status (Mockup 2026-10-03): welche Tonies gerade ein Lauf sperrt,
    als Zeilennummer der Datenbank, nie als Tonie-ID."""
    tonies = db.scalars(select(CreativeTonie).order_by(CreativeTonie.id))
    return {"busy": [t.id for t in tonies if tonie_run_active(t.tonie_id)]}


# --- Kalender (R17) ------------------------------------------------------------------


def create_calendar(db: Session, name: str) -> Campaign:
    """Ein Kalender mit 24 Tagen. `app/admin/setup.py::create_campaign`
    erlaubt nur eine Kampagne; mehrere Kalender legt das Setup an."""
    calendar = new_campaign(db, name=name)
    db.commit()
    return calendar


@router.post("/kalender", response_class=HTMLResponse)
def create_calendar_route(
    request: Request,
    name: str = Form(""),
    db: Session = Depends(get_db),
    _admin=Depends(setup_admin),
):
    name = name.strip()
    if not name:
        flash(request, "Bitte einen Namen für den Kalender eingeben.", "err")
        return redirect(f"{PAGE}#s-kal")
    create_calendar(db, name)
    flash(request, f"Kalender „{name}“ mit 24 Tagen angelegt.")
    return redirect(f"{PAGE}#s-kal")


@router.post("/kalender/{calendar_id}", response_class=HTMLResponse)
def rename_calendar_route(
    calendar_id: int,
    request: Request,
    name: str = Form(""),
    db: Session = Depends(get_db),
    _admin=Depends(setup_admin),
):
    calendar = db.get(Campaign, calendar_id)
    if calendar is None:
        raise HTTPException(status_code=404)
    name = name.strip()
    if not name:
        flash(request, "Ein Kalender braucht einen Namen.", "err")
    else:
        calendar.name = name
        db.commit()
        flash(request, f"Kalender heißt jetzt „{name}“.")
    return redirect(f"{PAGE}#s-kal")


def _calendar(db: Session, calendar_id: int) -> Campaign:
    calendar = db.get(Campaign, calendar_id)
    if calendar is None:
        raise HTTPException(status_code=404)
    return calendar


def calendar_delete_blocker(db: Session, calendar: Campaign) -> str | None:
    """Erst trennen (das raeumt die App-Kapitel ab), und der letzte Kalender
    bleibt -- er ist der Platzhalter fuer Laeufe ohne Kalender (KTD10)."""
    if calendar.tonies:
        name = calendar.tonies[0].name or short_id(calendar.tonies[0].tonie_id)
        return f"Zu „{calendar.name}“ gehört noch {name}. Bitte den Tonie zuerst trennen."
    if db.scalar(select(func.count(Campaign.id))) == 1:
        return "Der letzte Kalender lässt sich nicht löschen."
    return None


def drafts_after_delete(db: Session, calendar: Campaign) -> list[Auftrag]:
    """Auftraege, die nur in diesem Kalender liegen und danach Entwurf sind."""
    auftraege = db.scalars(
        select(Auftrag).join(Slot).where(Slot.campaign_id == calendar.id).distinct()
    ).all()
    return [a for a in auftraege if all(s.campaign_id == calendar.id for s in a.slots)]


# Laeufe ohne Tagesbezug, die auch ein Tonie ohne Kalender macht -- sie tragen
# dann den Platzhalter (KTD10) und gehoeren nicht zum Verlauf des Kalenders.
PLATZHALTER_LAEUFE = ("manuell", "abraeumen", "aufraeumen")


def delete_calendar(db: Session, calendar: Campaign) -> None:
    """Kalender komplett loeschen: Tage und Verlauf gehen, Auftraege nur hier
    werden Entwurf, Aufnahmen bleiben. Ist er der Platzhalter, wandern die
    Laeufe ohne Tagesbezug zum naechsten Kalender, dem neuen Platzhalter
    (Heuristik, Nutzerentscheidung 2026-10-03)."""
    ids = db.scalars(select(Campaign.id).order_by(Campaign.id)).all()
    successor = next(i for i in ids if i != calendar.id)
    for run in db.scalars(select(DeliveryRun).where(DeliveryRun.campaign_id == calendar.id)):
        if calendar.id == ids[0] and run.run_type in PLATZHALTER_LAEUFE:
            run.campaign_id = successor
        else:
            db.delete(run)
    for slot in calendar.slots:
        db.delete(slot)
    db.delete(calendar)
    db.commit()


@router.get("/kalender/{calendar_id}/loeschen", response_class=HTMLResponse)
def delete_calendar_page(
    calendar_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    calendar = _calendar(db, calendar_id)
    return render(
        request,
        "admin/setup_kalender_loeschen.html",
        {
            "calendar": calendar,
            "blocker": calendar_delete_blocker(db, calendar),
            "drafts": drafts_after_delete(db, calendar),
        },
        tab="setup",
    )


@router.post("/kalender/{calendar_id}/loeschen", response_class=HTMLResponse)
def delete_calendar_route(
    calendar_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _admin=Depends(setup_admin),
):
    calendar = _calendar(db, calendar_id)
    blocker = calendar_delete_blocker(db, calendar)
    if blocker is not None:
        flash(request, blocker, "err")
    else:
        name = calendar.name
        delete_calendar(db, calendar)
        flash(request, f"Kalender „{name}“ ist gelöscht.")
    return redirect(f"{PAGE}#s-kal")


# --- tonies-Konten (R19, R20, R21, R25) -------------------------------------------------


def _run_check(
    check: Callable[[str, str], KontoPruefung], username: str, password: str
) -> KontoPruefung:
    """Die Tonies des Kontos oder der Grund des Scheiterns (nie mit Passwort,
    R20); Netzfehler werden zum Grund."""
    try:
        return check(username, password)
    except (LoginTimeoutError, httpx.TimeoutException):
        return KontoPruefung(fehler=KONTO_ZEITUEBERSCHREITUNG)
    except (TonieCloudError, httpx.HTTPError):
        logger.warning("Konto-Pruefung: Toniecloud nicht erreichbar", exc_info=True)
        return KontoPruefung(fehler=KONTO_NICHT_ERREICHBAR)


def _link_tonies(db: Session, konto: TonieKonto, listed: list[dict]) -> list[CreativeTonie]:
    """Neue Tonies des Kontos immer "ohne Kalender" (Abnahme 2026-10-02); eine
    schon bekannte Tonie-ID wird nicht doppelt angelegt und bleibt bei ihrem
    Konto, bis der Admin sie aus dieser Liste uebernimmt (Zuordnung)."""
    rows = []
    for item in listed:
        tonie = db.scalars(
            select(CreativeTonie).where(CreativeTonie.tonie_id == item["id"])
        ).first()
        if tonie is None:
            tonie = CreativeTonie(tonie_id=item["id"], konto_id=konto.id, name=item.get("name", ""))
            db.add(tonie)
        elif tonie.konto_id is None:
            tonie.konto_id = konto.id
        if tonie.konto_id == konto.id and item.get("name"):
            tonie.name = item["name"]
        rows.append(tonie)
    db.commit()
    return rows


def _busy_tonie(konto: TonieKonto) -> CreativeTonie | None:
    return next((t for t in konto.tonies if tonie_run_active(t.tonie_id)), None)


def _busy_message(tonie: CreativeTonie) -> str:
    name = tonie.name or short_id(tonie.tonie_id)
    return (
        f"Für {name} läuft gerade ein Lauf. Zugangsdaten und Zuordnung sind gesperrt, "
        "bis der Lauf endet. Bitte später erneut versuchen."
    )


@router.post("/konten", response_class=HTMLResponse)
def create_konto_route(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
    check: Callable[[str, str], KontoPruefung] = Depends(get_konto_check),
    _admin=Depends(setup_admin),
):
    username = username.strip()
    eingaben = {"konto_neu": {"username": username}}
    if not username or not password:
        fehler = {"konto_neu": "Bitte E-Mail und Passwort des tonies-Kontos eingeben."}
        return _page(request, db, fehler=fehler, eingaben=eingaben)
    result = _run_check(check, username, password)
    if not result.ok:
        return _page(request, db, fehler={"konto_neu": result.fehler}, eingaben=eingaben)
    now = get_now(request)
    try:
        konto = settings.create_konto(
            db,
            request.app.state.config.credentials_key,
            username=username,
            password=password,
            now=now,
        )
    except settings.SettingsError as exc:
        return _page(request, db, fehler={"konto_neu": str(exc)}, eingaben=eingaben)
    tonies = _link_tonies(db, konto, list(result.tonies))
    flash(request, f"Anmeldung bei {mask_email(username)} gelungen. Das Konto ist gespeichert.")
    return _page(request, db, geprueft={konto.id: tonies})


@router.post("/konten/{konto_id}/pruefen", response_class=HTMLResponse)
def check_konto_route(
    konto_id: int,
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
    check: Callable[[str, str], KontoPruefung] = Depends(get_konto_check),
    _admin=Depends(setup_admin),
):
    """Zugangsdaten aendern, erneut pruefen oder Tonies neu einlesen. Leeres
    Passwortfeld heisst: das gespeicherte bleibt (R20), nur bei unveraenderter
    E-Mail -- sonst ginge es an die Anmeldung eines fremden Kontos."""
    konto = db.get(TonieKonto, konto_id)
    if konto is None:
        raise HTTPException(status_code=404)
    busy = _busy_tonie(konto)
    if busy is not None:
        flash(request, _busy_message(busy), "err")
        return redirect(f"{PAGE}#s-konten")
    key = request.app.state.config.credentials_key
    username = username.strip() or konto.username
    stored = None
    if not password:
        if username != konto.username:
            fehler = {f"konto_{konto.id}": "Neue E-Mail: bitte das Passwort neu eingeben."}
            return _page(request, db, fehler=fehler)
        zugang = settings.konto_zugang(db, key, konto.id)
        if zugang is None:
            return _page(
                request, db, fehler={f"konto_{konto.id}": "Bitte das Passwort neu eingeben."}
            )
        stored = zugang.password
    result = _run_check(check, username, password or stored)
    if not result.ok:
        reason = f"{result.fehler} Die bisherigen Zugangsdaten bleiben aktiv."
        return _page(request, db, fehler={f"konto_{konto.id}": reason})
    now = get_now(request)
    try:
        if password or username != konto.username:
            settings.set_konto_password(
                db, key, konto.id, password or stored, now=now, username=username
            )
        else:
            settings.mark_konto_checked(db, konto.id, now=now)
    except settings.SettingsError as exc:
        return _page(request, db, fehler={f"konto_{konto.id}": str(exc)})
    tonies = _link_tonies(db, konto, list(result.tonies))
    flash(request, f"Anmeldung bei {mask_email(username)} gelungen.")
    return _page(request, db, geprueft={konto.id: tonies})


def _konto(db: Session, konto_id: int) -> TonieKonto:
    konto = db.get(TonieKonto, konto_id)
    if konto is None:
        raise HTTPException(status_code=404)
    return konto


def konto_delete_blocker(konto: TonieKonto) -> str | None:
    """Ein Tonie mit Kalender oder App-Kapitel haelt sein Konto fest: ohne
    Konto kaeme die App nie wieder an den Tonie, um abzuraeumen."""
    busy = _busy_tonie(konto)
    if busy is not None:
        return _busy_message(busy)
    for tonie in konto.tonies:
        name = tonie.name or short_id(tonie.tonie_id)
        if tonie.campaign is not None:
            return f"{name} gehört noch zu „{tonie.campaign.name}“. Bitte {name} zuerst trennen."
        if load_app_chapters(tonie.app_chapters) or tonie.abraeumen_offen:
            return (
                f"Auf {name} liegt noch ein Kapitel der App. Erst wenn es abgeräumt ist, "
                "lässt sich das Konto löschen."
            )
    return None


@router.get("/konten/{konto_id}/loeschen", response_class=HTMLResponse)
def delete_konto_page(
    konto_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    konto = _konto(db, konto_id)
    return render(
        request,
        "admin/setup_konto_loeschen.html",
        {
            "konto": konto,
            "blocker": konto_delete_blocker(konto),
            "tonies": sorted(konto.tonies, key=lambda t: t.id),
            "mask_email": mask_email,
            "short_id": short_id,
        },
        tab="setup",
    )


@router.post("/konten/{konto_id}/loeschen", response_class=HTMLResponse)
def delete_konto_route(
    konto_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _admin=Depends(setup_admin),
):
    konto = _konto(db, konto_id)
    blocker = konto_delete_blocker(konto)
    if blocker is not None:
        flash(request, blocker, "err")
    else:
        name = konto.label or mask_email(konto.username)
        settings.delete_konto(db, konto)
        flash(request, f"Konto {name} ist gelöscht.")
    return redirect(f"{PAGE}#s-konten")


# --- Tonie-Zuordnung (R5, R25, R37) -------------------------------------------------------


def _calendar_or_none(db: Session, value: str | None) -> Campaign | None:
    if not value:
        return None
    try:
        calendar = db.get(Campaign, int(value))
    except ValueError:
        calendar = None
    if calendar is None:
        raise HTTPException(status_code=400, detail="Unbekannter Kalender.")
    return calendar


def _confirm_url(tonie: CreativeTonie, target: Campaign | None) -> str:
    query = urlencode({"kalender": target.id if target else ""})
    return f"{PAGE}/tonies/{tonie.id}/umhaengen?{query}"


@router.post("/konten/{konto_id}/zuordnung", response_class=HTMLResponse)
def assign_route(
    konto_id: int,
    request: Request,
    form: FormData = Depends(_form),
    db: Session = Depends(get_db),
    _admin=Depends(setup_admin),
):
    """Ein Tonie ohne Kalender wird direkt zugeordnet. Gehoert er schon einem
    Kalender, geht ein Wechsel nur ueber die Rueckfrage (Umhaengen oder
    Trennen), nie stillschweigend (R5). `wechsel` nennt Tonies, die der Admin
    aus der Liste dieses Kontos uebernimmt -- ihr Konto wechselt hierher."""
    konto = db.get(TonieKonto, konto_id)
    if konto is None:
        raise HTTPException(status_code=404)
    wechsel = {int(v) for v in form.getlist("wechsel") if str(v).isdigit()}
    tonies = db.scalars(
        select(CreativeTonie)
        .where((CreativeTonie.konto_id == konto.id) | CreativeTonie.id.in_(wechsel))
        .order_by(CreativeTonie.id)
    ).all()
    confirm: list[tuple[CreativeTonie, Campaign | None]] = []
    busy: list[CreativeTonie] = []
    changed = False
    for tonie in tonies:
        field_name = f"kalender_{tonie.id}"
        if field_name not in form:
            continue
        target = _calendar_or_none(db, form.get(field_name))
        switch = tonie.konto_id != konto.id
        moves = (target.id if target else None) != tonie.campaign_id
        if not (switch or moves):
            continue
        if tonie_run_active(tonie.tonie_id):
            busy.append(tonie)
            continue
        if switch:
            tonie.konto_id = konto.id
            changed = True
        if moves and tonie.campaign_id is None:
            tonie.campaign_id = target.id
            changed = True
        elif moves:
            confirm.append((tonie, target))
    db.commit()
    if busy:
        flash(request, _busy_message(busy[0]), "err")
    elif confirm:
        if len(confirm) > 1:
            flash(request, "Mehrere Tonies wechseln den Kalender. Bitte jeden einzeln bestätigen.")
        return redirect(_confirm_url(*confirm[0]))
    elif changed:
        flash(request, "Zuordnung gespeichert.")
    return redirect(f"{PAGE}#s-konten")


def _tonie(db: Session, tonie_id: int) -> CreativeTonie:
    tonie = db.get(CreativeTonie, tonie_id)
    if tonie is None:
        raise HTTPException(status_code=404)
    return tonie


@router.get("/tonies/{tonie_id}/umhaengen", response_class=HTMLResponse)
def move_confirm_page(
    tonie_id: int,
    request: Request,
    kalender: str = "",
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    tonie = _tonie(db, tonie_id)
    target = _calendar_or_none(db, kalender)
    others = []
    if tonie.campaign is not None:
        others = [t for t in tonie.campaign.tonies if t.id != tonie.id]
    return render(
        request,
        "admin/setup_umhaengen.html",
        {
            "tonie": tonie,
            "target": target,
            "others": others,
            "busy": tonie_run_active(tonie.tonie_id),
        },
        tab="setup",
    )


@router.post("/tonies/{tonie_id}/umhaengen", response_class=HTMLResponse)
def move_route(
    tonie_id: int,
    request: Request,
    kalender: str = Form(""),
    db: Session = Depends(get_db),
    factory: TonieCloudFactory = Depends(get_toniecloud_factory),
    _admin=Depends(setup_admin),
):
    """Umhaengen oder Trennen (R37): das App-Kapitel geht sofort herunter
    (Abraeumlauf aus U7), danach gilt der neue Kalender. Haelt ein Lauf die
    Sperre, aendert sich nichts (R25)."""
    tonie = _tonie(db, tonie_id)
    target = _calendar_or_none(db, kalender)
    name = tonie.name or short_id(tonie.tonie_id)
    if tonie_run_active(tonie.tonie_id):
        flash(request, _busy_message(tonie), "err")
        return redirect(f"{PAGE}#s-konten")
    outcome = None
    if tonie.campaign_id is not None and tonie.campaign_id != (target.id if target else None):
        try:
            outcome = detach_tonie(db, client_for(factory, db, tonie), tonie)
        except TonieBusyError:
            flash(request, _busy_message(tonie), "err")
            return redirect(f"{PAGE}#s-konten")
    if target is not None:
        tonie.campaign_id = target.id
        db.commit()
    if outcome is not None and not outcome.success:
        flash(request, ABRAEUMEN_OFFEN, "err")
    elif target is None:
        flash(request, f"{name} ist getrennt und gehört zu keinem Kalender mehr.")
    else:
        flash(request, f"{name} gehört jetzt zu „{target.name}“.")
    return redirect(f"{PAGE}#s-konten")


# --- Mailversand (R19, R20) ------------------------------------------------------------


@router.post("/smtp", response_class=HTMLResponse)
def smtp_route(
    request: Request,
    host: str = Form(""),
    port: str = Form(""),
    user: str = Form(""),
    password: str = Form(""),
    from_address: str = Form(""),
    db: Session = Depends(get_db),
    _admin=Depends(setup_admin),
):
    """Pruefen per Testmail an den Admin, erst danach speichern. Ein leeres
    Passwortfeld behaelt das gespeicherte nur bei unveraendertem Server, Port
    und Benutzer -- sonst ginge es an einen fremden Server."""
    config = request.app.state.config
    host, user, from_address = host.strip(), user.strip(), from_address.strip()
    eingaben = {"smtp": {"host": host, "port": port, "user": user, "from_address": from_address}}

    def fail(reason: str, key: str = "smtp") -> HTMLResponse:
        return _page(request, db, fehler={key: reason}, eingaben=eingaben)

    try:
        port_number = int(port)
    except ValueError:
        port_number = 0
    if not (host and user and from_address) or not 0 < port_number < 65536:
        return fail("Bitte Server, Port, Benutzer und Absender vollständig eingeben.")
    row = settings.get_einstellungen(db)
    same_target = (host, port_number, user) == (row.smtp_host, row.smtp_port, row.smtp_user)
    stored = None
    if not password:
        if not same_target:
            return fail(SMTP_PASSWORT_NEU, "smtp_passwort")
        if row.smtp_password is None or row.smtp_needs_reentry:
            return fail("Bitte das Passwort neu eingeben.", "smtp_passwort")
        try:
            stored = decrypt(config.credentials_key, row.smtp_password)
        except CredentialsUnavailable:
            return fail("Bitte das Passwort neu eingeben.", "smtp_passwort")
    result = check_smtp(
        host=host,
        port=port_number,
        user=user,
        password=password or stored,
        from_address=from_address,
        to_address=config.admin_email,
    )
    if not result.ok:
        previous = f" ({row.smtp_host})" if row.smtp_host else ""
        return fail(f"{result.grund}. Mails gehen weiter über den bisherigen Zugang{previous}.")
    try:
        settings.set_smtp(
            db,
            config.credentials_key,
            host=host,
            port=port_number,
            user=user,
            password=password or None,
            from_address=from_address,
            now=get_now(request),
        )
    except settings.SettingsError as exc:
        return fail(str(exc))
    flash(
        request,
        f"Testmail an {mask_email(config.admin_email)} ist verschickt. "
        "Der SMTP-Zugang gilt ab sofort.",
    )
    return redirect(f"{PAGE}#s-smtp")


# --- Termine und Name (R17, R22, R26, R43) -------------------------------------------------


def _parse_date(value: str) -> date | None:
    return date.fromisoformat(value) if value else None


@router.post("/termine", response_class=HTMLResponse)
def termine_route(
    request: Request,
    delivery_time: str = Form(""),
    recording_deadline: str = Form(""),
    invitation_date: str = Form(""),
    magic_link_valid_until: str = Form(""),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
    _admin=Depends(setup_admin),
):
    eingaben = {
        "termine": {
            "delivery_time": delivery_time,
            "recording_deadline": recording_deadline,
            "invitation_date": invitation_date,
            "magic_link_valid_until": magic_link_valid_until,
        }
    }
    row = settings.get_einstellungen(db)
    try:
        new_time = time.fromisoformat(delivery_time)
        dates = {
            "recording_deadline": _parse_date(recording_deadline),
            "invitation_date": _parse_date(invitation_date),
            "magic_link_valid_until": _parse_date(magic_link_valid_until),
        }
    except ValueError:
        return _page(
            request,
            db,
            fehler={"termine": "Bitte gültige Zeit- und Datumswerte."},
            eingaben=eingaben,
        )
    if dates["recording_deadline"] is None or dates["magic_link_valid_until"] is None:
        return _page(
            request,
            db,
            fehler={"termine": "Aufnahmefrist und Linkgültigkeit brauchen ein Datum."},
            eingaben=eingaben,
        )
    shown = _time_text(row)
    if f"{new_time:%H:%M}" != shown:
        try:
            settings.set_delivery_time(db, new_time, now=now)
        except settings.SettingsError:
            reason = LIEFERZEIT_FENSTER + (f" Es bleibt bei {shown}." if shown else "")
            return _page(request, db, fehler={"lieferzeit": reason}, eingaben=eingaben)
    current = {
        "recording_deadline": settings.recording_deadline(db),
        "invitation_date": row.invitation_date,
        "magic_link_valid_until": settings.magic_link_valid_until(db),
    }
    for name, value in dates.items():
        if current[name] != value:
            settings.set_value(db, name, value, now=now)
    message = "Termine gespeichert."
    if row.delivery_time_pending and f"{new_time:%H:%M}" != shown:
        message = (
            f"Gespeichert. Die neue Lieferzeit {row.delivery_time_pending} gilt ab morgen. "
            f"Heute bleibt es bei {row.delivery_time}, weil das Lieferfenster schon offen ist."
        )
    flash(request, message)
    return redirect(f"{PAGE}#s-termine")


@router.post("/name", response_class=HTMLResponse)
def name_route(
    request: Request,
    admin_display_name: str = Form(""),
    kind_name: str = Form(""),
    db: Session = Depends(get_db),
    now: datetime = Depends(get_now),
    _admin=Depends(setup_admin),
):
    settings.set_value(db, "admin_display_name", admin_display_name.strip() or None, now=now)
    settings.set_value(db, "kind_name", kind_name.strip() or None, now=now)
    flash(request, "Namen gespeichert.")
    return redirect(f"{PAGE}#s-name")
