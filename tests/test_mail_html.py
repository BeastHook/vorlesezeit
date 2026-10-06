from email.message import EmailMessage

import jinja2
import pytest

from app.mail.html import _jinja, attach_html
from tests.mailutil import html_text, plain_text

# Testeigenes Template statt einer Datei im Repo (Final-Review-Fund): ueber
# einen zusaetzlichen DictLoader in dieselbe Jinja-Umgebung gehaengt, damit
# attach_html() unveraendert bleibt und "mail/_base.html" normal erbt.
_TEST_TEMPLATE_SOURCE = '{% extends "mail/_base.html" %}{% block body %}{{ text }}{% endblock %}'


@pytest.fixture(autouse=True)
def _mail_test_template():
    original_loader = _jinja.loader
    _jinja.loader = jinja2.ChoiceLoader(
        [jinja2.DictLoader({"mail/_test.html": _TEST_TEMPLATE_SOURCE}), original_loader]
    )
    try:
        yield
    finally:
        _jinja.loader = original_loader


def _message():
    m = EmailMessage()
    m["Subject"] = "x"
    m.set_content("Klartext bleibt\n")
    return m


def test_attach_html_makes_alternative_with_embedded_ornament():
    m = _message()
    attach_html(m, "mail/_test.html", {"text": "<b>Hallo</b>"}, ornament="stern")
    assert m.get_content_type() == "multipart/alternative"
    assert plain_text(m) == "Klartext bleibt\n"
    html = html_text(m)
    assert "&lt;b&gt;Hallo&lt;/b&gt;" in html
    images = [p for p in m.walk() if p.get_content_type() == "image/png"]
    assert len(images) == 1
    cid = images[0]["Content-ID"].strip("<>")
    assert f"cid:{cid}" in html
    assert 'alt=""' in html


def test_attach_html_without_ornament_has_no_image():
    m = _message()
    attach_html(m, "mail/_test.html", {"text": "x"}, ornament=None)
    assert not [p for p in m.walk() if p.get_content_type() == "image/png"]


def test_ornament_is_centered_by_table_cell_for_gmail():
    """Gmail ignoriert margin:auto an Bildern (echter Mailtest 2026-10-04): das
    Ornament steht in einer zentrierten Tabellenzelle."""
    m = _message()
    attach_html(m, "mail/_test.html", {"text": "x"}, ornament="stern")
    import re

    html = html_text(m)
    assert re.search(r'<td align="center"[^>]*>\s*<img src="cid:', html)
