"""Kalenderbezug fuer den Auslieferungsvorgang (U5, R29/KTD10).

Diese App laeuft fuer genau einen Advent (2026, siehe CLAUDE.md) -- deshalb
reicht Monat/Tag ohne Jahresbezug: jeder Dezember-Tag 1-24 ist ein
Adventstag, unabhaengig vom Jahr des uebergebenen Datums.
"""

from __future__ import annotations

from datetime import date, timedelta


def advent_day_for(day: date) -> int | None:
    """Adventstag (1-24) fuer ein Kalenderdatum, oder None ausserhalb des Fensters."""
    if day.month == 12 and 1 <= day.day <= 24:
        return day.day
    return None


def is_probe_evening(evening: date) -> bool:
    """R48: ein Abend im November, an dem noch kein Adventstag Zieltag ist."""
    return evening.month == 11 and advent_day_for(evening + timedelta(days=1)) is None


def is_cleanup_evening(evening: date) -> bool:
    """R51: am Abend des 25.12. raeumt die App ihr Kapitel ab."""
    return evening.month == 12 and evening.day == 25
