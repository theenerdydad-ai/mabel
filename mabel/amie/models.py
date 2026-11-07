"""Generic AMIE metadata models that preserve unknown repository fields."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any


class AmieMetadataError(ValueError):
    """Raised when declared AMIE metadata cannot be traversed safely."""


_DISCOVERED_PATH = object()


@dataclass(frozen=True)
class DiscoveredPath:
    """An exact repository path obtained from AMIE metadata."""

    value: str
    _provenance: object = field(repr=False, compare=False)

    @classmethod
    def from_metadata(cls, value: object, location: str) -> "DiscoveredPath":
        if not isinstance(value, str) or not value:
            raise AmieMetadataError(f"{location} must be a non-empty path string.")
        parsed = PurePosixPath(value)
        if not parsed.is_absolute() or ".." in parsed.parts:
            raise AmieMetadataError(f"{location} must be an absolute repository path without traversal.")
        return cls(value=value, _provenance=_DISCOVERED_PATH)

    def require_discovered(self) -> None:
        """Reject a path that did not originate in AMIE metadata."""
        if self._provenance is not _DISCOVERED_PATH:
            raise AmieMetadataError("AMIE paths must originate from discovered metadata.")


@dataclass(frozen=True)
class AmieFile:
    """One exact file response from AMIE's read primitive."""

    path: DiscoveredPath
    content: str
    sha: str | None
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class RelatedFile:
    """A relation-labeled repository file declared by a manifest entry."""

    path: DiscoveredPath
    relation: str
    raw: Mapping[str, Any]

    @classmethod
    def from_data(cls, data: Mapping[str, Any], location: str) -> "RelatedFile":
        relation = data.get("relation")
        if not isinstance(relation, str) or not relation:
            raise AmieMetadataError(f"{location}.relation must be a non-empty string.")
        return cls(
            path=DiscoveredPath.from_metadata(data.get("path"), f"{location}.path"),
            relation=relation,
            raw=data,
        )


@dataclass(frozen=True)
class ManifestEntry:
    """One discoverable manifest entry, retaining its unmodeled fields."""

    collection: str
    path: DiscoveredPath
    manifest_path: DiscoveredPath | None
    content_type: str | None
    related_files: tuple[RelatedFile, ...]
    raw: Mapping[str, Any]

    @classmethod
    def from_data(
        cls, collection: str, data: Mapping[str, Any], default_content_type: str | None
    ) -> "ManifestEntry":
        related_data = data.get("related_files", [])
        if not isinstance(related_data, Sequence) or isinstance(related_data, (str, bytes)):
            raise AmieMetadataError(f"{collection} related_files must be an array when present.")
        related_files = tuple(
            RelatedFile.from_data(item, f"{collection} related_files[{index}]")
            for index, item in enumerate(related_data)
            if isinstance(item, Mapping)
        )
        if len(related_files) != len(related_data):
            raise AmieMetadataError(f"{collection} related_files entries must be objects.")

        manifest_value = data.get("manifest")
        return cls(
            collection=collection,
            path=DiscoveredPath.from_metadata(data.get("path"), f"{collection}.path"),
            manifest_path=(
                DiscoveredPath.from_metadata(manifest_value, f"{collection}.manifest")
                if manifest_value is not None
                else None
            ),
            content_type=(
                data["content_type"]
                if isinstance(data.get("content_type"), str)
                else default_content_type
            ),
            related_files=related_files,
            raw=data,
        )


@dataclass(frozen=True)
class Manifest:
    """A parsed AMIE manifest with generic entries and original data intact."""

    raw: Mapping[str, Any]
    entries: tuple[ManifestEntry, ...]

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> "Manifest":
        default_content_type = data.get("content_type")
        if not isinstance(default_content_type, str):
            default_content_type = None

        entries: list[ManifestEntry] = []
        for collection, records in data.items():
            if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
                continue
            for record in records:
                if not isinstance(record, Mapping) or "path" not in record:
                    continue
                entries.append(ManifestEntry.from_data(collection, record, default_content_type))
        return cls(raw=data, entries=tuple(entries))
