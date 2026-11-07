"""FastAPI management surface for generic registered connections."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from mabel.api.ownership import DevelopmentOwnerContext
from mabel.connections.models import Connection
from mabel.services.connections import (
    ConnectionDisabledError,
    ConnectionNotFoundError,
    ConnectionService,
    ConnectionValidationError,
)


class CreateConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1)
    connection_type: str
    transport: str
    endpoint: str = Field(min_length=1)
    amie_api_token: str | None = Field(default=None, min_length=1)


class UpdateConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1)
    endpoint: str | None = Field(default=None, min_length=1)
    enabled: bool | None = None
    amie_api_token: str | None = Field(default=None, min_length=1)
    clear_amie_api_token: bool = False


class ConnectionResponse(BaseModel):
    id: UUID
    display_name: str
    connection_type: str
    transport: str
    endpoint: str
    credential_configured: bool
    enabled: bool
    created_at: datetime
    updated_at: datetime


def create_connection_router(
    service: ConnectionService, owner_context: DevelopmentOwnerContext, require_csrf: object | None = None
) -> APIRouter:
    """Create routes that delegate all connection semantics to one service."""
    router = APIRouter(prefix="/api/connections", tags=["connections"])
    owner = Annotated[str, Depends(owner_context.require_owner_id)]

    unsafe = [] if require_csrf is None else [Depends(require_csrf)]

    @router.post("", response_model=ConnectionResponse, status_code=status.HTTP_201_CREATED, dependencies=unsafe)
    async def create_connection(request: CreateConnectionRequest, owner_id: owner) -> ConnectionResponse:
        try:
            connection = service.create(owner_id=owner_id, **request.model_dump())
        except ConnectionValidationError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
        return _response(connection)

    @router.get("", response_model=list[ConnectionResponse])
    async def list_connections(owner_id: owner) -> list[ConnectionResponse]:
        return [_response(connection) for connection in service.list(owner_id)]

    @router.get("/{connection_id}", response_model=ConnectionResponse)
    async def get_connection(connection_id: UUID, owner_id: owner) -> ConnectionResponse:
        return _response(_get(service, connection_id, owner_id))

    @router.patch("/{connection_id}", response_model=ConnectionResponse, dependencies=unsafe)
    async def update_connection(
        connection_id: UUID, request: UpdateConnectionRequest, owner_id: owner
    ) -> ConnectionResponse:
        if request.clear_amie_api_token and "amie_api_token" in request.model_fields_set:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="AMIE API token cannot be set and cleared together.",
            )
        changes: dict[str, object] = {}
        for field in ("display_name", "endpoint", "enabled", "amie_api_token"):
            if field in request.model_fields_set:
                changes[field] = getattr(request, field)
        if request.clear_amie_api_token:
            changes["clear_amie_api_token"] = True
        try:
            connection = service.update(connection_id, owner_id, **changes)
        except ConnectionNotFoundError as error:
            raise _not_found(error) from error
        except ConnectionValidationError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
        return _response(connection)

    @router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=unsafe)
    async def delete_connection(connection_id: UUID, owner_id: owner) -> Response:
        try:
            service.delete(connection_id, owner_id)
        except ConnectionNotFoundError as error:
            raise _not_found(error) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/{connection_id}/test", dependencies=unsafe)
    async def test_connection(connection_id: UUID, owner_id: owner) -> dict[str, object]:
        try:
            return (await service.test(connection_id, owner_id)).as_dict()
        except ConnectionNotFoundError as error:
            raise _not_found(error) from error
        except ConnectionDisabledError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error

    return router


def _response(connection: Connection) -> ConnectionResponse:
    return ConnectionResponse(
        id=connection.id,
        display_name=connection.display_name,
        connection_type=connection.connection_type,
        transport=connection.transport,
        endpoint=connection.endpoint,
        credential_configured=connection.credential_id is not None,
        enabled=connection.enabled,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
    )


def _get(service: ConnectionService, connection_id: UUID, owner_id: str) -> Connection:
    try:
        return service.get(connection_id, owner_id)
    except ConnectionNotFoundError as error:
        raise _not_found(error) from error


def _not_found(error: ConnectionNotFoundError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
