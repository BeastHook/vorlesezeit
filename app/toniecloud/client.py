"""Eigener HTTP-Client gegen die oeffentliche Toniecloud-Spec (U4, KTD2).

Baustein-Ebene: anmelden, Datei anlegen, hochladen, Kapitelliste ersetzen,
Zustand pruefen. Die Auslieferungslogik (Idempotenz, Sperren, Rollback,
Ersatzbeitrag) baut U5 aus diesen Bausteinen -- hier steckt bewusst keine
Orchestrierung.

Endpunkte und Verhalten sind gegen die oeffentliche OpenAPI-Spec
(https://api.tonie.cloud/v2/doc/?format=openapi) und die Rechercheergebnisse
in .claude/skills/toniecloud-api/SKILL.md abgesichert, nicht gegen eine
Referenz-Bibliothek.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from app.crypto import fingerprint
from app.models import CreativeTonie, TonieKonto
from app.settings import konto_zugang
from app.toniecloud.models import (
    Chapter,
    ConfigLimits,
    CreativeTonieState,
    UploadTarget,
    VerificationResult,
)

TOKEN_URL = "https://login.tonies.com/auth/realms/tonies/protocol/openid-connect/token"
API_BASE_URL = "https://api.tonie.cloud/v2"
CLIENT_ID = "my-tonies"

# Der Anmeldeendpunkt ist bekanntermassen gelegentlich traege (SKILL.md).
LOGIN_TIMEOUT_SECONDS = 30.0
LOGIN_MAX_ATTEMPTS = 3
LOGIN_RETRY_DELAY_SECONDS = 2.0

# U6-Approach-Schritt 2: mindestens 120s Verifikationsbudget je Schritt --
# ein fester Minutentakt kollidiert sonst mit einer Verarbeitung, die laut
# Spec bis zu 60 Sekunden dauern darf.
DEFAULT_POLL_TIMEOUT_SECONDS = 120.0
DEFAULT_POLL_INTERVAL_SECONDS = 2.0


class TonieCloudError(Exception):
    """Basisklasse fuer alle Fehler dieses Clients."""


class LoginRejectedError(TonieCloudError):
    """Die Anmeldung wurde abgelehnt (falsches Passwort o.ae.). Kein Retry.

    Traegt ausschliesslich den Statuscode -- nie Body oder Header des
    Anmelde-Requests (Plan, U4-Approach Punkt 6).
    """

    def __init__(self, status_code: int) -> None:
        super().__init__(f"Anmeldung abgelehnt (HTTP {status_code})")
        self.status_code = status_code


class LoginTimeoutError(TonieCloudError):
    """Die Anmeldung ist nach der festen Obergrenze an Versuchen gescheitert."""

    def __init__(self, attempts: int) -> None:
        super().__init__(
            f"Anmeldung nach {attempts} Versuchen wegen Zeitueberschreitung gescheitert"
        )
        self.attempts = attempts


class TermsOfUseRequiredError(TonieCloudError):
    """POST /v2/file antwortete mit 409: Nutzungsbedingungen erneut zu bestaetigen.

    Wird nie automatisch geloest -- das ist eine Entscheidung des
    Kontoinhabers (Plan, U4-Approach Punkt 7).
    """

    def __init__(self) -> None:
        super().__init__(
            "Datei konnte nicht angelegt werden: Nutzungsbedingungen erneut zu bestaetigen"
        )


class UnsupportedFormatError(TonieCloudError):
    """Das Dateiformat steht nicht in der von /v2/config gelesenen Liste."""

    def __init__(self, extension: str, accepts: Sequence[str]) -> None:
        super().__init__(f"Format {extension!r} nicht akzeptiert (erlaubt: {', '.join(accepts)})")
        self.extension = extension


def validate_format(filename: str, limits: ConfigLimits) -> None:
    """Lehnt ein Format vor dem Upload ab, wenn es nicht in `limits.accepts` steht."""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in limits.accepts:
        raise UnsupportedFormatError(extension, limits.accepts)


def verify_upload(
    patch_state: CreativeTonieState,
    final_state: CreativeTonieState,
    expected_titles: Sequence[str] | None = None,
) -> VerificationResult:
    """Entscheidet allein anhand der Kapitel-`id`-Folge ueber Erfolg (Plan, U4-Approach Punkt 5).

    Gegen das echte Konto bestaetigt (siehe SKILL.md): die urspruengliche
    `fileId` aus `POST /v2/file` bleibt nach Abschluss des Transcodings
    NICHT in `chapter.file` oder `chapter.id` erhalten -- `file` beginnt
    unmittelbar nach dem PATCH als die hochgeladene `fileId`, wird nach dem
    Transcoding aber zu einem Duplikat von `id`. Die einzige stabile,
    beweiskraeftige Groesse ist `chapter.id`, wie sie **derselbe PATCH-Aufruf**
    (`patch_state`, die Rueckgabe von `replace_chapters`) sofort vergibt --
    sie uebersteht das Transcoding unveraendert. Verifikation heisst deshalb:
    dieselbe `id`-Folge nach dem Warten wiederfinden, nicht gegen die
    urspruengliche fileId pruefen.

    Kapitelzahl und Laenge allein beweisen nichts -- sie sind fuer die
    Geschichte des Vortags, den Ersatzbeitrag und den Tagesbeitrag
    ununterscheidbar. Ein abweichender Titel bei sonst passender id-Folge
    wird gemeldet, laesst den Lauf aber nicht scheitern.
    """
    if final_state.transcoding_errors:
        reasons = ", ".join(e.reason for e in final_state.transcoding_errors)
        return VerificationResult(success=False, reason=f"Verarbeitungsfehler: {reasons}")

    expected_ids = [chapter.id for chapter in patch_state.chapters]
    actual_ids = [chapter.id for chapter in final_state.chapters]
    if actual_ids != expected_ids:
        return VerificationResult(
            success=False,
            reason=f"Kapitel-id-Folge stimmt nicht: erwartet {expected_ids}, erhalten {actual_ids}",
        )

    title_mismatches: list[tuple[str, str]] = []
    if expected_titles is not None:
        if len(expected_titles) != len(final_state.chapters):
            return VerificationResult(
                success=False,
                reason=(
                    f"Kapitelzahl stimmt nicht: erwartet {len(expected_titles)}, "
                    f"erhalten {len(final_state.chapters)}"
                ),
            )
        for expected_title, chapter in zip(expected_titles, final_state.chapters, strict=True):
            if expected_title != chapter.title:
                title_mismatches.append((expected_title, chapter.title))

    return VerificationResult(success=True, title_mismatches=tuple(title_mismatches))


class TonieCloudClient:
    """Ein Client je tonies-Konto (Mehrkalender KTD8). Das Passwort steht nur
    im privaten Attribut -- nie im `repr`, nie in einer Fehlermeldung."""

    def __init__(
        self,
        username: str,
        password: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._username = username
        self._password = password
        self._sleep = sleep
        self._http = httpx.Client(transport=transport)
        self._access_token: str | None = None

    def __repr__(self) -> str:
        return f"TonieCloudClient(username={self._username!r})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> TonieCloudClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- Anmeldung -------------------------------------------------

    def _ensure_token(self) -> str:
        if self._access_token is not None:
            return self._access_token
        self._access_token = self._login()
        return self._access_token

    def _login(self) -> str:
        last_timeout: Exception | None = None
        for attempt in range(1, LOGIN_MAX_ATTEMPTS + 1):
            try:
                response = self._http.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "password",
                        "client_id": CLIENT_ID,
                        "scope": "openid",
                        "username": self._username,
                        "password": self._password,
                    },
                    timeout=LOGIN_TIMEOUT_SECONDS,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_timeout = exc
                if attempt < LOGIN_MAX_ATTEMPTS:
                    self._sleep(LOGIN_RETRY_DELAY_SECONDS)
                continue

            if response.status_code == 200:
                return response.json()["access_token"]
            if response.status_code >= 500:
                last_timeout = TonieCloudError(
                    f"Serverfehler bei Anmeldung: HTTP {response.status_code}"
                )
                if attempt < LOGIN_MAX_ATTEMPTS:
                    self._sleep(LOGIN_RETRY_DELAY_SECONDS)
                continue
            # 4xx ausserhalb eines erneuten Versuchs: abgelehnt, sofortiger Abbruch.
            raise LoginRejectedError(response.status_code)

        raise LoginTimeoutError(LOGIN_MAX_ATTEMPTS) from last_timeout

    def _relogin(self) -> str:
        self._access_token = None
        return self._ensure_token()

    def _authenticated_request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        token = self._ensure_token()
        response = self._http.request(
            method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
        if response.status_code == 401:
            token = self._relogin()
            response = self._http.request(
                method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs
            )
        return response

    # --- Grenzwerte --------------------------------------------------

    def get_config(self) -> ConfigLimits:
        response = self._http.get(f"{API_BASE_URL}/config")
        response.raise_for_status()
        return ConfigLimits.from_api_dict(response.json())

    # --- Household-Aufloesung ----------------------------------------

    def find_household_id(self, creative_tonie_id: str) -> str:
        response = self._authenticated_request("GET", f"{API_BASE_URL}/households")
        response.raise_for_status()
        for household in response.json():
            household_id = household["id"]
            tonies_response = self._authenticated_request(
                "GET", f"{API_BASE_URL}/households/{household_id}/creativetonies"
            )
            tonies_response.raise_for_status()
            for tonie in tonies_response.json():
                if tonie["id"] == creative_tonie_id:
                    return household_id
        raise TonieCloudError(f"Kein Household fuer Creative Tonie {creative_tonie_id} gefunden")

    def list_creative_tonies(self) -> list[dict]:
        """Alle Creative Tonies ueber alle Households -- nur fuer
        administrative Zwecke (U6-Generalprobe-Setup: der Nutzer waehlt das
        Ziel-Tonie ausserhalb des Chats per Index, nie per roher ID). Kein
        Produktionspfad."""
        response = self._authenticated_request("GET", f"{API_BASE_URL}/households")
        response.raise_for_status()
        result: list[dict] = []
        for household in response.json():
            household_id = household["id"]
            tonies_response = self._authenticated_request(
                "GET", f"{API_BASE_URL}/households/{household_id}/creativetonies"
            )
            tonies_response.raise_for_status()
            for tonie in tonies_response.json():
                result.append(
                    {"household_id": household_id, "id": tonie["id"], "name": tonie.get("name", "")}
                )
        return result

    # --- Zustand -------------------------------------------------------

    def get_state(self, household_id: str, creative_tonie_id: str) -> CreativeTonieState:
        response = self._authenticated_request(
            "GET", f"{API_BASE_URL}/households/{household_id}/creativetonies/{creative_tonie_id}"
        )
        response.raise_for_status()
        return CreativeTonieState.from_api_dict(response.json())

    # --- Datei anlegen und hochladen ------------------------------------

    def create_file(self) -> UploadTarget:
        response = self._authenticated_request("POST", f"{API_BASE_URL}/file")
        if response.status_code == 409:
            raise TermsOfUseRequiredError
        response.raise_for_status()
        return UploadTarget.from_api_dict(response.json())

    def upload_bytes(
        self, target: UploadTarget, data: bytes, *, filename: str, content_type: str
    ) -> None:
        files = {"file": (filename, data, content_type)}
        response = self._http.post(target.s3_url, data=target.s3_fields, files=files)
        response.raise_for_status()

    # --- Kapitelliste ersetzen ------------------------------------------

    def replace_chapters(
        self, household_id: str, creative_tonie_id: str, chapters: Sequence[Chapter]
    ) -> CreativeTonieState:
        response = self._authenticated_request(
            "PATCH",
            f"{API_BASE_URL}/households/{household_id}/creativetonies/{creative_tonie_id}",
            json={"chapters": [chapter.to_api_dict() for chapter in chapters]},
        )
        response.raise_for_status()
        return CreativeTonieState.from_api_dict(response.json())

    # --- Verarbeitung abwarten -------------------------------------------

    def wait_until_processed(
        self,
        household_id: str,
        creative_tonie_id: str,
        *,
        timeout: float = DEFAULT_POLL_TIMEOUT_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> CreativeTonieState:
        elapsed = 0.0
        state = self.get_state(household_id, creative_tonie_id)
        while state.transcoding and elapsed < timeout:
            self._sleep(poll_interval)
            elapsed += poll_interval
            state = self.get_state(household_id, creative_tonie_id)
        if state.transcoding:
            raise TonieCloudError(
                f"Verarbeitung nach {timeout}s nicht abgeschlossen "
                f"(Creative Tonie {creative_tonie_id})"
            )
        return state


# --- Mehrkalender U6: Client je Konto (KTD8, R2/R5/R19/R21) -------------------


class KontoUnavailable(TonieCloudError):
    """Fuer das Konto gibt es keine nutzbaren Zugangsdaten (fehlt, "neu
    eingeben", nicht lesbar) -- es wurde kein Anmeldeversuch unternommen (R21).
    Die Meldung nennt nur Bezeichnung oder Nummer des Kontos."""


def _konto_name(konto: TonieKonto) -> str:
    return f"„{konto.label}“" if konto.label else f"Nr. {konto.id}"


class UnavailableClient:
    """Rueckfallregel (app/delivery/job.py::client_for): steht hinter dem
    Tonie kein nutzbares Konto, bekommt der Lauf diesen Stellvertreter.
    Jeder Toniecloud-Aufruf wirft `KontoUnavailable` ohne HTTP; der Lauf endet
    damit wie bei einem Toniecloud-Fehler mit Verlaufseintrag und Meldung."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def __repr__(self) -> str:
        return f"UnavailableClient({self._reason!r})"

    def close(self) -> None:
        pass

    def __getattr__(self, name: str) -> Callable[..., object]:
        def unavailable(*args: object, **kwargs: object) -> object:
            raise KontoUnavailable(self._reason)

        return unavailable


class TonieCloudFactory:
    """Liefert den Client je Konto aus dem Einstellungsdienst (KTD8). Lebt
    prozessweit: die Clients -- und damit ihre Tokens -- bleiben je Konto
    zwischengespeichert, solange Benutzername und Passwort gleich bleiben.
    Schluessel des Zwischenspeichers ist ein HMAC-Fingerabdruck der
    Zugangsdaten, nie der Klartext; ein Passwortwechsel ergibt einen neuen
    Fingerabdruck und damit einen neuen Client mit frischer Anmeldung."""

    def __init__(
        self,
        credentials_key: str | None,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._key = credentials_key
        self._transport = transport
        self._sleep = sleep
        self._clients: dict[int, tuple[str, TonieCloudClient]] = {}
        self._guard = threading.Lock()

    def for_konto(self, db: Session, konto_id: int) -> TonieCloudClient:
        konto = db.get(TonieKonto, konto_id)
        if konto is None:
            raise KontoUnavailable(f"tonies-Konto Nr. {konto_id} existiert nicht.")
        zugang = konto_zugang(db, self._key, konto_id)
        if zugang is None:
            raise KontoUnavailable(
                f"Zugangsdaten des tonies-Kontos {_konto_name(konto)} müssen neu eingegeben werden."
            )
        # Lesbares Passwort heisst: der Schluessel ist brauchbar, es gibt
        # einen Fingerabdruck.
        fp = fingerprint(self._key, f"{zugang.username}\0{zugang.password}")
        with self._guard:
            cached = self._clients.get(konto_id)
            if cached is not None and cached[0] == fp:
                return cached[1]
            # Den alten Client nicht schliessen: ein laufender Lauf benutzt ihn
            # vielleicht noch.
            client = TonieCloudClient(
                zugang.username, zugang.password, transport=self._transport, sleep=self._sleep
            )
            self._clients[konto_id] = (fp, client)
            return client

    def for_tonie(self, db: Session, tonie: CreativeTonie) -> TonieCloudClient:
        if tonie.konto_id is None:
            raise KontoUnavailable("Für diesen Tonie ist kein tonies-Konto hinterlegt.")
        return self.for_konto(db, tonie.konto_id)


ANMELDUNG_FEHLGESCHLAGEN = "Anmeldung fehlgeschlagen"


@dataclass(frozen=True)
class KontoPruefung:
    """Ergebnis von `check_konto`: die Tonies des Kontos oder der Grund."""

    tonies: tuple[dict, ...] = ()
    fehler: str | None = None

    @property
    def ok(self) -> bool:
        return self.fehler is None


def check_konto(
    username: str,
    password: str,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> KontoPruefung:
    """R19: Test-Anmeldung, die zugleich die Creative Tonies des Kontos zur
    Auswahl listet. Speichert nichts. Eine abgelehnte Anmeldung ist ein
    Ergebnis ("Anmeldung fehlgeschlagen"); jeder andere Fehler (Zeitueber-
    schreitung, Serverfehler) geht als Ausnahme an den Aufrufer."""
    with TonieCloudClient(username, password, transport=transport, sleep=sleep) as client:
        try:
            return KontoPruefung(tonies=tuple(client.list_creative_tonies()))
        except LoginRejectedError:
            return KontoPruefung(fehler=ANMELDUNG_FEHLGESCHLAGEN)
