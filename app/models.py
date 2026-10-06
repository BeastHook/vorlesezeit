"""Datenmodell (U2): Kampagne, Slot, Beitrag, Person.

Neutrale Begriffe -- die Adventslogik sitzt in den Daten (24 feste Slots),
nicht in den Namen. Siehe Plan, U2-Approach Schritt 1.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Person(Base):
    __tablename__ = "persons"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(unique=True, index=True)
    display_name: Mapped[str] = mapped_column(default="")
    is_admin: Mapped[bool] = mapped_column(default=False)
    # R39: bei Erhoehung werden alle ausstehenden Login-Links und alle
    # laufenden Sitzungen dieser Person in einem Schritt entwertet.
    access_version: Mapped[int] = mapped_column(default=0)
    # Mehrkalender U2: Zeitpunkt der letzten Einladung, None = nie eingeladen.
    invited_at: Mapped[datetime | None] = mapped_column(default=None)
    # Zeitpunkt der letzten erfolgreich verschickten Erinnerung, None = nie.
    reminded_at: Mapped[datetime | None] = mapped_column(default=None)


class TonieKonto(Base):
    """Mehrkalender U2: ein tonies-Konto. `password` traegt spaeter ein
    Fernet-Token (U3), nie Klartext im Log -- deshalb eigenes `__repr__`."""

    __tablename__ = "tonie_konten"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(default="")
    username: Mapped[str] = mapped_column()
    password: Mapped[str | None] = mapped_column(default=None)
    # R21: gespeichertes Passwort nicht mehr lesbar -> "neu eingeben".
    needs_reentry: Mapped[bool] = mapped_column(default=False)
    # R41: None = "ungeprueft".
    checked_at: Mapped[datetime | None] = mapped_column(default=None)

    tonies: Mapped[list["CreativeTonie"]] = relationship(back_populates="konto")

    def __repr__(self) -> str:
        return f"TonieKonto(id={self.id!r}, label={self.label!r}, username={self.username!r})"


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    # F6 "Tonie-Konto verknuepfen": nur die Datenreferenz. Die eigentliche
    # Toniecloud-Anbindung (Login, Upload) ist U4.
    creative_tonie_id: Mapped[str | None] = mapped_column(default=None)
    # R21: fester Ersatzbeitrag. Wie er inhaltlich befuellt wird, ist ein
    # offener Punkt -- siehe Plan.
    replacement_beitrag_id: Mapped[int | None] = mapped_column(
        ForeignKey("beitraege.id"), default=None
    )

    # "Verifiziert-Zustand" (U5): Bruecke zwischen unserer Beitrag.id und der
    # von der Toniecloud vergebenen (opaken) Kapitel-id, ueber Prozess-
    # grenzen hinweg. Widerlegbar -- ein Kontrolllauf verwirft ihn, wenn der
    # Live-Zustand nicht mehr passt. Wird nur von automatischen Laeufen
    # (vorabend/kontrolllauf) gesetzt, siehe app/delivery/job.py.
    verified_beitrag_id: Mapped[int | None] = mapped_column(
        ForeignKey("beitraege.id"), default=None
    )
    verified_chapter_id: Mapped[str | None] = mapped_column(default=None)
    # U16/KTD20: die Kapitel, die die App selbst aufgespielt hat, als
    # JSON-Liste [{"id": ..., "seconds": ...}] in Tonie-Reihenfolge. Nur diese
    # entfernt ein Lauf; alles andere ist Bestand der Familie (R49). Bewusst
    # getrennt vom Verifiziert-Zustand: dessen Zuruecksetzen darf nie
    # vergessen lassen, welche Kapitel der App gehoeren. Zugriff nur ueber
    # app/delivery/chapters.py.
    app_chapters: Mapped[str | None] = mapped_column(default=None)
    verified_for_day: Mapped[int | None] = mapped_column(default=None)
    # Mehrkalender U2: Name des Kalenders. In der DB nullable (additiv per
    # ALTER TABLE ergaenzt), die Migration setzt Altbestaende auf "Familie".
    name: Mapped[str] = mapped_column(default="Familie", nullable=True)

    slots: Mapped[list["Slot"]] = relationship(back_populates="campaign")
    tonies: Mapped[list["CreativeTonie"]] = relationship(back_populates="campaign")


class CreativeTonie(Base):
    """Mehrkalender U2: ein bespielter Creative Tonie. Traegt den Tonie-
    bezogenen Zustand, der bisher auf `Campaign` lag (App-Kapitel,
    Verifiziert-Zustand). Gehoert hoechstens einem Kalender."""

    __tablename__ = "creative_tonies"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Die Toniecloud-ID des Creative Tonie.
    tonie_id: Mapped[str] = mapped_column(unique=True)
    konto_id: Mapped[int | None] = mapped_column(ForeignKey("tonie_konten.id"), default=None)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), default=None)
    name: Mapped[str] = mapped_column(default="")
    # JSON wie bisher `Campaign.app_chapters`, siehe app/delivery/chapters.py.
    app_chapters: Mapped[str | None] = mapped_column(default=None)
    verified_beitrag_id: Mapped[int | None] = mapped_column(
        ForeignKey("beitraege.id"), default=None
    )
    verified_chapter_id: Mapped[str | None] = mapped_column(default=None)
    verified_for_day: Mapped[int | None] = mapped_column(default=None)
    abraeumen_offen: Mapped[bool] = mapped_column(default=False)

    konto: Mapped[TonieKonto | None] = relationship(back_populates="tonies")
    campaign: Mapped[Campaign | None] = relationship(back_populates="tonies")


class Auftrag(Base):
    """Mehrkalender U2: eine Geschichte (Person, Titel, Vorlesetext), die an je
    einem Tag in einem oder mehreren Kalendern liegt (`Slot.auftrag_id`).
    Ohne Person = noch nicht vergeben."""

    __tablename__ = "auftraege"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("persons.id"), default=None)
    title: Mapped[str | None] = mapped_column(default=None)
    vorlesetext: Mapped[str | None] = mapped_column(default=None)

    person: Mapped[Person | None] = relationship()
    slots: Mapped[list["Slot"]] = relationship(back_populates="auftrag")
    beitraege: Mapped[list["Beitrag"]] = relationship(back_populates="auftrag")


class Slot(Base):
    __tablename__ = "slots"
    __table_args__ = (UniqueConstraint("campaign_id", "day", name="uq_slot_campaign_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"))
    day: Mapped[int]
    # R24: admin-gepflegter Vorschlag, optional -- nicht der Titel des
    # tatsaechlich eingereichten Beitrags (der liegt auf Beitrag.title).
    title: Mapped[str | None] = mapped_column(default=None)
    vorlesetext: Mapped[str | None] = mapped_column(default=None)
    assigned_person_id: Mapped[int | None] = mapped_column(ForeignKey("persons.id"), default=None)
    invitation_ready: Mapped[bool] = mapped_column(default=False)
    # Mehrkalender U2: loest title/vorlesetext/assigned_person_id ab (die
    # Altspalten bleiben stehen, additiv).
    auftrag_id: Mapped[int | None] = mapped_column(ForeignKey("auftraege.id"), default=None)

    campaign: Mapped[Campaign] = relationship(back_populates="slots")
    assigned_person: Mapped[Person | None] = relationship()
    auftrag: Mapped[Auftrag | None] = relationship(back_populates="slots")
    beitraege: Mapped[list["Beitrag"]] = relationship(back_populates="slot")


class Beitrag(Base):
    __tablename__ = "beitraege"

    id: Mapped[int] = mapped_column(primary_key=True)
    # R35: Eigentum bleibt dauerhaft bei der einreichenden Person, auch nach
    # einer Neuzuweisung des Slots.
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id"))
    # Nullable: eine freie Einreichung hat keinen Slot.
    slot_id: Mapped[int | None] = mapped_column(ForeignKey("slots.id"), default=None)
    # R24: serverseitig auf 100 Zeichen begrenzt, von Zeilenumbruechen und
    # Steuerzeichen befreit -- durchgesetzt in app/admin/setup.py bzw. der
    # spaeteren Einreichungsroute (U7), nicht hier im Modell selbst.
    title: Mapped[str | None] = mapped_column(default=None)
    # Schluessel der Datei im Objektspeicher; befuellt durch den Upload (U7).
    # Die App liefert sie nach der R28-Pruefung selbst aus (KTD17).
    audio_object_key: Mapped[str | None] = mapped_column(default=None)
    # R11/R13/R30: Freigabe-Zeitpunkt, None = nicht freigegeben. Die
    # Freigabe-UI selbst ist U8; U5 braucht das Feld, um "der freigegebene
    # Beitrag fuer Slot X" zu bestimmen.
    approved_at: Mapped[datetime | None] = mapped_column(default=None)
    # R13/R30 (U8): Ablehnung oder zurueckgenommene Freigabe. Der Beitrag
    # behaelt seinen Slotbezug (sonst landete er als freie Einreichung im
    # Eingang), blockiert den Slot aber nicht mehr -- siehe
    # app/recording/routing.py::is_slot_open. Eine neue Aufnahme der
    # Person (R10) setzt das Feld wieder auf None.
    rejected_at: Mapped[datetime | None] = mapped_column(default=None)
    # R35 (U8): Zeitpunkt, zu dem eine Neuzuweisung den Beitrag vom Slot
    # geloest hat. Unterscheidet einen verwaisten Beitrag (slot_id None,
    # detached_at gesetzt -- nie ausgeliefert, nicht im Eingang) von einer
    # freien Einreichung (slot_id None, detached_at None).
    detached_at: Mapped[datetime | None] = mapped_column(default=None)
    # R41: Zuschnitt-Angabe, keine Aenderung an der abgelegten Datei. None =
    # kein Zuschnitt. Die Zuschneide-UI (Wellenform) ist U8; U5 wendet die
    # Marken beim Ausliefern an.
    cut_start_seconds: Mapped[float | None] = mapped_column(default=None)
    cut_end_seconds: Mapped[float | None] = mapped_column(default=None)
    # R42: vom Admin ueberschreibbarer Kapitelname, getrennt vom
    # Einreichungstitel. None = Fallback-Kette zur Laufzeit (Slot.title ->
    # Beitrag.title -> "Tuerchen <Tag>"), siehe app/delivery/job.py.
    chapter_title: Mapped[str | None] = mapped_column(default=None)
    # Mehrkalender U2: loest slot_id ab; None bei freien und geloesten Beitraegen.
    auftrag_id: Mapped[int | None] = mapped_column(ForeignKey("auftraege.id"), default=None)

    person: Mapped[Person] = relationship(foreign_keys=[person_id])
    slot: Mapped[Slot | None] = relationship(back_populates="beitraege", foreign_keys=[slot_id])
    auftrag: Mapped[Auftrag | None] = relationship(back_populates="beitraege")


class DeliveryRun(Base):
    """R44: Verlaufseintrag -- jeder Lauf hinterlaesst einen, auch der
    Trockenlauf und der abgebrochene Lauf. Nachlesbare Fassung dessen, was
    die Abendmeldung verschickt, nicht ihr Ersatz (U5-Approach Punkt 9)."""

    __tablename__ = "delivery_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"))
    # vorabend | kontrolllauf | anstoss | manuell | trockenlauf | probelauf | aufraeumen
    run_type: Mapped[str] = mapped_column()
    target_day: Mapped[int | None] = mapped_column(default=None)
    # U10/KTD13: Berliner Datum des Abends, dem ein automatischer Lauf
    # zugeordnet ist (nach Mitternacht der Vorabend). Schluessel fuer
    # Wiederholungsregel und "erledigt"; beim Probelauf (R48) steht er statt
    # eines Adventstags. None bei Laeufen, die der Admin ausloest.
    evening: Mapped[date | None] = mapped_column(default=None)
    started_at: Mapped[datetime] = mapped_column()
    # gestartet (U10: offen, vor dem ersten Toniecloud-Aufruf geschrieben) |
    # erfolg | ersatzbeitrag | fehlschlag | uebersprungen |
    # abgebrochen (offener Eintrag ohne gehaltene Sperre, z. B. nach Neustart)
    outcome: Mapped[str] = mapped_column()
    reason: Mapped[str | None] = mapped_column(default=None)
    # Komma-getrennte Beitrag-IDs, nur zu Lesezwecken im Verlauf -- nie eine
    # Query-Bedingung, deshalb bewusst kein eigenes Association-Objekt.
    beitrag_ids: Mapped[str | None] = mapped_column(default=None)
    # Mehrkalender U2: Toniecloud-ID des Tonie, den der Lauf bespielt hat.
    tonie_id: Mapped[str | None] = mapped_column(default=None)


class Einstellungen(Base):
    """Mehrkalender U2: Instanz-Einstellungen, genau eine Zeile (id=1).
    `smtp_password` traegt spaeter ein Fernet-Token -- eigenes `__repr__`."""

    __tablename__ = "einstellungen"

    id: Mapped[int] = mapped_column(primary_key=True)
    smtp_host: Mapped[str | None] = mapped_column(default=None)
    smtp_port: Mapped[int | None] = mapped_column(default=None)
    smtp_user: Mapped[str | None] = mapped_column(default=None)
    smtp_password: Mapped[str | None] = mapped_column(default=None)
    smtp_from_address: Mapped[str | None] = mapped_column(default=None)
    smtp_needs_reentry: Mapped[bool] = mapped_column(default=False)
    smtp_checked_at: Mapped[datetime | None] = mapped_column(default=None)
    # "letzte Mail scheiterte"
    mail_failed_at: Mapped[datetime | None] = mapped_column(default=None)
    # "HH:MM"; eine Aenderung gilt ab `delivery_time_pending_from`.
    delivery_time: Mapped[str | None] = mapped_column(default=None)
    delivery_time_pending: Mapped[str | None] = mapped_column(default=None)
    delivery_time_pending_from: Mapped[date | None] = mapped_column(default=None)
    recording_deadline: Mapped[date | None] = mapped_column(default=None)
    invitation_date: Mapped[date | None] = mapped_column(default=None)
    magic_link_valid_until: Mapped[date | None] = mapped_column(default=None)
    admin_display_name: Mapped[str | None] = mapped_column(default=None)
    # Fuer wen der Kalender ist, frei ("Emma & Lukas"); in Einladung und Familientexten.
    kind_name: Mapped[str | None] = mapped_column(default=None)
    # Hoechste id einer geloeschten Person: neue Personen bekommen eine hoehere,
    # sonst oeffnete der alte Link der geloeschten die neue (SQLite vergibt
    # ohne AUTOINCREMENT max(id)+1 neu).
    hoechste_person_id: Mapped[int | None] = mapped_column(default=None)
    # JSON: Herkunft je Feld (z. B. Umgebung oder Setup-Reiter).
    herkunft: Mapped[str | None] = mapped_column(default=None)

    def __repr__(self) -> str:
        return (
            f"Einstellungen(id={self.id!r}, smtp_host={self.smtp_host!r}, "
            f"smtp_user={self.smtp_user!r})"
        )


class Einrichtungslink(Base):
    """Mehrkalender U2: Einmal-Einrichtungslink; gespeichert nur als Hash."""

    __tablename__ = "einrichtungslinks"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column()
    expires_at: Mapped[datetime] = mapped_column()
    used_at: Mapped[datetime | None] = mapped_column(default=None)
    invalidated_at: Mapped[datetime | None] = mapped_column(default=None)


class Abendmeldung(Base):
    """Mehrkalender U2: eine verschickte Abendmeldung je Abend (Berliner Datum)."""

    __tablename__ = "abendmeldungen"

    id: Mapped[int] = mapped_column(primary_key=True)
    evening: Mapped[date] = mapped_column(unique=True)
    sent_at: Mapped[datetime] = mapped_column()
