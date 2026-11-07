"""Owner-scoped API-token lifecycle and bearer resolution."""

import hashlib
import secrets
from datetime import datetime, timezone

from mabel.auth.models import ApiIdentity, ApiTokenStatus, IssuedApiToken
from mabel.state.api_tokens import ApiTokenStore


class InvalidBearerTokenError(ValueError):
    """Raised without preserving a caller-supplied bearer token."""


class ApiTokenService:
    """Issue and resolve MABEL bearer tokens without retaining plaintext values."""

    _PREFIX = "mabel_"

    def __init__(self, store: ApiTokenStore) -> None:
        self._store = store

    def generate_or_regenerate(self, owner_id: str) -> IssuedApiToken:
        if not owner_id.strip():
            raise ValueError("API tokens require an owner.")
        token = f"{self._PREFIX}{secrets.token_urlsafe(32)}"
        created_at = datetime.now(timezone.utc)
        self._store.replace_for_owner(owner_id, _hash_token(token), created_at)
        return IssuedApiToken(token=token, created_at=created_at)

    def status(self, owner_id: str) -> ApiTokenStatus:
        return ApiTokenStatus(has_token=self._store.has_token_for_owner(owner_id))

    def resolve_bearer_token(self, token: str) -> ApiIdentity:
        if not token.startswith(self._PREFIX):
            raise InvalidBearerTokenError("Invalid API bearer token.")
        identity = self._store.identity_for_hash(_hash_token(token))
        if identity is None:
            raise InvalidBearerTokenError("Invalid API bearer token.")
        return identity


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
