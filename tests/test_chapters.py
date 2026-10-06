"""U16: reine Bestandslogik (KTD20, R49, R50)."""

from __future__ import annotations

from app.delivery.chapters import (
    AppChapter,
    Space,
    beitrag_seconds,
    dump_app_chapters,
    format_minutes,
    load_app_chapters,
    missing_space,
    space_for,
    stock_loss,
    stock_of,
)
from app.toniecloud.models import Chapter, ConfigLimits, CreativeTonieState
from tests.fixtures.audio import make_tone_mp3

LIMITS = ConfigLimits(max_chapters=250, max_seconds=5400, max_bytes=1, accepts=("mp3",))


def state(ids: list[str], seconds: float = 0.0) -> CreativeTonieState:
    return CreativeTonieState(
        id="T",
        household_id="H",
        chapters=tuple(Chapter(title=f"Titel {i}", file=i, id=i) for i in ids),
        transcoding=False,
        chapters_present=len(ids),
        seconds_present=seconds,
        transcoding_errors=(),
        last_update=None,
    )


def test_load_and_dump_round_trip():
    chapters = [AppChapter("a", 120.0), AppChapter("b", None)]
    assert load_app_chapters(dump_app_chapters(chapters)) == chapters
    assert load_app_chapters(None) == []
    assert dump_app_chapters([]) is None


def test_stock_drops_app_chapter_at_any_position_and_keeps_order():
    live = state(["familie-1", "app-alt", "familie-2"])
    stock = stock_of(live, [AppChapter("app-alt", 100.0)])
    assert [c.id for c in stock] == ["familie-1", "familie-2"]


def test_stock_without_app_chapters_is_everything():
    live = state(["familie-1", "familie-2"])
    assert [c.id for c in stock_of(live, [])] == ["familie-1", "familie-2"]


def test_space_subtracts_only_app_chapters_still_present():
    # Review Focus 2: die Familie hat "app-weg" schon selbst geloescht.
    live = state(["app-alt", "familie-1"], seconds=1000.0)
    space = space_for(live, [AppChapter("app-alt", 200.0), AppChapter("app-weg", 300.0)], LIMITS)
    assert space.stock_count == 1
    assert space.stock_seconds == 800.0
    assert space.free_seconds == 4600.0
    assert space.free_chapters == 249


def test_space_with_unknown_app_seconds_counts_them_as_stock():
    # Review Focus 3: nach der Migration ist die Dauer unbekannt -> vorsichtig.
    live = state(["app-alt", "familie-1"], seconds=1000.0)
    space = space_for(live, [AppChapter("app-alt", None)], LIMITS)
    assert space.stock_seconds == 1000.0


def test_missing_space_none_when_it_fits():
    space = Space(stock_count=6, stock_seconds=2400.0, max_chapters=250, max_seconds=5400)
    assert missing_space(space, [300.0]) is None


def test_missing_space_fits_exactly_at_the_limit():
    space = Space(stock_count=249, stock_seconds=5100.0, max_chapters=250, max_seconds=5400)
    assert missing_space(space, [300.0]) is None


def test_missing_space_names_seconds_shortfall():
    space = Space(stock_count=6, stock_seconds=5300.0, max_chapters=250, max_seconds=5400)
    reason = missing_space(space, [300.0])
    assert reason is not None
    assert "Kein Platz" in reason
    assert "6 Kapitel" in reason


def test_missing_space_names_chapter_shortfall():
    space = Space(stock_count=249, stock_seconds=0.0, max_chapters=250, max_seconds=5400)
    assert missing_space(space, [10.0, 10.0]) is not None


def test_format_minutes_uses_german_decimal_comma():
    assert format_minutes(2490.0) == "41,5 Min."
    assert format_minutes(0.0) == "0,0 Min."


def test_stock_loss_none_when_stock_follows_new_chapters():
    stock = list(state(["familie-1", "familie-2"]).chapters)
    final = state(["neu", "familie-1", "familie-2"])
    assert stock_loss(stock, final, new_count=1) is None


def test_stock_loss_detects_missing_chapter():
    stock = list(state(["familie-1", "familie-2"]).chapters)
    final = state(["neu", "familie-2"])
    assert "Bestand" in stock_loss(stock, final, new_count=1)


def test_stock_loss_detects_changed_order():
    stock = list(state(["familie-1", "familie-2"]).chapters)
    final = state(["neu", "familie-2", "familie-1"])
    assert stock_loss(stock, final, new_count=1) is not None


def test_stock_loss_detects_unexpected_extra_chapter_at_end():
    stock = list(state(["familie-1"]).chapters)
    final = state(["neu", "familie-1", "fremd"])
    assert stock_loss(stock, final, new_count=1) is not None


def test_beitrag_seconds_uses_cut_markers():
    audio = make_tone_mp3(4.0)
    assert 3.5 <= beitrag_seconds(audio, None, None) <= 4.5
    assert 1.5 <= beitrag_seconds(audio, 1.0, 3.0) <= 2.5
    assert 2.5 <= beitrag_seconds(audio, 1.0, None) <= 3.5


def test_beitrag_seconds_clamps_cut_end_beyond_audio_length():
    audio = make_tone_mp3(4.0)
    assert 2.5 <= beitrag_seconds(audio, 1.0, 60.0) <= 3.5
