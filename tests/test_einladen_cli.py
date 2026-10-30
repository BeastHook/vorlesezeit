"""One-Klick-Setup Baustein 2: Einladungsskript gegen ein Cloudflare-Double."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.einladungscode import Einladung, EinladungscodeError, decode, encode
from scripts.einladen import cli
from scripts.einladen.cli import EinladenError, liste, neu, pruefe_name, widerrufen
from scripts.einladen.cloudflare import CloudflareError, DnsEintrag, Mailregel, Zieladresse

ADMIN = "freund@example.org"


class FakeCloudflare:
    def __init__(self) -> None:
        self.dns: dict[str, DnsEintrag] = {}
        self.tunnel: dict[str, dict] = {}
        self.geraete: dict[str, int] = {}
        self.adressen: list[Zieladresse] = [Zieladresse("a0", ADMIN, True)]
        self.regeln: dict[str, Mailregel] = {}
        self.aenderungen: list[str] = []
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

    def zieladressen(self):
        self._pruefe("zieladressen")
        return list(self.adressen)

    def mailregeln(self):
        self._pruefe("mailregeln")
        return list(self.regeln.values())

    def mailregel_anlegen(self, adresse, ziel):
        self.aenderungen.append("mailregel_anlegen")
        self._pruefe("mailregel_anlegen")
        mid = self._id("m")
        self.regeln[mid] = Mailregel(mid, adresse, (ziel,))
        # Cloudflare hat die Regel schon angelegt, die Antwort geht verloren.
        self._pruefe("mailregel_anlegen_nach_anlage")
        return mid

    def mailregel_loeschen(self, regel_id):
        self._pruefe("mailregel_loeschen")
        del self.regeln[regel_id]

    def tunnel_anlegen(self, name):
        self.aenderungen.append("tunnel_anlegen")
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
        ADMIN,
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
    [regel] = cf.regeln.values()
    assert (regel.an, regel.ziele) == ("mueller@vorlesezeit.app", (ADMIN,))


def test_merkliste_ohne_geheimnisse(cf, merkliste):
    code = neu(
        "mueller", ADMIN, cf=cf, merkliste=merkliste, smtp_login="l", frage_schluessel=_schluessel
    )
    text = merkliste.read_text()
    eintrag = json.loads(text)["mueller"]
    assert set(eintrag) == {"host", "tunnel_id", "dns_id", "admin_mail", "regel_id", "angelegt"}
    assert eintrag["admin_mail"] == ADMIN
    assert eintrag["regel_id"] in cf.regeln
    assert "token-" not in text and "xsmtpsib" not in text and code not in text


def test_name_wird_klein_geschrieben(cf, merkliste):
    neu("Mueller", ADMIN, cf=cf, merkliste=merkliste, smtp_login=None)
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
            ADMIN,
            cf=cf,
            merkliste=merkliste,
            smtp_login="l",
            frage_schluessel=lambda p: gefragt.append(p) or "k",
        )
    assert cf.tunnel == {}
    assert gefragt == []
    assert list(cf.dns) == ["x"]


def test_doppelter_name_in_der_merkliste(cf, merkliste):
    neu("mueller", ADMIN, cf=cf, merkliste=merkliste, smtp_login=None)
    cf.dns.clear()
    with pytest.raises(EinladenError, match="gibt es schon"):
        neu("mueller", ADMIN, cf=cf, merkliste=merkliste, smtp_login=None)


def test_abbruch_beim_schluessel_legt_nichts_an(cf, merkliste):
    def abbruch(_prompt):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        neu("mueller", ADMIN, cf=cf, merkliste=merkliste, smtp_login="l", frage_schluessel=abbruch)
    assert cf.tunnel == {} and cf.dns == {} and not merkliste.exists()


def test_leerer_schluessel_heisst_ohne_mail(cf, merkliste):
    code = neu(
        "mueller", ADMIN, cf=cf, merkliste=merkliste, smtp_login="l", frage_schluessel=lambda p: " "
    )
    assert decode(code).smtp is None


def test_ohne_smtp_login_keine_frage(cf, merkliste):
    code = neu(
        "mueller",
        ADMIN,
        cf=cf,
        merkliste=merkliste,
        smtp_login=None,
        frage_schluessel=lambda p: pytest.fail("nicht fragen"),
    )
    assert decode(code).smtp is None


@pytest.mark.parametrize(
    "schritt",
    [
        "weiterleitung_setzen",
        "cname_anlegen",
        "mailregel_anlegen",
        "mailregel_anlegen_nach_anlage",
        "tunnel_token",
    ],
)
def test_fehler_nach_dem_tunnel_raeumt_alles_ab(cf, merkliste, schritt):
    cf.scheitert.add(schritt)
    with pytest.raises(CloudflareError):
        neu("mueller", ADMIN, cf=cf, merkliste=merkliste, smtp_login=None)
    assert cf.tunnel == {} and cf.dns == {} and cf.regeln == {} and not merkliste.exists()


def test_cname_angelegt_aber_antwort_verloren_wird_abgeraeumt(cf, merkliste):
    cf.scheitert.add("cname_anlegen_nach_anlage")
    with pytest.raises(CloudflareError):
        neu("mueller", ADMIN, cf=cf, merkliste=merkliste, smtp_login=None)
    assert cf.dns == {} and cf.tunnel == {} and not merkliste.exists()


def test_scheitert_dns_abraeumen_wird_tunnel_trotzdem_geloescht(cf, merkliste, capsys):
    cf.scheitert.update({"tunnel_token", "dns_loeschen"})
    with pytest.raises(CloudflareError, match="tunnel_token: kaputt"):
        neu("mueller", ADMIN, cf=cf, merkliste=merkliste, smtp_login=None)
    assert cf.tunnel == {}
    assert "mueller.vorlesezeit.app" in capsys.readouterr().err


def test_scheitert_tunnel_abraeumen_bleibt_der_urspruengliche_fehler(cf, merkliste, capsys):
    cf.scheitert.update({"tunnel_token", "tunnel_loeschen"})
    with pytest.raises(CloudflareError, match="tunnel_token: kaputt"):
        neu("mueller", ADMIN, cf=cf, merkliste=merkliste, smtp_login=None)
    assert cf.dns == {}
    assert "vorlesezeit-mueller" in capsys.readouterr().err


def test_liste_zeigt_verbindungen_und_warnt_bei_mehreren(cf, merkliste):
    for name in ("anna", "ben", "carl"):
        neu(name, ADMIN, cf=cf, merkliste=merkliste, smtp_login=None)
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
    neu("mueller", ADMIN, cf=cf, merkliste=merkliste, smtp_login=None)
    cf.dns["txt"] = DnsEintrag("txt", "TXT", "mueller.vorlesezeit.app", "fremd")
    cf.dns["adv"] = DnsEintrag("adv", "CNAME", "advent.vorlesezeit.app", "x.cfargotunnel.com")
    widerrufen("mueller", cf=cf, merkliste=merkliste)
    assert cf.tunnel == {}
    assert set(cf.dns) == {"txt", "adv"}
    assert json.loads(merkliste.read_text()) == {}


def test_widerrufen_bei_aktiver_verbindung_ist_wiederholbar(cf, merkliste):
    neu("mueller", ADMIN, cf=cf, merkliste=merkliste, smtp_login=None)
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
    neu("mueller", ADMIN, cf=cf, merkliste=merkliste, smtp_login=None)
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


def test_fremde_mailregeln_bleiben_beim_abraeumen(cf, merkliste):
    cf.regeln["fremd"] = Mailregel("fremd", "advent@vorlesezeit.app", (ADMIN,))
    cf.scheitert.add("tunnel_token")
    with pytest.raises(CloudflareError):
        neu("mueller", ADMIN, cf=cf, merkliste=merkliste, smtp_login=None)
    assert set(cf.regeln) == {"fremd"}


def test_scheitert_mailregel_abraeumen_wird_der_rest_trotzdem_abgeraeumt(cf, merkliste, capsys):
    cf.scheitert.update({"tunnel_token", "mailregel_loeschen"})
    with pytest.raises(CloudflareError, match="tunnel_token: kaputt"):
        neu("mueller", ADMIN, cf=cf, merkliste=merkliste, smtp_login=None)
    assert cf.tunnel == {} and cf.dns == {}
    assert "Mailregel mueller@vorlesezeit.app" in capsys.readouterr().err


def _neu_mit_frage(cf, merkliste, admin=ADMIN):
    gefragt = []
    neu(
        "mueller",
        admin,
        cf=cf,
        merkliste=merkliste,
        smtp_login="l",
        frage_schluessel=lambda p: gefragt.append(p) or "k",
    )
    return gefragt


def test_fehlende_zieladresse_bricht_vor_jeder_aenderung_ab(cf, merkliste):
    cf.adressen = [Zieladresse("a1", "jemand@example.org", True)]
    with pytest.raises(EinladenError, match="fehlt in Cloudflare") as exc:
        _neu_mit_frage(cf, merkliste)
    assert ADMIN in str(exc.value)
    assert cf.aenderungen == [] and not merkliste.exists()


def test_unbestaetigte_zieladresse_bricht_vor_jeder_aenderung_ab(cf, merkliste):
    cf.adressen = [Zieladresse("a0", ADMIN, False)]
    with pytest.raises(EinladenError, match="nicht bestätigt") as exc:
        _neu_mit_frage(cf, merkliste)
    assert ADMIN in str(exc.value)
    assert cf.aenderungen == [] and not merkliste.exists()


def test_zieladresse_ohne_ruecksicht_auf_gross_und_kleinschreibung(cf, merkliste):
    cf.adressen = [Zieladresse("a0", "Freund@Example.org", True)]
    _neu_mit_frage(cf, merkliste, admin=" freund@example.ORG ")
    [regel] = cf.regeln.values()
    assert regel.ziele == ("freund@example.org",)


def test_zieladresse_wird_vor_dem_schluessel_geprueft(cf, merkliste):
    cf.adressen = []
    gefragt = []
    with pytest.raises(EinladenError):
        neu(
            "mueller",
            ADMIN,
            cf=cf,
            merkliste=merkliste,
            smtp_login="l",
            frage_schluessel=lambda p: gefragt.append(p) or "k",
        )
    assert gefragt == []


@pytest.mark.parametrize("admin", ["", "  ", "kein-at-zeichen", "a@", "@b.de", "a b@c.de"])
def test_unzulaessige_admin_mail(cf, merkliste, admin):
    with pytest.raises(EinladenError, match="Admin-Mail"):
        _neu_mit_frage(cf, merkliste, admin=admin)
    assert cf.aenderungen == []


def test_bestehende_mailregel_verhindert_alles(cf, merkliste):
    cf.regeln["alt"] = Mailregel("alt", "mueller@vorlesezeit.app", ("jemand@example.org",))
    with pytest.raises(EinladenError, match="schon eine Mailregel"):
        _neu_mit_frage(cf, merkliste)
    assert cf.aenderungen == [] and set(cf.regeln) == {"alt"}


def test_widerrufen_entfernt_nur_die_eigene_mailregel(cf, merkliste):
    neu("mueller", ADMIN, cf=cf, merkliste=merkliste, smtp_login=None)
    cf.regeln["adv"] = Mailregel("adv", "advent@vorlesezeit.app", (ADMIN,))
    cf.regeln["um"] = Mailregel("um", "mueller@vorlesezeit.app", ("anders@example.org",))
    widerrufen("mueller", cf=cf, merkliste=merkliste)
    assert set(cf.regeln) == {"adv", "um"}
    assert json.loads(merkliste.read_text()) == {}


def test_widerrufen_alter_eintrag_ohne_admin_mail(cf, merkliste):
    neu("mueller", ADMIN, cf=cf, merkliste=merkliste, smtp_login=None)
    daten = json.loads(merkliste.read_text())
    del daten["mueller"]["admin_mail"], daten["mueller"]["regel_id"]
    merkliste.write_text(json.dumps(daten))
    cf.regeln.clear()
    cf.scheitert.add("mailregeln")
    widerrufen("mueller", cf=cf, merkliste=merkliste)
    assert cf.tunnel == {} and json.loads(merkliste.read_text()) == {}


def test_widerrufen_scheitert_mailregel_bleibt_wiederholbar(cf, merkliste):
    neu("mueller", ADMIN, cf=cf, merkliste=merkliste, smtp_login=None)
    cf.scheitert.add("mailregel_loeschen")
    with pytest.raises(CloudflareError):
        widerrufen("mueller", cf=cf, merkliste=merkliste)
    assert "mueller" in json.loads(merkliste.read_text())
    cf.scheitert.clear()
    widerrufen("mueller", cf=cf, merkliste=merkliste)
    assert cf.regeln == {} and cf.tunnel == {} and json.loads(merkliste.read_text()) == {}


def test_main_neu_verlangt_admin_mail(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["neu", "mueller"])
    assert exc.value.code == 2
    assert "admin_mail" in capsys.readouterr().err


def test_main_neu_reicht_admin_mail_durch(cf, merkliste, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_cloudflare", lambda: cf)
    monkeypatch.setattr(cli, "MERKLISTE", merkliste)
    monkeypatch.delenv("BREVO_SMTP_LOGIN", raising=False)
    assert cli.main(["neu", "mueller", ADMIN]) == 0
    [regel] = cf.regeln.values()
    assert regel.ziele == (ADMIN,)
    assert "VZ1-" in capsys.readouterr().out


def test_main_neu_ohne_zieladresse_meldet_klar(cf, merkliste, monkeypatch, capsys):
    cf.adressen = []
    monkeypatch.setattr(cli, "_cloudflare", lambda: cf)
    monkeypatch.setattr(cli, "MERKLISTE", merkliste)
    assert cli.main(["neu", "mueller", ADMIN]) == 1
    err = capsys.readouterr().err
    assert "fehlt in Cloudflare" in err and "Traceback" not in err
