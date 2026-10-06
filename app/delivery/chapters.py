"""Bestand auf dem Tonie (U16, KTD20): welche Kapitel der App gehoeren, was
Bestand der Familie ist, wie viel Platz bleibt und ob der Bestand einen Lauf
unversehrt ueberstanden hat. Rein -- kein Netzwerk, keine Datenbank."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from app.delivery.audio import probe_duration_seconds
from app.toniecloud.models import Chapter, ConfigLimits, CreativeTonieState


@dataclass(frozen=True)
class AppChapter:
    id: str
    seconds: float | None


def load_app_chapters(raw: str | None) -> list[AppChapter]:
    if not raw:
        return []
    return [AppChapter(entry["id"], entry.get("seconds")) for entry in json.loads(raw)]


def dump_app_chapters(chapters: Sequence[AppChapter]) -> str | None:
    if not chapters:
        return None
    return json.dumps([{"id": c.id, "seconds": c.seconds} for c in chapters])


def stock_of(state: CreativeTonieState, app: Sequence[AppChapter]) -> list[Chapter]:
    """Bestand = alles, was nicht nachweislich von der App stammt (R49)."""
    app_ids = {c.id for c in app}
    return [c for c in state.chapters if c.id not in app_ids]


@dataclass(frozen=True)
class Space:
    stock_count: int
    stock_seconds: float
    max_chapters: int
    max_seconds: float

    @property
    def free_seconds(self) -> float:
        return max(self.max_seconds - self.stock_seconds, 0.0)

    @property
    def free_chapters(self) -> int:
        return max(self.max_chapters - self.stock_count, 0)


def space_for(state: CreativeTonieState, app: Sequence[AppChapter], limits: ConfigLimits) -> Space:
    """Der Dienst liefert nur die Gesamtdauer. Abgezogen wird die bekannte
    Dauer der App-Kapitel, die noch auf dem Tonie liegen; eine unbekannte
    Dauer bleibt im Bestand -- lieber zu wenig Platz melden als zu viel."""
    present = {c.id for c in state.chapters}
    app_seconds = sum(c.seconds or 0.0 for c in app if c.id in present)
    return Space(
        stock_count=len(stock_of(state, app)),
        stock_seconds=max(state.seconds_present - app_seconds, 0.0),
        max_chapters=limits.max_chapters,
        max_seconds=limits.max_seconds,
    )


def format_minutes(seconds: float) -> str:
    return f"{seconds / 60:.1f}".replace(".", ",") + " Min."


def missing_space(space: Space, new_seconds: Sequence[float]) -> str | None:
    """R50: None, wenn die neuen Kapitel neben den Bestand passen."""
    needed = sum(new_seconds)
    if needed <= space.free_seconds and len(new_seconds) <= space.free_chapters:
        return None
    return (
        f"Kein Platz auf dem Tonie: Bestand {space.stock_count} Kapitel, "
        f"{format_minutes(space.stock_seconds)}; frei {format_minutes(space.free_seconds)} "
        f"und {space.free_chapters} Kapitel, gebraucht {format_minutes(needed)} "
        f"und {len(new_seconds)} Kapitel."
    )


def stock_loss(stock: Sequence[Chapter], final: CreativeTonieState, new_count: int) -> str | None:
    """R49: der Bestand muss direkt hinter den neuen Kapiteln stehen,
    vollstaendig und in derselben Reihenfolge."""
    expected = [c.id for c in stock]
    actual = [c.id for c in final.chapters[new_count:]]
    if actual == expected:
        return None
    return f"Bestand nicht vollständig erhalten: erwartet {expected}, erhalten {actual}"


def beitrag_seconds(audio: bytes, cut_start: float | None, cut_end: float | None) -> float:
    """Laenge nach Zuschnitt (R41), aus der Gesamtlaenge gerechnet statt neu
    kodiert -- fuer die Platzanzeige ueber viele Beitraege."""
    full = probe_duration_seconds(audio)
    end = min(cut_end, full) if cut_end is not None else full
    start = cut_start or 0.0
    return max(end - start, 0.0)
