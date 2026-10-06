"""One-Klick-Setup Baustein 1: Compose-Vorlage für Freunde."""

from __future__ import annotations

import re
from pathlib import Path

from scripts.einladen.cli import CLOUDFLARED_IMAGE

VORLAGE = Path(__file__).resolve().parent.parent / "deploy/freunde/compose.yml"


def _images() -> list[str]:
    return re.findall(r"^\s+image:\s*(.+?)\s*$", VORLAGE.read_text(), re.M)


def test_nur_feste_versionen():
    text = VORLAGE.read_text()
    assert ":latest" not in text
    assert "build:" not in text
    for image in _images():
        assert image.startswith("${VORLESEZEIT_IMAGE") or "@sha256:" in image, image


def test_app_scheduler_backup_aus_demselben_image():
    assert _images().count("${VORLESEZEIT_IMAGE:?VORLESEZEIT_IMAGE fehlt}") == 3


def test_cloudflared_wie_im_einladungsskript():
    assert CLOUDFLARED_IMAGE in _images()


def test_kein_port_nach_aussen_und_kein_docker_socket():
    text = VORLAGE.read_text()
    assert "ports:" not in text
    assert "docker.sock" not in text
