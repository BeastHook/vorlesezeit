FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/vorlesezeit

COPY pyproject.toml ./
COPY app ./app
# U6-Generalprobe: scripts/rehearsal.py laeuft im Container (siehe
# `docker compose exec app python -m scripts.rehearsal ...`), tests/fixtures
# liefert dafuer die synthetischen Audio-Fixtures. Kein voller tests/-Kopie,
# damit die restliche Testsuite nicht ins Produktionsimage wandert.
COPY scripts ./scripts
COPY tests/fixtures ./tests/fixtures
# U14: Zeitplan und Sicherung laufen als eigene Dienste aus diesem Image
# (docker-compose.heimserver.yml), damit kein weiteres Image noetig ist.
COPY deploy ./deploy

RUN pip install --no-cache-dir .

EXPOSE 8000

# --forwarded-allow-ips='*': die App liegt immer hinter einem TLS-
# terminierenden Vorbau (lokal ein Tunnel, spaeter der echte Reverse-Proxy
# aus KTD11) und ist nie direkt aus dem Internet erreichbar. Ohne das
# vertraut Uvicorn den X-Forwarded-Proto-Header nur von 127.0.0.1 (Default)
# -- der unmittelbare Peer ist hier aber die Docker-Host-Gateway-IP, nicht
# localhost, wodurch request.url_for() faelschlich auf http statt https
# faellt (Review-Fund: dadurch blockiert der Browser den Aufnahme-Upload
# als Mixed Content, obwohl die Seite selbst ueber https laeuft).
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
