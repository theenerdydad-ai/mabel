"""Trusted construction of AMIE readers from registered connections."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from mabel.amie.mcp import AmieMcpReader
from mabel.amie.reader import AmieReader
from mabel.connections.credentials import CredentialProvider
from mabel.connections.models import Connection


class ReaderFactory(Protocol):
    """Open a temporary reader for a supported registered connection."""

    def open(self, connection: Connection) -> AsyncIterator[AmieReader]:
        """Yield a reader and close its transport when use completes."""


class UnsupportedConnectionError(ValueError):
    """Raised when no trusted reader implementation exists for a connection."""


class AmieMcpStreamableHttpReaderFactory:
    """Open the currently supported AMIE Streamable HTTP transport."""

    def __init__(self, credential_provider: CredentialProvider) -> None:
        self._credential_provider = credential_provider

    @asynccontextmanager
    async def open(self, connection: Connection) -> AsyncIterator[AmieReader]:
        if connection.connection_type != "amie" or connection.transport != "mcp_streamable_http":
            raise UnsupportedConnectionError("No reader is registered for this connection type.")

        authentication = None
        if connection.credential_id is not None:
            authentication = await self._credential_provider.resolve(str(connection.credential_id))

        transport = StreamableHttpTransport(connection.endpoint, auth=authentication)
        client = Client(transport)
        async with client:
            yield AmieMcpReader(client)
