"""Current AMIE MCP transport for the generic read contract."""

from collections.abc import Mapping
from typing import Any, Protocol

from mabel.amie.models import AmieFile, DiscoveredPath


class AmieMcpError(RuntimeError):
    """Raised when AMIE MCP does not return a valid read response."""


class McpToolCaller(Protocol):
    """The narrow FastMCP client surface this transport needs."""

    async def call_tool(self, name: str, arguments: dict[str, object]) -> Any:
        """Call an MCP tool and return its client result."""


class AmieMcpReader:
    """Map AMIE's current `getManifest` and `getFile` MCP tools to AmieReader."""

    _BOOTSTRAP_PATH = DiscoveredPath.from_metadata(
        "/instructions/start.md", "AMIE bootstrap path"
    )

    def __init__(self, client: McpToolCaller) -> None:
        self._client = client

    async def read_bootstrap(self) -> AmieFile:
        return await self._read_file(self._BOOTSTRAP_PATH)

    async def read_root_manifest(self) -> Mapping[str, Any]:
        data = await self._tool_data("getManifest", {})
        if not isinstance(data, Mapping):
            raise AmieMcpError("AMIE getManifest did not return an object.")
        return data

    async def read_file(self, path: DiscoveredPath) -> AmieFile:
        path.require_discovered()
        return await self._read_file(path)

    async def _read_file(self, path: DiscoveredPath) -> AmieFile:
        data = await self._tool_data("getFile", {"path": path.value})
        if not isinstance(data, Mapping):
            raise AmieMcpError("AMIE getFile did not return an object.")
        content = data.get("content")
        returned_path = data.get("path")
        sha = data.get("sha")
        if not isinstance(content, str) or not isinstance(returned_path, str):
            raise AmieMcpError("AMIE getFile response is missing path or content.")
        if returned_path != path.value:
            raise AmieMcpError("AMIE getFile returned a path different from the requested path.")
        return AmieFile(
            path=path,
            content=content,
            sha=sha if isinstance(sha, str) else None,
            raw=data,
        )

    async def _tool_data(self, name: str, arguments: dict[str, object]) -> Any:
        result = await self._client.call_tool(name, arguments)
        data = getattr(result, "data", None)
        if data is None:
            raise AmieMcpError(f"AMIE MCP tool returned no structured data: {name}")
        return data
