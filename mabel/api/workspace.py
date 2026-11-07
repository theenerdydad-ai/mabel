"""Thin portal adapter for the shared read-only context workspace service."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from mabel.api.ownership import DevelopmentOwnerContext
from mabel.services.workspace import ContextWorkspaceService, WorkspaceServiceError


def create_workspace_router(
    service: ContextWorkspaceService, owner_context: DevelopmentOwnerContext, require_csrf: object | None = None
) -> APIRouter:
    router = APIRouter(prefix="/api/portal/workspace", tags=["portal workspace"])
    owner = Annotated[str, Depends(owner_context.require_owner_id)]
    unsafe = [] if require_csrf is None else [Depends(require_csrf)]

    @router.get("")
    async def get_workspace(owner_id: owner) -> dict[str, object]:
        return await _call(service.snapshot, owner_id)

    @router.post("/refresh", dependencies=unsafe)
    async def refresh_workspace(owner_id: owner) -> dict[str, object]:
        return await _call(service.snapshot, owner_id)

    @router.get("/tree")
    async def get_tree(owner_id: owner, commit: str = Query(min_length=40, max_length=40)) -> dict[str, object]:
        return await _call(service.tree, owner_id, commit)

    @router.get("/file")
    async def get_file(
        owner_id: owner, path: str = Query(min_length=1), commit: str = Query(min_length=40, max_length=40)
    ) -> dict[str, object]:
        return await _call(service.read_file, owner_id, commit, path)

    @router.get("/capabilities/{capability_id}")
    async def get_capability(
        capability_id: str, owner_id: owner, commit: str = Query(min_length=40, max_length=40)
    ) -> dict[str, object]:
        return await _call(service.capability_detail, owner_id, commit, capability_id)

    @router.post("/capabilities/{capability_id}/run", dependencies=unsafe)
    async def run_capability(capability_id: str, owner_id: owner) -> dict[str, object]:
        return await _call(service.run_capability, owner_id, capability_id)

    return router


async def _call(call: object, *arguments: object) -> dict[str, object]:
    try:
        result = await call(*arguments)
        return result.as_dict() if hasattr(result, "as_dict") else result
    except WorkspaceServiceError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
