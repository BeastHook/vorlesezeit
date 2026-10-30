"""Rahmen-Audio: Vorspann und Abspann um die Geschichte (Advent U4, KTD2, KTD3).

Die mitgelieferte Melodie wird zur Laufzeit aus einer Notenliste synthetisiert
und einmal im Speicher abgelegt -- Audiodateien kommen nie ins Repo (KTD3).
Das Zusammenfuegen baut je Aufruf eine neue Datei und speichert sie nicht
(KTD2). Alle Teile sind schon normalisiert: die Geschichte beim Einreichen,
die Melodie beim Erzeugen, eigene Dateien beim Hochladen.
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.orm import Session

from app.delivery.audio import FFMPEG_TIMEOUT_SECONDS, probe_duration_seconds
from app.models import Campaign
from app.recording.normalize import (
    FFMPEG_PROTOCOL_WHITELIST,
    EmptyRecordingError,
    normalize_recording,
)
from app.settings import get_einstellungen
from app.storage import ObjectMissing, ObjectStorage

# Versioniert: eine neue Melodie bekommt einen neuen Schluessel, alte
# Speicherstaende stoeren dann nicht.
MELODIE_KEY = "rahmen/melodie-v1.mp3"
PAUSE_SEKUNDEN = 1.0

_ACHTEL = 0.3
_EINBLENDEN = 0.02
_AUSBLENDEN = 0.08

# Anfang von "Leise rieselt der Schnee" (Eduard Ebel, 1895, gemeinfrei),
# G-Dur im 6/8-Takt: "Leise rieselt der Schnee, still und starr ruht der See".
# (Frequenz in Hz, Dauer in Achteln)
_NOTEN = [
    (493.88, 2),  # H4 Lei-
    (440.00, 1),  # A4 se
    (493.88, 2),  # H4 rie-
    (440.00, 1),  # A4 selt
    (392.00, 1),  # G4 der
    (392.00, 5),  # G4 Schnee
    (293.66, 2),  # D4 still
    (329.63, 1),  # E4 und
    (392.00, 2),  # G4 starr
    (369.99, 1),  # Fis4 ruht
    (329.63, 1),  # E4 der
    (293.66, 5),  # D4 See
]

# Ein Prozess erzeugt die Melodie nur einmal, auch bei gleichzeitigen Laeufen.
_MELODIE_LOCK = threading.Lock()


class RahmenFehler(Exception):
    """Der Rahmen liess sich nicht bauen; die Geschichte selbst ist davon
    nicht betroffen."""


def melodie(storage: ObjectStorage) -> bytes:
    """Liest die Melodie aus dem Speicher; fehlt sie, wird sie erzeugt und
    abgelegt."""
    with _MELODIE_LOCK:
        try:
            return storage.read(MELODIE_KEY).data
        except ObjectMissing:
            pass
        data = _melodie_erzeugen()
        storage.put(MELODIE_KEY, data, "audio/mpeg")
        return data


def mit_rahmen(
    session: Session, storage: ObjectStorage, campaign: Campaign, geschichte: bytes
) -> bytes | None:
    """Geschichte mit Vorspann und Abspann des Kalenders, oder None, wenn der
    Kalender keinen Rahmen hat. Jeder Fehler wird zu RahmenFehler."""
    with _als_rahmen_fehler():
        vorspann, abspann = _vorspann_abspann(session, storage, campaign)
        if vorspann is None and abspann is None:
            return None
        return _zusammenfuegen([t for t in (vorspann, geschichte, abspann) if t is not None])


def rahmen_sekunden(session: Session, storage: ObjectStorage, campaign: Campaign) -> float:
    """Was der Rahmen des Kalenders an Laenge zu jeder Geschichte hinzufuegt
    (Teile plus je eine Pause, U5/R10); 0.0 ohne Rahmen. Jeder Fehler wird zu
    RahmenFehler."""
    with _als_rahmen_fehler():
        teile = [t for t in _vorspann_abspann(session, storage, campaign) if t is not None]
        return sum(probe_duration_seconds(t) + PAUSE_SEKUNDEN for t in teile)


def _vorspann_abspann(
    session: Session, storage: ObjectStorage, campaign: Campaign
) -> tuple[bytes | None, bytes | None]:
    """Die Teile des Rahmens; beide None, wenn der Kalender keinen hat."""
    if get_einstellungen(session).rahmen_aus:
        return None, None
    if campaign.vorspann_aus:
        vorspann = None
    elif campaign.vorspann_key:
        vorspann = storage.read(campaign.vorspann_key).data
    else:
        vorspann = melodie(storage)
    abspann = storage.read(campaign.abspann_key).data if campaign.abspann_key else None
    return vorspann, abspann


@contextmanager
def _als_rahmen_fehler() -> Iterator[None]:
    try:
        yield
    except RahmenFehler:
        raise
    except Exception as exc:
        raise RahmenFehler("Der Rahmen liess sich nicht bauen.") from exc


def _melodie_erzeugen() -> bytes:
    """Sinustoene mit weicher Huellkurve (gegen Knackser), aneinandergereiht
    und auf das Zielprofil normalisiert."""
    filter_teile = []
    for i, (frequenz, achtel) in enumerate(_NOTEN):
        dauer = achtel * _ACHTEL
        filter_teile.append(
            f"sine=frequency={frequenz}:duration={dauer}:sample_rate=44100,"
            f"afade=t=in:d={_EINBLENDEN},"
            f"afade=t=out:st={dauer - _AUSBLENDEN:.2f}:d={_AUSBLENDEN}[n{i}]"
        )
    labels = "".join(f"[n{i}]" for i in range(len(_NOTEN)))
    filter_teile.append(f"{labels}concat=n={len(_NOTEN)}:v=0:a=1[out]")

    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "melodie.wav"
        try:
            _ffmpeg(["-filter_complex", ";".join(filter_teile), "-map", "[out]", str(out_path)])
            return normalize_recording(out_path.read_bytes())
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            EmptyRecordingError,
        ) as exc:
            raise RahmenFehler("Die Melodie liess sich nicht erzeugen.") from exc


def _zusammenfuegen(teile: list[bytes]) -> bytes:
    """concat-Filter mit Neukodierung auf das Normalisierungsprofil; die
    Pausen entstehen im Filtergraphen (KTD2)."""
    profil = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=mono"
    with tempfile.TemporaryDirectory() as tmp:
        args = []
        filter_teile = []
        labels = []
        for i, data in enumerate(teile):
            path = Path(tmp) / f"teil{i}.mp3"
            path.write_bytes(data)
            args += ["-protocol_whitelist", FFMPEG_PROTOCOL_WHITELIST, "-i", str(path)]
            filter_teile.append(f"[{i}:a]{profil}[a{i}]")
            if i > 0:
                filter_teile.append(
                    f"anullsrc=r=44100:cl=mono,atrim=duration={PAUSE_SEKUNDEN},{profil}[p{i}]"
                )
                labels.append(f"[p{i}]")
            labels.append(f"[a{i}]")
        filter_teile.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[out]")

        out_path = Path(tmp) / "rahmen.mp3"
        args += [
            "-filter_complex",
            ";".join(filter_teile),
            "-map",
            "[out]",
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
        ]
        _ffmpeg(args)
        return out_path.read_bytes()


def _ffmpeg(args: list[str]) -> None:
    subprocess.run(
        ["ffmpeg", "-y", *args],
        check=True,
        capture_output=True,
        timeout=FFMPEG_TIMEOUT_SECONDS,
    )
