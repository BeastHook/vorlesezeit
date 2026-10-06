"""Objektspeicher-Zugriff: ablegen, lesen (auch als Byte-Bereich), loeschen.

Faehrt gegen ein S3-kompatibles API (KTD11 verlangt Objektspeicher, kein
lokales Dateisystem). Der Endpunkt entscheidet, ob das versitygw (lokaler Beweis)
oder ein echter Anbieter ist -- der Code hier kennt den Unterschied nicht.

Der Speicher ist nur fuer die App erreichbar (KTD17): Audio liefert
`audio_response` nach der Berechtigungspruefung der Route aus, mit
Bereichsanfragen, weil iOS Safari ein <audio> ohne sie nicht spult.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError
from fastapi import HTTPException, Response

from app.config import Config

logger = logging.getLogger(__name__)

# Genau ein Bereich; alles andere (mehrere Bereiche, Unsinn) wird wie ohne
# Range-Header beantwortet -- das erlaubt RFC 9110 ausdruecklich.
_SINGLE_RANGE = re.compile(r"bytes=(\d*)-(\d*)")


class ObjectMissing(Exception):
    """Unter dem Schluessel liegt keine Datei."""


class RangeNotSatisfiable(Exception):
    """Der angefragte Bereich beginnt hinter dem Dateiende."""


@dataclass(frozen=True)
class StoredObject:
    data: bytes
    total: int
    content_type: str
    content_range: str | None  # gesetzt, wenn ein Bereich gelesen wurde


def delete_quietly(storage: ObjectStorage, key: str) -> None:
    """Aufraeumen ohne eigenen Fehlerpfad: ein Fehlschlag wird geloggt, nicht
    geworfen (R40 -- der Aufrufer hat bereits einen Ausgang)."""
    try:
        storage.delete(key)
    except Exception:
        logger.exception("Aufnahme konnte nicht geloescht werden: key=%s", key)


def commit_or_discard(db, storage: ObjectStorage, key: str) -> None:
    """Commit nach dem Ablegen von `key`; scheitert er, zeigt keine Zeile auf
    die Datei -- sie wird entfernt und der Fehler weitergereicht (R40)."""
    try:
        db.commit()
    except Exception:
        db.rollback()
        delete_quietly(storage, key)
        raise


class ObjectStorage:
    def __init__(self, config: Config) -> None:
        self._bucket = config.storage_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=config.storage_endpoint_url,
            aws_access_key_id=config.storage_access_key,
            aws_secret_access_key=config.storage_secret_key,
            region_name=config.storage_region,
            config=BotoConfig(signature_version="s3v4"),
        )

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)

    def get(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        return response["Body"].read()

    def read(self, key: str, byte_range: str | None = None) -> StoredObject:
        """Liest die ganze Datei oder einen Bereich im Format des
        Range-Headers ("bytes=0-1", "bytes=1000-", "bytes=-500") und liefert
        Gesamtgroesse und Inhaltstyp mit."""
        params = {"Bucket": self._bucket, "Key": key}
        if byte_range is not None:
            params["Range"] = byte_range
        try:
            response = self._client.get_object(**params)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"NoSuchKey", "404"}:
                raise ObjectMissing(key) from exc
            if code == "InvalidRange":
                raise RangeNotSatisfiable(key) from exc
            raise
        content_range = response.get("ContentRange")
        total = int(content_range.rsplit("/", 1)[1]) if content_range else response["ContentLength"]
        return StoredObject(
            data=response["Body"].read(),
            total=total,
            content_type=response.get("ContentType") or "application/octet-stream",
            content_range=content_range,
        )

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)


def _single_range(header: str | None) -> str | None:
    match = _SINGLE_RANGE.fullmatch(header.strip()) if header else None
    if match is None:
        return None
    first, last = match.groups()
    if not first and not last:
        return None
    if first and last and int(first) > int(last):
        return None
    return match.group(0)


def audio_response(storage: ObjectStorage, key: str, range_header: str | None) -> Response:
    """R28/KTD17: liefert die Datei nach der Pruefung der aufrufenden Route
    aus -- 206 mit Content-Range bei Bereichsanfrage, sonst 200."""
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=300"}
    try:
        stored = storage.read(key, _single_range(range_header))
    except ObjectMissing as exc:
        raise HTTPException(status_code=404) from exc
    except RangeNotSatisfiable:
        return Response(status_code=416, headers=headers)
    if stored.content_range is not None:
        headers["Content-Range"] = stored.content_range
    return Response(
        stored.data,
        status_code=206 if stored.content_range is not None else 200,
        media_type=stored.content_type,
        headers=headers,
    )
