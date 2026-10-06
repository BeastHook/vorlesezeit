"""U6 -- Generalprobe: gestauchte 24-Tage-Sequenz gegen den echten Tonie.

Laeuft im App-Container, nicht auf dem Host (DATABASE_PATH und
STORAGE_ENDPOINT_URL zeigen dort auf Docker-interne Namen):

    docker compose exec app python -m scripts.rehearsal <subcommand>

Subcommands: list-tonies, setup --tonie-index N, fill --tonie-index N,
run --tonie-index N, restore-baseline --tonie-index N --from <pfad>.

Mehrkalender (docs/plans/2026-10-01-2242-feat-setup-mehrkalender-plan.md,
U13): der Tonie kommt aus der Tabelle `creative_tonies` (im Setup angelegt),
seine Zugangsdaten aus dem tonies-Konto ueber `TonieCloudFactory`, nie aus
`TONIE_USERNAME`/`TONIE_PASSWORD`. Die Probe arbeitet mit dem Kalender des
Tonies; `fill` verlangt einen Kalender ohne belegte Tuerchen (eigenen
Generalprobe-Kalender im Setup anlegen), denn jeder Vorabend-Lauf macht den
Kalendertag fest (R13).

`run` ausserhalb des Zeitplanfensters (17:00-01:45 Europe/Berlin) fahren oder
den Zeitplan-Container stoppen: das Skript laeuft in eigenem Prozess und teilt
die Sperre je Tonie nicht mit der App. Siehe
docs/plans/2026-09-16-1348-feat-sprachaufnahmen-adventskalender-plan.md,
Abschnitt "U6. Generalprobe", fuer die Anforderung.

Sicherheit (gelernte Lektion aus der U4-Session, siehe CLAUDE.md "Known
Pitfalls"): `run` sichert die Kapitelliste des echten Tonie VOR jeder
Aenderung in eine lokale Datei und spielt sie in einem `finally` garantiert
zurueck -- kein `assert` vor dieser Stelle, das den Rollback verhindern
koennte.

Kennungen (Household-/Tonie-ID) werden nie als volles Kommandozeilen-
Argument entgegengenommen, nur als Index in die von `list-tonies` maskiert
ausgegebene Liste -- sie sollen nicht im Sessionverlauf eines begleitenden
Chats landen.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import settings
from app.admin.setup import (
    AuftragError,
    add_calendar_day,
    attach_beitrag,
    create_auftrag,
    create_person,
    set_replacement_beitrag,
)
from app.config import Config, load_config
from app.db import create_db_engine, init_db, make_session_factory
from app.delivery.chapters import load_app_chapters, stock_loss, stock_of
from app.delivery.job import run_delivery
from app.mail.report import mask_tonie_id
from app.models import Beitrag, Campaign, CreativeTonie, Person
from app.storage import ObjectStorage
from app.toniecloud.client import (
    KontoUnavailable,
    TonieCloudClient,
    TonieCloudFactory,
    UnsupportedFormatError,
    validate_format,
)
from app.toniecloud.models import Chapter
from tests.fixtures.audio import WEBM_PLACEHOLDER_FILENAME, make_tone_mp3, make_wrong_format_mp3

REHEARSAL_PERSON_EMAIL = "rehearsal@vorlesezeit.local"
SLOT_COUNT = 24

# Approach-Schritt 4: bewusst eingestreute Fehlerfaelle.
MISSING_DAY = 5  # kein Beitrag -> Ersatzbeitrag-Pfad ueber den fehlenden Tag.
WRONG_FORMAT_DAY = 8  # zulaessiges Format, von der Verarbeitung verworfener Inhalt.
# Approach-Schritt 4 (doppelter Lauf) und Schritt 5 (Kontrolllauf-Reparatur).
DUPLICATE_DAY = 1
KONTROLLLAUF_DAY = 3

LOG_DIR = Path(".remember/logs")
TMP_DIR = Path(".remember/tmp")


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str
    seconds: float = 0.0


@dataclass
class Protocol:
    steps: list[StepResult] = field(default_factory=list)

    def record(self, name: str, ok: bool, detail: str, seconds: float = 0.0) -> None:
        self.steps.append(StepResult(name, ok, detail, seconds))
        marker = "OK" if ok else "FEHLER"
        print(f"[{marker}] {name} ({seconds:.1f}s): {detail}")

    def unexplained_failures(self) -> list[StepResult]:
        return [s for s in self.steps if not s.ok]

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Generalprobe-Protokoll",
            "",
            "| Schritt | Ausgang | Dauer | Detail |",
            "|---|---|---|---|",
        ]
        for s in self.steps:
            outcome = "OK" if s.ok else "FEHLER"
            lines.append(f"| {s.name} | {outcome} | {s.seconds:.1f}s | {s.detail} |")
        path.write_text("\n".join(lines) + "\n")


def _wiring(
    transport: httpx.BaseTransport | None = None,
) -> tuple[Config, Session, ObjectStorage, TonieCloudFactory]:
    config = load_config()
    engine = create_db_engine(config)
    init_db(engine)
    session_factory = make_session_factory(engine)
    db = session_factory()
    storage = ObjectStorage(config)
    factory = TonieCloudFactory(config.credentials_key, transport=transport)
    return config, db, storage, factory


def _tonies(db: Session) -> list[CreativeTonie]:
    return list(db.scalars(select(CreativeTonie).order_by(CreativeTonie.id)))


def _masked(tonie: CreativeTonie) -> str:
    return mask_tonie_id(tonie.tonie_id)


def select_tonie(db: Session, index: int) -> CreativeTonie:
    tonies = _tonies(db)
    if not 0 <= index < len(tonies):
        raise SystemExit(
            f"Ungueltiger Index {index}: {len(tonies)} Tonies angelegt -- "
            "Liste mit 'list-tonies' ansehen."
        )
    return tonies[index]


def tonie_client(factory: TonieCloudFactory, db: Session, tonie: CreativeTonie) -> TonieCloudClient:
    """Zugangsdaten aus dem Einstellungsdienst ueber die Fabrik (U6). Ohne
    nutzbares Konto bricht die Probe ab, bevor sie den Tonie anfasst."""
    try:
        return factory.for_tonie(db, tonie)
    except KontoUnavailable as exc:
        raise SystemExit(f"{exc} Im Admin-Bereich unter Setup nachholen.") from exc


def calendar_of(db: Session, tonie: CreativeTonie) -> Campaign:
    if tonie.campaign_id is None:
        raise SystemExit(
            f"Tonie {tonie.name!r} ({_masked(tonie)}) ist keinem Kalender zugeordnet -- "
            "im Admin-Bereich unter Setup zuordnen."
        )
    return db.get(Campaign, tonie.campaign_id)


def _rehearsal_person(db: Session) -> Person:
    person = db.execute(
        select(Person).where(Person.email == REHEARSAL_PERSON_EMAIL)
    ).scalar_one_or_none()
    if person is None:
        person = create_person(db, REHEARSAL_PERSON_EMAIL, "Generalprobe")
    return person


# --- list-tonies -------------------------------------------------------


def cmd_list_tonies(_args: argparse.Namespace) -> None:
    _, db, _, _ = _wiring()
    tonies = _tonies(db)
    if not tonies:
        print("Keine Creative Tonies angelegt -- im Admin-Bereich unter Setup hinzufuegen.")
        return
    for index, tonie in enumerate(tonies):
        calendar = f"Kalender {tonie.campaign.name!r}" if tonie.campaign else "kein Kalender"
        print(f"[{index}] {tonie.name!r} ({_masked(tonie)}), {calendar}")


# --- setup ---------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> None:
    _, db, _, factory = _wiring()
    tonie = select_tonie(db, args.tonie_index)
    campaign = calendar_of(db, tonie)
    tonie_client(factory, db, tonie)
    _rehearsal_person(db)
    print(
        f"Generalprobe vorbereitet: Tonie {tonie.name!r} ({_masked(tonie)}), "
        f"Kalender {campaign.name!r}."
    )


# --- fill ------------------------------------------------------------------


def fill(
    db: Session, storage: ObjectStorage, tonie: CreativeTonie, *, now: datetime
) -> tuple[int, int]:
    """Platzhalterbeitraege ueber Auftraege am Kalender des Tonies (Mehrkalender
    KTD1). `now` zeitzonenbewusst (Europe/Berlin). Liefert (Anzahl
    Tagesbeitraege, ID des Ersatzbeitrags)."""
    campaign = calendar_of(db, tonie)
    slots = {slot.day: slot for slot in campaign.slots}
    if any(slot.auftrag_id is not None for slot in slots.values()):
        raise SystemExit(
            f"Kalender {campaign.name!r} hat bereits belegte Tuerchen -- die Generalprobe "
            "nur auf einem eigenen, leeren Kalender fahren."
        )
    person = _rehearsal_person(db)
    delivery_time = settings.delivery_time_for(db, now.date())

    tone = make_tone_mp3()
    wrong_format = make_wrong_format_mp3()
    approved_at = now.astimezone(UTC).replace(tzinfo=None)
    prefix = f"rehearsal/kalender-{campaign.id}"

    created = 0
    try:
        for day in range(1, SLOT_COUNT + 1):
            if day == MISSING_DAY:
                continue  # Approach-Schritt 4: fehlender Tagesbeitrag.

            content = wrong_format if day == WRONG_FORMAT_DAY else tone
            key = f"{prefix}/day-{day}.mp3"
            storage.put(key, content, content_type="audio/mpeg")

            auftrag = create_auftrag(db, person_id=person.id, title=f"Generalprobe Tag {day}")
            add_calendar_day(db, auftrag.id, slots[day].id, now=now, delivery_time=delivery_time)
            beitrag = attach_beitrag(db, auftrag, person_id=person.id, audio_object_key=key)
            beitrag.title = f"Generalprobe Tag {day}"
            beitrag.approved_at = approved_at
            beitrag.chapter_title = f"Tag {day}"
            created += 1
    except AuftragError as exc:
        db.rollback()
        raise SystemExit(f"Abbruch, nichts angelegt: {exc}") from exc

    replacement_key = f"{prefix}/replacement.mp3"
    storage.put(replacement_key, tone, content_type="audio/mpeg")
    replacement = Beitrag(
        person_id=person.id,
        title="Generalprobe Ersatzbeitrag",
        audio_object_key=replacement_key,
        approved_at=approved_at,
        chapter_title="Ersatzbeitrag Generalprobe",
    )
    db.add(replacement)
    db.commit()
    db.refresh(replacement)

    set_replacement_beitrag(db, replacement.id, campaign.id)
    return created, replacement.id


def cmd_fill(args: argparse.Namespace) -> None:
    config, db, storage, _ = _wiring()
    tonie = select_tonie(db, args.tonie_index)
    created, replacement_id = fill(db, storage, tonie, now=datetime.now(ZoneInfo(config.timezone)))
    print(
        f"{created} Tagesbeitraege angelegt (Tag {MISSING_DAY} bewusst ausgelassen, "
        f"Tag {WRONG_FORMAT_DAY} mit ungueltigem Inhalt),\n"
        f"Ersatzbeitrag {replacement_id} verknuepft."
    )


# --- run ---------------------------------------------------------------


def _save_baseline(chapters: tuple[Chapter, ...]) -> Path:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    path = TMP_DIR / f"rehearsal-baseline-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps([c.to_api_dict() for c in chapters]))
    return path


def _load_baseline(path: Path) -> list[Chapter]:
    return [Chapter.from_api_dict(d) for d in json.loads(path.read_text())]


def _restore(
    client: TonieCloudClient, household_id: str, tonie_id: str, baseline: list[Chapter]
) -> str:
    client.replace_chapters(household_id, tonie_id, baseline)
    fresh = client.get_state(household_id, tonie_id)
    expected_titles = sorted(c.title for c in baseline)
    actual_titles = sorted(c.title for c in fresh.chapters)
    if expected_titles == actual_titles and len(fresh.chapters) == len(baseline):
        return f"wiederhergestellt: {len(fresh.chapters)} Kapitel, Titel stimmen ueberein."
    return (
        f"ABWEICHUNG nach Restore: erwartet {expected_titles}, live {actual_titles} -- "
        "manuell pruefen."
    )


def cmd_run(args: argparse.Namespace) -> None:
    _, db, storage, factory = _wiring()
    tonie = select_tonie(db, args.tonie_index)
    calendar_of(db, tonie)
    client = tonie_client(factory, db, tonie)
    print(
        "Hinweis: ausserhalb des Zeitplanfensters 17:00-01:45 fahren oder den "
        "Zeitplan-Container stoppen (eigener Prozess, keine gemeinsame Sperre je Tonie)."
    )

    tonie_id = tonie.tonie_id
    household_id = client.find_household_id(tonie_id)

    baseline_state = client.get_state(household_id, tonie_id)
    baseline_path = _save_baseline(baseline_state.chapters)
    stock = stock_of(baseline_state, load_app_chapters(tonie.app_chapters))
    print(f"Baseline gesichert ({len(baseline_state.chapters)} Kapitel) -> {baseline_path}")

    protocol = Protocol()

    # Schritt 0: lokal abgelehnte, unzulaessige Datei -- kein Netzwerkzugriff.
    limits = client.get_config()
    try:
        validate_format(WEBM_PLACEHOLDER_FILENAME, limits)
        protocol.record("lokal-reject", False, "webm wurde faelschlich akzeptiert.")
    except UnsupportedFormatError as exc:
        protocol.record("lokal-reject", True, f"korrekt lokal abgelehnt: {exc}")

    try:
        for day in range(1, SLOT_COUNT + 1):
            start = time.monotonic()
            outcome = run_delivery(db, client, storage, tonie, run_type="vorabend", target_day=day)
            protocol.record(
                f"tag-{day}",
                outcome.success,
                f"ersatzbeitrag={outcome.used_replacement} grund={outcome.reason}",
                time.monotonic() - start,
            )
            # R49: der Bestand steht nach jedem erfolgreichen Lauf unveraendert
            # hinter dem App-Kapitel. Nach einem gescheiterten Lauf liegt der
            # Laufbeginn-Stand zurueck -- dort ist Platz 1 nicht zwingend ein
            # App-Kapitel, der Fehlschlag steht schon im Protokoll.
            if outcome.success:
                live = client.get_state(household_id, tonie_id)
                loss = stock_loss(stock, live, new_count=1)
                protocol.record(
                    f"tag-{day}-bestand",
                    loss is None,
                    loss or f"{len(stock)} Kapitel unveraendert dahinter",
                )

            if day == DUPLICATE_DAY:
                before = client.get_state(household_id, tonie_id).last_update
                start = time.monotonic()
                dup_outcome = run_delivery(
                    db, client, storage, tonie, run_type="vorabend", target_day=day
                )
                after = client.get_state(household_id, tonie_id).last_update
                no_reupload = before == after
                protocol.record(
                    f"tag-{day}-duplikat",
                    dup_outcome.success and no_reupload,
                    f"lastUpdate unveraendert={no_reupload} ({before} == {after})",
                    time.monotonic() - start,
                )

            if day == KONTROLLLAUF_DAY:
                start = time.monotonic()
                client.replace_chapters(household_id, tonie_id, list(stock))
                protocol.record(
                    f"tag-{day}-verworfen",
                    True,
                    "App-Kapitel entfernt (simulierter Verlust), Bestand bleibt",
                    time.monotonic() - start,
                )
                start = time.monotonic()
                repair = run_delivery(
                    db, client, storage, tonie, run_type="kontrolllauf", target_day=day
                )
                live = client.get_state(household_id, tonie_id)
                expected_title = f"Tag {day}"
                repaired = repair.success and any(c.title == expected_title for c in live.chapters)
                protocol.record(
                    f"tag-{day}-kontrolllauf-repariert",
                    repaired,
                    f"grund={repair.reason} live_titel={[c.title for c in live.chapters]}",
                    time.monotonic() - start,
                )
    finally:
        # Restaurierung darf den Protokoll-Schreibvorgang nicht verhindern --
        # scheitert sie, muss trotzdem sichtbar sein, WAS schiefging und wo
        # die Baseline zum manuellen Nachholen liegt (kein assert, kein
        # unbehandelter Abbruch vor dieser Stelle, siehe CLAUDE.md Known
        # Pitfalls).
        start = time.monotonic()
        try:
            detail = _restore(client, household_id, tonie_id, list(baseline_state.chapters))
            restored_ok = detail.startswith("wiederhergestellt")
        except Exception as exc:
            detail = (
                f"RESTORE FEHLGESCHLAGEN: {exc!r} -- Baseline liegt in {baseline_path}, "
                f"manuell erneut versuchen mit: restore-baseline --from {baseline_path}"
            )
            restored_ok = False
        protocol.record("restore-baseline", restored_ok, detail, time.monotonic() - start)

        log_path = LOG_DIR / f"rehearsal-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.md"
        protocol.write(log_path)
        print(f"Protokoll geschrieben: {log_path}")

    failures = protocol.unexplained_failures()
    if failures:
        print(f"{len(failures)} unerklaerte Fehlschlaege:", file=sys.stderr)
        for f in failures:
            print(f"  - {f.name}: {f.detail}", file=sys.stderr)
        sys.exit(1)
    print("Generalprobe abgeschlossen, keine unerklaerten Fehlschlaege.")


# --- restore-baseline ------------------------------------------------------


def cmd_restore_baseline(args: argparse.Namespace) -> None:
    _, db, _, factory = _wiring()
    tonie = select_tonie(db, args.tonie_index)
    client = tonie_client(factory, db, tonie)
    household_id = client.find_household_id(tonie.tonie_id)
    baseline = _load_baseline(Path(args.from_path))
    detail = _restore(client, household_id, tonie.tonie_id, baseline)
    print(detail)
    if not detail.startswith("wiederhergestellt"):
        sys.exit(1)


# --- CLI ---------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="U6-Generalprobe gegen den echten Tonie.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-tonies").set_defaults(func=cmd_list_tonies)

    # Der Tonie kommt immer als Index aus 'list-tonies', nie als volle ID.
    for name, func in (
        ("setup", cmd_setup),
        ("fill", cmd_fill),
        ("run", cmd_run),
        ("restore-baseline", cmd_restore_baseline),
    ):
        command = sub.add_parser(name)
        command.add_argument("--tonie-index", type=int, required=True)
        command.set_defaults(func=func)
        if name == "restore-baseline":
            command.add_argument("--from", dest="from_path", required=True)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
