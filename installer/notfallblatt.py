"""Notfallblatt als PDF, ohne PDF-Bibliothek (wäre eine neue Abhängigkeit).

Eine Seite A4, Schrift Helvetica mit WinAnsi-Kodierung (deckt die Umlaute ab),
Inhaltsstrom unkomprimiert. Inhalt nach dem Design-Dokument: Adresse,
Version, CREDENTIALS_KEY, Schritte zur Wiederherstellung und die Bitte um
getrennte Aufbewahrung.
"""

from __future__ import annotations

from datetime import date

_ZEILENABSTAND = 16
_LINKS = 56
_OBEN = 780


def _text(zeile: str) -> bytes:
    roh = zeile.encode("cp1252", errors="replace")
    return roh.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _zeilen(adresse: str, version: str, credentials_key: str) -> list[tuple[int, str]]:
    """(Schriftgröße, Text); Größe 0 ist eine Leerzeile."""
    return [
        (20, "Vorlesezeit: Notfallblatt"),
        (0, ""),
        (11, "Bitte getrennt von diesem Rechner aufbewahren, am besten ausgedruckt."),
        (11, "Wer dieses Blatt hat, kann die gespeicherten Zugangsdaten lesen."),
        (0, ""),
        (13, "Deine Installation"),
        (11, f"Adresse: https://{adresse}"),
        (11, f"Version: {version}"),
        (11, f"Erstellt am: {date.today().strftime('%d.%m.%Y')}"),
        (0, ""),
        (13, "Schlüssel für die Zugangsdaten (CREDENTIALS_KEY)"),
        (11, credentials_key),
        (0, ""),
        (13, "Wiederherstellung nach einem Defekt"),
        (11, "1. Auf dem neuen Rechner Docker Desktop installieren."),
        (11, "2. Die Festplatte mit dem Ordner „Vorlesezeit-Sicherung“ anschließen."),
        (11, "3. Die Startdatei öffnen und dieselbe Adresse und denselben Code verwenden."),
        (11, "4. Die Rücksicherung aus der Sicherung einspielen (docs/betrieb.md,"),
        (11, "    Abschnitt „Sicherung und Rücksicherung“)."),
        (11, "5. Den CREDENTIALS_KEY von diesem Blatt in die .env eintragen. Ohne ihn"),
        (11, "    musst du alle Passwörter in Vorlesezeit neu eingeben."),
        (0, ""),
        (11, "Hilfe bekommst du bei der Person, die dich eingeladen hat."),
    ]


def _inhaltsstrom(adresse: str, version: str, credentials_key: str) -> bytes:
    teile = [b"BT", f"{_LINKS} {_OBEN} Td".encode()]
    erste = True
    for groesse, zeile in _zeilen(adresse, version, credentials_key):
        if not erste:
            teile.append(f"0 -{_ZEILENABSTAND + (8 if groesse > 11 else 0)} Td".encode())
        erste = False
        if groesse:
            teile.append(f"/F1 {groesse} Tf".encode())
            teile.append(b"(" + _text(zeile) + b") Tj")
    teile.append(b"ET")
    return b"\n".join(teile)


def notfallblatt_pdf(adresse: str, version: str, credentials_key: str) -> bytes:
    strom = _inhaltsstrom(adresse, version, credentials_key)
    objekte = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(strom)).encode() + b" >>\nstream\n" + strom + b"\nendstream",
    ]
    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for nummer, objekt in enumerate(objekte, start=1):
        offsets.append(len(pdf))
        pdf += f"{nummer} 0 obj\n".encode() + objekt + b"\nendobj\n"
    xref = len(pdf)
    pdf += f"xref\n0 {len(objekte) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        pdf += f"{offset:010d} 00000 n \n".encode()
    pdf += f"trailer\n<< /Size {len(objekte) + 1} /Root 1 0 R >>\n".encode()
    pdf += f"startxref\n{xref}\n%%EOF\n".encode()
    return bytes(pdf)
