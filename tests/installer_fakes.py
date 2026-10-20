"""Gemeinsame Fakes für die Installer-Tests: Docker, Uhr, Gerät."""

from __future__ import annotations

import json

from installer.docker import Docker, Ergebnis
from installer.geraet import Messung, TlsErgebnis

OK = Ergebnis(0, "", "")


def verb(args: list[str]) -> str:
    """Kurzname eines docker-Aufrufs: `compose pull`, `compose up`, `run`, ..."""
    if args[0] == "compose":
        rest = [a for a in args[1:] if not a.startswith("-") and "/" not in a]
        return f"compose {rest[0]}"
    return args[0]


class FakeDocker:
    """Antwortet je Kurzname mit einem festen Ergebnis und merkt sich die Aufrufe."""

    def __init__(self, **antworten: Ergebnis) -> None:
        self.antworten = {
            "compose pull": OK,
            "compose up": OK,
            "compose ps": Ergebnis(0, json.dumps({"Service": "app", "Health": "healthy"}), ""),
            "compose exec": OK,
            "compose stop": OK,
            "compose run": OK,
            "compose logs": OK,
            "compose down": OK,
            "inspect": Ergebnis(
                0, json.dumps([{"Source": "/Volumes", "Destination": "/laufwerke"}]), ""
            ),
            "run": OK,
        }
        self.antworten.update({k.replace("_", " "): v for k, v in antworten.items()})
        self.aufrufe: list[list[str]] = []

    def __call__(self, args: list[str], timeout: float | None) -> Ergebnis:
        self.aufrufe.append(args)
        return self.antworten[verb(args)]

    @property
    def verben(self) -> list[str]:
        return [verb(a) for a in self.aufrufe]


def fake_docker(install_dir, **antworten) -> tuple[Docker, FakeDocker]:
    fake = FakeDocker(**antworten)
    return Docker(install_dir, container_id="abc123", ausfuehren=fake), fake


class FakeUhr:
    """Monotone Uhr, die nur beim Schlafen weiterläuft."""

    def __init__(self) -> None:
        self.jetzt = 1000.0

    def __call__(self) -> float:
        return self.jetzt

    def schlafen(self, sekunden: float) -> None:
        self.jetzt += sekunden


def messung_mac() -> Messung:
    return Messung(maschine="arm64", frei_bytes=182 * 10**9, ram_bytes=16 * 10**9)


def tls_ok() -> TlsErgebnis:
    return TlsErgebnis("ok")


def tls_proxy() -> TlsErgebnis:
    return TlsErgebnis("proxy", aussteller="Zscaler Intermediate Root CA")
