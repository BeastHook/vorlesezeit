"""U7: serverseitige Normalisierung (KTD5, KTD7).

Testet echte ffmpeg-Verarbeitung gegen zur Laufzeit erzeugte Fixtures --
kein Mock, weil die Normalisierung genau das ffmpeg-Verhalten beweisen muss,
das spaeter live gegen die Toniecloud laeuft (Plan, U7-Execution-note).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.recording.normalize import EmptyRecordingError, normalize_recording
from tests.fixtures.audio import (
    make_silence_mp3,
    make_tone_mp3_with_cover_art,
    make_tone_mp4_aac,
    make_tone_webm,
)


def _ffprobe_streams(data: bytes) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.mp3"
        path.write_bytes(data)
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)["streams"]


def _ffprobe_format_tags(data: bytes) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.mp3"
        path.write_bytes(data)
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-of", "json", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)["format"].get("tags", {})


def test_normalizes_webm_opus_to_target_profile():
    raw = make_tone_webm(seconds=1.5)
    out = normalize_recording(raw)
    streams = _ffprobe_streams(out)
    assert len(streams) == 1
    audio = streams[0]
    assert audio["codec_name"] == "mp3"
    assert audio["sample_rate"] == "44100"
    assert audio["channels"] == 1


def test_normalizes_mp4_aac_to_target_profile():
    raw = make_tone_mp4_aac(seconds=1.5)
    out = normalize_recording(raw)
    streams = _ffprobe_streams(out)
    assert len(streams) == 1
    audio = streams[0]
    assert audio["codec_name"] == "mp3"
    assert audio["sample_rate"] == "44100"
    assert audio["channels"] == 1


def test_strips_embedded_cover_art_to_audio_only():
    raw = make_tone_mp3_with_cover_art(seconds=1.5)
    out = normalize_recording(raw)
    streams = _ffprobe_streams(out)
    assert len(streams) == 1
    assert streams[0]["codec_type"] == "audio"


def test_strips_device_metadata():
    raw = make_tone_mp3_with_cover_art(seconds=1.5)
    out = normalize_recording(raw)
    tags = _ffprobe_format_tags(out)
    joined = json.dumps(tags)
    assert "Testgeraet" not in joined


def test_rejects_zero_second_recording():
    # Review-Fund: seconds=0.0 liesse die Fixture selbst schon b"" liefern
    # (kurzschliesst vor ffmpeg) und war damit deckungsgleich mit
    # test_rejects_empty_bytes -- eine winzige positive Dauer durchlaeuft
    # stattdessen echt den Messdurchlauf und rundet dort auf "00:00:00.00",
    # was tatsaechlich den duration<=0-Zweig in normalize_recording ausloest.
    raw = make_silence_mp3(seconds=0.001)
    with pytest.raises(EmptyRecordingError):
        normalize_recording(raw)


def test_rejects_empty_bytes():
    with pytest.raises(EmptyRecordingError):
        normalize_recording(b"")
