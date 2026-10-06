"""One-Klick-Setup Baustein 2: Cloudflare-Client gegen eine nachgebaute API."""

from __future__ import annotations

import json

import httpx
import pytest

from scripts.einladen.cloudflare import API_BASE_URL, Cloudflare, CloudflareError, DnsEintrag

ACCOUNT = "acc123"
ZONE = "zone456"
API_TOKEN = "cf-api-token-fake"
TUNNEL = "6ff42ae2-765d-4adf-8112-31c55c1551ef"


def ok(result) -> httpx.Response:
    return httpx.Response(
        200, json={"success": True, "errors": [], "messages": [], "result": result}
    )


class Api:
    def __init__(self, antworten: dict[tuple[str, str], httpx.Response]):
        self.antworten = antworten
        self.aufrufe: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.aufrufe.append(request)
        pfad = request.url.path.removeprefix("/client/v4")
        return self.antworten[(request.method, pfad)]


def client(api: Api) -> Cloudflare:
    return Cloudflare(
        token=API_TOKEN, account_id=ACCOUNT, zone_id=ZONE, transport=httpx.MockTransport(api)
    )


def test_basis_und_anmeldung():
    api = Api({("GET", f"/zones/{ZONE}/dns_records"): ok([])})
    client(api).dns_eintraege("x.vorlesezeit.app")
    anfrage = api.aufrufe[0]
    assert str(anfrage.url).startswith(API_BASE_URL)
    assert anfrage.headers["Authorization"] == f"Bearer {API_TOKEN}"
    assert anfrage.url.params["name"] == "x.vorlesezeit.app"


def test_dns_eintraege_aller_typen():
    api = Api(
        {
            ("GET", f"/zones/{ZONE}/dns_records"): ok(
                [
                    {"id": "r1", "type": "TXT", "name": "x.vorlesezeit.app", "content": "v=1"},
                    {
                        "id": "r2",
                        "type": "CNAME",
                        "name": "x.vorlesezeit.app",
                        "content": "t.cfargotunnel.com",
                    },
                ]
            )
        }
    )
    assert client(api).dns_eintraege("x.vorlesezeit.app") == [
        DnsEintrag("r1", "TXT", "x.vorlesezeit.app", "v=1"),
        DnsEintrag("r2", "CNAME", "x.vorlesezeit.app", "t.cfargotunnel.com"),
    ]
    assert "type" not in api.aufrufe[0].url.params


def test_tunnel_anlegen_fernverwaltet():
    api = Api({("POST", f"/accounts/{ACCOUNT}/cfd_tunnel"): ok({"id": TUNNEL})})
    assert client(api).tunnel_anlegen("vorlesezeit-x") == TUNNEL
    assert json.loads(api.aufrufe[0].content) == {
        "name": "vorlesezeit-x",
        "config_src": "cloudflare",
    }


def test_weiterleitung_mit_auffangregel():
    pfad = f"/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL}/configurations"
    api = Api({("PUT", pfad): ok({"tunnel_id": TUNNEL})})
    client(api).weiterleitung_setzen(TUNNEL, "x.vorlesezeit.app")
    assert json.loads(api.aufrufe[0].content) == {
        "config": {
            "ingress": [
                {
                    "hostname": "x.vorlesezeit.app",
                    "service": "http://app:8000",
                    "originRequest": {},
                },
                {"service": "http_status:404"},
            ]
        }
    }


def test_cname_proxied_auf_tunnel():
    api = Api({("POST", f"/zones/{ZONE}/dns_records"): ok({"id": "rec9"})})
    assert client(api).cname_anlegen("x.vorlesezeit.app", TUNNEL) == "rec9"
    assert json.loads(api.aufrufe[0].content) == {
        "type": "CNAME",
        "name": "x.vorlesezeit.app",
        "content": f"{TUNNEL}.cfargotunnel.com",
        "proxied": True,
    }


def test_tunnel_token():
    api = Api({("GET", f"/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL}/token"): ok("tunnel-token-fake")})
    assert client(api).tunnel_token(TUNNEL) == "tunnel-token-fake"


def test_verbundene_geraete_zaehlt_connectoren():
    pfad = f"/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL}/connections"
    api = Api({("GET", pfad): ok([{"id": "c1", "conns": [{}, {}]}, {"id": "c2", "conns": [{}]}])})
    assert client(api).verbundene_geraete(TUNNEL) == 2


def test_tunnel_loeschen_raeumt_erst_verbindungen_ab():
    basis = f"/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL}"
    api = Api({("DELETE", f"{basis}/connections"): ok(None), ("DELETE", basis): ok({"id": TUNNEL})})
    client(api).tunnel_loeschen(TUNNEL)
    assert [(a.method, a.url.path.removeprefix("/client/v4")) for a in api.aufrufe] == [
        ("DELETE", f"{basis}/connections"),
        ("DELETE", basis),
    ]


def test_dns_loeschen():
    api = Api({("DELETE", f"/zones/{ZONE}/dns_records/rec9"): ok({"id": "rec9"})})
    client(api).dns_loeschen("rec9")
    assert len(api.aufrufe) == 1


def test_fehler_nennt_cloudflare_meldung_aber_nie_das_token():
    antwort = httpx.Response(
        403,
        json={
            "success": False,
            "errors": [{"code": 10000, "message": "Authentication error"}],
            "messages": [],
            "result": None,
        },
    )
    api = Api({("POST", f"/accounts/{ACCOUNT}/cfd_tunnel"): antwort})
    with pytest.raises(CloudflareError, match="Authentication error") as exc:
        client(api).tunnel_anlegen("vorlesezeit-x")
    assert API_TOKEN not in str(exc.value)


def test_antwort_ohne_json():
    api = Api(
        {
            ("GET", f"/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL}/token"): httpx.Response(
                502, text="bad gateway"
            )
        }
    )
    with pytest.raises(CloudflareError, match="502"):
        client(api).tunnel_token(TUNNEL)
