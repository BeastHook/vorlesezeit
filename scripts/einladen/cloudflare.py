"""Schmaler Client für die Cloudflare-API des Einladungsskripts.

Nur die Aufrufe, die `neu`, `liste` und `widerrufen` brauchen (Spec
2026-10-04, Baustein 2). Das API-Token steht nur im Authorization-Header und
nie in einer Fehlermeldung.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

API_BASE_URL = "https://api.cloudflare.com/client/v4"
APP_SERVICE = "http://app:8000"


class CloudflareError(RuntimeError):
    pass


@dataclass(frozen=True)
class DnsEintrag:
    id: str
    type: str
    name: str
    content: str


class Cloudflare:
    def __init__(
        self,
        *,
        token: str,
        account_id: str,
        zone_id: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=API_BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            transport=transport,
        )
        self._tunnel = f"/accounts/{account_id}/cfd_tunnel"
        self._dns = f"/zones/{zone_id}/dns_records"

    def _call(self, method: str, path: str, **kwargs) -> object:
        response = self._http.request(method, path, **kwargs)
        try:
            body = response.json()
        except ValueError:
            raise CloudflareError(f"{method} {path}: HTTP {response.status_code}") from None
        if not body.get("success"):
            meldungen = "; ".join(e.get("message", "") for e in body.get("errors") or [])
            raise CloudflareError(f"{method} {path}: {meldungen or response.status_code}")
        return body.get("result")

    def dns_eintraege(self, name: str) -> list[DnsEintrag]:
        result = self._call("GET", self._dns, params={"name": name})
        return [DnsEintrag(r["id"], r["type"], r["name"], r["content"]) for r in result]

    def tunnel_anlegen(self, name: str) -> str:
        result = self._call("POST", self._tunnel, json={"name": name, "config_src": "cloudflare"})
        return result["id"]

    def weiterleitung_setzen(self, tunnel_id: str, host: str) -> None:
        ingress = [
            {"hostname": host, "service": APP_SERVICE, "originRequest": {}},
            {"service": "http_status:404"},
        ]
        self._call(
            "PUT",
            f"{self._tunnel}/{tunnel_id}/configurations",
            json={"config": {"ingress": ingress}},
        )

    def cname_anlegen(self, host: str, tunnel_id: str) -> str:
        result = self._call(
            "POST",
            self._dns,
            json={
                "type": "CNAME",
                "name": host,
                "content": f"{tunnel_id}.cfargotunnel.com",
                "proxied": True,
            },
        )
        return result["id"]

    def tunnel_token(self, tunnel_id: str) -> str:
        return self._call("GET", f"{self._tunnel}/{tunnel_id}/token")

    def verbundene_geraete(self, tunnel_id: str) -> int:
        return len(self._call("GET", f"{self._tunnel}/{tunnel_id}/connections"))

    def tunnel_loeschen(self, tunnel_id: str) -> None:
        # Cloudflare löscht nur Tunnel ohne aktive Verbindungen.
        self._call("DELETE", f"{self._tunnel}/{tunnel_id}/connections")
        self._call("DELETE", f"{self._tunnel}/{tunnel_id}")

    def dns_loeschen(self, eintrag_id: str) -> None:
        self._call("DELETE", f"{self._dns}/{eintrag_id}")
