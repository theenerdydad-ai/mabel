"""Portal token management and a minimal machine-facing identity endpoint."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from mabel.api.ownership import DevelopmentOwnerContext
from mabel.auth.models import ApiIdentity
from mabel.services.api_tokens import ApiTokenService


class ApiTokenStatusResponse(BaseModel):
    has_token: bool


class IssuedApiTokenResponse(BaseModel):
    """The plaintext token is intentionally only constructed for this response."""

    token: str
    created_at: datetime


class MachineIdentityResponse(BaseModel):
    owner_id: str
    authentication: str


def create_access_router(
    service: ApiTokenService,
    owner_context: DevelopmentOwnerContext,
    require_bearer_identity: object,
    require_csrf: object | None = None,
) -> APIRouter:
    """Expose browser-managed issuance separately from bearer machine identity."""
    router = APIRouter(tags=["api-access"])
    owner = Annotated[str, Depends(owner_context.require_owner_id)]
    identity = Annotated[ApiIdentity, Depends(require_bearer_identity)]
    unsafe = [] if require_csrf is None else [Depends(require_csrf)]

    @router.get("/api/access/token-status", response_model=ApiTokenStatusResponse)
    async def token_status(owner_id: owner) -> ApiTokenStatusResponse:
        return ApiTokenStatusResponse(**service.status(owner_id).__dict__)

    @router.post("/api/access/token", response_model=IssuedApiTokenResponse, dependencies=unsafe)
    async def generate_token(owner_id: owner) -> IssuedApiTokenResponse:
        issued = service.generate_or_regenerate(owner_id)
        return IssuedApiTokenResponse(token=issued.token, created_at=issued.created_at)

    @router.get("/api/machine/identity", response_model=MachineIdentityResponse)
    async def machine_identity(caller: identity) -> MachineIdentityResponse:
        return MachineIdentityResponse(owner_id=caller.owner_id, authentication=caller.authentication)

    return router
