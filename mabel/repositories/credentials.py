"""Encrypted, repository-specific GitHub credential storage."""

import base64
import hashlib
from datetime import datetime, timezone
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken

from mabel.connections.credentials import CredentialProvider, CredentialUnavailableError
from mabel.state.database import SqliteDatabase


class GitHubTokenCredential:
    """Invocation-scoped token material for trusted GitHub adapter code only."""

    def __init__(self, token: str) -> None:
        self._token = token

    @property
    def token(self) -> str:
        return self._token


class ManagedRepositoryCredentialStore(CredentialProvider):
    """Store encrypted fine-grained tokens separately from repository metadata."""

    def __init__(self, database: SqliteDatabase, server_secret: str) -> None:
        self._database = database
        self._cipher = _cipher(server_secret)

    def store(self, owner_id: str, token: str) -> UUID:
        cipher = self._require_cipher()
        cleaned = _token(token)
        credential_id = uuid4()
        now = _timestamp()
        with self._database.connect() as database:
            database.execute(
                """
                INSERT INTO managed_repository_credentials (
                    id, owner_id, encrypted_secret, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (str(credential_id), owner_id, cipher.encrypt(cleaned.encode("utf-8")).decode("utf-8"), now, now),
            )
        return credential_id

    def replace(self, credential_id: UUID, owner_id: str, token: str) -> None:
        cipher = self._require_cipher()
        cleaned = _token(token)
        with self._database.connect() as database:
            result = database.execute(
                """
                UPDATE managed_repository_credentials
                SET encrypted_secret = ?, updated_at = ?
                WHERE id = ? AND owner_id = ?
                """,
                (cipher.encrypt(cleaned.encode("utf-8")).decode("utf-8"), _timestamp(), str(credential_id), owner_id),
            )
        if result.rowcount != 1:
            raise CredentialUnavailableError("Repository credential was not found.")

    def delete(self, credential_id: UUID, owner_id: str) -> None:
        with self._database.connect() as database:
            database.execute(
                "DELETE FROM managed_repository_credentials WHERE id = ? AND owner_id = ?",
                (str(credential_id), owner_id),
            )

    async def resolve(self, reference: str) -> object:
        cipher = self._require_cipher()
        try:
            credential_id = UUID(reference)
        except ValueError as error:
            raise CredentialUnavailableError("Repository credential reference is invalid.") from error
        with self._database.connect() as database:
            row = database.execute(
                "SELECT encrypted_secret FROM managed_repository_credentials WHERE id = ?",
                (str(credential_id),),
            ).fetchone()
        if row is None:
            raise CredentialUnavailableError("Repository credential was not found.")
        try:
            token = cipher.decrypt(row["encrypted_secret"].encode("utf-8")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as error:
            raise CredentialUnavailableError("Repository credential cannot be decrypted.") from error
        return GitHubTokenCredential(token)

    def _require_cipher(self) -> Fernet:
        return self._cipher


def _cipher(server_secret: str) -> Fernet:
    if len(server_secret) < 32:
        raise ValueError("MABEL_SERVER_SECRET must be at least 32 characters long.")
    key_material = hashlib.sha256(
        f"{server_secret}:managed-repository-credentials:v1".encode("utf-8")
    ).digest()
    return Fernet(base64.urlsafe_b64encode(key_material))


def _token(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("GitHub token is required.")
    return cleaned


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
