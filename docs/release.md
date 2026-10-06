# Release

1. `version` in `pyproject.toml` erhöhen (SemVer), committen.
2. `git tag v<version> && git push origin main v<version>`.
3. Der Workflow „Release" testet und legt `ghcr.io/<owner>/vorlesezeit:<version>`
   für amd64 und arm64 ab. Schlägt er fehl, Tag löschen, beheben, neu taggen.
4. Release auf GitHub anlegen (`gh release create v<version>`). **Erste Zeile
   der Notizen:** `dringend: ja` oder `dringend: nein`. Vom 1. bis 25.12.
   bieten Freunde-Instanzen nur `dringend: ja` als Update an.
5. Kein `latest`: Freunde ziehen nur Versionen, die ihr Wartungsassistent nennt.
