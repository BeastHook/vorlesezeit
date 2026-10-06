import pytest

from app.advent import ORNAMENTE, initialen, ornament_name, siegel_kontur


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Miri", "M"),
        ("Oma Lore", "OL"),
        ("Anna Maria Huber", "AH"),
        ("anna-lena huber", "AH"),
        ('  Oma   "Lore"  ', "OL"),
        ("🎄 Miri", "M"),
        ("Miri 🎄", "M"),
        ("Ömer Çelik", "ÖÇ"),
        ("", ""),
        ("   ", ""),
        (None, ""),
        ("🎄", ""),
    ],
)
def test_initialen(name, expected):
    assert initialen(name) == expected


def test_ornament_name_is_stable_and_cycles():
    assert ORNAMENTE == ("stern", "tannenzweig", "schneeflocke", "kerze", "stechpalme")
    assert [ornament_name(i) for i in range(5)] == list(ORNAMENTE)
    assert ornament_name(7) == ornament_name(7) == "schneeflocke"


def test_ornament_without_auftrag_is_kerze():
    assert ornament_name(None) == "kerze"


def test_siegel_kontur_range():
    assert {siegel_kontur(i) for i in range(20)} == {1, 2, 3, 4, 5}
    assert siegel_kontur(None) == 1


def test_filters_registered():
    from app.templating import templates

    environment = getattr(templates, "env")
    for name in ("initialen", "ornament_name", "siegel_kontur"):
        assert name in environment.filters


def test_pages_carry_advent_foundation(client):
    html = client.get("/login").text
    assert "family=Fraunces" in styles_css()
    assert 'id="wachs-3"' in html
    assert "/static/ornaments/kerze-marke.svg" in html  # Logo-Kerze seit 2026-10-04
    assert 'class="bereich-familie"' in html


def styles_css():
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / "app/static/styles.css").read_text()


def test_visible_name_is_vorlesezeit_with_logo_candle(client):
    """Nutzerentscheidung 2026-10-04: sichtbarer Name "Vorlesezeit" (Domain),
    kraeftigere Logo-Kerze. Seit 2026-10-06 heisst auch der Code vorlesezeit."""
    html = client.get("/login").text
    assert "ToniApply" not in html
    assert "Vorlesezeit</div>" in html
    assert "· Vorlesezeit</title>" in html
    assert "/static/ornaments/kerze-marke.svg" in html


def test_report_subjects_use_vorlesezeit():
    from datetime import date

    from app.mail.report import SummaryRow, build_summary_message

    rows = [SummaryRow("Familie", "Tonie", "abcd1234", "erfolg", 6)]
    message = build_summary_message(
        to_address="a@e.t", evening=date(2026, 12, 5), run_type="vorabend", rows=rows
    )
    assert message["Subject"].startswith("Vorlesezeit: Abendmeldung")
