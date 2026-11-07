"""Transport-independent traversal of AMIE's manifest-declared relationships."""

import json
from collections.abc import Mapping

from mabel.amie.models import AmieFile, AmieMetadataError, Manifest, ManifestEntry, RelatedFile
from mabel.amie.reader import AmieReader


class AmieDiscoveryError(ValueError):
    """Raised when an AMIE traversal request is not declared by repository metadata."""


class AmieDiscovery:
    """Resolve only bootstrap, manifests, entries, and relations declared by AMIE."""

    def __init__(self, reader: AmieReader) -> None:
        self._reader = reader

    async def load_bootstrap(self) -> AmieFile:
        return await self._reader.read_bootstrap()

    async def load_root_manifest(self) -> Manifest:
        return Manifest.from_data(await self._reader.read_root_manifest())

    async def load_nested_manifest(self, entry: ManifestEntry) -> Manifest:
        if entry.manifest_path is None:
            raise AmieDiscoveryError("Entry does not declare a nested manifest.")
        return self._parse_manifest(await self._reader.read_file(entry.manifest_path))

    async def load_leaf_file(self, entry: ManifestEntry) -> AmieFile:
        if entry.manifest_path is not None:
            raise AmieDiscoveryError("Collection entries must be traversed through their manifest.")
        return await self._reader.read_file(entry.path)

    async def load_related_file(self, entry: ManifestEntry, relation: str) -> AmieFile:
        related_file = self._related_file(entry, relation)
        return await self._reader.read_file(related_file.path)

    @staticmethod
    def _parse_manifest(file: AmieFile) -> Manifest:
        try:
            data = json.loads(file.content)
        except json.JSONDecodeError as error:
            raise AmieDiscoveryError(f"Declared manifest is not valid JSON: {file.path.value}") from error
        if not isinstance(data, Mapping):
            raise AmieDiscoveryError(f"Declared manifest must be a JSON object: {file.path.value}")
        try:
            return Manifest.from_data(data)
        except AmieMetadataError as error:
            raise AmieDiscoveryError(str(error)) from error

    @staticmethod
    def _related_file(entry: ManifestEntry, relation: str) -> RelatedFile:
        matches = [related_file for related_file in entry.related_files if related_file.relation == relation]
        if not matches:
            raise AmieDiscoveryError(f"Entry does not declare a related file with relation: {relation}")
        if len(matches) > 1:
            raise AmieDiscoveryError(f"Entry declares multiple related files with relation: {relation}")
        return matches[0]
