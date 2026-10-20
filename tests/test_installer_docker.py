"""One-Klick-Setup Baustein 3: installer/docker.py gegen einen Docker-Fake."""

from __future__ import annotations

import json

from installer.docker import Docker, Ergebnis
from tests.installer_fakes import fake_docker


def test_compose_aufrufe_nennen_installationsordner_und_vorlage(tmp_path):
    docker, fake = fake_docker(tmp_path)

    assert docker.pull().code == 0
    assert docker.up().code == 0

    pull, up = fake.aufrufe
    assert pull[:5] == [
        "compose",
        "--project-directory",
        str(tmp_path),
        "-f",
        str(tmp_path / "compose.yml"),
    ]
    assert pull[5:] == ["pull"]
    assert up[5:] == ["up", "-d"]


def test_app_gesund_liest_den_healthcheck_des_dienstes_app(tmp_path):
    zeilen = json.dumps({"Service": "app", "Health": "starting"})
    docker, fake = fake_docker(tmp_path, compose_ps=Ergebnis(0, zeilen, ""))
    assert docker.app_gesund() is False
    assert fake.aufrufe[0][5:] == ["ps", "--format", "json", "app"]

    fake.antworten["compose ps"] = Ergebnis(0, json.dumps({"Service": "app", "Health": "healthy"}))
    assert docker.app_gesund() is True

    fake.antworten["compose ps"] = Ergebnis(1, "", "no such service")
    assert docker.app_gesund() is False


def test_tunnel_wird_aus_dem_container_app_abgefragt(tmp_path):
    docker, fake = fake_docker(tmp_path)
    assert docker.tunnel_verbunden() is True
    befehl = fake.aufrufe[0][5:]
    assert befehl[:3] == ["exec", "-T", "app"]
    assert "http://cloudflared:2000/ready" in befehl[-1]

    fake.antworten["compose exec"] = Ergebnis(1, "", "HTTP Error 503")
    assert docker.tunnel_verbunden() is False


def test_host_pfad_aus_den_eigenen_mounts(tmp_path):
    docker, fake = fake_docker(tmp_path)
    assert docker.host_pfad("/laufwerke/Samsung T7") == "/Volumes/Samsung T7"
    assert fake.aufrufe[0] == ["inspect", "--format", "{{json .Mounts}}", "abc123"]
    assert docker.host_pfad("/woanders") is None


def test_ordner_anlegen_ueber_hilfscontainer_mit_schreibprobe(tmp_path):
    docker, fake = fake_docker(tmp_path)
    docker.ordner_anlegen("vorlesezeit-installer:0.3.0", "/Volumes/Samsung T7")
    args = fake.aufrufe[0]
    assert args[:4] == [
        "run",
        "--rm",
        "--mount",
        'type=bind,"source=/Volumes/Samsung T7",target=/ziel',
    ]
    assert args[4] == "vorlesezeit-installer:0.3.0"
    assert "mkdir -p /ziel/Vorlesezeit-Sicherung" in args[-1]
    assert "touch" in args[-1]


def test_ordner_anlegen_mit_doppelpunkt_im_laufwerksnamen(tmp_path):
    docker, fake = fake_docker(tmp_path)
    docker.ordner_anlegen("img", "/Volumes/Foto:2024")
    assert fake.aufrufe[0][:5] == [
        "run",
        "--rm",
        "--mount",
        'type=bind,"source=/Volumes/Foto:2024",target=/ziel',
        "img",
    ]


def test_ordner_anlegen_mit_komma_und_anfuehrungszeichen(tmp_path):
    # --mount ist eine CSV-Zeile: das Feld in "" hält das Komma, ein " wird zu "".
    docker, fake = fake_docker(tmp_path)
    docker.ordner_anlegen("img", '/Volumes/Fotos, "alt"')
    assert fake.aufrufe[0][3] == 'type=bind,"source=/Volumes/Fotos, ""alt""",target=/ziel'


def test_standard_ausfuehrung_ruft_die_docker_kommandozeile(monkeypatch, tmp_path):
    gesehen = {}

    def fake_run(befehl, **kwargs):
        gesehen["befehl"] = befehl
        gesehen["timeout"] = kwargs["timeout"]

        class R:
            returncode, stdout, stderr = 0, "ok", ""

        return R()

    monkeypatch.setattr("installer.docker.subprocess.run", fake_run)
    ergebnis = Docker(tmp_path, container_id="x").tunnel_verbunden()
    assert ergebnis is True
    assert gesehen["befehl"][0] == "docker"
