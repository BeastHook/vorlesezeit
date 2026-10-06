"""Advent-Design: feste Zuordnungen fuer Ornament, Siegelkontur und Initialen.

Spec docs/superpowers/specs/2026-10-03-advent-design-design.md, Abschnitte 2/3.
Ornament und Kontur haengen fest an der ID, damit dieselbe Geschichte bzw.
derselbe Beitrag ueberall gleich aussieht.
"""

from __future__ import annotations

ORNAMENTE = ("stern", "tannenzweig", "schneeflocke", "kerze", "stechpalme")
SIEGEL_KONTUREN = 5


def _first_letter(word: str) -> str | None:
    return next((ch for ch in word if ch.isalpha()), None)


def initialen(name: str | None) -> str:
    """Erster Buchstabe des ersten und des letzten Wortes, hoechstens zwei.
    Woerter ohne Buchstaben (Emoji, Satzzeichen) zaehlen nicht."""
    letters = [
        letter
        for word in (name or "").replace("-", " ").split()
        if (letter := _first_letter(word)) is not None
    ]
    if not letters:
        return ""
    picked = letters[0] if len(letters) == 1 else letters[0] + letters[-1]
    return picked.upper()


def ornament_name(auftrag_id: int | None) -> str:
    if auftrag_id is None:
        return "kerze"
    return ORNAMENTE[auftrag_id % len(ORNAMENTE)]


def siegel_kontur(beitrag_id: int | None) -> int:
    return 1 + (beitrag_id or 0) % SIEGEL_KONTUREN
