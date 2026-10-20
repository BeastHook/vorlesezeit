"""Anmeldelink fuer den Wartungsassistenten (One-Klick-Setup Baustein 4).

    python -m app.auth.anmeldelink

Wer am Geraet sitzt, gilt als vertrauenswuerdig: der Assistent ruft das per
`docker compose exec -T app` auf. Erzeugt einen Einrichtungslink wie der Start
ohne SMTP (aeltere offene Links entwertet, nur der Hash in der Datenbank, 24 h
gueltig) und gibt nur den Pfad `/einrichtung/<token>` auf stdout aus. Ins Log
schreibt der Aufruf nichts.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from app.auth.einrichtung import issue_link
from app.config import load_config
from app.db import create_db_engine, make_session_factory


def main() -> int:
    engine = create_db_engine(load_config())
    with make_session_factory(engine)() as session:
        token = issue_link(session, now=datetime.now(UTC))
    print(f"/einrichtung/{token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
