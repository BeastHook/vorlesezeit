"""Datenmodelle fuer den Toniecloud-Client (U4).

Reine Datenklassen, kein Verhalten -- die Spec-Feldnamen (camelCase) werden
beim Parsen auf snake_case uebersetzt, sonst 1:1 uebernommen. `Chapter`
transportiert `file` unveraendert in beide Richtungen: fuer ein neues Kapitel
ist es die hochgeladene `fileId`, fuer ein bestehendes der von der Toniecloud
gelesene opake Blob, der beim Zurueckschreiben unveraendert mitgesendet
werden muss (Rollback-Pfad, siehe .claude/skills/toniecloud-api/SKILL.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ConfigLimits:
    max_chapters: int
    max_seconds: float
    max_bytes: int
    accepts: tuple[str, ...]

    @classmethod
    def from_api_dict(cls, data: dict) -> ConfigLimits:
        return cls(
            max_chapters=data["maxChapters"],
            max_seconds=data["maxSeconds"],
            max_bytes=data["maxBytes"],
            accepts=tuple(data["accepts"]),
        )


@dataclass(frozen=True)
class Chapter:
    title: str
    file: str
    id: str | None = None
    additional_data: dict | None = None

    @classmethod
    def from_api_dict(cls, data: dict) -> Chapter:
        return cls(
            title=data["title"],
            file=data["file"],
            id=data.get("id"),
            additional_data=data.get("additionalData"),
        )

    def to_api_dict(self) -> dict:
        payload: dict = {"title": self.title, "file": self.file}
        if self.id is not None:
            payload["id"] = self.id
        if self.additional_data is not None:
            payload["additionalData"] = self.additional_data
        return payload


@dataclass(frozen=True)
class UploadTarget:
    file_id: str
    s3_url: str
    s3_fields: dict[str, str]

    @classmethod
    def from_api_dict(cls, data: dict) -> UploadTarget:
        return cls(
            file_id=data["fileId"],
            s3_url=data["request"]["url"],
            s3_fields=dict(data["request"]["fields"]),
        )


@dataclass(frozen=True)
class TranscodingError:
    reason: str
    deleted_chapter_titles: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_api_dict(cls, data: dict) -> TranscodingError:
        return cls(
            reason=data["reason"],
            deleted_chapter_titles=tuple(
                entry["title"] for entry in data.get("deletedChapters", [])
            ),
        )


@dataclass(frozen=True)
class CreativeTonieState:
    id: str
    household_id: str
    chapters: tuple[Chapter, ...]
    transcoding: bool
    chapters_present: int
    seconds_present: float
    transcoding_errors: tuple[TranscodingError, ...]
    last_update: str | None

    @classmethod
    def from_api_dict(cls, data: dict) -> CreativeTonieState:
        return cls(
            id=data["id"],
            household_id=data["householdId"],
            chapters=tuple(Chapter.from_api_dict(c) for c in data.get("chapters", [])),
            transcoding=data["transcoding"],
            chapters_present=data["chaptersPresent"],
            seconds_present=data["secondsPresent"],
            transcoding_errors=tuple(
                TranscodingError.from_api_dict(e) for e in data.get("transcodingErrors", [])
            ),
            last_update=data.get("lastUpdate"),
        )


@dataclass(frozen=True)
class VerificationResult:
    success: bool
    reason: str | None = None
    title_mismatches: tuple[tuple[str, str], ...] = field(default_factory=tuple)
