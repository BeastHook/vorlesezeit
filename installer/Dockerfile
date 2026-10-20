# Installationsassistent (One-Klick-Setup, Baustein 3). Bau aus der Repo-Wurzel:
#   docker build -f installer/Dockerfile -t vorlesezeit-installer .
# Fremd-Images nur mit festem Digest des Multi-Arch-Index (amd64 und arm64),
# nie "latest". Digest ermitteln mit
#   docker buildx imagetools inspect <image:tag> --format '{{json .Manifest.Digest}}'
# Die docker-Kommandozeile und das Compose-Plugin kommen aus dem offiziellen
# docker:cli-Image (statische Binärdateien, laufen auch auf Debian).
FROM docker:28-cli@sha256:625d9431a9f54c5a2bc90f24f0e1c3d55b1349fd857dd85035f98c2c9acbdd4d AS docker-cli

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

WORKDIR /srv/vorlesezeit

COPY pyproject.toml ./
COPY app ./app
COPY installer ./installer
# Die Compose-Vorlage, die der Assistent nach /install/compose.yml kopiert.
COPY deploy/freunde/compose.yml ./deploy/freunde/compose.yml

RUN pip install --no-cache-dir .

EXPOSE 8000

# Ohne Zugriffsprotokoll: die erste Adresse trägt das Einmal-Token (?t=...).
# 0.0.0.0 nur im Container; die Startdatei veröffentlicht den Port nur auf
# 127.0.0.1 des Geräts.
CMD ["uvicorn", "installer:create_installer", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
