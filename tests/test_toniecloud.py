"""U4: Toniecloud-Client -- test-first gegen aufgezeichnete Antworten.

Execution note im Plan: reine Logik mit unreparierbaren Folgen (ein Fehler
kann Dateien auf dem echten Tonie falsch ersetzen), deshalb hier ausschliess-
lich gegen ein `httpx.MockTransport` mit exakt vorgegebener Aufrufreihenfolge
und an die echte OpenAPI-Spec angelehnten Antwortkoerpern -- kein Zugriff auf
das echte Konto. Der reale Kontoabgleich (Verification Contract der Einheit)
laeuft separat und manuell.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import parse_qs

import httpx
import pytest

from app import settings as st
from app.models import CreativeTonie, TonieKonto
from app.toniecloud.client import (
    ANMELDUNG_FEHLGESCHLAGEN,
    API_BASE_URL,
    TOKEN_URL,
    KontoUnavailable,
    LoginRejectedError,
    LoginTimeoutError,
    TermsOfUseRequiredError,
    TonieCloudClient,
    TonieCloudFactory,
    UnsupportedFormatError,
    check_konto,
    validate_format,
    verify_upload,
)
from app.toniecloud.models import Chapter, ConfigLimits, CreativeTonieState, TranscodingError

USERNAME = "family@example.test"
PASSWORD = "hunter2"

HOUSEHOLD_ID = "11111111-1111-1111-1111-111111111111"
TONIE_ID = "1234ABCDE00304E0"


def token_response(*, access_token: str = "access-token-1") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": access_token,
            "expires_in": 300,
            "refresh_token": "refresh-token",
            "token_type": "Bearer",
        },
    )


def rejected_login_response() -> httpx.Response:
    return httpx.Response(
        401, json={"error": "invalid_grant", "error_description": "Invalid user credentials"}
    )


def creative_tonie_response(
    *,
    chapters: list[dict] | None = None,
    transcoding: bool = False,
    transcoding_errors: list[dict] | None = None,
    chapters_present: int | None = None,
    seconds_present: float = 12.0,
    last_update: str | None = "2026-09-17T20:00:00+0200",
) -> httpx.Response:
    chapters = chapters if chapters is not None else []
    return httpx.Response(
        200,
        json={
            "id": TONIE_ID,
            "householdId": HOUSEHOLD_ID,
            "name": "Adventskalender",
            "chapters": chapters,
            "transcoding": transcoding,
            "chaptersPresent": chapters_present if chapters_present is not None else len(chapters),
            "secondsPresent": seconds_present,
            "transcodingErrors": transcoding_errors or [],
            "lastUpdate": last_update,
        },
    )


class ScriptedTransport:
    """Spielt eine feste Folge von (Methode, exakte URL) -> Antwort/Fehler ab.

    Jeder Aufruf muss dem naechsten Skript-Eintrag entsprechen -- das ist die
    "aufgezeichnete Antwort" aus der Execution note: kein Routing nach
    Muster, sondern eine geprueft Reihenfolge, wie sie ein echter Lauf hat.
    """

    def __init__(self, steps: list[tuple[str, str, httpx.Response | Exception]]) -> None:
        self._steps = list(steps)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, str(request.url)))
        if not self._steps:
            raise AssertionError(f"Unerwarteter Aufruf ohne Skript: {request.method} {request.url}")
        expected_method, expected_url, outcome = self._steps.pop(0)
        if request.method != expected_method or str(request.url) != expected_url:
            raise AssertionError(
                f"Erwartet {expected_method} {expected_url}, bekam {request.method} {request.url}"
            )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def assert_exhausted(self) -> None:
        assert self._steps == [], f"Nicht abgerufene Skript-Schritte: {self._steps}"


def make_client(steps, *, sleep=lambda seconds: None) -> tuple[TonieCloudClient, ScriptedTransport]:
    script = ScriptedTransport(steps)
    transport = httpx.MockTransport(script)
    client = TonieCloudClient(USERNAME, PASSWORD, transport=transport, sleep=sleep)
    return client, script


# --- Anmeldung ---------------------------------------------------------


def test_login_rejected_fails_immediately_without_retry():
    client, script = make_client(
        [("POST", TOKEN_URL, rejected_login_response())],
    )

    with pytest.raises(LoginRejectedError) as excinfo:
        client._ensure_token()

    assert "401" in str(excinfo.value)
    # Body/Header duerfen nie in der Fehlermeldung auftauchen.
    assert "hunter2" not in str(excinfo.value)
    assert "family@example.test" not in str(excinfo.value)
    script.assert_exhausted()


def test_login_timeout_retries_then_succeeds():
    sleeps: list[float] = []
    client, script = make_client(
        [
            ("POST", TOKEN_URL, httpx.TimeoutException("timed out")),
            ("POST", TOKEN_URL, httpx.TimeoutException("timed out")),
            ("POST", TOKEN_URL, token_response()),
        ],
        sleep=sleeps.append,
    )

    client._ensure_token()

    assert len(script.calls) == 3
    assert len(sleeps) == 2
    script.assert_exhausted()


def test_login_timeout_fails_after_fixed_cap():
    client, script = make_client(
        [
            ("POST", TOKEN_URL, httpx.TimeoutException("timed out")),
            ("POST", TOKEN_URL, httpx.TimeoutException("timed out")),
            ("POST", TOKEN_URL, httpx.TimeoutException("timed out")),
        ],
        sleep=lambda seconds: None,
    )

    with pytest.raises(LoginTimeoutError):
        client._ensure_token()

    script.assert_exhausted()


def test_expired_token_triggers_single_relogin_not_loop():
    get_url = f"{API_BASE_URL}/households/{HOUSEHOLD_ID}/creativetonies/{TONIE_ID}"
    client, script = make_client(
        [
            ("POST", TOKEN_URL, token_response(access_token="access-token-1")),
            ("GET", get_url, creative_tonie_response()),
            ("GET", get_url, httpx.Response(401, json={"detail": "token expired"})),
            ("POST", TOKEN_URL, token_response(access_token="access-token-2")),
            ("GET", get_url, creative_tonie_response()),
        ],
    )

    client.get_state(HOUSEHOLD_ID, TONIE_ID)
    client.get_state(HOUSEHOLD_ID, TONIE_ID)

    token_calls = [c for c in script.calls if c[1] == TOKEN_URL]
    assert len(token_calls) == 2
    script.assert_exhausted()


# --- Datei anlegen und hochladen ---------------------------------------


def test_create_file_409_raises_terms_of_use_required():
    client, script = make_client(
        [
            ("POST", TOKEN_URL, token_response()),
            ("POST", f"{API_BASE_URL}/file", httpx.Response(409)),
        ],
    )

    with pytest.raises(TermsOfUseRequiredError):
        client.create_file()

    script.assert_exhausted()


def test_upload_rejects_unsupported_format_locally():
    limits = ConfigLimits(
        max_chapters=250, max_seconds=5400, max_bytes=2**30, accepts=("mp3", "wav")
    )

    with pytest.raises(UnsupportedFormatError):
        validate_format("aufnahme.webm", limits)

    # Wirft nicht fuer ein akzeptiertes Format.
    validate_format("aufnahme.mp3", limits)


def test_upload_bytes_sends_multipart_with_s3_fields():
    upload_url = "https://tonie-uploads.s3.amazonaws.com/"
    client, script = make_client(
        [
            ("POST", upload_url, httpx.Response(204)),
        ],
    )
    from app.toniecloud.models import UploadTarget

    target = UploadTarget(
        file_id="fileid-abc",
        s3_url=upload_url,
        s3_fields={"key": "uploads/fileid-abc.mp3", "policy": "xyz"},
    )

    client.upload_bytes(target, b"fake-mp3-bytes", filename="story.mp3", content_type="audio/mpeg")

    method, url = script.calls[0]
    assert method == "POST"
    assert url == upload_url
    script.assert_exhausted()


# --- Household-Aufloesung (U6: Generalprobe-Setup) -----------------------


def test_list_creative_tonies_collects_across_households():
    other_household = "22222222-2222-2222-2222-222222222222"
    client, script = make_client(
        [
            ("POST", TOKEN_URL, token_response()),
            (
                "GET",
                f"{API_BASE_URL}/households",
                httpx.Response(
                    200,
                    json=[
                        {"id": HOUSEHOLD_ID, "name": "Zuhause", "access": "owner"},
                        {"id": other_household, "name": "Anderswo", "access": "owner"},
                    ],
                ),
            ),
            (
                "GET",
                f"{API_BASE_URL}/households/{HOUSEHOLD_ID}/creativetonies",
                httpx.Response(
                    200,
                    json=[{"id": TONIE_ID, "householdId": HOUSEHOLD_ID, "name": "Adventskalender"}],
                ),
            ),
            (
                "GET",
                f"{API_BASE_URL}/households/{other_household}/creativetonies",
                httpx.Response(200, json=[]),
            ),
        ],
    )

    result = client.list_creative_tonies()

    assert result == [{"household_id": HOUSEHOLD_ID, "id": TONIE_ID, "name": "Adventskalender"}]
    script.assert_exhausted()


# --- Kapitelliste ersetzen ----------------------------------------------


def test_replace_chapters_sends_full_list_not_delta():
    patch_url = f"{API_BASE_URL}/households/{HOUSEHOLD_ID}/creativetonies/{TONIE_ID}"
    existing = Chapter(id="chapter-old", title="Tuerchen 1", file="opaque-blob-1")
    new_chapter = Chapter(title="Tuerchen 2", file="fileid-new")

    captured_body: dict = {}

    def capture_patch(request: httpx.Request) -> httpx.Response:
        import json

        captured_body.update(json.loads(request.content))
        return creative_tonie_response(
            chapters=[
                {"id": "chapter-old", "title": "Tuerchen 1", "file": "opaque-blob-1"},
                {"id": "chapter-new", "title": "Tuerchen 2", "file": "opaque-blob-2"},
            ]
        )

    # Der PATCH-Schritt braucht dynamisches Verhalten (Body pruefen), daher
    # hier ein eigener Transport statt des generischen ScriptedTransport.
    call_log: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        call_log.append(request)
        if request.method == "POST" and str(request.url) == TOKEN_URL:
            return token_response()
        if request.method == "PATCH" and str(request.url) == patch_url:
            return capture_patch(request)
        raise AssertionError(f"Unerwarteter Aufruf: {request.method} {request.url}")

    client = TonieCloudClient(
        USERNAME, PASSWORD, transport=httpx.MockTransport(handler), sleep=lambda seconds: None
    )

    result = client.replace_chapters(HOUSEHOLD_ID, TONIE_ID, [existing, new_chapter])

    assert captured_body["chapters"] == [
        {"id": "chapter-old", "title": "Tuerchen 1", "file": "opaque-blob-1"},
        {"title": "Tuerchen 2", "file": "fileid-new"},
    ]
    assert result.chapters_present == 2


# --- verify_upload (reine Logik, kein Netzwerk) -------------------------


def make_state(
    *,
    chapters: list[Chapter],
    transcoding_errors: tuple[TranscodingError, ...] = (),
) -> CreativeTonieState:
    return CreativeTonieState(
        id=TONIE_ID,
        household_id=HOUSEHOLD_ID,
        chapters=tuple(chapters),
        transcoding=False,
        chapters_present=len(chapters),
        seconds_present=10.0,
        transcoding_errors=transcoding_errors,
        last_update="2026-09-17T20:00:00+0200",
    )


def test_verify_upload_succeeds_when_ids_survive_transcoding():
    patch_state = make_state(
        chapters=[Chapter(id="server-id-a", title="Tuerchen 1", file="fileid-a")]
    )
    final_state = make_state(
        chapters=[Chapter(id="server-id-a", title="Tuerchen 1", file="server-id-a")]
    )

    result = verify_upload(patch_state, final_state)

    assert result.success is True
    assert result.title_mismatches == ()


def test_verify_upload_fails_when_transcoding_error_reported():
    patch_state = make_state(
        chapters=[Chapter(id="server-id-a", title="Tuerchen 1", file="fileid-a")]
    )
    final_state = make_state(
        chapters=[],
        transcoding_errors=(
            TranscodingError(reason="wrongFormat", deleted_chapter_titles=("Tuerchen 1",)),
        ),
    )

    result = verify_upload(patch_state, final_state)

    assert result.success is False
    assert "wrongFormat" in result.reason


def test_verify_upload_fails_on_foreign_id():
    patch_state = make_state(
        chapters=[Chapter(id="server-id-a", title="Tuerchen 1", file="fileid-a")]
    )
    final_state = make_state(
        chapters=[Chapter(id="server-id-b", title="Tuerchen 1", file="server-id-b")]
    )

    result = verify_upload(patch_state, final_state)

    assert result.success is False


def test_verify_upload_fails_on_wrong_order():
    patch_state = make_state(
        chapters=[
            Chapter(id="server-id-a", title="Tuerchen 1", file="fileid-a"),
            Chapter(id="server-id-b", title="Tuerchen 2", file="fileid-b"),
        ]
    )
    final_state = make_state(
        chapters=[
            Chapter(id="server-id-b", title="Tuerchen 2", file="server-id-b"),
            Chapter(id="server-id-a", title="Tuerchen 1", file="server-id-a"),
        ]
    )

    result = verify_upload(patch_state, final_state)

    assert result.success is False


def test_verify_upload_fails_on_wrong_count_despite_matching_length():
    # Kapitelzahl und Laenge allein beweisen nichts -- hier stimmt die
    # Sekundenzahl der Ansicht nach, aber ein Kapitel fehlt in der id-Folge.
    patch_state = make_state(
        chapters=[
            Chapter(id="server-id-a", title="Tuerchen 1", file="fileid-a"),
            Chapter(id="server-id-b", title="Tuerchen 2", file="fileid-b"),
        ]
    )
    final_state = make_state(
        chapters=[Chapter(id="server-id-a", title="Tuerchen 1", file="server-id-a")]
    )

    result = verify_upload(patch_state, final_state)

    assert result.success is False


def test_verify_upload_reports_title_mismatch_without_failing():
    patch_state = make_state(
        chapters=[Chapter(id="server-id-a", title="Tuerchen 1", file="fileid-a")]
    )
    final_state = make_state(
        chapters=[Chapter(id="server-id-a", title="Anderer Titel", file="server-id-a")]
    )

    result = verify_upload(patch_state, final_state, expected_titles=["Tuerchen 1"])

    assert result.success is True
    assert result.title_mismatches == (("Tuerchen 1", "Anderer Titel"),)


def test_verify_upload_fails_with_reason_when_chapter_count_differs_from_titles():
    # U16: fehlt ein Bestandskapitel schon in der PATCH-Antwort, sind beide
    # id-Folgen gleich, die erwarteten Titel aber laenger -- benannter
    # Fehlschlag statt ValueError aus zip(strict=True).
    state = make_state(chapters=[Chapter(id="a", title="A", file="a")])

    result = verify_upload(state, state, expected_titles=["A", "Familie"])

    assert result.success is False
    assert "Kapitelzahl" in result.reason


def test_verify_upload_empty_lists_succeed():
    patch_state = make_state(chapters=[])
    final_state = make_state(chapters=[])

    result = verify_upload(patch_state, final_state)

    assert result.success is True


# --- Verarbeitung abwarten -----------------------------------------------


def test_wait_until_processed_polls_until_transcoding_finishes():
    get_url = f"{API_BASE_URL}/households/{HOUSEHOLD_ID}/creativetonies/{TONIE_ID}"
    sleeps: list[float] = []
    client, script = make_client(
        [
            ("POST", TOKEN_URL, token_response()),
            ("GET", get_url, creative_tonie_response(transcoding=True)),
            ("GET", get_url, creative_tonie_response(transcoding=True)),
            (
                "GET",
                get_url,
                creative_tonie_response(
                    transcoding=False,
                    chapters=[{"id": "file-a", "title": "Tuerchen 1", "file": "blob-1"}],
                ),
            ),
        ],
        sleep=sleeps.append,
    )

    state = client.wait_until_processed(HOUSEHOLD_ID, TONIE_ID, poll_interval=1.0, timeout=10.0)

    assert state.transcoding is False
    assert len(sleeps) == 2
    script.assert_exhausted()


# --- Vollstaendiger Ablauf (Integration, nur Transport gemockt) ----------


def test_full_upload_and_replace_flow_succeeds():
    """Login -> Config -> Formatpruefung -> Datei anlegen -> Upload ->
    Baseline lesen -> Ersetzen -> Pollen -> Verify -- der komplette
    U4-Ablauf aus dem Plan-Approach, ohne Mocks auf Anwendungsebene."""
    file_url = f"{API_BASE_URL}/file"
    s3_url = "https://tonie-uploads.s3.amazonaws.com/"
    tonie_url = f"{API_BASE_URL}/households/{HOUSEHOLD_ID}/creativetonies/{TONIE_ID}"

    # Gegen das echte Konto bestaetigt (SKILL.md): der PATCH vergibt sofort
    # eine eigene, stabile Kapitel-`id` (hier "server-id-a"), verschieden von
    # der hochgeladenen `fileId` ("file-a"). `file` zeigt unmittelbar nach
    # dem PATCH noch die fileId, wird nach dem Transcoding aber zum Duplikat
    # von `id`. Verify prueft deshalb die `id`-Folge zwischen PATCH- und
    # End-Zustand, nicht gegen die urspruengliche fileId.
    patch_chapters = [{"id": "server-id-a", "title": "Tuerchen 1", "file": "file-a"}]
    final_chapters = [{"id": "server-id-a", "title": "Tuerchen 1", "file": "server-id-a"}]

    # GET auf den Tonie-Endpunkt hat drei Bedeutungen je nach Reihenfolge:
    # zuerst die Rollback-Baseline (leer), danach zwei Poll-Antworten
    # (noch in Verarbeitung), zuletzt die fertige Verarbeitung.
    tonie_get_responses = iter(
        [
            creative_tonie_response(chapters=[]),
            creative_tonie_response(transcoding=True),
            creative_tonie_response(transcoding=True),
            creative_tonie_response(transcoding=False, chapters=final_chapters),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url == TOKEN_URL:
            return token_response()
        if request.method == "GET" and url == f"{API_BASE_URL}/config":
            return httpx.Response(
                200,
                json={
                    "maxChapters": 250,
                    "maxSeconds": 5400,
                    "maxBytes": 1073741824,
                    "accepts": ["mp3", "wav"],
                },
            )
        if request.method == "POST" and url == file_url:
            return httpx.Response(
                200,
                json={
                    "fileId": "file-a",
                    "request": {"url": s3_url, "fields": {"key": "uploads/file-a.mp3"}},
                },
            )
        if request.method == "POST" and url == s3_url:
            return httpx.Response(204)
        if request.method == "GET" and url == tonie_url:
            return next(tonie_get_responses)
        if request.method == "PATCH" and url == tonie_url:
            return creative_tonie_response(transcoding=True, chapters=patch_chapters)
        raise AssertionError(f"Unerwarteter Aufruf: {request.method} {request.url}")

    client = TonieCloudClient(
        USERNAME,
        PASSWORD,
        transport=httpx.MockTransport(handler),
        sleep=lambda seconds: None,
    )

    limits = client.get_config()
    validate_format("aufnahme.mp3", limits)

    target = client.create_file()
    client.upload_bytes(target, b"fake-mp3-bytes", filename="story.mp3", content_type="audio/mpeg")

    client.get_state(HOUSEHOLD_ID, TONIE_ID)  # Rollback-Stand lesen
    patch_state = client.replace_chapters(
        HOUSEHOLD_ID, TONIE_ID, [Chapter(title="Tuerchen 1", file=target.file_id)]
    )
    final_state = client.wait_until_processed(HOUSEHOLD_ID, TONIE_ID, poll_interval=0.0)

    result = verify_upload(patch_state, final_state, expected_titles=["Tuerchen 1"])

    assert result.success is True
    assert final_state.transcoding is False


# --- Mehrkalender U6: Client je Konto, Fabrik, Pruefung (KTD8, R2/R5/R19/R21) ---

KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
NOW = datetime(2026, 10, 2, 12, 0)


class AccountTransport:
    """Antwortet je Benutzername: Anmeldung vergibt `token-<user>-<n>`,
    Haushalte/Tonies aus `tonies_by_user`. Zaehlt jeden Aufruf."""

    def __init__(self, tonies_by_user: dict[str, list[dict]] | None = None) -> None:
        self.tonies_by_user = tonies_by_user or {}
        self.calls: list[httpx.Request] = []
        self.logins: list[str] = []
        self.rejected: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        if url == TOKEN_URL:
            user = parse_qs(request.content.decode())["username"][0]
            if user in self.rejected:
                return rejected_login_response()
            self.logins.append(user)
            return token_response(access_token=f"token-{user}-{len(self.logins)}")
        user = request.headers["Authorization"].removeprefix("Bearer token-").rsplit("-", 1)[0]
        if url == f"{API_BASE_URL}/households":
            return httpx.Response(200, json=[{"id": f"hh-{user}", "name": "Zuhause"}])
        if url == f"{API_BASE_URL}/households/hh-{user}/creativetonies":
            return httpx.Response(200, json=self.tonies_by_user.get(user, []))
        raise AssertionError(f"Unerwarteter Aufruf: {request.method} {url}")

    def tokens_used(self) -> set[str]:
        return {
            r.headers["Authorization"].removeprefix("Bearer ")
            for r in self.calls
            if "Authorization" in r.headers
        }

    def login_passwords(self) -> list[str]:
        return [
            parse_qs(r.content.decode())["password"][0]
            for r in self.calls
            if str(r.url) == TOKEN_URL
        ]


def make_factory(transport: AccountTransport) -> TonieCloudFactory:
    return TonieCloudFactory(KEY, transport=httpx.MockTransport(transport), sleep=lambda s: None)


def add_konto(db_session, username: str, password: str, label: str = "") -> int:
    konto = st.create_konto(
        db_session, KEY, username=username, password=password, label=label, now=NOW
    )
    return konto.id


def test_two_kontos_get_two_clients_with_separate_tokens(db_session):
    transport = AccountTransport()
    factory = make_factory(transport)
    oma = add_konto(db_session, "oma@example.test", "pw-oma")
    opa = add_konto(db_session, "opa@example.test", "pw-opa")

    client_oma = factory.for_konto(db_session, oma)
    client_opa = factory.for_konto(db_session, opa)
    client_oma.list_creative_tonies()
    client_opa.list_creative_tonies()

    assert client_oma is not client_opa
    assert transport.logins == ["oma@example.test", "opa@example.test"]
    assert transport.tokens_used() == {"token-oma@example.test-1", "token-opa@example.test-2"}


def test_token_cache_is_per_konto_and_survives_new_requests(db_session):
    transport = AccountTransport()
    factory = make_factory(transport)
    oma = add_konto(db_session, "oma@example.test", "pw-oma")

    factory.for_konto(db_session, oma).list_creative_tonies()
    factory.for_konto(db_session, oma).list_creative_tonies()

    assert transport.logins == ["oma@example.test"]  # eine Anmeldung fuer beide Anfragen


def test_password_change_drops_cached_token_of_this_konto_only(db_session):
    transport = AccountTransport()
    factory = make_factory(transport)
    oma = add_konto(db_session, "oma@example.test", "pw-oma")
    opa = add_konto(db_session, "opa@example.test", "pw-opa")
    factory.for_konto(db_session, oma).list_creative_tonies()
    factory.for_konto(db_session, opa).list_creative_tonies()

    st.set_konto_password(db_session, KEY, oma, "pw-oma-neu", now=NOW)
    factory.for_konto(db_session, oma).list_creative_tonies()
    factory.for_konto(db_session, opa).list_creative_tonies()

    assert transport.logins == ["oma@example.test", "opa@example.test", "oma@example.test"]
    assert transport.login_passwords()[-1] == "pw-oma-neu"


def test_konto_needing_reentry_raises_named_error_without_any_http_call(db_session):
    transport = AccountTransport()
    factory = make_factory(transport)
    oma = add_konto(db_session, "oma@example.test", "pw-oma", label="Oma")
    db_session.get(TonieKonto, oma).needs_reentry = True
    db_session.commit()

    with pytest.raises(KontoUnavailable) as excinfo:
        factory.for_konto(db_session, oma)

    assert transport.calls == []
    assert "Oma" in str(excinfo.value)
    assert "pw-oma" not in str(excinfo.value)


def test_unreadable_password_raises_named_error_without_any_http_call(db_session):
    transport = AccountTransport()
    oma = add_konto(db_session, "oma@example.test", "pw-oma")
    other_key = TonieCloudFactory(
        "MTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTE=",
        transport=httpx.MockTransport(transport),
    )

    with pytest.raises(KontoUnavailable):
        other_key.for_konto(db_session, oma)

    assert transport.calls == []


def test_missing_konto_raises_named_error(db_session):
    with pytest.raises(KontoUnavailable):
        make_factory(AccountTransport()).for_konto(db_session, 4711)


def test_for_tonie_uses_the_konto_of_the_creative_tonie(db_session):
    transport = AccountTransport()
    factory = make_factory(transport)
    opa = add_konto(db_session, "opa@example.test", "pw-opa")
    tonie = CreativeTonie(tonie_id=TONIE_ID, konto_id=opa)
    db_session.add(tonie)
    db_session.commit()

    factory.for_tonie(db_session, tonie).list_creative_tonies()

    assert transport.logins == ["opa@example.test"]


def test_for_tonie_without_konto_raises_named_error(db_session):
    tonie = CreativeTonie(tonie_id=TONIE_ID)
    db_session.add(tonie)
    db_session.commit()

    with pytest.raises(KontoUnavailable):
        make_factory(AccountTransport()).for_tonie(db_session, tonie)


def test_check_konto_lists_tonies_without_storing(db_session):
    transport = AccountTransport({"oma@example.test": [{"id": TONIE_ID, "name": "Kalender"}]})

    result = check_konto("oma@example.test", "pw-oma", transport=httpx.MockTransport(transport))

    assert result.ok is True
    assert result.fehler is None
    assert result.tonies == (
        {"household_id": "hh-oma@example.test", "id": TONIE_ID, "name": "Kalender"},
    )
    assert db_session.query(TonieKonto).count() == 0


def test_check_konto_with_rejected_login_reports_named_result(db_session):
    transport = AccountTransport()
    transport.rejected.add("oma@example.test")

    result = check_konto(
        "oma@example.test", "falsch-geheim", transport=httpx.MockTransport(transport)
    )

    assert result.ok is False
    assert result.fehler == ANMELDUNG_FEHLGESCHLAGEN == "Anmeldung fehlgeschlagen"
    assert result.tonies == ()
    assert "falsch-geheim" not in repr(result)
    assert db_session.query(TonieKonto).count() == 0


def test_same_tonie_visible_from_two_kontos_has_same_id():
    """R5: ueber zwei Konten sichtbar -- beide Listen tragen dieselbe Tonie-ID."""
    shared = {"id": TONIE_ID, "name": "Adventskalender"}
    transport = AccountTransport({"oma@example.test": [shared], "opa@example.test": [shared]})
    mock = httpx.MockTransport(transport)

    via_oma = check_konto("oma@example.test", "pw-oma", transport=mock)
    via_opa = check_konto("opa@example.test", "pw-opa", transport=mock)

    assert [t["id"] for t in via_oma.tonies] == [TONIE_ID]
    assert [t["id"] for t in via_opa.tonies] == [TONIE_ID]


def test_password_never_in_client_repr_or_errors():
    client, _ = make_client([("POST", TOKEN_URL, rejected_login_response())])

    assert PASSWORD not in repr(client)
    assert USERNAME in repr(client)
    with pytest.raises(LoginRejectedError) as excinfo:
        client._ensure_token()
    assert PASSWORD not in str(excinfo.value)
