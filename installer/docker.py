"""Der einzige Weg des Assistenten zu Docker: die `docker`-Kommandozeile im Image.

Das Python-SDK wäre eine neue Abhängigkeit. Die Ausführfunktion ist
austauschbar, damit Tests einen Docker-Fake einsetzen. Kein Aufruf übergibt
Geheimnisse als Argument: die stehen nur in der .env, die compose selbst liest.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Laden der Images darf dauern (rund 1 GB), alles andere nicht.
LADEN_TIMEOUT_SECONDS = 1800
KURZ_TIMEOUT_SECONDS = 60
SICHERUNGSORDNER = "Vorlesezeit-Sicherung"
_TUNNEL_PROBE = (
    "import urllib.request; urllib.request.urlopen('http://cloudflared:2000/ready', timeout=5)"
)
_BACKUP = ("python", "deploy/backup/backup.py")
_BEISEITE = (
    'for f in "$DATABASE_PATH" "$DATABASE_PATH-wal" "$DATABASE_PATH-shm"; do '
    'if [ -e "$f" ]; then mv "$f" "$f.vor-rueckfall"; fi; done'
)


@dataclass(frozen=True)
class Ergebnis:
    code: int
    ausgabe: str = ""
    fehler: str = ""


Ausfuehren = Callable[[list[str], float | None], Ergebnis]


def ausfuehren(args: list[str], timeout: float | None) -> Ergebnis:
    try:
        r = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return Ergebnis(-1, "", f"Zeitüberschreitung nach {timeout:.0f} s")
    except FileNotFoundError:
        return Ergebnis(-1, "", "docker-Kommandozeile fehlt")
    return Ergebnis(r.returncode, r.stdout, r.stderr)


class Docker:
    def __init__(
        self, install_dir: Path, *, container_id: str, ausfuehren: Ausfuehren = ausfuehren
    ) -> None:
        self.install_dir = install_dir
        self.container_id = container_id
        self._ausfuehren = ausfuehren

    def _compose(self, *args: str, timeout: float | None = KURZ_TIMEOUT_SECONDS) -> Ergebnis:
        basis = ["compose", "--project-directory", str(self.install_dir)]
        datei = ["-f", str(self.install_dir / "compose.yml")]
        return self._ausfuehren([*basis, *datei, *args], timeout)

    def pull(self) -> Ergebnis:
        return self._compose("pull", timeout=LADEN_TIMEOUT_SECONDS)

    def up(self) -> Ergebnis:
        return self._compose("up", "-d", timeout=LADEN_TIMEOUT_SECONDS)

    def app_gesund(self) -> bool:
        r = self._compose("ps", "--format", "json", "app")
        if r.code != 0:
            return False
        # Neuere compose-Versionen schreiben ein Objekt je Zeile, ältere eine Liste.
        for zeile in r.ausgabe.splitlines():
            try:
                daten = json.loads(zeile)
            except ValueError:
                continue
            for dienst in daten if isinstance(daten, list) else [daten]:
                if dienst.get("Health") == "healthy":
                    return True
        return False

    def tunnel_verbunden(self) -> bool:
        """cloudflared meldet `/ready` nur im internen Netz, deshalb aus `app` heraus."""
        return self._compose("exec", "-T", "app", "python", "-c", _TUNNEL_PROBE).code == 0

    def stop(self, *dienste: str) -> Ergebnis:
        return self._compose("stop", *dienste)

    def down(self, *, volumes: bool) -> Ergebnis:
        return self._compose("down", *(["--volumes"] if volumes else []), "--rmi", "all")

    def sichern(self) -> Ergebnis:
        """Sofort-Sicherung im laufenden Dienst backup (Datenbank und Aufnahmen)."""
        return self._compose(
            "exec", "-T", "backup", *_BACKUP, "--once", timeout=LADEN_TIMEOUT_SECONDS
        )

    def db_beiseite(self) -> Ergebnis:
        """Datenbank samt -wal und -shm umbenennen: `restore` braucht eine leere
        Instanz, und so geht nichts verloren."""
        return self._compose("run", "--rm", "--no-deps", "backup", "sh", "-c", _BEISEITE)

    def rueckspielen(self, datei: str) -> Ergebnis:
        return self._compose(
            "run",
            "--rm",
            "--no-deps",
            "backup",
            *_BACKUP,
            "restore",
            datei,
            timeout=LADEN_TIMEOUT_SECONDS,
        )

    def anmeldelink(self) -> Ergebnis:
        return self._compose("exec", "-T", "app", "python", "-m", "app.auth.anmeldelink")

    def logs(self) -> Ergebnis:
        return self._compose("logs", "--since", "168h", "--timestamps", "--no-color")

    def _mounts(self) -> list[tuple[str, str]] | None:
        """(Pfad auf dem Gerät, Pfad im Assistenten-Container) je Mount."""
        r = self._ausfuehren(
            ["inspect", "--format", "{{json .Mounts}}", self.container_id], KURZ_TIMEOUT_SECONDS
        )
        if r.code != 0:
            return None
        return [
            (m["Source"].rstrip("/"), m["Destination"].rstrip("/"))
            for m in json.loads(r.ausgabe or "[]")
        ]

    def host_pfad(self, pfad: str) -> str | None:
        """Pfad auf dem Gerät zu einem Pfad im Assistenten-Container, aus dessen Mounts."""
        for quelle, ziel in self._mounts() or []:
            if pfad == ziel or pfad.startswith(ziel + "/"):
                return quelle + pfad[len(ziel) :]
        return None

    def container_pfad(self, pfad: str) -> str | None:
        """Umkehrung von host_pfad: Pfad im Assistenten-Container zu einem Pfad auf dem Gerät."""
        for quelle, ziel in self._mounts() or []:
            if pfad == quelle or pfad.startswith(quelle + "/"):
                return ziel + pfad[len(quelle) :]
        return None

    def _hilfscontainer(self, image: str, host_laufwerk: str, skript: str) -> Ergebnis:
        # `-v` zerlegt Namen mit `:`. `--mount` liest eine CSV-Zeile: das Feld in
        # `"` hält Kommas, ein `"` darin wird verdoppelt.
        quelle = host_laufwerk.replace('"', '""')
        mount = f'type=bind,"source={quelle}",target=/ziel'
        return self._ausfuehren(
            ["run", "--rm", "--mount", mount, image, "sh", "-c", skript],
            KURZ_TIMEOUT_SECONDS,
        )

    def ordner_anlegen(self, image: str, host_laufwerk: str) -> Ergebnis:
        """Die Laufwerke sind im Assistenten nur lesbar eingebunden: ein kurzer
        Hilfscontainer legt den Ordner an und prüft, ob er beschreibbar ist."""
        ordner = f"/ziel/{SICHERUNGSORDNER}"
        skript = f"mkdir -p {ordner} && touch {ordner}/.probe && rm {ordner}/.probe"
        return self._hilfscontainer(image, host_laufwerk, skript)

    def ordner_loeschen(self, image: str, host_laufwerk: str) -> Ergebnis:
        return self._hilfscontainer(image, host_laufwerk, f"rm -rf /ziel/{SICHERUNGSORDNER}")
