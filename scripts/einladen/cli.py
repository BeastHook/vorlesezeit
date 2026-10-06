"""Einladungsskript für befreundete Instanzen (One-Klick-Setup, Baustein 2).

Läuft beim Organisator, nie im Container:

    python -m scripts.einladen neu <name>
    python -m scripts.einladen liste
    python -m scripts.einladen widerrufen <name>
    python -m scripts.einladen testen

Zugang über CLOUDFLARE_API_TOKEN (Konto: Cloudflare Tunnel Bearbeiten, Zone:
DNS Bearbeiten), CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_ZONE_ID; optional
BREVO_SMTP_LOGIN. Brevo-SMTP-Schlüssel lassen sich nicht per API anlegen: im
Dashboard unter SMTP & API > SMTP je Freund einen erzeugen und bei `neu`
verdeckt eingeben. Bestehende DNS-Einträge fasst das Skript nie an.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import httpx

from app.einladungscode import Einladung, EinladungscodeError, SmtpZugang, decode, encode
from scripts.einladen.cloudflare import Cloudflare, CloudflareError

ZONE = "vorlesezeit.app"
BREVO_HOST = "smtp-relay.brevo.com"
BREVO_PORT = 587
MERKLISTE = Path.home() / ".vorlesezeit" / "einladungen.json"
RESERVIERT = {"advent", "www", "mail", "smtp", "api", "admin", "app", "status"}
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,28}[a-z0-9]$")
_PFLICHT = ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ZONE_ID")
CLOUDFLARED_IMAGE = (
    "cloudflare/cloudflared@sha256:072c067d25ccbe61d46e18f0d0723255f2bb5304f7317caa95b27031520ff92c"
)
_TEST_CONTAINER = "vorlesezeit-einladung-test"


class EinladenError(RuntimeError):
    pass


def pruefe_name(name: str) -> str:
    name = name.strip().lower()
    if not _NAME.match(name) or name in RESERVIERT or name.startswith("brevo"):
        raise EinladenError(
            f"„{name}“ geht nicht: 2-30 Zeichen aus a-z, 0-9 und -, "
            "nicht am Rand, keine reservierten Namen."
        )
    return name


def _lies(merkliste: Path) -> dict:
    return json.loads(merkliste.read_text()) if merkliste.exists() else {}


def _schreib(merkliste: Path, daten: dict) -> None:
    merkliste.parent.mkdir(parents=True, exist_ok=True)
    tmp = merkliste.with_suffix(".tmp")
    tmp.write_text(json.dumps(daten, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(merkliste)


def neu(name, *, cf, merkliste: Path, smtp_login: str | None, frage_schluessel=getpass.getpass):
    name = pruefe_name(name)
    host = f"{name}.{ZONE}"
    eintraege = _lies(merkliste)
    if name in eintraege:
        raise EinladenError(f"Eine Einladung für „{name}“ gibt es schon.")
    if cf.dns_eintraege(host):
        raise EinladenError(f"{host} ist schon belegt; bestehende Einträge bleiben unberührt.")

    # Vor dem ersten Anlegen fragen: ein Abbruch hier hinterlässt nichts.
    smtp = None
    if smtp_login:
        key = frage_schluessel(f"Brevo-SMTP-Schlüssel für {name} (unsichtbar, leer = ohne Mail): ")
        if key.strip():
            smtp = SmtpZugang(BREVO_HOST, BREVO_PORT, smtp_login, key.strip(), f"{name}@{ZONE}")

    tunnel_id = cf.tunnel_anlegen(f"vorlesezeit-{name}")
    dns_id = None
    try:
        cf.weiterleitung_setzen(tunnel_id, host)
        dns_id = cf.cname_anlegen(host, tunnel_id)
        code = encode(Einladung(host=host, tunnel_token=cf.tunnel_token(tunnel_id), smtp=smtp))
    except BaseException:
        # Nicht auf dns_id verlassen: Cloudflare kann den Eintrag angelegt
        # haben, obwohl die Antwort scheiterte. Das Ziel trifft nur den eben
        # angelegten Tunnel. Jeder Schritt einzeln, der Ursprungsfehler bleibt.
        ziel = f"{tunnel_id}.cfargotunnel.com"
        try:
            for eintrag in cf.dns_eintraege(host):
                if eintrag.type == "CNAME" and eintrag.content == ziel:
                    cf.dns_loeschen(eintrag.id)
        except Exception:
            print(f"Bitte im Dashboard löschen: DNS-Eintrag {host}", file=sys.stderr)
        try:
            cf.tunnel_loeschen(tunnel_id)
        except Exception:
            print(f"Bitte im Dashboard löschen: Tunnel vorlesezeit-{name}", file=sys.stderr)
        raise
    eintraege[name] = {
        "host": host,
        "tunnel_id": tunnel_id,
        "dns_id": dns_id,
        "angelegt": date.today().isoformat(),
    }
    _schreib(merkliste, eintraege)
    return code


def liste(*, cf, merkliste: Path) -> list[str]:
    eintraege = _lies(merkliste)
    if not eintraege:
        return ["Noch keine Einladungen."]
    zeilen = []
    for name, e in eintraege.items():
        n = cf.verbundene_geraete(e["tunnel_id"])
        if n == 0:
            status = "nicht verbunden"
        elif n == 1:
            status = "verbunden"
        else:
            status = f"WARNUNG: {n} Geräte verbunden, Code doppelt benutzt?"
        zeilen.append(f"{name:<16} {e['host']:<36} {status}")
    return zeilen


def widerrufen(name, *, cf, merkliste: Path) -> None:
    name = name.strip().lower()
    eintraege = _lies(merkliste)
    if name not in eintraege:
        raise EinladenError(f"Für „{name}“ gibt es keine Einladung.")
    e = eintraege[name]
    ziel = f"{e['tunnel_id']}.cfargotunnel.com"
    for eintrag in cf.dns_eintraege(e["host"]):
        if eintrag.type == "CNAME" and eintrag.content == ziel:
            cf.dns_loeschen(eintrag.id)
    # Scheitert das (Freund noch verbunden), bleibt der Merklisten-Eintrag
    # stehen und ein zweiter Aufruf vollendet den Widerruf.
    cf.tunnel_loeschen(e["tunnel_id"])
    del eintraege[name]
    _schreib(merkliste, eintraege)


def _hole_zeitzone(url: str) -> str | None:
    try:
        return httpx.get(url, timeout=10).json().get("timezone")
    except (httpx.HTTPError, ValueError):
        return None


def testen(
    *,
    frage_code=getpass.getpass,
    run=subprocess.run,
    hole_zeitzone=_hole_zeitzone,
    warte=time.sleep,
    netz: str = "vorlesezeit_default",
    versuche: int = 24,
) -> bool:
    """Braucht den laufenden Entwicklungs-Stack (`docker compose up -d`)."""
    einladung = decode(frage_code("Einladungscode (unsichtbar): "))
    # Token nur über die Umgebung, nie als Argument: Argumente sind für
    # jeden Prozess auf dem Rechner sichtbar.
    env = {**os.environ, "TUNNEL_TOKEN": einladung.tunnel_token}
    run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            _TEST_CONTAINER,
            "--network",
            netz,
            "-e",
            "TUNNEL_TOKEN",
            CLOUDFLARED_IMAGE,
            "tunnel",
            "--no-autoupdate",
            "run",
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    try:
        for _ in range(versuche):
            if hole_zeitzone(f"https://{einladung.host}/status") == "Europe/Berlin":
                print(f"OK: https://{einladung.host} antwortet.")
                return True
            warte(5)
        print(f"Keine Antwort von https://{einladung.host}.", file=sys.stderr)
        return False
    finally:
        run(["docker", "stop", _TEST_CONTAINER], check=False, capture_output=True)


def _cloudflare() -> Cloudflare | None:
    fehlend = [v for v in _PFLICHT if not os.environ.get(v)]
    if fehlend:
        print("Es fehlen Umgebungsvariablen: " + ", ".join(fehlend), file=sys.stderr)
        return None
    return Cloudflare(
        token=os.environ["CLOUDFLARE_API_TOKEN"],
        account_id=os.environ["CLOUDFLARE_ACCOUNT_ID"],
        zone_id=os.environ["CLOUDFLARE_ZONE_ID"],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Einladungen für befreundete Instanzen.")
    sub = parser.add_subparsers(dest="befehl", required=True)
    sub.add_parser("neu").add_argument("name")
    sub.add_parser("liste")
    sub.add_parser("widerrufen").add_argument("name")
    sub.add_parser("testen")
    args = parser.parse_args(argv)

    if args.befehl == "testen":
        try:
            return 0 if testen() else 1
        except EinladungscodeError as exc:
            print(exc, file=sys.stderr)
            return 1

    cf = _cloudflare()
    if cf is None:
        return 2
    try:
        if args.befehl == "neu":
            code = neu(
                args.name,
                cf=cf,
                merkliste=MERKLISTE,
                smtp_login=os.environ.get("BREVO_SMTP_LOGIN") or None,
            )
            print("\nEinladungscode (nur jetzt sichtbar, nirgends gespeichert):\n")
            print(code)
            print(
                "\nSchick ihn per Messenger. Widerruf: python -m scripts.einladen widerrufen "
                + pruefe_name(args.name)
            )
        elif args.befehl == "liste":
            print("\n".join(liste(cf=cf, merkliste=MERKLISTE)))
        else:
            widerrufen(args.name, cf=cf, merkliste=MERKLISTE)
            print(
                "Widerrufen. Den Brevo-SMTP-Schlüssel bitte im Dashboard löschen "
                "(SMTP & API > SMTP)."
            )
    except EinladenError as exc:
        print(exc, file=sys.stderr)
        return 1
    except (CloudflareError, httpx.HTTPError) as exc:
        nochmal = (
            " Bitte in ein paar Minuten erneut ausführen." if args.befehl == "widerrufen" else ""
        )
        print(f"Cloudflare-Aufruf gescheitert: {exc}.{nochmal}", file=sys.stderr)
        return 1
    return 0
