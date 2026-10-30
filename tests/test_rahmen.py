"""Advent-Anreicherung U4: Rahmen-Audio (KTD2, KTD3).

Echtes ffmpeg, kein Mock (Plan, U4-Execution-note); Audio nur aus
tests/fixtures/audio.py. Der Speicher ist ein Ersatz im Speicher, damit die
Tests ohne S3 laufen.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.delivery import rahmen
from app.delivery.audio import probe_duration_seconds
from app.delivery.rahmen import MELODIE_KEY, PAUSE_SEKUNDEN, RahmenFehler, melodie, mit_rahmen
from app.models import Campaign
from app.recording.normalize import normalize_recording
from app.settings import get_einstellungen
from app.storage import ObjectMissing, StoredObject
from tests.fixtures.audio import make_tone_mp3, make_wrong_format_mp3

TOLERANZ = 0.3


class FakeStorage:
    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = dict(objects or {})
        self.reads: list[str] = []
        self.puts: list[str] = []
        self._lock = threading.Lock()

    def read(self, key: str) -> StoredObject:
        with self._lock:
            self.reads.append(key)
        if key not in self.objects:
            raise ObjectMissing(key)
        data = self.objects[key]
        return StoredObject(
            data=data, total=len(data), content_type="audio/mpeg", content_range=None
        )

    def put(self, key: str, data: bytes, content_type: str) -> None:
        with self._lock:
            self.puts.append(key)
            self.objects[key] = data


@pytest.fixture(scope="module")
def geschichte() -> bytes:
    return normalize_recording(make_tone_mp3(3.0, 440))


@pytest.fixture(scope="module")
def eigene_datei() -> bytes:
    return normalize_recording(make_tone_mp3(2.0, 660))


@pytest.fixture(scope="module")
def melodie_bytes() -> bytes:
    return melodie(FakeStorage())


def _campaign(db: Session, **kw) -> Campaign:
    campaign = Campaign(name="Familie", **kw)
    db.add(campaign)
    db.commit()
    return campaign


def _stream(data: bytes) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.mp3"
        path.write_bytes(data)
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
    streams = json.loads(result.stdout)["streams"]
    assert len(streams) == 1
    return streams[0]


def _integrierte_lautheit(data: bytes) -> float:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.mp3"
        path.write_bytes(data)
        result = subprocess.run(
            ["ffmpeg", "-i", str(path), "-af", "loudnorm=print_format=json", "-f", "null", "-"],
            check=True,
            capture_output=True,
            text=True,
        )
    stderr = result.stderr
    return float(json.loads(stderr[stderr.rfind("{") : stderr.rfind("}") + 1])["input_i"])


def _assert_mp3_profil(data: bytes) -> None:
    stream = _stream(data)
    assert stream["codec_name"] == "mp3"
    assert stream["channels"] == 1
    assert stream["sample_rate"] == "44100"


def test_melodie_is_mono_mp3_44100(melodie_bytes):
    _assert_mp3_profil(melodie_bytes)


def test_melodie_lasts_between_4_and_10_seconds(melodie_bytes):
    assert 4.0 <= probe_duration_seconds(melodie_bytes) <= 10.0


def test_melodie_is_stored_under_versioned_key_and_read_back():
    storage = FakeStorage()
    erste = melodie(storage)
    assert storage.puts == [MELODIE_KEY]
    assert MELODIE_KEY == "rahmen/melodie-v1.mp3"

    zweite = melodie(storage)

    assert storage.puts == [MELODIE_KEY]
    assert zweite == erste


def test_concurrent_melodie_calls_create_it_once():
    storage = FakeStorage()
    ergebnisse: list[bytes] = []
    threads = [
        threading.Thread(target=lambda: ergebnisse.append(melodie(storage))) for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert storage.puts == [MELODIE_KEY]
    assert len(ergebnisse) == 2
    assert ergebnisse[0] == ergebnisse[1]


def test_melodie_and_own_abspann_frame_the_story(
    db_session, geschichte, eigene_datei, melodie_bytes
):
    storage = FakeStorage({MELODIE_KEY: melodie_bytes, "rahmen/abspann.mp3": eigene_datei})
    campaign = _campaign(db_session, abspann_key="rahmen/abspann.mp3")

    ergebnis = mit_rahmen(db_session, storage, campaign, geschichte)

    erwartet = (
        probe_duration_seconds(melodie_bytes)
        + PAUSE_SEKUNDEN
        + probe_duration_seconds(geschichte)
        + PAUSE_SEKUNDEN
        + probe_duration_seconds(eigene_datei)
    )
    assert PAUSE_SEKUNDEN == 1.0
    assert abs(probe_duration_seconds(ergebnis) - erwartet) <= TOLERANZ
    _assert_mp3_profil(ergebnis)
    assert storage.puts == []


def test_framed_result_has_target_loudness(db_session, geschichte, eigene_datei, melodie_bytes):
    storage = FakeStorage({MELODIE_KEY: melodie_bytes, "rahmen/abspann.mp3": eigene_datei})
    campaign = _campaign(db_session, abspann_key="rahmen/abspann.mp3")

    ergebnis = mit_rahmen(db_session, storage, campaign, geschichte)

    assert -18.0 <= _integrierte_lautheit(ergebnis) <= -14.0


def test_result_carries_no_metadata(db_session, geschichte, melodie_bytes):
    storage = FakeStorage({MELODIE_KEY: melodie_bytes})
    campaign = _campaign(db_session)

    ergebnis = mit_rahmen(db_session, storage, campaign, geschichte)

    assert not ergebnis.startswith(b"ID3")
    assert b"TAG" != ergebnis[-128:-125]


def test_vorspann_aus_without_abspann_gives_no_frame(db_session, geschichte):
    storage = FakeStorage()
    campaign = _campaign(db_session, vorspann_aus=True)

    assert mit_rahmen(db_session, storage, campaign, geschichte) is None
    assert storage.reads == []


def test_rahmen_aus_gives_no_frame_even_with_own_files(db_session, geschichte, eigene_datei):
    storage = FakeStorage({"rahmen/vorspann.mp3": eigene_datei, "rahmen/abspann.mp3": eigene_datei})
    campaign = _campaign(
        db_session, vorspann_key="rahmen/vorspann.mp3", abspann_key="rahmen/abspann.mp3"
    )
    get_einstellungen(db_session).rahmen_aus = True
    db_session.commit()

    assert mit_rahmen(db_session, storage, campaign, geschichte) is None
    assert storage.reads == []


def test_only_vorspann_gives_vorspann_pause_story(db_session, geschichte, eigene_datei):
    storage = FakeStorage({"rahmen/vorspann.mp3": eigene_datei})
    campaign = _campaign(db_session, vorspann_key="rahmen/vorspann.mp3")

    ergebnis = mit_rahmen(db_session, storage, campaign, geschichte)

    erwartet = (
        probe_duration_seconds(eigene_datei) + PAUSE_SEKUNDEN + probe_duration_seconds(geschichte)
    )
    assert abs(probe_duration_seconds(ergebnis) - erwartet) <= TOLERANZ


def test_own_vorspann_skips_melodie(db_session, geschichte, eigene_datei):
    storage = FakeStorage({"rahmen/vorspann.mp3": eigene_datei})
    campaign = _campaign(db_session, vorspann_key="rahmen/vorspann.mp3")

    mit_rahmen(db_session, storage, campaign, geschichte)

    assert MELODIE_KEY not in storage.reads
    assert storage.puts == []


def test_broken_vorspann_raises_rahmen_fehler(db_session, geschichte):
    storage = FakeStorage({"rahmen/vorspann.mp3": make_wrong_format_mp3()})
    campaign = _campaign(db_session, vorspann_key="rahmen/vorspann.mp3")

    with pytest.raises(RahmenFehler):
        mit_rahmen(db_session, storage, campaign, geschichte)


def test_missing_vorspann_file_raises_rahmen_fehler(db_session, geschichte):
    storage = FakeStorage()
    campaign = _campaign(db_session, vorspann_key="rahmen/fehlt.mp3")

    with pytest.raises(RahmenFehler):
        mit_rahmen(db_session, storage, campaign, geschichte)


def test_storage_error_raises_rahmen_fehler(db_session, geschichte):
    class KaputterSpeicher(FakeStorage):
        def read(self, key: str) -> StoredObject:
            raise RuntimeError("Speicher nicht erreichbar")

    campaign = _campaign(db_session)

    with pytest.raises(RahmenFehler):
        mit_rahmen(db_session, KaputterSpeicher(), campaign, geschichte)


def test_failed_melodie_synthesis_raises_rahmen_fehler(monkeypatch):
    def kaputt(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "ffmpeg")

    monkeypatch.setattr(rahmen.subprocess, "run", kaputt)

    with pytest.raises(RahmenFehler):
        melodie(FakeStorage())
