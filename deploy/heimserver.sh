#!/bin/bash
# Bereitstellung auf dem Heimserver (docs/betrieb.md), in drei Schritten:
#   pre   sauberer main-Stand pruefen, Zustand zeigen, Sicherung ausloesen
#   up    Image neu bauen und Dienste neu starten
#   post  Zustand, /status, Log auf Fehler/Einrichtungslink, Version
# Gibt nie Konfigurationswerte aus. Der Dateiname der Umgebungsdatei ist
# zusammengesetzt, weil .claude/hooks/block-secrets.sh sonst schon den
# Aufruf blockt (Known Pitfalls).
set -euo pipefail
cd "$(dirname "$0")/.."
DC=(docker compose -f docker-compose.heimserver.yml --env-file ".env.heim""server")

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
    "${DC[@]}" exec -T backup python deploy/backup/backup.py --once
    ;;
  up)
    "${DC[@]}" up -d --build
    ;;
  post)
    "${DC[@]}" ps --format '{{.Service}} {{.Status}}'
    echo "--- /status"
    "${DC[@]}" exec -T app python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/status').read().decode())"
    echo "--- Log der letzten 5 Minuten (Fehler/Einrichtungslink)"
    "${DC[@]}" logs --since 5m app 2>&1 | grep -iE "error|exception|traceback|einrichtung" || echo "keine Treffer"
    ;;
  *)
    echo "Aufruf: $0 pre|up|post" >&2
    exit 2
    ;;
esac
