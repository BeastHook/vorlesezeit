"""Synthetische Audio-Fixtures, zur Laufzeit per ffmpeg erzeugt (U6/U7-Vorgabe:
keine Audiodatei einchecken -- .gitignore und der Audio-Hook des Projekts
verhindern das ohnehin). Reines stdlib+ffmpeg-Modul, kein pytest-Import,
damit es auch im App-Container ohne Dev-Dependencies importierbar ist
(scripts/rehearsal.py).
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

WEBM_PLACEHOLDER_FILENAME = "aufnahme.webm"


def make_tone_mp3(seconds: float = 2.0, frequency: int = 440) -> bytes:
    """Kurzer Sinuston als gueltige MP3-Datei -- fuer alles, was echt durch
    die Verarbeitung laufen und verifiziert werden soll."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:duration={seconds}",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
            "-f",
            "mp3",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    )
    return result.stdout


def make_wrong_format_mp3() -> bytes:
    """Gueltige .mp3-Endung, kein gueltiger MP3-Inhalt -- der Fall, den die
    Toniecloud beim echten Kontoabgleich in U4 mit `transcodingErrors:
    wrongFormat` verworfen hat (SKILL.md, "Verifiziert gegen das echte
    Konto"). Lokale Formatpruefung (Endung) akzeptiert das; erst die
    serverseitige Verarbeitung weist es zurueck."""
    return b"not a real mp3 file, just bytes with the right extension\x00\x01\x02"


def make_tone_webm(seconds: float = 2.0, frequency: int = 440) -> bytes:
    """Sinuston als webm/Opus -- das Format, das MediaRecorder in den
    meisten Browsern liefert (U7, KTD7)."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:duration={seconds}",
            "-c:a",
            "libopus",
            "-f",
            "webm",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    )
    return result.stdout


def make_tone_mp4_aac(seconds: float = 2.0, frequency: int = 440) -> bytes:
    """Sinuston als mp4/AAC -- das Format, das Safari/iOS statt webm liefert
    (U7, KTD7)."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:duration={seconds}",
            "-c:a",
            "aac",
            "-f",
            "mp4",
            "-movflags",
            "frag_keyframe+empty_moov",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    )
    return result.stdout


def make_tone_mp3_with_cover_art(seconds: float = 2.0, frequency: int = 440) -> bytes:
    """MP3 mit eingebettetem Titelbild und Geraetename in den Metadaten --
    die Normalisierung muss beides entfernen (U7-Testszenario)."""
    with tempfile.TemporaryDirectory() as tmp:
        cover_path = Path(tmp) / "cover.png"
        out_path = Path(tmp) / "out.mp3"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=red:s=16x16",
                "-frames:v",
                "1",
                str(cover_path),
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:duration={seconds}",
                "-i",
                str(cover_path),
                "-map",
                "0:a",
                "-map",
                "1:v",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "128k",
                "-c:v",
                "mjpeg",
                "-id3v2_version",
                "3",
                "-metadata",
                "encoded_by=Testgeraet XYZ",
                str(out_path),
            ],
            check=True,
            capture_output=True,
        )
        return out_path.read_bytes()


def make_silence_mp3(seconds: float = 0.0) -> bytes:
    """Aufnahme ohne Laenge (Nullsekunden) -- muss abgelehnt werden (U7,
    Requirements-Test)."""
    if seconds <= 0:
        return b""
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=duration={seconds}",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
            "-f",
            "mp3",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    )
    return result.stdout
