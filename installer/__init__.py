"""Installationsassistent für befreundete Organisatoren (One-Klick-Setup, Baustein 3).

Nach dem Abschluss der Einrichtung öffnet er die Wartung unter `/wartung`
(Baustein 4). Ohne Anfrage seit 30 Minuten und ohne laufenden Vorgang beendet
er sich, denn den Docker-Socket hat er nur, solange er offen ist.

Läuft als eigener Container, solange der Assistent offen ist: die Startdatei
startet ihn mit Docker-Socket, Installationsordner (`/install`) und den
Laufwerken (nur lesend, `/laufwerke`) und öffnet den Browser auf
`http://127.0.0.1:<port>/?t=<token>`. Ohne gültiges Token antwortet er mit
404 und leerem Inhalt.

    uvicorn installer:create_installer --factory

Das Token, der Einladungscode und Passwörter landen nie im Log; deshalb läuft
uvicorn im Image ohne Zugriffsprotokoll (die erste Adresse trägt das Token).
"""

from __future__ import annotations

import logging
import os
import secrets
import signal
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.herkunft import HerkunftMiddleware
from app.mail.smtp import check_smtp
from installer.antworten import Antworten
from installer.docker import Docker
from installer.geraet import messen, tls_pruefen

COOKIE = "vorlesezeit_einrichtung"
HOST_OS = ("mac", "windows", "linux")
LEERLAUF_SECONDS = 30 * 60
WACHE_TAKT_SECONDS = 60
_HIER = Path(__file__).resolve().parent
_APP_STATIC = _HIER.parent / "app" / "static"


@dataclass
class Umgebung:
    token: str
    host_os: str
    install_dir: Path
    laufwerke_dir: Path
    host_install_dir: str
    installer_image: str
    container_id: str = field(default_factory=socket.gethostname)

    @classmethod
    def aus_env(cls) -> Umgebung:
        quelle = os.environ
        return cls(
            token=quelle["INSTALLER_TOKEN"],
            host_os=quelle["HOST_OS"],
            install_dir=Path(quelle.get("INSTALL_DIR", "/install")),
            laufwerke_dir=Path(quelle.get("LAUFWERKE_DIR", "/laufwerke")),
            host_install_dir=quelle["HOST_INSTALL_DIR"],
            installer_image=quelle["INSTALLER_IMAGE"],
        )


def _im_hintergrund(f: Callable[[], None]) -> None:
    threading.Thread(target=f, daemon=True).start()


def _prozess_beenden() -> None:
    # Kurz warten, damit die Seite „Geschafft“ noch beim Browser ankommt.
    threading.Timer(1.0, os.kill, (os.getpid(), signal.SIGTERM)).start()


def _jetzt() -> datetime:
    return datetime.now(UTC)


def _wache(pruefe: Callable[[], bool]) -> None:
    """Prüft jede Minute, ob der Assistent sich beenden soll (siehe _leerlauf)."""

    def schleife() -> None:
        while not pruefe():
            time.sleep(WACHE_TAKT_SECONDS)

    threading.Thread(target=schleife, daemon=True).start()


@dataclass
class Werkzeuge:
    docker: Docker | None = None
    messung: Callable = messen
    tls: Callable = tls_pruefen
    smtp_pruefung: Callable = check_smtp
    http: httpx.Client | None = None
    uhr: Callable[[], float] = time.monotonic
    schlafen: Callable[[float], None] = time.sleep
    starte: Callable[[Callable[[], None]], None] = _im_hintergrund
    beenden: Callable[[], None] = _prozess_beenden
    jetzt: Callable[[], datetime] = _jetzt
    wache: Callable[[Callable[[], bool]], None] = _wache


class TokenMiddleware:
    """Ohne gültiges Token (Adresse `?t=` oder Cookie) gibt es nur 404 ohne Inhalt."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token.encode()

    def _gueltig(self, wert: str | None) -> bool:
        return wert is not None and secrets.compare_digest(wert.encode(), self.token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        aus_adresse = parse_qs(scope.get("query_string", b"").decode()).get("t", [None])[0]
        if self._gueltig(aus_adresse):

            async def mit_cookie(message: Message) -> None:
                if message["type"] == "http.response.start":
                    MutableHeaders(scope=message).append(
                        "set-cookie",
                        f"{COOKIE}={self.token.decode()}; HttpOnly; SameSite=Strict; Path=/",
                    )
                await send(message)

            await self.app(scope, receive, mit_cookie)
            return
        if self._gueltig(request.cookies.get(COOKIE)):
            await self.app(scope, receive, send)
            return
        await Response(status_code=404)(scope, receive, send)


def _logging_einrichten() -> None:
    """Wie `app.configure_logging`: nur der eigene Logger, nicht die Wurzel."""
    logger = logging.getLogger("installer")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _beschaeftigt(app: FastAPI) -> bool:
    return any(v is not None and v.laeuft for v in (app.state.lauf, app.state.vorgang))


def _leerlauf(app: FastAPI) -> bool:
    """Beendet den Assistenten nach 30 Minuten ohne Anfrage, nie mitten in einem Vorgang."""
    w = app.state.werkzeuge
    if _beschaeftigt(app) or w.uhr() - app.state.letzte_anfrage < LEERLAUF_SECONDS:
        return False
    w.beenden()
    return True


def create_installer(
    umgebung: Umgebung | None = None, werkzeuge: Werkzeuge | None = None
) -> FastAPI:
    from installer.views import router, setze_fehlerseiten
    from installer.wartung_views import einstieg
    from installer.wartung_views import router as wartung_router

    _logging_einrichten()
    umgebung = umgebung or Umgebung.aus_env()
    if not umgebung.token or umgebung.host_os not in HOST_OS:
        raise RuntimeError("INSTALLER_TOKEN fehlt oder HOST_OS ist nicht mac, windows oder linux")
    werkzeuge = werkzeuge or Werkzeuge()
    if werkzeuge.docker is None:
        werkzeuge.docker = Docker(umgebung.install_dir, container_id=umgebung.container_id)
    if werkzeuge.http is None:
        werkzeuge.http = httpx.Client(timeout=10)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.umgebung = umgebung
    app.state.werkzeuge = werkzeuge
    app.state.antworten = Antworten()
    app.state.lauf = None
    app.state.notfallblatt_geladen = False
    app.state.vorgang = None
    app.state.protokoll = None
    app.state.letzte_anfrage = werkzeuge.uhr()
    app.mount("/static", StaticFiles(directory=_APP_STATIC), name="static")
    app.mount("/assistent", StaticFiles(directory=_HIER / "static"), name="assistent")
    app.include_router(einstieg)
    app.include_router(router)
    app.include_router(wartung_router)
    setze_fehlerseiten(app)

    @app.middleware("http")
    async def anfrage_merken(request: Request, call_next):
        app.state.letzte_anfrage = werkzeuge.uhr()
        return await call_next(request)

    app.add_middleware(HerkunftMiddleware)
    app.add_middleware(TokenMiddleware, token=umgebung.token)
    werkzeuge.wache(lambda: _leerlauf(app))
    return app
