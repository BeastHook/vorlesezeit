"""Gemeinsame Bausteine der Admin-Reiter (U8).

Jeder Reiter ist eine server-gerenderte Seite; Handlungen sind Formulare,
die per 303 auf die Seite zurueckleiten und eine Rueckmeldung als
Hinweisbalken hinterlassen (abgenommenes Mockup, statt Toast).
"""

from __future__ import annotations

from datetime import datetime, time
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin.state import SELECTION_KEY, Selection, chosen_value, current_selection, selections
from app.auth.tokens import create_magic_link_token
from app.delivery.trigger import evening_of, get_now
from app.mail.report import mask_tonie_id
from app.models import Campaign, CreativeTonie, Person, Slot
from app.settings import delivery_time_for
from app.templating import templates

TABS = (
    ("kalender", "Kalender"),
    ("aufnahmen", "Aufnahmen"),
    ("geschichten", "Geschichten"),
    ("personen", "Personen"),
    ("auslieferung", "Auslieferung"),
    # Mehrkalender U11 baut die Seite.
    ("setup", "Setup"),
)


# R29: einzige Zeitbasis ist die konfigurierte Zeitzone; dieselbe Funktion
# wie die Dependency app/delivery/trigger.py::get_now.
berlin_now = get_now


def login_url(request: Request, person: Person, *, next_path: str | None = None) -> str:
    """Frischer Magic-Link fuer eine Admin-Mail; `next_path` fuehrt nach dem
    Login direkt dorthin (R27, siehe app/auth/routes.py::_safe_next)."""
    params = {"token": create_magic_link_token(request.app.state.config, person)}
    if next_path is not None:
        params["next"] = next_path
    return f"{request.url_for('show_confirm_page')}?{urlencode(params)}"


def selection(db: Session, request: Request | None = None) -> Selection | None:
    """KTD12: die Wahl des Tonie-Umschalters, ungueltig gewordene faellt auf
    die Vorgabe zurueck (app/admin/state.py::current_selection)."""
    return current_selection(db, chosen_value(request))


def get_campaign(db: Session, request: Request | None = None) -> Campaign | None:
    """Der Kalender des gewaehlten Tonie bzw. der gewaehlte Kalender ohne
    Tonie; None bei einem Tonie ohne Kalender. Ohne `request` der erste
    Kalender der Instanz."""
    if request is None:
        return first_campaign(db)
    chosen = selection(db, request)
    return chosen.campaign if chosen is not None else None


def first_campaign(db: Session) -> Campaign | None:
    return db.execute(select(Campaign).order_by(Campaign.id).limit(1)).scalar_one_or_none()


def tonie_name(tonie: CreativeTonie) -> str:
    """Anzeigename; ohne Namen nur die maskierte Tonie-ID."""
    return tonie.name or "Tonie " + mask_tonie_id(tonie.tonie_id)


def scope_names(campaign: Campaign) -> list[str]:
    """R40: die Tonies, fuer die eine Aenderung an diesem Kalender gilt."""
    return [tonie_name(t) for t in sorted(campaign.tonies, key=lambda t: t.id)]


def delivery_time_at(db: Session, now: datetime) -> time:
    """Die Lieferzeit des Abends, zu dem `now` gehoert (KTD16)."""
    return delivery_time_for(db, evening_of(now))


def campaign_slots(db: Session, campaign_id: int) -> list[Slot]:
    return list(
        db.execute(select(Slot).where(Slot.campaign_id == campaign_id).order_by(Slot.day)).scalars()
    )


def flash(request: Request, message: str, kind: str = "ok") -> None:
    """kind: "ok" (gruen) oder "err" (orange, benannter Fehler)."""
    request.session["admin_flash"] = {"message": message, "kind": kind}


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def _switcher(request: Request) -> dict | None:
    """R14: Zeilen des Umschalters, gruppiert nach Kalender. Eigene, kurze
    Sitzung, damit auch Reiter ohne DB-Abhaengigkeit ihn zeigen."""
    with request.app.state.session_factory() as db:
        rows = selections(db)
        if not rows:
            return None
        chosen = current_selection(db, chosen_value(request), rows=rows)
        groups: list[tuple[str, list[tuple[str, str]]]] = []
        for row in rows:
            if row.campaign is None:
                group, label = "Ohne Kalender", tonie_name(row.tonie)
            elif row.tonie is None:
                group, label = row.campaign.name, f"{row.campaign.name} (kein Tonie)"
            else:
                group = row.campaign.name
                label = f"{tonie_name(row.tonie)} · {row.campaign.name}"
            if not groups or groups[-1][0] != group:
                groups.append((group, []))
            groups[-1][1].append((row.value, label))
        return {"groups": groups, "value": chosen.value}


def render(request: Request, name: str, context: dict, *, tab: str | None) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        name,
        {
            **context,
            "tabs": TABS,
            "tab": tab,
            "switcher": _switcher(request) if tab else None,
            "flash": request.session.pop("admin_flash", None),
        },
    )


def remember_selection(request: Request, value: str) -> None:
    request.session[SELECTION_KEY] = value
