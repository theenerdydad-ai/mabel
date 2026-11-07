"""Read-only Portal operations API for owner-scoped workflow executions."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from mabel.api.ownership import DevelopmentOwnerContext
from mabel.services.workflow_operations import (
    WorkflowExecutionNotFoundError,
    WorkflowExecutionOperationsError,
    WorkflowExecutionOperationsService,
)
from mabel.workflows.reporting import (
    serialize_workflow_execution_detail,
    serialize_workflow_execution_list_item,
)


def create_workflow_execution_router(
    service: WorkflowExecutionOperationsService,
    owner_context: DevelopmentOwnerContext,
) -> APIRouter:
    router = APIRouter(prefix="/api/portal/workflow-executions", tags=["portal workflow executions"])
    owner = Annotated[str, Depends(owner_context.require_owner_id)]

    @router.get("")
    async def list_workflow_executions(
        owner_id: owner,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> dict[str, object]:
        try:
            records = service.list(owner_id, limit)
        except WorkflowExecutionOperationsError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Workflow execution state is unavailable.") from error
        return {
            "executions": [serialize_workflow_execution_list_item(record) for record in records.executions],
            "limit": records.limit,
            "has_more": records.has_more,
        }

    @router.get("/{execution_id}")
    async def get_workflow_execution(execution_id: str, owner_id: owner) -> dict[str, object]:
        try:
            execution = service.get(owner_id, execution_id)
        except WorkflowExecutionNotFoundError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow execution was not found.") from error
        except WorkflowExecutionOperationsError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Workflow execution state is unavailable.") from error
        return serialize_workflow_execution_detail(execution)

    return router
