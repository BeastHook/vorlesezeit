"""U1: Objektspeicher-Roundtrip gegen den bereitgestellten Speicher.

Laeuft gegen echten S3-Speicher (versitygw) (kein Mock) -- vorher starten:

    docker compose up -d s3

Endpunkt/Zugangsdaten entsprechen den Vorgaben in docker-compose.yml und
lassen sich per Umgebungsvariable ueberschreiben.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import fields
from datetime import date
from datetime import time as time_of_day
from pathlib import Path

import pytest

from app.config import Config
from app.storage import ObjectMissing, ObjectStorage, RangeNotSatisfiable

TEST_CONFIG = Config(
    timezone="Europe/Berlin",
    storage_endpoint_url=os.environ.get("STORAGE_ENDPOINT_URL", "http://localhost:9000"),
    storage_access_key=os.environ.get("STORAGE_ACCESS_KEY", "vorlesezeit"),
    storage_secret_key=os.environ.get("STORAGE_SECRET_KEY", "vorlesezeit-dev-secret"),
    storage_bucket=os.environ.get("STORAGE_BUCKET", "vorlesezeit-test"),
    storage_region=os.environ.get("STORAGE_REGION", "us-east-1"),
    database_path=":memory:",
    session_secret_key="test-session-secret",
    admin_email="admin@example.test",
    magic_link_valid_until=date(2026, 11, 30),
    smtp_host="smtp-relay.brevo.com",
    smtp_port=587,
    smtp_user="test-smtp-user",
    smtp_password="test-smtp-pass",
    smtp_from_address="vorlesezeit@example.test",
    tonie_username="test-tonie-user",
    tonie_password="test-tonie-pass",
    tonie_delivery_time=time_of_day(20, 0),
    trigger_secret="test-trigger-secret",
)


@pytest.fixture
def storage() -> ObjectStorage:
    return ObjectStorage(TEST_CONFIG)


def _key(name: str) -> str:
    # Eindeutig: ein zweiter Testlauf kann gleichzeitig gegen denselben Speicher laufen.
    return f"u13-proof/{uuid.uuid4().hex}-{name}"


def test_put_and_read_roundtrip(storage: ObjectStorage):
    key = _key("roundtrip.txt")
    payload = b"vorlesezeit u1 proof"
    storage.put(key, payload, content_type="text/plain")

    stored = storage.read(key)

    assert stored.data == payload
    assert stored.total == len(payload)
    assert stored.content_type == "text/plain"
    assert stored.content_range is None


def test_read_byte_range_reports_total_size(storage: ObjectStorage):
    """U13/KTD17: Bereichsanfragen, damit iOS Safari spulen kann."""
    key = _key("range.bin")
    payload = bytes(range(256)) * 6  # 1536 Bytes
    storage.put(key, payload, content_type="audio/mpeg")

    first_two = storage.read(key, "bytes=0-1")
    tail = storage.read(key, "bytes=1000-")

    assert first_two.data == payload[:2]
    assert first_two.content_range == f"bytes 0-1/{len(payload)}"
    assert first_two.total == len(payload)
    assert first_two.content_type == "audio/mpeg"
    assert tail.data == payload[1000:]
    assert tail.content_range == f"bytes 1000-{len(payload) - 1}/{len(payload)}"


def test_read_range_beyond_end_is_not_satisfiable(storage: ObjectStorage):
    key = _key("short.bin")
    storage.put(key, b"kurz", content_type="audio/mpeg")

    with pytest.raises(RangeNotSatisfiable):
        storage.read(key, "bytes=1000-")


def test_delete_removes_object(storage: ObjectStorage):
    """U7/R10/R40: eine ersetzte Aufnahme wird automatisch entfernt."""
    key = _key("to-delete.txt")
    storage.put(key, b"temporary", content_type="text/plain")

    storage.delete(key)

    with pytest.raises(ObjectMissing):
        storage.read(key)


def test_no_signed_urls_left_in_app_code():
    """U13/KTD17: der Speicher ist nicht mehr oeffentlich, also gibt es auch
    keine signierten Adressen und keinen oeffentlichen Endpunkt mehr."""
    app_dir = Path(__file__).resolve().parent.parent / "app"
    offenders = [
        str(path)
        for path in app_dir.rglob("*")
        if path.suffix in {".py", ".html", ".js"}
        and any(
            needle in path.read_text(encoding="utf-8")
            for needle in ("presigned", "PUBLIC_ENDPOINT", "public_endpoint", "X-Amz")
        )
    ]
    assert offenders == []
    assert "storage_public_endpoint_url" not in {f.name for f in fields(Config)}
