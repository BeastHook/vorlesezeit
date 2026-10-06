"""One-Klick-Setup Baustein 2: Einladungsskript gegen ein Cloudflare-Double."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.einladungscode import Einladung, EinladungscodeError, decode, encode
from scripts.einladen import cli
from scripts.einladen.cli import EinladenError, liste, neu, pruefe_name, widerrufen
from scripts.einladen.cloudflare import CloudflareError, DnsEintrag


class FakeCloudflare:
    def __init__(self) -> None:
        self.dns: dict[str, DnsEintrag] = {}
        self.tunnel: dict[str, dict] = {}
        self.geraete: dict[str, int] = {}
        self.scheitert: set[str] = set()
        self._n = 0

    def _id(self, art: str) -> str:
        self._n += 1
        return f"{art}{self._n}"

    def _pruefe(self, schritt: str) -> None:
        if schritt in self.scheitert:
            raise CloudflareError(f"{schritt}: kaputt")

    def dns_eintraege(self, name):
        return [e for e in self.dns.values() if e.name == name]

    def tunnel_anlegen(self, name):
        self._pruefe("tunnel_anlegen")
        tid = self._id("t")
        self.tunnel[tid] = {"name": name, "host": None}
        return tid

    def weiterleitung_setzen(self, tunnel_id, host):
        self._pruefe("weiterleitung_setzen")
        self.tunnel[tunnel_id]["host"] = host

    def cname_anlegen(self, host, tunnel_id):
        self._pruefe("cname_anlegen")
        rid = self._id("r")
        self.dns[rid] = DnsEintrag(rid, "CNAME", host, f"{tunnel_id}.cfargotunnel.com")
        # Cloudflare hat den Eintrag schon angelegt, die Antwort geht verloren.
        self._pruefe("cname_anlegen_nach_anlage")
        return rid

    def tunnel_token(self, tunnel_id):
        self._pruefe("tunnel_token")
        return f"token-{tunnel_id}"

    def verbundene_geraete(self, tunnel_id):
        return self.geraete.get(tunnel_id, 0)

    def tunnel_loeschen(self, tunnel_id):
        self._pruefe("tunnel_loeschen")
        del self.tunnel[tunnel_id]

    def dns_loeschen(self, eintrag_id):
        self._pruefe("dns_loeschen")
        del self.dns[eintrag_id]


@pytest.fixture
def cf():
    return FakeCloudflare()


@pytest.fixture
def merkliste(tmp_path) -> Path:
    return tmp_path / "vz" / "einladungen.json"


def _schluessel(_prompt):
    return "xsmtpsib-fake"


def test_neu_legt_tunnel_weiterleitung_und_cname_an(cf, merkliste):
    code = neu(
        "mueller",
        cf=cf,
        merkliste=merkliste,
        smtp_login="login@smtp-brevo.com",
        frage_schluessel=_schluessel,
    )
    einladung = decode(code)
    assert einladung.host == "mueller.vorlesezeit.app"
    [tid] = cf.tunnel
    assert cf.tunnel[tid] == {"name": "vorlesezeit-mueller", "host": "mueller.vorlesezeit.app"}
    assert einladung.tunnel_token == f"token-{tid}"
    [eintrag] = cf.dns.values()
    assert (eintrag.type, eintrag.content) == ("CNAME", f"{tid}.cfargotunnel.com")
    assert einladung.smtp.host == "smtp-relay.brevo.com"
    assert einladung.smtp.port == 587
    assert einladung.smtp.user == "login@smtp-brevo.com"
    assert einladung.smtp.key == "xsmtpsib-fake"
    assert einladung.smtp.absender == "mueller@vorlesezeit.app"


def test_merkliste_ohne_geheimnisse(cf, merkliste):
    code = neu("mueller", cf=cf, merkliste=merkliste, smtp_login="l", frage_schluessel=_schluessel)
    text = merkliste.read_text()
    eintrag = json.loads(text)["mueller"]
    assert set(eintrag) == {"host", "tunnel_id", "dns_id", "angelegt"}
    assert "token-" not in text and "xsmtpsib" not in text and code not in text


def test_name_wird_klein_geschrieben(cf, merkliste):
    neu("Mueller", cf=cf, merkliste=merkliste, smtp_login=None)
    assert "mueller" in json.loads(merkliste.read_text())


@pytest.mark.parametrize(
    "name", ["advent", "www", "mail", "brevo1", "a", "x_y", "-ab", "ab-", "ä", "a" * 31, ""]
)
def test_unzulaessige_namen(name):
    with pytest.raises(EinladenError):
        pruefe_name(name)


@pytest.mark.parametrize("typ", ["TXT", "MX", "CNAME", "A"])
def test_bestehender_eintrag_jedes_typs_verhindert_alles(cf, merkliste, typ):
    cf.dns["x"] = DnsEintrag("x", typ, "mueller.vorlesezeit.app", "irgendwas")
    gefragt = []
    with pytest.raises(EinladenError, match="schon belegt"):
        neu(
            "mueller",
            cf=cf,
            merkliste=merkliste,
            smtp_login="l",
            frage_schluessel=lambda p: gefragt.append(p) or "k",
        )
    assert cf.tunnel == {}
    assert gefragt == []
    assert list(cf.dns) == ["x"]


def test_doppelter_name_in_der_merkliste(cf, merkliste):
    neu("mueller", cf=cf, merkliste=merkliste, smtp_login=None)
    cf.dns.clear()
    with pytest.raises(EinladenError, match="gibt es schon"):
        neu("mueller", cf=cf, merkliste=merkliste, smtp_login=None)


def test_abbruch_beim_schluessel_legt_nichts_an(cf, merkliste):
    def abbruch(_prompt):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        neu("mueller", cf=cf, merkliste=merkliste, smtp_login="l", frage_schluessel=abbruch)
    assert cf.tunnel == {} and cf.dns == {} and not merkliste.exists()


def test_leerer_schluessel_heisst_ohne_mail(cf, merkliste):
    code = neu(
        "mueller", cf=cf, merkliste=merkliste, smtp_login="l", frage_schluessel=lambda p: " "
    )
    assert decode(code).smtp is None


def test_ohne_smtp_login_keine_frage(cf, merkliste):
    code = neu(
        "mueller",
        cf=cf,
        merkliste=merkliste,
        smtp_login=None,
        frage_schluessel=lambda p: pytest.fail("nicht fragen"),
    )
    assert decode(code).smtp is None


@pytest.mark.parametrize("schritt", ["weiterleitung_setzen", "cname_anlegen", "tunnel_token"])
def test_fehler_nach_dem_tunnel_raeumt_alles_ab(cf, merkliste, schritt):
    cf.scheitert.add(schritt)
    with pytest.raises(CloudflareError):
        neu("mueller", cf=cf, merkliste=merkliste, smtp_login=None)
    assert cf.tunnel == {} and cf.dns == {} and not merkliste.exists()


def test_cname_angelegt_aber_antwort_verloren_wird_abgeraeumt(cf, merkliste):
    cf.scheitert.add("cname_anlegen_nach_anlage")
    with pytest.raises(CloudflareError):
        neu("mueller", cf=cf, merkliste=merkliste, smtp_login=None)
    assert cf.dns == {} and cf.tunnel == {} and not merkliste.exists()


def test_scheitert_dns_abraeumen_wird_tunnel_trotzdem_geloescht(cf, merkliste, capsys):
    cf.scheitert.update({"tunnel_token", "dns_loeschen"})
    with pytest.raises(CloudflareError, match="tunnel_token: kaputt"):
        neu("mueller", cf=cf, merkliste=merkliste, smtp_login=None)
    assert cf.tunnel == {}
    assert "mueller.vorlesezeit.app" in capsys.readouterr().err


def test_scheitert_tunnel_abraeumen_bleibt_der_urspruengliche_fehler(cf, merkliste, capsys):
    cf.scheitert.update({"tunnel_token", "tunnel_loeschen"})
    with pytest.raises(CloudflareError, match="tunnel_token: kaputt"):
        neu("mueller", cf=cf, merkliste=merkliste, smtp_login=None)
    assert cf.dns == {}
    assert "vorlesezeit-mueller" in capsys.readouterr().err


def test_liste_zeigt_verbindungen_und_warnt_bei_mehreren(cf, merkliste):
    for name in ("anna", "ben", "carl"):
        neu(name, cf=cf, merkliste=merkliste, smtp_login=None)
    ids = {n: e["tunnel_id"] for n, e in json.loads(merkliste.read_text()).items()}
    cf.geraete[ids["ben"]] = 1
    cf.geraete[ids["carl"]] = 2
    zeilen = liste(cf=cf, merkliste=merkliste)
    assert len(zeilen) == 3
    assert "nicht verbunden" in zeilen[0]
    assert zeilen[1].endswith("verbunden") and "nicht" not in zeilen[1]
    assert "WARNUNG" in zeilen[2] and "2 Geräte" in zeilen[2]


def test_liste_leer(cf, merkliste):
    assert liste(cf=cf, merkliste=merkliste) == ["Noch keine Einladungen."]


def test_widerrufen_loescht_nur_eigenes(cf, merkliste):
    neu("mueller", cf=cf, merkliste=merkliste, smtp_login=None)
    cf.dns["txt"] = DnsEintrag("txt", "TXT", "mueller.vorlesezeit.app", "fremd")
    cf.dns["adv"] = DnsEintrag("adv", "CNAME", "advent.vorlesezeit.app", "x.cfargotunnel.com")
    widerrufen("mueller", cf=cf, merkliste=merkliste)
    assert cf.tunnel == {}
    assert set(cf.dns) == {"txt", "adv"}
    assert json.loads(merkliste.read_text()) == {}


def test_widerrufen_bei_aktiver_verbindung_ist_wiederholbar(cf, merkliste):
    neu("mueller", cf=cf, merkliste=merkliste, smtp_login=None)
    cf.scheitert.add("tunnel_loeschen")
    with pytest.raises(CloudflareError):
        widerrufen("mueller", cf=cf, merkliste=merkliste)
    assert cf.dns == {}
    assert "mueller" in json.loads(merkliste.read_text())
    cf.scheitert.clear()
    widerrufen("mueller", cf=cf, merkliste=merkliste)
    assert cf.tunnel == {} and json.loads(merkliste.read_text()) == {}


def test_widerrufen_unbekannt(cf, merkliste):
    with pytest.raises(EinladenError, match="keine Einladung"):
        widerrufen("niemand", cf=cf, merkliste=merkliste)


def test_main_ohne_umgebung_nennt_nur_namen(monkeypatch, capsys):
    for var in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ZONE_ID"):
        monkeypatch.delenv(var, raising=False)
    assert cli.main(["liste"]) == 2
    err = capsys.readouterr().err
    assert "CLOUDFLARE_API_TOKEN" in err and "CLOUDFLARE_ZONE_ID" in err


def test_main_widerrufen_bei_aktiver_verbindung_ohne_traceback(cf, merkliste, monkeypatch, capsys):
    neu("mueller", cf=cf, merkliste=merkliste, smtp_login=None)
    cf.scheitert.add("tunnel_loeschen")
    monkeypatch.setattr(cli, "_cloudflare", lambda: cf)
    monkeypatch.setattr(cli, "MERKLISTE", merkliste)
    assert cli.main(["widerrufen", "mueller"]) == 1
    err = capsys.readouterr().err
    assert "erneut ausführen" in err
    assert "Traceback" not in err


def test_main_testen_mit_kaputtem_code(monkeypatch, capsys):
    def kaputt():
        raise EinladungscodeError("Der Einladungscode ist unvollständig.")

    monkeypatch.setattr(cli, "testen", kaputt)
    assert cli.main(["testen"]) == 1
    assert "unvollständig" in capsys.readouterr().err


def _code():
    return encode(Einladung(host="probe.vorlesezeit.app", tunnel_token="geheim-token", smtp=None))


def test_testen_startet_cloudflared_ohne_token_in_argumenten_und_stoppt_ihn():
    aufrufe = []

    def run(args, **kw):
        aufrufe.append((args, kw.get("env", {}).get("TUNNEL_TOKEN")))

    antworten = iter([None, None, "Europe/Berlin"])
    ok = cli.testen(
        frage_code=lambda p: _code(),
        run=run,
        hole_zeitzone=lambda url: next(antworten),
        warte=lambda s: None,
    )
    assert ok is True
    start, stopp = aufrufe[0], aufrufe[-1]
    assert start[0][:2] == ["docker", "run"] and cli.CLOUDFLARED_IMAGE in start[0]
    assert "geheim-token" not in " ".join(start[0])
    assert start[1] == "geheim-token"
    assert stopp[0][:2] == ["docker", "stop"]


def test_testen_ohne_antwort_meldet_fehlschlag_und_stoppt():
    aufrufe = []
    ok = cli.testen(
        frage_code=lambda p: _code(),
        run=lambda a, **kw: aufrufe.append(a),
        hole_zeitzone=lambda url: None,
        warte=lambda s: None,
        versuche=3,
    )
    assert ok is False
    assert aufrufe[-1][:2] == ["docker", "stop"]
