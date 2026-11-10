"""ZIP-Antwort aus gespeicherten Aufnahmen (Admin-Download, Jahresrueckblick)."""

from __future__ import annotations

import logging
import re
import tempfile
import zipfile
from collections import Counter

from fastapi.responses import StreamingResponse

from app.models import Beitrag
from app.storage import ObjectMissing

logger = logging.getLogger(__name__)

# Pfadtrenner und was Windows in Dateinamen nicht erlaubt.
_UNSAFE_IN_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def clean_name_part(part: str) -> str:
    return " ".join(_UNSAFE_IN_NAME.sub(" ", part).split())


def zip_response(
    storage,
    entries: list[tuple[str, Beitrag]],
    filename: str,
    missing_heading: str,
) -> StreamingResponse:
    """ZIP der Dateien unveraendert aus dem Speicher. MP3 ist schon
    komprimiert, deshalb ZIP_STORED; die Datei wandert ab 10 MB aus dem
    Arbeitsspeicher auf die Platte."""
    duplicates = {n for n, count in Counter(name for name, _ in entries).items() if count > 1}

    spool = tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024)
    missing = []
    with zipfile.ZipFile(spool, "w", zipfile.ZIP_STORED) as archive:
        for name, beitrag in entries:
            if name in duplicates:
                name = f"{name} - {beitrag.id}"
            try:
                data = storage.read(beitrag.audio_object_key).data
            except ObjectMissing:
                logger.warning("Download: Datei fehlt im Speicher (Beitrag %s)", beitrag.id)
                missing.append(f"{name}.mp3")
                continue
            archive.writestr(f"{name}.mp3", data)
        if missing:
            archive.writestr("FEHLT.txt", missing_heading + "\n" + "\n".join(missing) + "\n")
    spool.seek(0)

    def chunks():
        with spool:
            while block := spool.read(1024 * 1024):
                yield block

    return StreamingResponse(
        chunks(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
