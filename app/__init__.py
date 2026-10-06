"""App-Factory: Zustandsendpunkt, ffmpeg- und Konfigurationspruefung beim Start.

Ein fehlendes ffmpeg oder eine fehlende Pflichtkonfiguration soll beim Start
auffallen, nicht beim ersten Upload (U1, Schritt 2).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from app import settings
from app.admin.deliveries import router as admin_deliveries_router
from app.admin.einstellungen import router as admin_einstellungen_router
from app.admin.overview import router as admin_overview_router
from app.admin.people import router as admin_people_router
from app.admin.review import router as admin_review_router
from app.admin.setup import router as admin_router
from app.archive.views import router as archive_router
from app.auth.einrichtung import issue_link_if_needed
from app.auth.einrichtung import router as einrichtung_router
from app.auth.routes import router as auth_router
from app.config import Config, load_config
from app.db import create_db_engine, init_db, make_session_factory
from app.delivery.trigger import check_trigger_secret
from app.delivery.trigger import router as delivery_router
from app.herkunft import HerkunftMiddleware
from app.models import Person
from app.recording.views import router as recording_router
from app.storage import ObjectStorage

# KTD19: aelter als das gilt die Sicherung als ausgefallen.
BACKUP_MAX_AGE = timedelta(hours=36)

try:
    __version__ = version("vorlesezeit")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"


def check_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg wurde nicht im PATH gefunden. Ohne ffmpeg kann die App "
            "keine Aufnahmen normalisieren (KTD5) -- Abbruch beim Start "
            "statt beim ersten Upload."
        )
    result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg meldet einen Fehler beim Start: {result.stderr.strip()}")


def sync_admin_person(session_factory, config: Config) -> None:
    """R33: die Admin-Identitaet kommt aus der Umgebung und ist ueber die
    App nicht aenderbar. Idempotent bei jedem Start."""
    with session_factory() as session:
        admin = session.execute(
            select(Person).where(Person.is_admin.is_(True))
        ).scalar_one_or_none()
        if admin is None:
            admin = session.execute(
                select(Person).where(Person.email == config.admin_email)
            ).scalar_one_or_none()
        if admin is None:
            session.add(Person(email=config.admin_email, is_admin=True))
        else:
            admin.email = config.admin_email
            admin.is_admin = True
        session.commit()


_TOKEN_PARAM = re.compile(r"([?&]token=)[^&\s]*")
# Mehrkalender U4: der Einrichtungs-Token steckt im Pfad.
_EINRICHTUNG_PATH = re.compile(r"^(/einrichtung/)[^/?#\s]+")


class RedactTokenFilter(logging.Filter):
    """Uvicorns Zugriffslog schreibt die volle Adresse. Ein Magic-Link-Token
    darin reicht zum Anmelden (bis zum Ende der Linkfrist) -- also nie ins
    Log, ebenso wenig ein Einrichtungs-Token im Pfad. Das Format von
    uvicorn.access: (Client, Methode, Pfad, HTTP, Status)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path = _TOKEN_PARAM.sub(r"\1[entfernt]", args[2])
            path = _EINRICHTUNG_PATH.sub(r"\1[entfernt]", path)
            record.args = (*args[:2], path, *args[3:])
        return True


def configure_logging() -> None:
    """Die eigenen Logzeilen (z. B. "Auslösung quelle=...") erreichen sonst
    nie das Containerlog -- Uvicorn richtet nur seine eigenen Logger ein.
    Bewusst nur `app`, nicht die Wurzel: httpx loggt auf INFO jede Adresse
    samt Haushalts- und Tonie-Kennung."""
    logger = logging.getLogger("app")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactTokenFilter) for f in access.filters):
        access.addFilter(RedactTokenFilter())


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging()
        config = load_config()
        check_ffmpeg()
        engine = create_db_engine(config)
        init_db(engine)
        session_factory = make_session_factory(engine)
        sync_admin_person(session_factory, config)
        # Mehrkalender U3: Startwerte einmalig uebernehmen (R28/R34), danach
        # jedes gespeicherte Passwort gegen den Schluessel pruefen (R21).
        with session_factory() as session:
            settings.seed_from_env(session, config, now=datetime.now(ZoneInfo(config.timezone)))
            settings.check_stored_credentials(session, config.credentials_key)
            # Mehrkalender U4 (R30): ohne nutzbaren SMTP-Zugang ein Einmal-Link ins Log.
            issue_link_if_needed(session, config.credentials_key, now=datetime.now(UTC))

        app.state.config = config
        app.state.storage = ObjectStorage(config)
        app.state.session_factory = session_factory
        yield

    app = FastAPI(title="Vorlesezeit", lifespan=lifespan)
    # SessionMiddleware braucht den Schluessel schon beim Bauen der App, also
    # vor der lifespan-Pruefung -- direkt aus os.environ gelesen statt ueber
    # load_config(), damit create_app() (und damit `app = create_app()` beim
    # Modulimport) ohne gesetzte Umgebung nicht schon hier abbricht. Die
    # eigentliche, vollstaendige Pflichtvariablen-Pruefung (inkl.
    # SESSION_SECRET_KEY) bleibt in der lifespan, wie bei jeder anderen
    # Konfiguration auch.
    app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SESSION_SECRET_KEY", "unset"))
    app.add_middleware(HerkunftMiddleware)
    app.include_router(auth_router)
    app.include_router(einrichtung_router)
    app.include_router(admin_router)
    app.include_router(admin_overview_router)
    app.include_router(admin_review_router)
    app.include_router(admin_people_router)
    app.include_router(admin_deliveries_router)
    app.include_router(admin_einstellungen_router)
    app.include_router(delivery_router)
    app.include_router(recording_router)
    app.include_router(archive_router)
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    @app.get("/status")
    def status() -> dict[str, object]:
        # R21: nur die Namen der neu einzugebenden Zugangsdaten, nie Werte.
        with app.state.session_factory() as session:
            neu_eingeben = settings.needs_reentry(session)
        return {
            "status": "ok",
            "version": __version__,
            "timezone": app.state.config.timezone,
            "neu_eingeben": neu_eingeben,
        }

    @app.get("/backup/check")
    def backup_check(x_trigger_secret: str = Header(default="")) -> JSONResponse:
        """KTD19: Alter der letzten erfolgreichen Sicherung fuer den zweiten
        cron-job.org-Auftrag. Bewusst nicht /status -- die Gesundheitspruefung
        des Containers soll die App wegen einer ausgefallenen Sicherung nicht
        neu starten. Geschuetzt wie /delivery/trigger, liefert nur das Alter."""
        config = app.state.config
        check_trigger_secret(config, x_trigger_secret)
        try:
            with open(config.backup_marker_path) as marker:
                last_success = datetime.fromisoformat(marker.read().strip())
        except (OSError, ValueError):
            return JSONResponse({"alter_stunden": None}, status_code=503)
        age = datetime.now(UTC) - last_success
        status_code = 200 if age <= BACKUP_MAX_AGE else 503
        return JSONResponse(
            {"alter_stunden": round(age.total_seconds() / 3600, 2)}, status_code=status_code
        )

    return app


app = create_app()
