"""Herkunftspruefung fuer abschickende Anfragen (CSRF).

Freunde-Instanzen liegen unter <name>.vorlesezeit.app und sind fuer den Browser
dieselbe Website wie jede andere Instanz dort: das Sitzungs-Cookie
(SameSite=Lax) geht bei einem Formular von einer Nachbaradresse mit. Deshalb
wird jede schreibende Anfrage abgewiesen, deren Origin nicht die eigene
Adresse ist (derselbe Vergleich wie `admin.einstellungen.setup_admin`).

Ohne Origin entscheidet Sec-Fetch-Site; fehlt auch das, ist es kein Browser
(Zeitplan-Container, cron-job.org, curl) und die Anfrage geht durch -- die
haben ohnehin kein Sitzungs-Cookie.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

LESEND = {"GET", "HEAD", "OPTIONS"}
VON_HIER = {"same-origin", "none"}


def fremde_herkunft(request: Request) -> bool:
    if request.method in LESEND:
        return False
    origin = request.headers.get("origin")
    if origin is not None:
        return origin != f"{request.url.scheme}://{request.url.netloc}"
    site = request.headers.get("sec-fetch-site")
    return site is not None and site not in VON_HIER


class HerkunftMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and fremde_herkunft(Request(scope)):
            response = PlainTextResponse("Formular kommt nicht von dieser Seite.", status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
