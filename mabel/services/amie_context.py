"""Trusted bridge from a declared MABEL primitive to ACE context operations."""

from collections.abc import Mapping

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from mabel.connections.credentials import CredentialProvider
from mabel.connections.models import Connection
from mabel.services.connections import ConnectionService


class AmieContextOperationError(ValueError):
    """Safe failure for the bounded ACE context-operation bridge."""


class AmieContextOperationService:
    """Call ACE's one context-binding MCP bridge with an owner-owned credential.

    Connection selection is deliberately not exposed to a capability caller.
    A MABEL owner must have exactly one enabled, credential-backed AMIE MCP
    connection for this trusted bridge.  This avoids silently choosing between
    multiple integrations with potentially different ACE accounts.
    """

    def __init__(self, connections: ConnectionService, credentials: CredentialProvider) -> None:
        self._connections = connections
        self._credentials = credentials

    async def run(
        self, owner_id: str, operation: str, arguments: Mapping[str, object] | None
    ) -> dict[str, object]:
        connection = self._connection(owner_id)
        try:
            token = await self._credentials.resolve(str(connection.credential_id))
            transport = StreamableHttpTransport(connection.endpoint, auth=token)
            async with Client(transport) as client:
                response = await client.call_tool(
                    "mabel_context_operation",
                    {"operation": operation, "arguments": dict(arguments or {})},
                )
            data = getattr(response, "data", None)
        except Exception as exc:
            # Endpoint and remote error details can carry sensitive metadata;
            # capability callers receive only the stable execution result.
            raise AmieContextOperationError("ACE context operation failed.") from exc
        if not isinstance(data, Mapping):
            raise AmieContextOperationError("ACE context operation returned an invalid result.")
        return dict(data)

    async def readiness_error_code(self, owner_id: str) -> str | None:
        """Return a bounded preflight result without contacting ACE."""
        try:
            connection = self._connection(owner_id)
            # Resolve only inside MABEL's trusted credential boundary.  The
            # value is deliberately discarded: readiness proves local
            # usability, not the remote ACE service or its bearer contents.
            await self._credentials.resolve(str(connection.credential_id))
        except Exception:
            return "amie_connection_not_configured"
        return None

    def _connection(self, owner_id: str) -> Connection:
        candidates = [
            connection
            for connection in self._connections.list(owner_id)
            if connection.connection_type == "amie"
            and connection.transport == "mcp_streamable_http"
            and connection.enabled
            and connection.credential_id is not None
        ]
        if len(candidates) != 1:
            raise AmieContextOperationError("A single enabled AMIE connection is required.")
        return candidates[0]
