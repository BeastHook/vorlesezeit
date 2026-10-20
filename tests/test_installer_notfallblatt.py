"""One-Klick-Setup Baustein 3: Notfallblatt als PDF ohne Bibliothek."""

from __future__ import annotations

import re

from installer.notfallblatt import notfallblatt_pdf

KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


def _inhalt(pdf: bytes) -> str:
    [strom] = re.findall(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.S)
    return strom.decode("cp1252")


def test_pdf_mit_schluessel_adresse_version_und_schritten():
    pdf = notfallblatt_pdf("berger.vorlesezeit.app", "0.3.0", KEY)

    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")
    inhalt = _inhalt(pdf)
    assert KEY in inhalt
    assert "https://berger.vorlesezeit.app" in inhalt
    assert "0.3.0" in inhalt
    assert "Wiederherstellung" in inhalt
    assert "getrennt" in inhalt
    assert "Rücksicherung" in inhalt  # Umlaute in WinAnsi


def test_querverweistabelle_zeigt_auf_die_objekte():
    pdf = notfallblatt_pdf("berger.vorlesezeit.app", "0.3.0", KEY)
    start = int(re.search(rb"startxref\s+(\d+)", pdf).group(1))
    assert pdf[start:].startswith(b"xref")
    offsets = [int(o) for o in re.findall(rb"(\d{10}) 00000 n", pdf)]
    for nummer, offset in enumerate(offsets, start=1):
        assert pdf[offset:].startswith(f"{nummer} 0 obj".encode())


def test_klammern_im_text_sind_maskiert():
    pdf = notfallblatt_pdf("a.b", "0.3.0", "x(y)z\\")
    assert "x\\(y\\)z\\\\" in _inhalt(pdf)
