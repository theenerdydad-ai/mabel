"""Lifecycle and bounded verification of registered AMIE connections."""

from dataclasses import replace
from datetime import datetime, timezone
from typing import Final
from urllib.parse import urlparse
from uuid import UUID, uuid4

from mabel.amie.discovery import AmieDiscovery, AmieDiscoveryError
from mabel.amie.mcp import AmieMcpError
from mabel.connections.credentials import AmieConnectionCredentialStore, CredentialUnavailableError
from mabel.connections.models import Connection, ConnectionTestResult
from mabel.connections.readers import ReaderFactory, UnsupportedConnectionError
from mabel.state.connections import ConnectionStore


class ConnectionError(ValueError):
    """Base error for safe connection lifecycle failures."""


class ConnectionNotFoundError(ConnectionError):
    """Raised when an owner cannot access a connection."""


class ConnectionDisabledError(ConnectionError):
    """Raised when an invocation targets a disabled connection."""


class ConnectionValidationError(ConnectionError):
    """Raised when connection metadata is unsupported or malformed."""


_UNSET: Final = object()


class ConnectionService:
    """Own connection lifecycle semantics independently of HTTP or MCP."""

    def __init__(
        self,
        store: ConnectionStore,
        reader_factory: ReaderFactory,
        credentials: AmieConnectionCredentialStore,
    ) -> None:
        self._store = store
        self._reader_factory = reader_factory
        self._credentials = credentials

    def create(
        self,
        *,
        owner_id: str,
        display_name: str,
        connection_type: str,
        transport: str,
        endpoint: str,
        amie_api_token: str | None = None,
    ) -> Connection:
        self._validate(owner_id, display_name, connection_type, transport, endpoint)
        try:
            credential_id = self._credentials.store(owner_id, amie_api_token) if amie_api_token is not None else None
        except ValueError as error:
            raise ConnectionValidationError(str(error)) from error
        now = _now()
        connection = Connection(
            id=uuid4(),
            owner_id=owner_id,
            display_name=display_name.strip(),
            connection_type=connection_type,
            transport=transport,
            endpoint=endpoint.strip(),
            credential_id=credential_id,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        try:
            return self._store.create(connection)
        except Exception:
            if credential_id is not None:
                self._credentials.delete(credential_id, owner_id)
            raise

    def list(self, owner_id: str) -> list[Connection]:
        return list(self._store.list_for_owner(owner_id))

    def get(self, connection_id: UUID, owner_id: str) -> Connection:
        connection = self._store.get_for_owner(connection_id, owner_id)
        if connection is None:
            raise ConnectionNotFoundError("Connection was not found.")
        return connection

    def update(
        self,
        connection_id: UUID,
        owner_id: str,
        *,
        display_name: str | object = _UNSET,
        endpoint: str | object = _UNSET,
        enabled: bool | object = _UNSET,
        amie_api_token: str | None | object = _UNSET,
        clear_amie_api_token: bool = False,
    ) -> Connection:
        current = self.get(connection_id, owner_id)
        if amie_api_token is not _UNSET and clear_amie_api_token:
            raise ConnectionValidationError("AMIE API token cannot be set and cleared together.")

        credential_id = current.credential_id
        created_credential_id: UUID | None = None
        if amie_api_token is not _UNSET:
            if not isinstance(amie_api_token, str):
                raise ConnectionValidationError("AMIE API token must be a string.")
            try:
                created_credential_id = self._credentials.store(owner_id, amie_api_token)
            except ValueError as error:
                raise ConnectionValidationError(str(error)) from error
            credential_id = created_credential_id
        elif clear_amie_api_token:
            credential_id = None

        updated = replace(
            current,
            display_name=current.display_name if display_name is _UNSET else str(display_name).strip(),
            endpoint=current.endpoint if endpoint is _UNSET else str(endpoint).strip(),
            enabled=current.enabled if enabled is _UNSET else bool(enabled),
            credential_id=credential_id,
            updated_at=_now(),
        )
        try:
            self._validate(
                updated.owner_id,
                updated.display_name,
                updated.connection_type,
                updated.transport,
                updated.endpoint,
            )
            saved = self._store.update(updated)
        except Exception:
            if created_credential_id is not None:
                self._credentials.delete(created_credential_id, owner_id)
            raise
        if current.credential_id is not None and current.credential_id != saved.credential_id:
            self._credentials.delete(current.credential_id, owner_id)
        return saved

    def delete(self, connection_id: UUID, owner_id: str) -> None:
        connection = self.get(connection_id, owner_id)
        if not self._store.delete_for_owner(connection_id, owner_id):
            raise ConnectionNotFoundError("Connection was not found.")
        if connection.credential_id is not None:
            self._credentials.delete(connection.credential_id, owner_id)

    async def test(self, connection_id: UUID, owner_id: str) -> ConnectionTestResult:
        connection = self.get(connection_id, owner_id)
        if not connection.enabled:
            raise ConnectionDisabledError("Disabled connections cannot be tested.")

        try:
            async with self._reader_factory.open(connection) as reader:
                discovery = AmieDiscovery(reader)
                bootstrap = await discovery.load_bootstrap()
                root_manifest = await discovery.load_root_manifest()
        except CredentialUnavailableError:
            return _failed_test(connection.id, "credential_unavailable")
        except UnsupportedConnectionError:
            return _failed_test(connection.id, "unsupported_connection")
        except (AmieDiscoveryError, AmieMcpError, ValueError):
            return _failed_test(connection.id, "invalid_amie_response")
        except Exception:
            # Transport and remote validation details can contain sensitive endpoint context.
            return _failed_test(connection.id, "connection_failed")

        return ConnectionTestResult(
            ok=True,
            connection_id=connection.id,
            bootstrap_loaded=True,
            bootstrap_sha=bootstrap.sha,
            root_manifest_loaded=True,
            collection_count=len(root_manifest.entries),
        )

    @staticmethod
    def _validate(
        owner_id: str,
        display_name: str,
        connection_type: str,
        transport: str,
        endpoint: str,
    ) -> None:
        if not owner_id.strip():
            raise ConnectionValidationError("Connection owner is required.")
        if not display_name.strip():
            raise ConnectionValidationError("Connection display name is required.")
        if connection_type != "amie" or transport != "mcp_streamable_http":
            raise ConnectionValidationError("Only AMIE Streamable HTTP connections are supported.")
        parsed_endpoint = urlparse(endpoint)
        if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.netloc:
            raise ConnectionValidationError("Connection endpoint must be an absolute HTTP URL.")
        if parsed_endpoint.username is not None or parsed_endpoint.password is not None:
            raise ConnectionValidationError("Connection endpoint must not contain credentials.")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _failed_test(connection_id: UUID, error_code: str) -> ConnectionTestResult:
    return ConnectionTestResult(
        ok=False,
        connection_id=connection_id,
        bootstrap_loaded=False,
        bootstrap_sha=None,
        root_manifest_loaded=False,
        collection_count=None,
        error_code=error_code,
    )
