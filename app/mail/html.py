"""HTML-Fassung der Mails (Spec Advent-Design, Abschnitt 6).

Der Textteil bleibt der verlaessliche Kern; HTML kommt als Alternative dazu.
Das Ornament wird als PNG eingebettet (CID) -- keine oeffentliche Adresse noetig,
und Gmail zeigt kein SVG.
"""

from __future__ import annotations

from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

from app.templating import templates

MAIL_STATIC = Path(__file__).resolve().parent.parent / "static" / "mail"
_jinja = getattr(templates, "env")


def render_text(template: str, context: dict) -> str:
    return _jinja.get_template(template).render(**context)


def attach_html(
    message: EmailMessage, template: str, context: dict, *, ornament: str | None
) -> None:
    cid = make_msgid(domain="vorlesezeit.local") if ornament else None
    html = _jinja.get_template(template).render(**context, ornament_cid=cid[1:-1] if cid else None)
    message.add_alternative(html, subtype="html")
    if cid:
        html_part = message.get_payload()[-1]
        png = (MAIL_STATIC / f"{ornament}.png").read_bytes()
        html_part.add_related(png, "image", "png", cid=cid, disposition="inline")
