# Betrieb auf dem Heimserver

Anleitung für den Admin. Grundlage: Plan-Einheiten U14 und U15, KTD11, KTD14,
KTD15, KTD19 des Hauptplans, für Setup, Zugangsdaten und mehrere Kalender
`docs/plans/2026-10-01-2242-feat-setup-mehrkalender-plan.md`. Der Aufbau steht
in `docker-compose.heimserver.yml`.

## Überblick

| Dienst | Aufgabe |
|---|---|
| `app` | Vorlesezeit, nur im internen Compose-Netz, Gesundheitsprüfung gegen `/status` |
| `s3` | Objektspeicher für die Aufnahmen (versitygw), nur intern; legt den Bucket beim Start als Verzeichnis an |
| `cloudflared` | Cloudflare Tunnel, der einzige Weg von außen zur App |
| `scheduler` | ruft alle 5 Minuten zwischen 17:00 und 01:45 (Europe/Berlin) `/delivery/trigger` auf, entscheidet selbst nichts |
| `backup` | sichert jede Nacht um 00:30 Datenbank und Aufnahmen auf das zweite Medium; verpasst es den Lauf (Ruhezustand, Neustart), holt es ihn nach dem Aufwachen nach |

Kein Dienst veröffentlicht einen Port, am Router wird nichts freigegeben.
Das Compose-Projekt heißt `vorlesezeit-heimserver`. Datenbank, Speicher und
Sicherungsmarke liegen in externen Volumes: Datenbank und Marke heißen aus der
Zeit vor der Umbenennung weiter `toniapply-heimserver_db-data` und
`…_backup-marker`, die Aufnahmen liegen seit dem Wechsel zu versitygw in
`vorlesezeit-heimserver_s3-data`; auch die Datenbankdatei heißt weiter
`/data/toniapply.db`. Die Demo-Daten des Entwicklungsaufbaus (Projekt
`vorlesezeit`) gehen dadurch nicht mit.

## Gerät wählen

Die App läuft auf einem der drei Geräte, nie auf dem Router selbst.

**Mac**
- Docker Desktop: „Start Docker Desktop when you sign in“ einschalten.
- Automatische Anmeldung des Benutzers einschalten. Das geht nur mit
  **ausgeschaltetem FileVault**. Mit FileVault wartet der Mac nach jedem
  Neustart am Entsperrbildschirm, und Docker Desktop läuft nur in einer
  angemeldeten Sitzung.
- Energie: Ruhezustand aus, „Nach Stromausfall automatisch starten“ an.
- Ein Laptop muss den ganzen Advent zu Hause, am Strom und aufgeklappt
  bleiben. Trifft das oder das FileVault-Kriterium nicht zu, scheidet der Mac
  aus.

**Mini-PC mit Linux**
- Docker Engine mit Compose-Plugin, Dienst aktiviert
  (`sudo systemctl enable --now docker`).
- Automatische Sicherheitsupdates (z. B. `unattended-upgrades`).
- Im BIOS/UEFI „Power on after power loss“ bzw. „Restore on AC power loss“
  einschalten.

**NAS**
- Als Projekt im Container-Manager des NAS anlegen (Compose-Datei dieses
  Repositorys).
- Volumes auf einem lokalen Datenträger des NAS, nie auf einer
  Netzwerkfreigabe: SQLite im WAL-Modus braucht ein lokales Dateisystem.
- CPU-Architektur prüfen (`uname -m`) und das Image auf dem NAS selbst bauen
  (`--build`), nicht von einem Rechner mit anderer Architektur übertragen.

Das Sicherungsziel (`BACKUP_TARGET_PATH`) ist ein **zweites Medium**: eine
externe Platte, eine NAS-Freigabe oder ein Time-Machine-Pfad. Nicht dieselbe
Platte, auf der die Volumes liegen.

## Cloudflare einrichten

Alles läuft im kostenlosen Free-Plan. Die Menüpfade können sich in der
Cloudflare-Oberfläche verschieben; die Bezeichnungen der Einstellungen bleiben
dieselben.

**Was am Ende feststehen muss** (und was davon wohin gehört):

| Was | Wohin |
|---|---|
| die Domain, z. B. `familie-beispiel.de` | nur Absprache, kein Geheimnis |
| der Hostname der App, z. B. `advent.familie-beispiel.de` | nur Absprache, kein Geheimnis |
| der Tunnel-Token | **nur** in `.env.heimserver` als `CLOUDFLARE_TUNNEL_TOKEN`, nie in den Chat, nie ins Repo |

### 1. Konto und Domain

1. Kostenloses Konto auf `dash.cloudflare.com` anlegen, mit
   Zwei-Faktor-Anmeldung.
2. Domain besorgen, eine von beiden Möglichkeiten:
   - neu über **Domain Registration → Register Domains** bei Cloudflare
     kaufen (Selbstkostenpreis, meist ca. 10 € im Jahr für `.de`/`.com`), oder
   - vorhandene Domain unter **Add a site / Add a domain** eintragen, Plan
     **Free** wählen und beim bisherigen Anbieter die zwei angezeigten
     Cloudflare-Nameserver eintragen. Bis die Domain in Cloudflare als
     **Active** erscheint, kann es einige Stunden dauern.

### 2. Tunnel anlegen

1. **Zero Trust** öffnen (beim ersten Mal einen Team-Namen vergeben und den
   Free-Plan wählen; eine Zahlungsmethode wird ggf. abgefragt, belastet wird
   nichts).
2. **Networks → Tunnels → Create a tunnel**, Typ **Cloudflared**, Name z. B.
   `vorlesezeit`.
3. Auf der Seite „Install and run a connector“ **nichts installieren**. Aus
   dem angezeigten Befehl nur den langen Token nach `--token` kopieren und
   auf dem Heimserver in `.env.heimserver` als `CLOUDFLARE_TUNNEL_TOKEN=…`
   eintragen. Den Connector startet unser Compose-Aufbau selbst.
4. Weiter zu **Public Hostnames → Add a public hostname**:
   - Subdomain `advent`, Domain aus Schritt 1
   - Service **Type** `HTTP`, **URL** `app:8000`
   - keine weiteren Optionen setzen.
5. Speichern. Der Tunnel steht auf **Healthy**, sobald der Heimserver-Aufbau
   läuft (`docker compose … up -d`).

**Nicht einschalten:** Cloudflare Access / Zero-Trust-Anmeldung vor dem
Hostnamen. Sie würde die Verwandten und cron-job.org aussperren; die App
hat ihre eigene Anmeldung per Magic Link.

### 3. Einstellungen der Domain

1. **SSL/TLS → Edge Certificates**: **Always Use HTTPS** an. Damit geht nie
   eine Sitzung über unverschlüsseltes HTTP (das Sitzungs-Cookie trägt noch
   kein Secure-Flag).
2. **Security → Bots**: **Bot Fight Mode aus**. Im Free-Plan gilt der
   Schalter für die ganze Domain; eingeschaltet prallt cron-job.org an einer
   Challenge ab, und die Wache meldet Fehlalarme.
3. **Rules → Configuration Rules → Create rule**, Name `vorlesezeit-automatik`:
   - Bedingung: **URI Path** *equals* `/delivery/trigger` **or** **URI Path**
     *equals* `/backup/check`
   - Einstellung: **Browser Integrity Check** aus.
4. Empfohlen, **Security → WAF → Rate limiting rules** (im Free-Plan ist eine
   Regel frei):
   - Bedingung: **URI Path** *equals* `/login` **and** **Request Method**
     *equals* `POST`
   - Grenze: 10 Anfragen je 1 Minute und IP, Aktion **Block** für 10 Minuten.

   Das verhindert, dass jemand über das Login-Formular das Tageslimit des
   Mailanbieters leer schickt und damit die Abendmeldung abschneidet.

Sonst nichts ändern: keine Page Rules zum Cachen, kein „Cache Everything“.
Die Audio-Adressen der App enden nicht auf `.mp3` und werden von Cloudflare
deshalb ohnehin nicht zwischengespeichert. Uploads bis 50 MB liegen unter der
Grenze des Free-Plans von 100 MB.

### 4. Prüfen

Aus einem fremden Netz, z. B. dem Handy im Mobilfunknetz:

- `https://advent.<domain>/status` liefert `"timezone":"Europe/Berlin"`.
- `http://advent.<domain>/status` leitet auf `https://` um.
- Ein Magic Link aus einer echten Einladungsmail beginnt mit
  `https://advent.<domain>/`.

## Umgebungsvariablen

Eine eigene Datei `.env.heimserver` im Repository-Verzeichnis auf dem Gerät,
nie committen (die Namen stehen in `.env.example`). Fehlt ein Pflichtwert,
bricht `docker compose` mit einer Meldung wie
`required variable SESSION_SECRET_KEY is missing a value` ab.

| Variable | Pflicht | Inhalt |
|---|---|---|
| `SESSION_SECRET_KEY` | ja | langer Zufallswert, z. B. `openssl rand -hex 32` |
| `TRIGGER_SECRET` | ja | langer Zufallswert, auch für cron-job.org |
| `STORAGE_ACCESS_KEY`, `STORAGE_SECRET_KEY` | ja | Zugang zum Objektspeicher (beliebiger Name) und langes Passwort |
| `STORAGE_BUCKET` | ja | Bucketname, z. B. `vorlesezeit` |
| `ADMIN_EMAIL` | ja | Admin-Adresse |
| `CREDENTIALS_KEY` | dringend empfohlen | Schlüssel für die Passwörter in der Datenbank, siehe „Schlüssel für die Zugangsdaten“ |
| `CLOUDFLARE_TUNNEL_TOKEN` | ja | aus Schritt 1 oben |
| `BACKUP_TARGET_PATH` | ja | absoluter Pfad auf dem zweiten Medium |
| `SMTPUSER`, `SMTPPW` | nein, nur Startwert | SMTP-Zugang des Mailanbieters (z. B. Brevo) |
| `TONIE_USERNAME`, `TONIE_PASSWORD` | nein, nur Startwert | Toniecloud-Konto |
| `TONIE_DELIVERY_TIME` | nein, nur Startwert | Vorgabe `20:00`, zulässig 17:00–23:00 |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM_ADDRESS`, `MAGIC_LINK_VALID_UNTIL`, `RECORDING_DEADLINE`, `INVITATION_DATE`, `ADMIN_DISPLAY_NAME` | nein, nur Startwert | Vorgaben wie in `.env.example` |

**Startwerte.** Alles mit „nur Startwert“ pflegt der Admin im Reiter
**Setup**. Ein gesetzter Wert aus der Umgebung wird je Feld genau einmal in
die Datenbank übernommen (beim ersten Start, der ihn sieht) und gilt sofort;
danach gilt nur noch das Setup. Ein Neustart holt den Wert aus der Umgebung
nicht zurück, auch nicht, wenn er im Setup geleert oder ein Konto gelöscht
wurde. Fehlen SMTP- und tonies-Werte ganz, startet die App trotzdem (die
frühere `:?`-Pflicht in der Compose-Datei ist entfallen); eingerichtet wird
dann über den Einrichtungslink unten. Nach der Übernahme dürfen die Werte aus
`.env.heimserver` entfernt werden.

Nie die Entwicklungswerte `vorlesezeit`, `vorlesezeit-dev-secret` oder
`vorlesezeit-dev-session-secret` verwenden: wer den eingecheckten
Sitzungsschlüssel kennt, fälscht eine Admin-Sitzung. Werte nie in den Chat
oder in Logs kopieren; zur Fehlersuche genügt die Meldung von compose bzw.
der `ConfigError` der App, die nur Namen nennen.

## Schlüssel für die Zugangsdaten

`CREDENTIALS_KEY` verschlüsselt die Passwörter der tonies-Konten und des
SMTP-Zugangs in der Datenbank. Die nächtliche Sicherung enthält die Datenbank
unverschlüsselt; ohne den Schlüssel sind die Passwörter darin wertlos.

Einmal erzeugen, auf dem Heimserver im Repository-Verzeichnis:

```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Ohne lokales Python (gleiches Format: 32 Zufallsbytes, URL-sicheres Base64):

```
openssl rand -base64 32 | tr '+/' '-_'
```

Der Weg über den App-Container funktioniert erst, wenn dort schon die
Mehrkalender-Version läuft (ältere Images enthalten `cryptography` nicht). Die Ausgabe in `.env.heimserver` als `CREDENTIALS_KEY=…` eintragen und
**getrennt von der Sicherung** aufbewahren, z. B. im Passwortmanager, nie auf
dem Sicherungsmedium, nie in den Chat.

Fehlt der Schlüssel oder passt er nicht (anderer Schlüssel, Rücksicherung auf
einem Gerät ohne den alten Schlüssel), startet die App trotzdem, meldet sich
mit den betroffenen Zugangsdaten nirgends an und verlangt, sie neu
einzugeben. Ein neuer Schlüssel heißt also immer: alle Passwörter im Setup
neu eingeben. Einen Schlüsselwechsel über die Oberfläche gibt es nicht.

## Einrichtungslink

Ist kein nutzbarer SMTP-Zugang da (keiner eingetragen, nicht entschlüsselbar,
oder die letzte Mail scheiterte), kann der Admin keinen Magic Link bekommen.
Dann schreibt die App **bei jedem Start** einen Einrichtungslink ins Log:

```
$DC logs app | grep einrichtung
```

Die Zeile nennt nur den Pfad `/einrichtung/<token>`; ihn an die eigene
Adresse hängen, also `https://advent.<domain>/einrichtung/<token>`, und im
Browser öffnen. Der Link führt als Admin direkt ins Setup, gilt 24 Stunden
und nur einmal; jeder neue Start entwertet ältere Links. Abgelaufen: App neu
starten (`$DC restart app`) und den neuen Link aus dem Log nehmen. Den Link
nie in den Chat kopieren, er ist bis zur Verwendung ein Admin-Zugang.

## Zugangsdaten „neu eingeben“

`https://advent.<domain>/status` nennt unter `neu_eingeben` die Zugangsdaten,
die die App nicht lesen kann: `smtp_password` bzw. `tonie_konto:<Nr.>`, nie
Werte. Ist die Liste nicht leer, im Reiter **Setup** das SMTP-Passwort bzw.
das Passwort des genannten tonies-Kontos neu eingeben und prüfen lassen.
Läufe auf Tonies dieses Kontos scheitern bis dahin mit benanntem Grund
(Verlaufseintrag und Meldung), ohne Anmeldeversuch. Ist SMTP betroffen, kommt
man über den Einrichtungslink oben ins Setup.

## Reiter Setup

Im Admin-Bereich pflegt der Reiter **Setup**: Kalender (anlegen, umbenennen),
tonies-Konten mit ihren Creative Tonies und deren Zuordnung zu Kalendern,
SMTP-Zugang und Absender, Lieferzeit, Aufnahmefrist, Einladungstermin,
Gültigkeit der Anmeldelinks und den Anzeigenamen des Admins. Jede Änderung
gilt ohne Neustart. Zugangsdaten werden vor dem Speichern mit einer
Test-Anmeldung geprüft. Eine geänderte Lieferzeit gilt bei schon offenem
Zeitfenster erst ab dem nächsten Abend.

## Generalprobe

`docker compose exec app python -m scripts.rehearsal list-tonies` listet die
im Setup angelegten Tonies mit Index, gekürzter Kennung und Kalender. Alle
weiteren Schritte (`setup`, `fill`, `run`, `restore-baseline --from <pfad>`)
nehmen `--tonie-index N`, nie die volle Kennung. Die Zugangsdaten kommen aus
dem tonies-Konto des Tonies. `fill` braucht einen eigenen, leeren Kalender:
jeder Vorabend-Lauf der Probe macht den Kalendertag fest. `run` **außerhalb
des Zeitplanfensters 17:00–01:45** fahren oder vorher den Zeitplan-Container
stoppen (`$DC stop scheduler`, danach `$DC start scheduler`): das Skript
läuft in eigenem Prozess und teilt die Sperre je Tonie nicht mit der App.

## Starten, stoppen, aktualisieren

Kurzform: `DC="docker compose -f docker-compose.heimserver.yml --env-file .env.heimserver"`

| Zweck | Kommando |
|---|---|
| Erster Start / Update | `git pull && $DC up -d --build` |
| Zustand | `$DC ps` (App soll `healthy` sein) |
| Logs | `$DC logs -f app scheduler backup` |
| Stoppen (Daten bleiben) | `$DC stop` bzw. `$DC down` |
| Konfiguration prüfen | `$DC config -q` (nur Exit-Code, gibt keine Werte aus) |

**Umstellung auf die Mehrkalender-Version** (einmalig): vor dem ersten Start
der neuen Version eine Sicherung auslösen (`$DC exec backup python
deploy/backup/backup.py --once`, siehe unten) und `CREDENTIALS_KEY` eintragen.
Die Umstellung der Datenbank ist additiv und läuft beim Start. Das alte Image
ist danach **kein sicherer Rückweg** mehr: nach den ersten Läufen der neuen
Version hielte es frisch aufgespielte App-Kapitel für Bestand. Der einzige
Rückweg ist die Sicherung von vor der Umstellung.

**Auf einem neuen Gerät** vor dem ersten Start die drei externen Volumes
anlegen (compose tut das nicht):
`for v in db-data backup-marker; do docker volume create toniapply-heimserver_$v; done; docker volume create vorlesezeit-heimserver_s3-data`

**Umstellung MinIO → versitygw** (einmalig, Oktober 2026): die MinIO-Images
sind nicht mehr öffentlich ladbar. Die Aufnahmen werden einmal vom alten
MinIO-Volume `toniapply-heimserver_minio-data` in das neue
`vorlesezeit-heimserver_s3-data` kopiert (Sicherung vorher, App gestoppt,
Anzahl und Prüfsummen verglichen). Das alte Volume und das MinIO-Image
bleiben als Rückweg liegen: alte `docker-compose.heimserver.yml` aus git, dann
`$DC up -d`. **Kein** `docker image prune`, solange dieser Rückweg gebraucht
wird.

**Niemals `down -v`** auf dem Heimserver: das löscht Datenbank und Aufnahmen.
Alle Dienste haben `restart: unless-stopped` und kommen nach einem Neustart
des Geräts von selbst wieder, sobald Docker läuft.

## Freunde einladen

Einladungscodes geben einem Freund die Adresse `<name>.vorlesezeit.app` und,
wenn gewünscht, den Mailversand über Brevo. Das Skript läuft auf deinem Mac,
nicht im Container.

Einmalig in Cloudflare unter **My Profile → API Tokens** ein Token anlegen:
Konto „Cloudflare Tunnel: Bearbeiten", Zone `vorlesezeit.app` „DNS:
Bearbeiten". In der Shell (nie in den Chat):

    export CLOUDFLARE_API_TOKEN=…   CLOUDFLARE_ACCOUNT_ID=…   CLOUDFLARE_ZONE_ID=…
    export BREVO_SMTP_LOGIN=…        # SMTP-Login aus Brevo, optional

Je Freund in Brevo unter **SMTP & API → SMTP** einen eigenen SMTP-Schlüssel
erzeugen (Name = Freund). Dann:

    python -m scripts.einladen neu mueller      # fragt den Schlüssel verdeckt ab
    docker compose up -d                         # Entwicklungs-Stack für den Test
    python -m scripts.einladen testen            # Code einfügen, prüft die Adresse
    python -m scripts.einladen liste             # wer ist verbunden?
    python -m scripts.einladen widerrufen mueller

`liste` warnt, wenn mehr als ein Gerät mit demselben Code verbunden ist.
Nach `widerrufen` den Brevo-Schlüssel im Dashboard löschen. Die Merkliste
`~/.vorlesezeit/einladungen.json` enthält keine Geheimnisse.

## Sicherung und Rücksicherung

Jede Nacht um 00:30 (verpasste Läufe holt die Sicherung nach dem Aufwachen bzw.
Neustart nach; scheitert ein Lauf, versucht sie es alle 30 Minuten erneut):
1. Datenbank über die Online-Sicherung von SQLite nach
   `<Ziel>/db/vorlesezeit-<UTC-Zeit>.db`. Die letzten **sieben Stände** bleiben;
   Stände von vor der Umbenennung heißen `toniapply-<UTC-Zeit>.db` und zählen mit.
2. Der Bucket wird nach `<Ziel>/objects/` gespiegelt, **einschließlich
   Löschungen**.
3. Erst danach schreibt die Sicherung ihre Zeitmarke.

Löschfrist (R40): eine gelöschte Aufnahme verschwindet mit der nächsten
Sicherungsnacht aus der Spiegelung. Ihre Datenbankzeile (Titel, Person,
Tag) steckt noch in älteren Datenbankständen und ist spätestens nach sieben
Nächten, wenn der letzte dieser Stände herausrotiert, ganz aus der Sicherung
verschwunden.

Sicherung sofort auslösen:
`$DC exec backup python deploy/backup/backup.py --once`

**Rücksicherung** (nur in eine leere Instanz, das Skript bricht sonst ab):
1. `$DC down` und die drei Volumes entfernen bzw. auf einem frischen Gerät
   starten. Das Sicherungsziel bleibt eingebunden. Die Volumes sind extern:
   `$DC down -v` entfernt sie nicht, und compose legt sie nicht selbst an.
   Danach leer neu anlegen:
   `for v in toniapply-heimserver_db-data toniapply-heimserver_backup-marker vorlesezeit-heimserver_s3-data; do docker volume rm $v; docker volume create $v; done`
   (auf einem frischen Gerät nur `docker volume create`).
2. `$DC up -d s3`
3. `ls <Ziel>/db/` und den gewünschten Stand wählen, dann
   `$DC run --rm --no-deps backup python deploy/backup/backup.py restore vorlesezeit-<UTC-Zeit>.db`
4. `$DC up -d` und prüfen: Kalender sichtbar, eine Aufnahme spielt ab,
   `/status` zeigt `neu_eingeben` leer. Ohne den alten `CREDENTIALS_KEY` sind
   alle Passwörter neu einzugeben (siehe „Zugangsdaten ‚neu eingeben‘“).

Eine Rücksicherung wird vor der Inbetriebnahme einmal in einer
Wegwerf-Instanz geprobt (anderer Projektname mit `-p`, eigenes Zielverzeichnis),
sonst ist die Sicherung unbewiesen.

**Schemaänderungen nach der Inbetriebnahme:** `init_db` legt nur fehlende
Tabellen an, keine Spalten. Jede spätere Änderung am Datenmodell braucht eine
additive Migration, vorher eine Sicherung und eine Probe auf einer Kopie der
Produktionsdatenbank. Ein `down -v` zum „Neuaufsetzen“ ist ausgeschlossen.

## cron-job.org

Zwei Aufträge, beide mit Zeitzone `Europe/Berlin`.

**Auftrag 1: externer Anstoß**
- URL `https://advent.<domain>/delivery/trigger`, Methode `POST`.
- Kopffelder: `X-Trigger-Secret: <TRIGGER_SECRET>`, `X-Trigger-Source: extern`.
- Zeitplan: alle 30 Minuten von 17:00 bis 01:30 (Minuten 0 und 30, Stunden
  17–23 und 0–1).
- Benachrichtigung: nach **2 Fehlschlägen in Folge** und bei
  **Wiederherstellung**.
- Antworten: `204` nicht fällig, `202` gestartet/läuft, `200` erledigt, `502`
  fehlgeschlagen oder Lauf hängt, `401` falsches Geheimnis. Dass cron-job.org
  202 und 204 als Erfolg wertet, wird in U15 einmal an einem echten
  Fehlschlag belegt.

**Auftrag 2: Sicherungsprüfung**
- URL `https://advent.<domain>/backup/check`, Methode `GET`, Kopffeld
  `X-Trigger-Secret: <TRIGGER_SECRET>`.
- Einmal täglich am Vormittag, z. B. 09:00. Benachrichtigung beim ersten
  Fehlschlag und bei Wiederherstellung.
- Antwort `200` mit `{"alter_stunden": …}` solange die letzte Sicherung
  höchstens 36 Stunden alt ist, sonst `503`.

## Was tun bei welcher Mail

| Mail | Bedeutung | Handgriff |
|---|---|---|
| cron-job.org, Auftrag 1, Fehlschlag mit 502 | App erreichbar, Lauf fehlgeschlagen | Abendmeldung der App und Reiter „Auslieferung“ ansehen, Ursache beheben, dort für den Tag „Jetzt aufspielen“. Hilft das nicht: manueller Weg unten. |
| cron-job.org, Auftrag 1, Zeitüberschreitung oder 5xx von Cloudflare (z. B. 530, 1033) | Heimserver, Tunnel, Strom oder Internet weg | Gerät prüfen, `$DC ps`, ggf. `$DC up -d`. Nicht vor 01:30 wieder da: manueller Weg unten. |
| cron-job.org, Auftrag 1, 401 | Geheimnis in cron-job.org passt nicht zu `TRIGGER_SECRET` | Kopffeld im Auftrag korrigieren. |
| cron-job.org, Auftrag 2, Fehlschlag | Sicherung älter als 36 h oder nie gelaufen | `$DC logs backup`, Sicherungsziel eingehängt und beschreibbar? Danach `--once` (siehe oben) und die Prüfadresse erneut aufrufen. |
| cron-job.org „wieder erreichbar“ | Wiederherstellung | nichts, außer ein Abend blieb ohne Lauf: Reiter „Auslieferung“ prüfen. |
| cron-job.org „Auftrag deaktiviert“ | 25 Fehlschläge in Folge | Ursache beheben, dann wieder einschalten (unten). |
| Abendmeldung der App mit Fehlschlag oder Ersatzbeitrag (eine Sammelmeldung je Abend über alle Tonies; ein Fehlschlag kommt zusätzlich sofort als Einzelmeldung mit der Zeile „Tonie: ••••XXXX“) | Lauf lief, aber nicht wie geplant | Mit dem Tonie-Umschalter den genannten Tonie wählen, dann Reiter „Auslieferung“ und „Aufnahmen“. |

Fällt der Mailanbieter aus (Drosselung, Sperre der Absender-IP), fehlen
Abendmeldung und Magic Links zugleich; die Mails von cron-job.org kommen
unabhängig davon an.

**Deaktivierten Auftrag wieder einschalten:** cron-job.org schaltet einen
Auftrag nach 25 Fehlschlägen in Folge ab, das ist bei rund 18 Aufrufen je
Nacht schon am zweiten Ausfallabend der Fall. Erst die Ursache beheben und
die Adresse einmal von Hand prüfen, dann in cron-job.org unter „Cronjobs“ den
Auftrag öffnen, „Aktiviert“ wieder einschalten und speichern, anschließend
„Jetzt ausführen“ (Test-Lauf) und das Ergebnis im Verlauf des Auftrags
ansehen.

## Manueller Weg über die Herstelleroberfläche

Wenn App, Heimserver oder Toniecloud-Schnittstelle an einem Abend nicht
rechtzeitig zu retten sind:
1. Die Aufnahme des nächsten Tages besorgen: im Reiter „Aufnahmen“ über das
   Menü des Players speichern oder, falls die App nicht läuft, aus
   `<Ziel>/objects/beitraege/` der letzten Sicherung. Die Dateinamen dort
   sind Zufallskennungen; welche Datei zu welchem Tag gehört, steht in der
   Spalte `audio_object_key` der Tabelle `beitraege` im letzten Datenbankstand
   (`sqlite3 <Ziel>/db/<Stand>.db`).
2. Unter my.tonies.com anmelden, den Creative Tonie öffnen, die vorhandenen
   Kapitel entfernen und die Datei hochladen. Der Tonie trägt immer nur die
   Geschichte des jeweiligen Tages.
3. Warten, bis die Verarbeitung durch ist, und den Tonie einmal an die Box
   stellen.
4. Sobald die App wieder läuft, gleicht der nächste Kontrolllauf den Stand ab;
   im Reiter „Auslieferung“ nachsehen.
