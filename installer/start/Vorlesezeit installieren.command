#!/bin/bash
# Vorlesezeit installieren (Mac). Doppelklick öffnet ein Terminalfenster,
# danach geht es im Browser weiter (One-Klick-Setup, Baustein 3).
# Beim ersten Öffnen fragt macOS nach: Systemeinstellungen, Datenschutz &
# Sicherheit, „Trotzdem öffnen“.
set -u
VERSION="0.4.0"
IMAGE="ghcr.io/beasthook/vorlesezeit-installer:$VERSION"
ORDNER="$HOME/Vorlesezeit"
export PATH="$PATH:/usr/local/bin:/opt/homebrew/bin:$HOME/.docker/bin:/Applications/Docker.app/Contents/Resources/bin"

ende() {
  printf '\n%s\n\n' "$1"
  read -r -p "Zum Schließen Enter drücken. " _
  exit 1
}

if ! command -v docker >/dev/null 2>&1; then
  open "https://www.docker.com/products/docker-desktop/"
  ende "Vorlesezeit braucht Docker Desktop. Installiere es von der Seite, die sich gerade geöffnet hat, und öffne diese Datei danach noch einmal."
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker Desktop startet, das dauert bis zu 2 Minuten ..."
  open -a Docker
  for _ in $(seq 60); do
    docker info >/dev/null 2>&1 && break
    sleep 2
  done
  docker info >/dev/null 2>&1 || ende "Docker Desktop startet nicht. Öffne es bitte von Hand und danach diese Datei noch einmal."
fi

# Die Kopie im Installationsordner ist später der Eingang zur Wartung.
mkdir -p "$ORDNER"
KOPIE="$ORDNER/Vorlesezeit installieren.command"
[ "$0" -ef "$KOPIE" ] || cp "$0" "$KOPIE"

# 32 Zufallsbytes = 256 Bit. Über die Umgebung statt als Argument übergeben,
# damit das Token nicht in der Prozessliste steht.
INSTALLER_TOKEN=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
export INSTALLER_TOKEN

echo "Der Assistent wird geladen ..."
docker pull "$IMAGE" >/dev/null || ende "Der Assistent ließ sich nicht herunterladen. Prüf die Internetverbindung und versuch es noch einmal."

# Erster freier Port ab 8099: Docker meldet einen belegten Port beim Start.
PORT=8099
while :; do
  docker rm -f vorlesezeit-installer >/dev/null 2>&1
  if FEHLER=$(docker run -d --rm --name vorlesezeit-installer \
      -p "127.0.0.1:$PORT:8000" \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -v "$ORDNER:/install" \
      -v /Volumes:/laufwerke:ro \
      -e INSTALLER_TOKEN -e HOST_OS=mac -e "HOST_INSTALL_DIR=$ORDNER" -e "INSTALLER_IMAGE=$IMAGE" \
      "$IMAGE" 2>&1 >/dev/null); then
    break
  fi
  case "$FEHLER" in
    *"already allocated"* | *"address already in use"*) ;;
    *) ende "Der Assistent startet nicht. $FEHLER" ;;
  esac
  PORT=$((PORT + 1))
  [ "$PORT" -le 8199 ] || ende "Zwischen 8099 und 8199 ist kein Port frei."
done

sleep 2
open "http://127.0.0.1:$PORT/?t=$INSTALLER_TOKEN"
echo "Der Assistent läuft jetzt im Browser. Dieses Fenster kannst du schließen."
