"""Serverseitige Normalisierung eingehender Aufnahmen (U7, KTD5, KTD7).

Der Recorder liefert Rohformate (webm/Opus, mp4/AAC, ...) ohne verlaessliche
Formatangabe -- ffmpeg erkennt den Container aus dem Inhalt, nie aus dem
gemeldeten Typ (KTD7); der clientseitig gemeldete Typ geht nur ins Log
(app/recording/views.py), nie hierher. Zielprofil: MP3, 128 kbit/s, mono,
44,1 kHz, zweistufige EBU-R128-Lautheitsnormalisierung auf -16 LUFS,
Metadaten und eingebettetes Titelbild entfernt. Keine automatische
Stille-Entfernung (bleibt nach KTD5 verboten) -- nur Lautheit, nie Inhalt,
wird angepasst.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

LOUDNORM_TARGET_I = -16.0
LOUDNORM_TARGET_TP = -1.5
LOUDNORM_TARGET_LRA = 11.0
# Grosszuegig genug fuer eine mehrminuetige Vorlesegeschichte, eng genug,
# dass ein haengender ffmpeg-Prozess (kaputte Eingabe) den Request-Thread
# nicht unbegrenzt blockiert (Review-Fund: fehlendes Timeout).
FFMPEG_TIMEOUT_SECONDS = 120
# Nur lokale Dateien -- verhindert, dass ffmpegs eigene Protokoll-/Demuxer-
# Erkennung eine praeparierte Eingabe (z. B. eine HLS/concat-Playlist) dazu
# bringt, selbst Netzwerk- oder andere Dateipfade nachzuladen (Review-Fund:
# SSRF/LFI ueber ffmpegs Inhalts-Autoprobing, CWE-918).
FFMPEG_PROTOCOL_WHITELIST = "file"


class EmptyRecordingError(ValueError):
    """Eine Nullsekunden- oder unlesbare Aufnahme wird abgelehnt und der
    Person benannt (U7-Requirements)."""


def normalize_recording(data: bytes) -> bytes:
    """Normalisiert eine rohe Aufnahme auf das Zielprofil und gibt die
    fertige MP3-Datei als Bytes zurueck."""
    if not data:
        raise EmptyRecordingError("Die Aufnahme ist leer.")

    with tempfile.TemporaryDirectory() as tmp:
        in_path = Path(tmp) / "in.bin"
        in_path.write_bytes(data)

        try:
            measured, duration = _measure_loudness(in_path)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as exc:
            raise EmptyRecordingError("Die Aufnahme ist leer oder unlesbar.") from exc
        if duration <= 0:
            raise EmptyRecordingError("Die Aufnahme ist leer.")

        try:
            out_path = Path(tmp) / "out.mp3"
            _apply_loudnorm(in_path, out_path, measured)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise EmptyRecordingError("Die Aufnahme laesst sich nicht verarbeiten.") from exc
        return out_path.read_bytes()


_TIME_PATTERN = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")


def _measure_loudness(in_path: Path) -> tuple[dict, float]:
    """Erster Durchlauf (loudnorm-Zweipass-Verfahren): misst nur, schreibt
    nichts. Liest die Gesamtdauer aus dem ffmpeg-Fortschritt statt aus dem
    Containerkopf -- ein per MediaRecorder ueber eine nicht rueckspulbare
    Quelle geschriebenes webm traegt dort haeufig keine Dauer (beobachtet
    an dieser Fixture-Erzeugung selbst: `duration=N/A` trotz gueltigen
    Inhalts), waehrend der Fortschritt beim vollstaendigen Decodieren
    immer bekannt ist."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-protocol_whitelist",
            FFMPEG_PROTOCOL_WHITELIST,
            "-i",
            str(in_path),
            "-map",
            "0:a:0",
            "-af",
            f"loudnorm=I={LOUDNORM_TARGET_I}:TP={LOUDNORM_TARGET_TP}:"
            f"LRA={LOUDNORM_TARGET_LRA}:print_format=json",
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=FFMPEG_TIMEOUT_SECONDS,
    )
    # loudnorm schreibt sein JSON-Messergebnis nach stderr, eingebettet in
    # den uebrigen ffmpeg-Log-Text. Ein ffmpeg-Lauf, der mit Exit-Code 0
    # endet, aber kein JSON schreibt (z.B. eine gueltige, aber leere
    # Tonspur), soll denselben benannten Fehlerzustand ausloesen wie ein
    # gescheiterter Aufruf -- nicht mit einer rohen ValueError/KeyError
    # durchschlagen (Review-Fund).
    stderr = result.stderr
    start = stderr.rfind("{")
    end = stderr.rfind("}") + 1
    if start == -1 or end <= start:
        raise ValueError("ffmpeg hat kein Lautheits-Messergebnis geliefert.")
    measured = json.loads(stderr[start:end])
    for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        if key not in measured:
            raise ValueError(f"ffmpeg-Messergebnis ohne erwartetes Feld {key!r}.")

    duration = 0.0
    matches = _TIME_PATTERN.findall(stderr)
    if matches:
        hours, minutes, seconds = matches[-1]
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return measured, duration


def _apply_loudnorm(in_path: Path, out_path: Path, measured: dict) -> None:
    """Zweiter Durchlauf: wendet die gemessene Lautheit an und erzeugt in
    einem Schritt das Zielprofil -- `-map 0:a:0` nimmt ausschliesslich die
    erste Tonspur mit, ein eingebettetes Titelbild bleibt aussen vor;
    `-map_metadata -1` und ein abgeschaltetes ID3v2/v1 entfernen
    Geraetename und alle uebrigen Metadaten (KTD5)."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-protocol_whitelist",
            FFMPEG_PROTOCOL_WHITELIST,
            "-i",
            str(in_path),
            "-map",
            "0:a:0",
            "-af",
            f"loudnorm=I={LOUDNORM_TARGET_I}:TP={LOUDNORM_TARGET_TP}:"
            f"LRA={LOUDNORM_TARGET_LRA}:"
            f"measured_I={measured['input_i']}:"
            f"measured_TP={measured['input_tp']}:"
            f"measured_LRA={measured['input_lra']}:"
            f"measured_thresh={measured['input_thresh']}:"
            f"offset={measured['target_offset']}:"
            "linear=true:print_format=summary",
            "-ar",
            "44100",
            "-ac",
            "1",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
            "-map_metadata",
            "-1",
            "-id3v2_version",
            "0",
            "-write_id3v1",
            "0",
            str(out_path),
        ],
        check=True,
        capture_output=True,
        timeout=FFMPEG_TIMEOUT_SECONDS,
    )
