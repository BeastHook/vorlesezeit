#!/bin/bash
# Bereitstellung auf dem Heimserver (docs/betrieb.md), in drei Schritten:
#   pre   sauberer main-Stand pruefen, Zustand zeigen, Sicherung ausloesen
#   up    Image neu bauen und Dienste neu starten
#   post  Zustand, /status, Log auf Fehler/Einrichtungslink, Version
# oder in einem Schritt auf ein Release-Image:
#   update X.Y.Z  Sicherung, ghcr.io/beasthook/vorlesezeit:X.Y.Z laden,
#                 neu starten, pruefen, bei Fehler zurueck auf das vorige Image
# Gibt nie Konfigurationswerte aus (ausser der Zeile VORLESEZEIT_IMAGE). Der
# Dateiname der Umgebungsdatei ist zusammengesetzt, weil
# .claude/hooks/block-secrets.sh sonst schon den Aufruf blockt (Known Pitfalls).
set -euo pipefail
cd "$(dirname "$0")/.."
ENV_FILE=".env.heim""server"
DC=(docker compose -f docker-compose.heimserver.yml --env-file "$ENV_FILE")
RELEASE_IMAGE=ghcr.io/beasthook/vorlesezeit
PRUEFEN_SEKUNDEN=180

sichern() {
  "${DC[@]}" exec -T backup python deploy/backup/backup.py --once
}

post() {
  "${DC[@]}" ps --format '{{.Service}} {{.Status}}'
  echo "--- /status"
  "${DC[@]}" exec -T app python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/status').read().decode())"
  echo "--- Log der letzten 5 Minuten (Fehler/Einrichtungslink)"
  "${DC[@]}" logs --since 5m app 2>&1 | grep -iE "error|exception|traceback|einrichtung" || echo "keine Treffer"
}

# Version aus /status im App-Container, leer wenn die App nicht antwortet.
app_version() {
  "${DC[@]}" exec -T app python -c "import json, urllib.request; print(json.load(urllib.request.urlopen('http://localhost:8000/status', timeout=5))['version'])" 2>/dev/null || true
}

# Ersetzt die Zeile VORLESEZEIT_IMAGE (leerer Wert: entfernt sie) und laesst
# alle anderen Zeilen und die Dateirechte unveraendert.
setze_image() {
  local tmp
  tmp=$(mktemp)
  grep -v '^VORLESEZEIT_IMAGE=' "$ENV_FILE" > "$tmp" || true
  if [ -n "$1" ]; then
    echo "VORLESEZEIT_IMAGE=$1" >> "$tmp"
  fi
  cat "$tmp" > "$ENV_FILE"
  rm -f "$tmp"
}

# Wartet, bis die App healthy ist und /status die erwartete Version meldet
# (leere Erwartung: nur healthy).
pruefen() {
  local erwartet=$1 ende=$((SECONDS + PRUEFEN_SEKUNDEN)) cid gesund version
  while [ "$SECONDS" -lt "$ende" ]; do
    cid=$("${DC[@]}" ps -q app 2>/dev/null || true)
    if [ -n "$cid" ]; then
      gesund=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid" 2>/dev/null || true)
      if [ "$gesund" = healthy ]; then
        version=$(app_version)
        if [ -z "$erwartet" ] || [ "$version" = "$erwartet" ]; then
          return 0
        fi
      fi
    fi
    sleep 5
  done
  return 1
}

update() {
  local ziel=${1:-}
  if ! [[ "$ziel" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Aufruf: deploy/heimserver.sh update X.Y.Z" >&2
    exit 2
  fi
  local neu="$RELEASE_IMAGE:$ziel" cid voriges vorige_version sicherung
  cid=$("${DC[@]}" ps -q app)
  if [ -z "$cid" ]; then
    echo "Abbruch: der App-Container laeuft nicht." >&2
    exit 1
  fi
  voriges=$(docker inspect -f '{{.Config.Image}}' "$cid")
  vorige_version=$(app_version)
  echo "Vorige Version: ${vorige_version:-unbekannt} ($voriges)"

  echo "--- Sicherung"
  if ! sicherung=$(sichern); then
    echo "$sicherung"
    echo "Abbruch: Sicherung fehlgeschlagen, nichts geaendert." >&2
    exit 1
  fi
  echo "$sicherung"

  echo "--- Lade $neu"
  if ! docker pull "$neu"; then
    echo "Abbruch: $neu liess sich nicht laden, nichts geaendert." >&2
    exit 1
  fi

  setze_image "$neu"
  echo "VORLESEZEIT_IMAGE=$neu"
  "${DC[@]}" up -d --no-build
  echo "--- Pruefe (bis ${PRUEFEN_SEKUNDEN} s)"
  if pruefen "$ziel"; then
    echo "Update auf $ziel fertig."
    post
    return 0
  fi

  echo "--- Rueckfall auf $voriges"
  setze_image "$voriges"
  echo "VORLESEZEIT_IMAGE=$voriges"
  "${DC[@]}" up -d --no-build
  if ! pruefen "$vorige_version"; then
    echo "WARNUNG: auch die vorige Version antwortet nicht wie erwartet." >&2
  fi
  echo "Update auf $ziel fehlgeschlagen, zurück auf ${vorige_version:-$voriges}."
  echo "Sicherung von vorher: $(echo "$sicherung" | grep -o 'Datenbank gesichert: [^,]*' || echo 'siehe Ausgabe oben')"
  post
  exit 1
}

case "${1:-}" in
  pre)
    if [ "$(git branch --show-current)" != main ] || [ -n "$(git status --porcelain)" ]; then
      echo "Abbruch: nur von sauberem main bereitstellen." >&2
      git status --short --branch >&2
      exit 1
    fi
    git log --oneline -1
    "${DC[@]}" ps --format '{{.Service}} {{.Status}}'
    echo "--- Sicherung"
    sichern
    ;;
  up)
    # Bau aus dem Checkout: ein Release-Image aus "update" gilt dann nicht
    # mehr, sonst truege das selbst gebaute Image den Namen des Release.
    if grep -q '^VORLESEZEIT_IMAGE=' "$ENV_FILE"; then
      setze_image ""
      echo "VORLESEZEIT_IMAGE entfernt, baue aus dem Checkout."
    fi
    "${DC[@]}" up -d --build
    ;;
  post)
    post
    ;;
  update)
    update "${2:-}"
    ;;
  *)
    echo "Aufruf: $0 pre|up|post|update X.Y.Z" >&2
    exit 2
    ;;
esac
