"""Zuschnitt einer bereits normalisierten Aufnahme vor dem Ausliefern (U5, R41).

Der Zuschnitt ist eine Angabe am Beitrag, keine Aenderung an der abgelegten
Datei (KTD5) -- diese Funktion arbeitet auf Bytes im Speicher, nie auf der
im Objektspeicher liegenden Originaldatei. Kein automatisches Stille-
Entfernen (bleibt verboten), nur die vom Admin gesetzten Start-/Endmarken.
ffmpeg ist beim App-Start bereits geprueft (app/__init__.py::check_ffmpeg).
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

# U11: dieselbe Zeitgrenze wie die Normalisierung (app/recording/normalize.py).
FFMPEG_TIMEOUT_SECONDS = 120


def apply_cut(data: bytes, *, start_seconds: float | None, end_seconds: float | None) -> bytes:
    """Schneidet `data` auf [start_seconds, end_seconds] zu. Ohne Marken
    unveraendert (Plan, U5-Approach Punkt 12)."""
    if start_seconds is None and end_seconds is None:
        return data

    with tempfile.TemporaryDirectory() as tmp:
        in_path = Path(tmp) / "in.mp3"
        out_path = Path(tmp) / "out.mp3"
        in_path.write_bytes(data)

        args = ["ffmpeg", "-y"]
        if start_seconds is not None:
            args += ["-ss", str(start_seconds)]
        args += ["-i", str(in_path)]
        if end_seconds is not None:
            duration = end_seconds - (start_seconds or 0.0)
            args += ["-t", str(duration)]
        args += ["-c:a", "copy", str(out_path)]

        subprocess.run(args, check=True, capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS)
        return out_path.read_bytes()


def probe_duration_seconds(data: bytes) -> float:
    """Liest die Laenge einer Audiodatei aus den Bytes (ffprobe) -- die
    erwartete Laenge fuer die spaetere Pruefung leitet sich aus dem
    Ausschnitt ab, nicht aus der abgelegten Datei (Plan, U5-Approach Punkt 12)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.mp3"
        path.write_bytes(data)
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
        return float(result.stdout.strip())
