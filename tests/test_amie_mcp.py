from dataclasses import dataclass, field
from typing import Any

import pytest

from mabel.amie.mcp import AmieMcpReader
from mabel.amie.models import AmieMetadataError, DiscoveredPath


@dataclass
class ToolResult:
    data: Any


@dataclass
class FakeMcpClient:
    responses: dict[tuple[str, str | None], Any]
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    async def call_tool(self, name: str, arguments: dict[str, object]) -> ToolResult:
        self.calls.append((name, arguments))
        key = (name, arguments.get("path") if isinstance(arguments.get("path"), str) else None)
        return ToolResult(data=self.responses[key])


async def test_amie_mcp_reader_maps_current_read_tools_and_preserves_response_fields() -> None:
    client = FakeMcpClient(
        responses={
            ("getManifest", None): {"projects": [{"id": "projects", "path": "/projects"}]},
            ("getFile", "/instructions/start.md"): {
                "ok": True,
                "path": "/instructions/start.md",
                "content": "# AMIE\n",
                "sha": "bootstrap-sha",
                "operating_context": {"preserved": True},
            },
            ("getFile", "/projects/manifest.json"): {
                "ok": True,
                "path": "/projects/manifest.json",
                "content": "{}",
                "sha": "projects-sha",
            },
        }
    )
    reader = AmieMcpReader(client)

    bootstrap = await reader.read_bootstrap()
    root = await reader.read_root_manifest()
    projects = await reader.read_file(
        DiscoveredPath.from_metadata("/projects/manifest.json", "test path")
    )

    assert bootstrap.raw["operating_context"] == {"preserved": True}
    assert root["projects"][0]["id"] == "projects"
    assert projects.sha == "projects-sha"
    assert client.calls == [
        ("getFile", {"path": "/instructions/start.md"}),
        ("getManifest", {}),
        ("getFile", {"path": "/projects/manifest.json"}),
    ]


async def test_amie_mcp_reader_rejects_a_non_discovered_path() -> None:
    reader = AmieMcpReader(FakeMcpClient(responses={}))

    with pytest.raises(AmieMetadataError, match="originate from discovered metadata"):
        await reader.read_file(DiscoveredPath("/arbitrary.md", object()))
