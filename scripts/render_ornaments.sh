#!/usr/bin/env bash
# Rendert die Mail-Ornamente (128x128, 2x von 64 px) aus den Web-SVGs.
# Einmalig bzw. nach einer Ornament-Aenderung lokal ausfuehren (rsvg-convert per Homebrew).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p app/static/mail
for name in stern tannenzweig schneeflocke kerze stechpalme; do
  rsvg-convert -w 128 -h 128 "app/static/ornaments/$name.svg" -o "app/static/mail/$name.png"
done
