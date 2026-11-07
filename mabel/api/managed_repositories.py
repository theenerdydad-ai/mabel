"""Thin FastAPI management surface for registered MABEL context repositories."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from mabel.api.ownership import DevelopmentOwnerContext
from mabel.connections.credentials import CredentialUnavailableError
from mabel.repositories.github import RepositoryAuthenticationError, RepositoryProviderError
from mabel.repositories.models import ManagedRepository
from mabel.services.managed_repositories import (
    ManagedRepositoryConflictError,
    ManagedRepositoryDisabledError,
    ManagedRepositoryNotFoundError,
    ManagedRepositoryService,
    ManagedRepositoryValidationError,
)


class RegisterManagedRepositoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1)
    provider: str
    purpose: str
    repository: str = Field(min_length=1)
    github_token: str = Field(min_length=1)


class UpdateManagedRepositoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1)
    enabled: bool | None = None


class ReconnectManagedRepositoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    github_token: str = Field(min_length=1)


class ManagedRepositoryResponse(BaseModel):
    id: UUID
    provider: str
    purpose: str
    display_name: str
    provider_repository_id: str
    canonical_locator: str
    default_branch: str
    status: str
    enabled: bool
    credential_configured: bool
    created_at: datetime
    updated_at: datetime


def create_managed_repository_router(
    service: ManagedRepositoryService, owner_context: DevelopmentOwnerContext, require_csrf: object | None = None
) -> APIRouter:
    router = APIRouter(prefix="/api/managed-repositories", tags=["managed repositories"])
    owner = Annotated[str, Depends(owner_context.require_owner_id)]
    unsafe = [] if require_csrf is None else [Depends(require_csrf)]

    @router.post("", response_model=ManagedRepositoryResponse, status_code=status.HTTP_201_CREATED, dependencies=unsafe)
    async def register_repository(
        request: RegisterManagedRepositoryRequest, owner_id: owner
    ) -> ManagedRepositoryResponse:
        try:
            repository = await service.register(owner_id=owner_id, **request.model_dump())
        except ManagedRepositoryValidationError as error:
            raise _unprocessable(error) from error
        except ManagedRepositoryConflictError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except CredentialUnavailableError as error:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
        except RepositoryAuthenticationError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
        except RepositoryProviderError as error:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
        return _response(repository)

    @router.get("", response_model=list[ManagedRepositoryResponse])
    async def list_repositories(owner_id: owner) -> list[ManagedRepositoryResponse]:
        return [_response(repository) for repository in service.list(owner_id)]

    @router.get("/{repository_id}", response_model=ManagedRepositoryResponse)
    async def get_repository(repository_id: UUID, owner_id: owner) -> ManagedRepositoryResponse:
        return _response(_get(service, repository_id, owner_id))

    @router.patch("/{repository_id}", response_model=ManagedRepositoryResponse, dependencies=unsafe)
    async def update_repository(
        repository_id: UUID, request: UpdateManagedRepositoryRequest, owner_id: owner
    ) -> ManagedRepositoryResponse:
        changes = {
            field: getattr(request, field)
            for field in ("display_name", "enabled")
            if field in request.model_fields_set
        }
        try:
            return _response(service.update(repository_id, owner_id, **changes))
        except ManagedRepositoryNotFoundError as error:
            raise _not_found(error) from error
        except ManagedRepositoryValidationError as error:
            raise _unprocessable(error) from error

    @router.post("/{repository_id}/test", dependencies=unsafe)
    async def test_repository(repository_id: UUID, owner_id: owner) -> dict[str, object]:
        try:
            return (await service.test(repository_id, owner_id)).as_dict()
        except ManagedRepositoryNotFoundError as error:
            raise _not_found(error) from error
        except ManagedRepositoryDisabledError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error

    @router.post("/{repository_id}/reconnect", response_model=ManagedRepositoryResponse, dependencies=unsafe)
    async def reconnect_repository(
        repository_id: UUID, request: ReconnectManagedRepositoryRequest, owner_id: owner
    ) -> ManagedRepositoryResponse:
        try:
            return _response(await service.reconnect(repository_id, owner_id, request.github_token))
        except ManagedRepositoryNotFoundError as error:
            raise _not_found(error) from error
        except ManagedRepositoryConflictError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except ManagedRepositoryValidationError as error:
            raise _unprocessable(error) from error
        except RepositoryAuthenticationError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
        except CredentialUnavailableError as error:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
        except RepositoryProviderError as error:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error

    @router.delete("/{repository_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=unsafe)
    async def delete_repository(repository_id: UUID, owner_id: owner) -> Response:
        try:
            service.delete(repository_id, owner_id)
        except ManagedRepositoryNotFoundError as error:
            raise _not_found(error) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


def _response(repository: ManagedRepository) -> ManagedRepositoryResponse:
    return ManagedRepositoryResponse(
        id=repository.id, provider=repository.provider, purpose=repository.purpose,
        display_name=repository.display_name, provider_repository_id=repository.provider_repository_id,
        canonical_locator=repository.canonical_locator, default_branch=repository.default_branch,
        status=repository.status, enabled=repository.enabled, credential_configured=True,
        created_at=repository.created_at, updated_at=repository.updated_at,
    )


def _get(service: ManagedRepositoryService, repository_id: UUID, owner_id: str) -> ManagedRepository:
    try:
        return service.get(repository_id, owner_id)
    except ManagedRepositoryNotFoundError as error:
        raise _not_found(error) from error


def _not_found(error: ManagedRepositoryNotFoundError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))


def _unprocessable(error: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error))
