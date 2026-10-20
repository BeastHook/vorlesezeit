"""One-Klick-Setup Baustein 1: Release-Konventionen."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_version_ist_semver_ohne_v():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    teile = version.split(".")
    assert len(teile) == 3 and all(t.isdigit() for t in teile)


def test_workflow_prueft_tag_gegen_version_und_baut_beide_architekturen():
    text = (ROOT / ".github/workflows/release.yml").read_text()
    assert 'tags: ["v*.*.*"]' in text
    assert "GITHUB_REF_NAME" in text and "pyproject.toml" in text
    assert "linux/amd64,linux/arm64" in text
    assert "needs: test" in text
    assert ":latest" not in text and "type=raw,value=latest" not in text


def test_workflow_baut_das_installer_image_fuer_beide_architekturen():
    text = (ROOT / ".github/workflows/release.yml").read_text()
    job = text.split("  installer-image:", 1)[1]
    assert "needs: test" in job
    assert "ghcr.io/${{ github.repository_owner }}/vorlesezeit-installer" in job
    assert "file: installer/Dockerfile" in job
    assert "linux/amd64,linux/arm64" in job
    assert "flavor: latest=false" in job
    assert ":latest" not in text and "type=raw,value=latest" not in text


def test_workflow_prueft_auch_das_installer_paket():
    text = (ROOT / ".github/workflows/release.yml").read_text()
    assert "ruff format --check app tests scripts installer" in text
