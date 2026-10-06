"""Zeitplan-Container (U14, KTD14): ruft die Auslösung im Takt auf, sonst nichts.

Alle fünf Minuten zwischen 17:00 und 01:45 Europe/Berlin ein POST auf
/delivery/trigger im internen Compose-Netz. Ob etwas fällig ist, entscheidet
die App (U10, KTD13) -- dieses Skript entscheidet nichts. Ist die App gerade
nicht erreichbar, wird das protokolliert und der nächste Takt abgewartet.

`--once` ruft sofort einmal auf, unabhängig vom Fenster (Rauchtest), und
endet mit Exit-Code 0 bei 2xx, sonst 1.
"""

from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from datetime import time as clock
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
WINDOW_START = clock(17, 0)
WINDOW_END = clock(1, 45)
INTERVAL = timedelta(minutes=5)
TIMEOUT_SECONDS = 30


def in_window(now: datetime) -> bool:
    return now.time() >= WINDOW_START or now.time() <= WINDOW_END


def next_tick(now: datetime) -> datetime:
    start = now.replace(second=0, microsecond=0)
    start -= timedelta(minutes=start.minute % 5)
    return start + INTERVAL


def log(message: str) -> None:
    print(f"{datetime.now(BERLIN):%Y-%m-%d %H:%M:%S %Z} zeitplan: {message}", flush=True)


def call(url: str, secret: str) -> int | None:
    request = urllib.request.Request(
        url,
        method="POST",
        headers={"X-Trigger-Secret": secret, "X-Trigger-Source": "zeitplan"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except OSError as exc:
        log(f"App nicht erreichbar: {exc}")
        return None
    log(f"Auslösung aufgerufen, Status {status}")
    return status


def main() -> int:
    url = os.environ.get("TRIGGER_URL", "http://app:8000/delivery/trigger")
    secret = os.environ["TRIGGER_SECRET"]

    if "--once" in sys.argv[1:]:
        status = call(url, secret)
        return 0 if status is not None and 200 <= status < 300 else 1

    log("gestartet, Fenster 17:00-01:45 Europe/Berlin, Takt 5 min")
    while True:
        tick = next_tick(datetime.now(BERLIN))
        time.sleep(max(0.0, (tick - datetime.now(BERLIN)).total_seconds()))
        if in_window(tick):
            call(url, secret)


if __name__ == "__main__":
    sys.exit(main())
