"""Transport-agnostic AMIE read contract."""

from collections.abc import Mapping
from typing import Any, Protocol

from mabel.amie.models import AmieFile, DiscoveredPath


class AmieReader(Protocol):
    """Minimal AMIE read primitives required by generic discovery."""

    async def read_bootstrap(self) -> AmieFile:
        """Read AMIE's authoritative operating bootstrap."""

    async def read_root_manifest(self) -> Mapping[str, Any]:
        """Read AMIE's current root manifest as structured metadata."""

    async def read_file(self, path: DiscoveredPath) -> AmieFile:
        """Read one exact file path that was already discovered from AMIE metadata."""
