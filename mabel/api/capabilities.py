"""Bearer-authenticated machine API for declared user capability operations."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from mabel.auth.models import ApiIdentity
from mabel.services.capabilities import (
    CapabilityArgumentsError,
    CapabilityService,
    CapabilityServiceError,
    operation_failure,
)


class RunCapabilitySuccessResponse(BaseModel):
    ok: bool
    capability_id: str
    repository_commit: str
    result: dict[str, object]


class RunCapabilityFailureResponse(BaseModel):
    ok: bool
    capability_id: str | None = None
    error: dict[str, str]


class CapabilityIndexEntryResponse(BaseModel):
    """One execution-eligible capability and its authoritative input schema."""

    id: str
    title: str
    path: str
    version: str
    input_contract: dict[str, object]


class CapabilityIndexResponse(BaseModel):
    """Commit-pinned, self-describing capability discovery response."""

    repository_commit: str
    capabilities: list[CapabilityIndexEntryResponse]


def create_capability_router(service: CapabilityService, require_bearer_identity: object) -> APIRouter:
    """Adapt the shared capability service; no repository semantics live in HTTP handlers."""
    router = APIRouter(prefix="/api/machine", tags=["capabilities"])
    identity = Annotated[ApiIdentity, Depends(require_bearer_identity)]

    @router.get("/mabel-manifest")
    async def get_mabel_manifest(caller: identity) -> dict[str, object]:
        return await _manifest(service, caller.owner_id)

    @router.get("/capability-index", response_model=CapabilityIndexResponse)
    async def get_capability_index(caller: identity) -> CapabilityIndexResponse:
        return CapabilityIndexResponse(**(await _index(service, caller.owner_id)))

    @router.post(
        "/capabilities/{capability_id}/run",
        response_model=RunCapabilitySuccessResponse | RunCapabilityFailureResponse,
    )
    async def run_capability(
        capability_id: str, caller: identity, request: Request
    ) -> RunCapabilitySuccessResponse | RunCapabilityFailureResponse:
        try:
            arguments = await _arguments_from_request(request)
            return RunCapabilitySuccessResponse(
                **(await service.run_capability(caller.owner_id, capability_id, arguments)).as_dict()
            )
        except CapabilityServiceError as error:
            return RunCapabilityFailureResponse(**operation_failure(error, capability_id).as_dict())

    return router


async def _arguments_from_request(request: Request) -> dict[str, object] | None:
    """Parse a small body without reflecting rejected caller content in errors."""
    if not await request.body():
        return None
    try:
        payload = await request.json()
    except ValueError as error:
        raise CapabilityArgumentsError(CapabilityArgumentsError.code) from error
    if not isinstance(payload, dict) or set(payload) != {"arguments"}:
        raise CapabilityArgumentsError(CapabilityArgumentsError.code)
    arguments = payload["arguments"]
    if arguments is not None and not isinstance(arguments, dict):
        raise CapabilityArgumentsError(CapabilityArgumentsError.code)
    return arguments


async def _manifest(service: CapabilityService, owner_id: str) -> dict[str, object]:
    try:
        return await service.get_manifest(owner_id)
    except CapabilityServiceError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


async def _index(service: CapabilityService, owner_id: str) -> dict[str, object]:
    try:
        return await service.get_capability_index(owner_id)
    except CapabilityServiceError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
