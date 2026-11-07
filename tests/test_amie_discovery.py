from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from mabel.amie.discovery import AmieDiscovery, AmieDiscoveryError
from mabel.amie.models import AmieFile, AmieMetadataError, DiscoveredPath, Manifest


def discovered_path(value: str) -> DiscoveredPath:
    return DiscoveredPath.from_metadata(value, "test path")


def amie_file(path: str, content: str) -> AmieFile:
    discovered = discovered_path(path)
    return AmieFile(path=discovered, content=content, sha="test-sha", raw={"path": path})


@dataclass
class FakeAmieReader:
    root_manifest: Mapping[str, Any]
    files: dict[str, AmieFile]
    reads: list[str] = field(default_factory=list)

    async def read_bootstrap(self) -> AmieFile:
        self.reads.append("/instructions/start.md")
        return self.files["/instructions/start.md"]

    async def read_root_manifest(self) -> Mapping[str, Any]:
        return self.root_manifest

    async def read_file(self, path: DiscoveredPath) -> AmieFile:
        self.reads.append(path.value)
        return self.files[path.value]


def reader() -> FakeAmieReader:
    return FakeAmieReader(
        root_manifest={
            "personas": [
                {
                    "id": "personas",
                    "path": "/personas",
                    "manifest": "/personas/manifest.json",
                    "unknown_container_field": {"preserved": True},
                }
            ]
        },
        files={
            "/instructions/start.md": amie_file("/instructions/start.md", "# AMIE\n"),
            "/personas/manifest.json": amie_file(
                "/personas/manifest.json",
                """{
  "content_type": "persona",
  "personas": [
    {
      "id": "sam",
      "path": "/personas/profiles/sam.md",
      "related_files": [
        {"path": "/personas/coldstarts/sam.md", "relation": "coldstart"}
      ],
      "future_field": ["preserved"]
    }
  ]
}""",
            ),
            "/personas/profiles/sam.md": amie_file(
                "/personas/profiles/sam.md", "# Sam profile\n"
            ),
            "/personas/coldstarts/sam.md": amie_file(
                "/personas/coldstarts/sam.md", "# Sam coldstart\n"
            ),
        },
    )


async def test_traverses_root_and_declared_nested_manifest_only() -> None:
    fake_reader = reader()
    discovery = AmieDiscovery(fake_reader)

    bootstrap = await discovery.load_bootstrap()
    root = await discovery.load_root_manifest()
    personas = root.entries[0]
    nested = await discovery.load_nested_manifest(personas)

    assert bootstrap.content == "# AMIE\n"
    assert personas.raw["unknown_container_field"] == {"preserved": True}
    assert nested.entries[0].content_type == "persona"
    assert nested.entries[0].raw["future_field"] == ["preserved"]
    assert fake_reader.reads == ["/instructions/start.md", "/personas/manifest.json"]


async def test_distinguishes_container_and_leaf_entries() -> None:
    discovery = AmieDiscovery(reader())
    root = await discovery.load_root_manifest()
    container = root.entries[0]
    leaf = (await discovery.load_nested_manifest(container)).entries[0]

    with pytest.raises(AmieDiscoveryError, match="traversed through their manifest"):
        await discovery.load_leaf_file(container)

    loaded = await discovery.load_leaf_file(leaf)

    assert loaded.path.value == "/personas/profiles/sam.md"
    assert loaded.content == "# Sam profile\n"


async def test_loads_only_a_declared_related_file() -> None:
    discovery = AmieDiscovery(reader())
    root = await discovery.load_root_manifest()
    leaf = (await discovery.load_nested_manifest(root.entries[0])).entries[0]

    coldstart = await discovery.load_related_file(leaf, "coldstart")

    assert coldstart.path.value == "/personas/coldstarts/sam.md"
    with pytest.raises(AmieDiscoveryError, match="does not declare"):
        await discovery.load_related_file(leaf, "profile")


def test_rejects_unsafe_paths_declared_in_manifest_metadata() -> None:
    with pytest.raises(AmieMetadataError, match="without traversal"):
        Manifest.from_data(
            {
                "items": [
                    {"id": "unsafe", "path": "/items/../outside.md"},
                ]
            }
        )
