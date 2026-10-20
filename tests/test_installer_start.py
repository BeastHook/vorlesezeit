"""One-Klick-Setup Baustein 3: Startdateien und Installer-Image (nur Textprüfungen,
kein Docker-Aufruf)."""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
START = ROOT / "installer/start"
MAC = START / "Vorlesezeit installieren.command"
WINDOWS = START / "Vorlesezeit installieren.bat"
LINUX = START / "install.sh"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
VERSION_ZEILE = {
    MAC: r'^VERSION="([^"]+)"$',
    LINUX: r'^VERSION="([^"]+)"$',
    WINDOWS: r'^set "VERSION=([^"]+)"\r?$',
}


@pytest.mark.parametrize("datei", [MAC, WINDOWS, LINUX], ids=lambda p: p.name)
def test_versionskonstante_gleich_pyproject(datei):
    assert re.findall(VERSION_ZEILE[datei], datei.read_text(), re.M) == [VERSION]


@pytest.mark.parametrize("datei", [MAC, WINDOWS, LINUX], ids=lambda p: p.name)
def test_port_nur_lokal_laufwerke_nur_lesend(datei):
    text = datei.read_text()
    assert re.search(r'-p "?127\.0\.0\.1:', text)
    assert "-p 0.0.0.0" not in text
    laufwerke = re.findall(r"/laufwerke\S*", text)
    assert laufwerke
    assert all(e.rstrip('"').endswith((":ro", ",readonly")) for e in laufwerke), laufwerke


@pytest.mark.parametrize(
    ("datei", "host_os"),
    [(MAC, "mac"), (WINDOWS, "windows"), (LINUX, "linux")],
    ids=lambda x: getattr(x, "name", x),
)
def test_start_des_assistenten(datei, host_os):
    text = datei.read_text()
    assert "ghcr.io/beasthook/vorlesezeit-installer:" in text
    assert "--rm" in text
    assert "docker.sock:/var/run/docker.sock" in text
    assert ":/install" in text
    assert "8099" in text
    for name in ("INSTALLER_TOKEN", "HOST_INSTALL_DIR", "INSTALLER_IMAGE"):
        assert f"-e {name}" in text or f'-e "{name}' in text, name
    assert f"HOST_OS={host_os}" in text
    assert "http://127.0.0.1:" in text and "/?t=" in text
    assert "Vorlesezeit" in text  # Installationsordner
    assert "docker.com" in text  # Downloadseite, wenn Docker fehlt


def test_laufwerke_je_system():
    assert "-v /Volumes:/laufwerke:ro" in MAC.read_text()
    linux = LINUX.read_text()
    assert "/media" in linux and "/mnt" in linux
    windows = WINDOWS.read_text()
    assert "wsl" in windows.lower()
    buchstaben = re.search(r"for %%L in \(([A-Z ]+)\)", windows).group(1).split()
    assert "C" not in buchstaben
    assert len(buchstaben) == 25


def test_install_sh_bindet_laufwerke_mit_mount_ein():
    # `-v` zerlegt Laufwerksnamen mit `:`, `--mount` nicht.
    linux = LINUX.read_text()
    assert '-v "$d' not in linux
    assert (
        'set -- "$@" --mount "type=bind,\\"source=$d\\",\\"target=/laufwerke/$n\\",readonly"'
        in linux
    )


def test_install_sh_ueberspringt_pfade_mit_anfuehrungszeichen():
    linux = LINUX.read_text()
    schleife = linux[linux.index("for d in /media") : linux.index("done", linux.index("for d in"))]
    assert 'case "$d" in *\'"\'*) continue ;; esac' in schleife
    assert schleife.index("'\"'*) continue") < schleife.index("--mount")


def test_token_mit_mindestens_128_bit():
    assert "od -An -N32 -tx1 /dev/urandom" in MAC.read_text()
    assert "od -An -N32 -tx1 /dev/urandom" in LINUX.read_text()
    assert "RNGCryptoServiceProvider" in WINDOWS.read_text() and "byte[] 32" in WINDOWS.read_text()


def test_bat_hat_windows_zeilenenden():
    roh = WINDOWS.read_bytes()
    assert roh.count(b"\r\n") == roh.count(b"\n")


@pytest.mark.parametrize("zeilenende", ["crlf", "lf"])
def test_bat_startet_zuerst_eine_kopie_mit_crlf(zeilenende):
    # Mit LF findet cmd.exe Sprungmarken falsch (Download über die Raw-Adresse).
    text = WINDOWS.read_bytes().decode()
    if zeilenende == "lf":
        text = text.replace("\r\n", "\n")
    zeilen = text.splitlines()
    erste_marke = next(i for i, z in enumerate(zeilen) if z.startswith(":"))
    kopie = next(i for i, z in enumerate(zeilen) if 'find /v ""' in z)
    assert kopie < erste_marke
    assert "%TEMP%" in zeilen[kopie]
    assert 'type "%~f0"' in zeilen[kopie]
    ende = next(i for i, z in enumerate(zeilen) if z.strip() == "exit /b")
    assert kopie < ende < erste_marke
    block = "\n".join(zeilen[: ende + 1])
    assert "if not defined VORLESEZEIT_CRLF" in block
    assert 'set "VORLESEZEIT_CRLF=1"' in block
    assert 'call "%TEMP%\\vorlesezeit-installieren.bat"' in block
    assert "pause" not in block
    for zeile in zeilen[: ende + 1]:
        befehl = zeile.strip().lower()
        assert not befehl.startswith(":"), zeile
        assert "goto" not in befehl and "call :" not in befehl, zeile


@pytest.mark.parametrize(
    ("shell", "datei"), [("bash", MAC), ("sh", LINUX)], ids=["command", "install.sh"]
)
def test_shell_syntax(shell, datei):
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} fehlt")
    subprocess.run([shell, "-n", str(datei)], check=True)


def test_install_sh_laeuft_auch_aus_einer_pipe():
    text = LINUX.read_text()
    assert "curl -fsSL" in text
    assert (
        "raw.githubusercontent.com/BeastHook/vorlesezeit/v$VERSION/installer/start/install.sh"
        in text
    )


@pytest.mark.parametrize("datei", [LINUX, ROOT / "docs/betrieb.md"], ids=lambda p: p.name)
def test_einzeiler_laedt_vom_release_tag(datei):
    muster = r"curl -fsSL https://raw\.githubusercontent\.com/\S+ \| sh"
    assert re.findall(muster, datei.read_text()) == [
        "curl -fsSL https://raw.githubusercontent.com/BeastHook/vorlesezeit/"
        f"v{VERSION}/installer/start/install.sh | sh"
    ]


def test_dockerfile_nur_fremd_images_mit_digest():
    text = (ROOT / "installer/Dockerfile").read_text()
    images = re.findall(r"^FROM\s+(\S+)", text, re.M)
    assert any(i.startswith("python:3.12-slim@") for i in images)
    for image in images:
        assert re.search(r"@sha256:[0-9a-f]{64}$", image), image
    assert "docker-compose" in text and "/usr/local/bin/docker" in text
    assert "COPY deploy/freunde/compose.yml" in text
    assert "--no-access-log" in text
    assert "installer:create_installer" in text
