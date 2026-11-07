"""Shared HTTP bearer extraction for MABEL machine-facing front doors."""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from mabel.auth.models import ApiIdentity
from mabel.services.api_tokens import ApiTokenService, InvalidBearerTokenError


_bearer_scheme = HTTPBearer(auto_error=False)


def create_bearer_identity_dependency(service: ApiTokenService):
    """Create an HTTP dependency backed by the same resolver MCP calls."""

    async def require_bearer_identity(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    ) -> ApiIdentity:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise _unauthorized()
        try:
            return service.resolve_bearer_token(credentials.credentials)
        except InvalidBearerTokenError as error:
            raise _unauthorized() from error

    return require_bearer_identity


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid Authorization: Bearer <token> header.",
        headers={"WWW-Authenticate": "Bearer"},
    )
