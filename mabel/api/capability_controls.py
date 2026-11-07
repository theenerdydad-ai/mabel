"""Browser management API for MABEL-owned capability controls."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from mabel.api.ownership import DevelopmentOwnerContext
from mabel.services.capability_controls import (
    CapabilityControlService,
    CapabilityControlServiceError,
    CapabilityNotDeclaredError,
)


class UpdateCapabilityControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


def create_capability_control_router(
    service: CapabilityControlService,
    owner_context: DevelopmentOwnerContext,
    require_csrf: object | None = None,
) -> APIRouter:
    """Expose only authenticated owners' mutable capability state."""
    router = APIRouter(prefix="/api/portal/capabilities", tags=["portal capabilities"])
    owner = Annotated[str, Depends(owner_context.require_owner_id)]
    unsafe = [] if require_csrf is None else [Depends(require_csrf)]

    @router.get("")
    async def list_capabilities(owner_id: owner) -> dict[str, object]:
        return await _call(service.list, owner_id)

    @router.patch("/{capability_id}", dependencies=unsafe)
    async def update_capability(
        capability_id: str, request: UpdateCapabilityControlRequest, owner_id: owner
    ) -> dict[str, object]:
        return await _call(service.set_enabled, owner_id, capability_id, request.enabled)

    @router.post("/{capability_id}/validate", dependencies=unsafe)
    async def validate_capability(capability_id: str, owner_id: owner) -> dict[str, object]:
        return await _call(service.validate, owner_id, capability_id)

    return router


async def _call(call: object, *arguments: object) -> dict[str, object]:
    try:
        return await call(*arguments)
    except CapabilityNotDeclaredError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except CapabilityControlServiceError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
