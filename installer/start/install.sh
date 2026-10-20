#!/bin/sh
# Vorlesezeit installieren (Linux, NAS). Danach geht es im Browser weiter
# (One-Klick-Setup, Baustein 3). Geht auch als Einzeiler:
#   curl -fsSL https://raw.githubusercontent.com/BeastHook/vorlesezeit/v0.4.0/installer/start/install.sh | sh
set -u
VERSION="0.4.0"
IMAGE="ghcr.io/beasthook/vorlesezeit-installer:$VERSION"
QUELLE="https://raw.githubusercontent.com/BeastHook/vorlesezeit/v$VERSION/installer/start/install.sh"
ORDNER="$HOME/Vorlesezeit"

ende() {
  printf '\n%s\n\n' "$1"
  exit 1
}

oeffne() {
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$1" >/dev/null 2>&1 &
  fi
  echo "Im Browser öffnen: $1"
}

if ! command -v docker >/dev/null 2>&1; then
  oeffne "https://docs.docker.com/engine/install/"
  ende "Vorlesezeit braucht Docker. Installiere es nach der Anleitung auf docs.docker.com und starte dieses Skript danach noch einmal."
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker startet, das dauert bis zu 2 Minuten ..."
  systemctl --user start docker-desktop >/dev/null 2>&1 || sudo systemctl start docker
  for _ in $(seq 60); do
    docker info >/dev/null 2>&1 && break
    sleep 2
  done
  docker info >/dev/null 2>&1 || ende "Docker läuft nicht, oder du darfst es nicht benutzen (Gruppe docker). Starte Docker und dieses Skript danach noch einmal."
fi

# Die Kopie im Installationsordner ist später der Eingang zur Wartung. Aus
# einer Pipe (curl ... | sh) gibt es keine Datei, dann kommt sie aus dem Repo.
mkdir -p "$ORDNER"
if [ -f "$0" ] && [ "$(basename "$0")" = "install.sh" ]; then
  [ "$0" -ef "$ORDNER/install.sh" ] || cp "$0" "$ORDNER/install.sh"
else
  curl -fsSL "$QUELLE" -o "$ORDNER/install.sh" || echo "Hinweis: Kopie der Startdatei nicht geladen."
fi
chmod +x "$ORDNER/install.sh" 2>/dev/null

# 32 Zufallsbytes = 256 Bit, über die Umgebung statt als Argument übergeben.
INSTALLER_TOKEN=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
export INSTALLER_TOKEN

# Laufwerke nur lesend, je eines unter seinem Namen. /media/<Benutzer> ist nur
# ein Sammelordner, dort zählen die Laufwerke darin. Kein `-v`: das zerlegt Namen
# mit `:`. Die mount-Angabe ist eine CSV-Zeile, ein `"` im Pfad bräche sie.
set --
NAMEN=" "
for d in /media/* /media/"${USER:-}"/* /mnt/*; do
  [ -d "$d" ] || continue
  [ "$d" = "/media/${USER:-}" ] && continue
  case "$d" in *'"'*) continue ;; esac
  n=$(basename "$d")
  case "$NAMEN" in *" $n "*) continue ;; esac
  NAMEN="$NAMEN$n "
  set -- "$@" --mount "type=bind,\"source=$d\",\"target=/laufwerke/$n\",readonly"
done

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
      "$@" \
      -e INSTALLER_TOKEN -e HOST_OS=linux -e "HOST_INSTALL_DIR=$ORDNER" -e "INSTALLER_IMAGE=$IMAGE" \
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
oeffne "http://127.0.0.1:$PORT/?t=$INSTALLER_TOKEN"
echo "Der Assistent läuft jetzt im Browser. Gerät ohne Bildschirm: vom eigenen Rechner aus"
echo "  ssh -L $PORT:127.0.0.1:$PORT <dieses Gerät>  und dann die Adresse oben öffnen."
