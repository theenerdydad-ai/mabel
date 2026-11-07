"""Trusted boundary for encrypted AMIE connection bearer credentials."""

import base64
import hashlib
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken

from mabel.state.database import SqliteDatabase


class CredentialUnavailableError(RuntimeError):
    """Raised when trusted code cannot resolve an opaque credential reference."""


class CredentialProvider(Protocol):
    """Resolve a reference to invocation-scoped transport authentication only."""

    async def resolve(self, reference: str) -> object:
        """Return authentication for one trusted transport invocation."""


class UnconfiguredCredentialProvider:
    """Safe default until MABEL is given a trusted secret-store integration."""

    async def resolve(self, reference: str) -> object:
        raise CredentialUnavailableError("No credential provider is configured.")


class AmieConnectionCredentialStore(CredentialProvider):
    """Store AMIE API tokens separately from connection metadata.

    The returned value exists only long enough for trusted HTTP transport setup;
    it is never included in a connection record or API response.
    """

    def __init__(self, database: SqliteDatabase, server_secret: str) -> None:
        self._database = database
        self._cipher = _cipher(server_secret)

    def store(self, owner_id: str, token: str) -> UUID:
        credential_id = uuid4()
        now = _timestamp()
        with self._database.connect() as database:
            database.execute(
                """
                INSERT INTO amie_connection_credentials (
                    id, owner_id, encrypted_token, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(credential_id),
                    owner_id,
                    self._cipher.encrypt(_token(token).encode("utf-8")).decode("utf-8"),
                    now,
                    now,
                ),
            )
        return credential_id

    def replace(self, credential_id: UUID, owner_id: str, token: str) -> None:
        with self._database.connect() as database:
            result = database.execute(
                """
                UPDATE amie_connection_credentials
                SET encrypted_token = ?, updated_at = ?
                WHERE id = ? AND owner_id = ?
                """,
                (
                    self._cipher.encrypt(_token(token).encode("utf-8")).decode("utf-8"),
                    _timestamp(),
                    str(credential_id),
                    owner_id,
                ),
            )
        if result.rowcount != 1:
            raise CredentialUnavailableError("AMIE connection credential was not found.")

    def delete(self, credential_id: UUID, owner_id: str) -> None:
        with self._database.connect() as database:
            database.execute(
                "DELETE FROM amie_connection_credentials WHERE id = ? AND owner_id = ?",
                (str(credential_id), owner_id),
            )

    async def resolve(self, reference: str) -> object:
        try:
            credential_id = UUID(reference)
        except ValueError as error:
            raise CredentialUnavailableError("AMIE connection credential reference is invalid.") from error
        with self._database.connect() as database:
            row = database.execute(
                "SELECT encrypted_token FROM amie_connection_credentials WHERE id = ?",
                (str(credential_id),),
            ).fetchone()
        if row is None:
            raise CredentialUnavailableError("AMIE connection credential was not found.")
        try:
            return self._cipher.decrypt(row["encrypted_token"].encode("utf-8")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as error:
            raise CredentialUnavailableError("AMIE connection credential cannot be decrypted.") from error


def _cipher(server_secret: str) -> Fernet:
    if len(server_secret) < 32:
        raise ValueError("MABEL_SERVER_SECRET must be at least 32 characters long.")
    key_material = hashlib.sha256(
        f"{server_secret}:amie-connection-credentials:v1".encode("utf-8")
    ).digest()
    return Fernet(base64.urlsafe_b64encode(key_material))


def _token(value: str) -> str:
    token = value.strip()
    if not token.startswith("ace_") or len(token) <= len("ace_"):
        raise ValueError("An AMIE API token beginning with 'ace_' is required.")
    return token


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
