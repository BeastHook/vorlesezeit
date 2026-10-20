# Release

1. `version` in `pyproject.toml` erhöhen (SemVer), dieselbe Version in den
   drei Startdateien unter `installer/start/` (`VERSION=`) und im Einzeiler
   (`/v<version>/` in `install.sh` und `docs/betrieb.md`), committen.
   `tests/test_installer_start.py` prüft den Gleichstand.
2. `git tag v<version> && git push origin main v<version>`.
3. Der Workflow „Release" testet und legt zwei Images für amd64 und arm64 ab:
   `ghcr.io/<owner>/vorlesezeit:<version>` (die App) und
   `ghcr.io/<owner>/vorlesezeit-installer:<version>` (der Installationsassistent,
   gebaut aus `installer/Dockerfile`). Schlägt er fehl, Tag löschen, beheben,
   neu taggen. Beide Pakete müssen auf ghcr.io öffentlich sein.
4. Release auf GitHub anlegen (`gh release create v<version>`). **Erste Zeile
   der Notizen:** `dringend: ja` oder `dringend: nein`. Vom 1. bis 25.12.
   bieten Freunde-Instanzen nur `dringend: ja` als Update an.
5. Kein `latest`: Freunde ziehen nur Versionen, die ihr Wartungsassistent nennt.
