"""Load and validate MABEL's repository-backed operating configuration."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from mabel.repository import Repository, RepositoryError


class ConfigurationError(ValueError):
    """Raised when MABEL's durable configuration cannot be loaded safely."""


@dataclass(frozen=True)
class MabelSystem:
    name: str
    expansion: str
    role: str

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "expansion": self.expansion,
            "role": self.role,
        }


@dataclass(frozen=True)
class MabelSchemaRegistry:
    """A root-manifest declaration for MABEL's durable schema index."""

    path: PurePosixPath
    manifest_path: PurePosixPath

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path.as_posix(), "manifest": self.manifest_path.as_posix()}


@dataclass(frozen=True)
class MabelManifest:
    schema_version: int
    system: MabelSystem
    start_instruction_path: PurePosixPath
    schema_registry: MabelSchemaRegistry

    @classmethod
    def from_data(cls, data: Any) -> "MabelManifest":
        root = _require_mapping(data, "manifest.json")
        schema_version = root.get("schema_version")
        if type(schema_version) is not int or schema_version != 1:
            raise ConfigurationError("manifest.json schema_version must be the integer 1.")

        system = _require_mapping(root.get("system"), "manifest.json system")
        instructions = _require_mapping(root.get("instructions"), "manifest.json instructions")
        schemas = _require_mapping(root.get("schemas"), "manifest.json schemas")
        start_path = _repository_path(
            _require_string(instructions.get("start"), "manifest.json instructions.start")
        )

        return cls(
            schema_version=schema_version,
            system=MabelSystem(
                name=_require_string(system.get("name"), "manifest.json system.name"),
                expansion=_require_string(system.get("expansion"), "manifest.json system.expansion"),
                role=_require_string(system.get("role"), "manifest.json system.role"),
            ),
            start_instruction_path=start_path,
            schema_registry=MabelSchemaRegistry(
                path=_repository_path(
                    _require_string(schemas.get("path"), "manifest.json schemas.path")
                ),
                manifest_path=_repository_path(
                    _require_string(schemas.get("manifest"), "manifest.json schemas.manifest")
                ),
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "system": self.system.as_dict(),
            "instructions": {"start": self.start_instruction_path.as_posix()},
            "schemas": self.schema_registry.as_dict(),
        }


@dataclass(frozen=True)
class LoadedInstruction:
    path: PurePosixPath
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path.as_posix(), "content": self.content}


@dataclass(frozen=True)
class MabelConfiguration:
    manifest: MabelManifest
    start_instruction: LoadedInstruction

    def as_dict(self) -> dict[str, object]:
        return {
            "manifest": self.manifest.as_dict(),
            "operating_instructions": {"start": self.start_instruction.as_dict()},
        }


class ConfigurationService:
    """Load the current MABEL manifest and its declared operating instructions."""

    def __init__(self, repository: Repository) -> None:
        self._repository = repository

    def load(self) -> MabelConfiguration:
        """Read, validate, and resolve the current repository configuration."""
        try:
            manifest_data = json.loads(self._repository.read_text(PurePosixPath("manifest.json")))
            manifest = MabelManifest.from_data(manifest_data)
            start_instruction = LoadedInstruction(
                path=manifest.start_instruction_path,
                content=self._repository.read_text(manifest.start_instruction_path),
            )
        except (RepositoryError, json.JSONDecodeError) as error:
            raise ConfigurationError(str(error)) from error
        return MabelConfiguration(manifest=manifest, start_instruction=start_instruction)


def _require_mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{location} must be a mapping.")
    return value


def _require_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{location} must be a non-empty string.")
    return value.strip()


def _repository_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ConfigurationError(
            "manifest paths must be repository-relative."
        )
    return path
