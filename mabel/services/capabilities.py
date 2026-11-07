"""Owner-scoped capability discovery and the first bounded execution dispatch."""

from dataclasses import dataclass
import time
import re
from collections.abc import Mapping

from mabel.context.discovery import CapabilityContractError, validate_capability_contract
from mabel.context.execution import BoundedCapabilityRunner, CapabilityExecutionError
from mabel.context.projection import ContextProjectionError, ContextProjectionService
from mabel.diagnostics import log_capability_diagnostic
from mabel.services.notifications import NotificationArgumentsError, NotificationService
from mabel.services.amie_context import AmieContextOperationError, AmieContextOperationService
from mabel.services.capability_controls import CapabilityControlService
from mabel.workflows.capability import WorkflowCapabilityError, WorkflowExecutionCapability


class CapabilityServiceError(ValueError):
    """Safe application-level failure for capability requests."""

    code = "capability_unavailable"


class CapabilityArgumentsError(CapabilityServiceError):
    """A stable capability input rejection that never includes caller content."""

    code = "invalid_arguments"


class CapabilityExecutionFailureError(CapabilityServiceError):
    """A bounded failure from a declared executor."""

    code = "execution_failed"


@dataclass(frozen=True)
class CapabilityRunResult:
    capability_id: str
    repository_commit: str
    result: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": True,
            "capability_id": self.capability_id,
            "repository_commit": self.repository_commit,
            "result": self.result,
        }


@dataclass(frozen=True)
class CapabilityOperationFailure:
    """A safe, transport-independent response for an expected operation failure."""

    code: str
    capability_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        response: dict[str, object] = {
            "ok": False,
            "error": {"code": self.code},
        }
        if self.capability_id is not None:
            response["capability_id"] = self.capability_id
        return response


_CAPABILITY_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")


def operation_failure(error: CapabilityServiceError, capability_id: str | None = None) -> CapabilityOperationFailure:
    """Reduce a service error to a stable result that contains no internal detail."""
    safe_capability_id = (
        capability_id
        if isinstance(capability_id, str) and _CAPABILITY_ID.fullmatch(capability_id)
        else None
    )
    return CapabilityOperationFailure(
        code=getattr(error, "code", CapabilityServiceError.code),
        capability_id=safe_capability_id,
    )


@dataclass(frozen=True)
class CapabilityExecutionReport:
    """A commit-pinned, bounded capability run report for portal consumers."""

    capability_id: str
    repository_commit: str
    status: str
    elapsed_ms: int
    result: dict[str, object] | None
    stdout: str
    stderr: str
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        response: dict[str, object] = {
            "capability_id": self.capability_id,
            "repository_commit": self.repository_commit,
            "status": self.status,
            "elapsed_ms": self.elapsed_ms,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }
        if self.result is not None:
            response["result"] = self.result
        if self.error is not None:
            response["error"] = self.error
        return response


class CapabilityService:
    """Keep manifest discovery and process execution behind one shared service."""

    def __init__(
        self,
        projections: ContextProjectionService,
        runner: BoundedCapabilityRunner,
        notifications: NotificationService | None = None,
        amie_context: AmieContextOperationService | None = None,
        controls: CapabilityControlService | None = None,
        workflow_execution: WorkflowExecutionCapability | None = None,
    ) -> None:
        if controls is None:
            raise ValueError("Capability controls are required for execution eligibility enforcement.")
        self._projections = projections
        self._runner = runner
        self._notifications = notifications
        self._amie_context = amie_context
        self._controls = controls
        self._workflow_execution = workflow_execution
        if workflow_execution is not None:
            self._controls.configure_workflow_execution()

    def configure_workflow_execution(self, workflow_execution: WorkflowExecutionCapability) -> None:
        """Attach the one app-composed workflow service after handlers exist."""
        self._workflow_execution = workflow_execution
        self._controls.configure_workflow_execution()

    async def get_manifest(self, owner_id: str) -> dict[str, object]:
        projection = await self._projection(owner_id, "get_mabel_manifest")
        result = projection.context.manifest.as_dict()
        result["repository_commit"] = projection.commit_sha
        if projection.context.collection_errors:
            result["collection_errors"] = dict(projection.context.collection_errors)
        return result

    async def get_capability_index(self, owner_id: str) -> dict[str, object]:
        projection = await self._projection(owner_id, "get_capability_index")
        capabilities = []
        for summary in projection.context.capabilities:
            try:
                capability = projection.discovery.load_capability(summary.id)
            except Exception as error:
                log_capability_diagnostic(
                    "get_capability_index", "capability_artifact_invalid", error,
                    commit_sha=projection.commit_sha, context_root=projection.root,
                )
                continue
            if (await self._controls.for_projection(owner_id, projection, capability)).eligible:
                capabilities.append(capability)
        return {
            "repository_commit": projection.commit_sha,
            "capabilities": [capability.as_index_dict() for capability in capabilities],
        }

    async def run_capability(
        self, owner_id: str, capability_id: str, arguments: Mapping[str, object] | None = None
    ) -> CapabilityRunResult:
        projection = await self._projection(owner_id, "run_capability")
        try:
            capability = projection.discovery.load_capability(capability_id)
            await self._require_eligible(owner_id, projection, capability)
            result = await self._run(owner_id, projection, capability, arguments)
        except CapabilityArgumentsError:
            raise
        except CapabilityContractError as error:
            raise CapabilityArgumentsError(CapabilityArgumentsError.code) from error
        except NotificationArgumentsError as error:
            raise CapabilityArgumentsError(CapabilityArgumentsError.code) from error
        except AmieContextOperationError as error:
            raise CapabilityExecutionFailureError() from error
        except WorkflowCapabilityError as error:
            if error.args and error.args[0] in {
                "invalid_arguments", "invalid_execution_id", "invalid_gate_decision", "invalid_execution_state",
                "invalid_workflow", "workflow_not_executable",
            }:
                raise CapabilityArgumentsError(CapabilityArgumentsError.code) from error
            raise CapabilityExecutionFailureError() from error
        except CapabilityExecutionError as error:
            raise CapabilityExecutionFailureError() from error
        except Exception as error:
            raise CapabilityExecutionFailureError() from error
        return CapabilityRunResult(capability.summary.id, projection.commit_sha, result)

    async def run_capability_report(
        self, owner_id: str, capability_id: str, arguments: Mapping[str, object] | None = None
    ) -> CapabilityExecutionReport:
        """Run through the existing bounded runner while retaining bounded diagnostics."""
        projection = await self._projection(owner_id, "run_capability_report")
        try:
            capability = projection.discovery.load_capability(capability_id)
            await self._require_eligible(owner_id, projection, capability)
            if self._uses_gateway_primitive(capability):
                started = time.monotonic()
                result = await self._run(owner_id, projection, capability, arguments)
                return CapabilityExecutionReport(
                    capability_id=capability.summary.id,
                    repository_commit=projection.commit_sha,
                    status="succeeded",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    result=result,
                    stdout="",
                    stderr="",
                )
            else:
                report = self._runner.run_report(
                    projection, capability, self._python_arguments(capability, arguments)
                )
        except CapabilityArgumentsError:
            raise
        except CapabilityContractError as error:
            raise CapabilityArgumentsError(CapabilityArgumentsError.code) from error
        except NotificationArgumentsError as error:
            raise CapabilityArgumentsError(CapabilityArgumentsError.code) from error
        except AmieContextOperationError as error:
            raise CapabilityServiceError("Capability execution could not be completed.") from error
        except Exception as error:
            raise CapabilityServiceError("Capability execution could not be completed.") from error
        return CapabilityExecutionReport(
            capability_id=capability.summary.id,
            repository_commit=projection.commit_sha,
            status=report.status,
            elapsed_ms=report.elapsed_ms,
            result=report.result,
            stdout=report.stdout,
            stderr=report.stderr,
            error=report.error,
        )

    async def _run(
        self, owner_id: str, projection, capability, arguments: Mapping[str, object] | None
    ) -> dict[str, object]:
        if arguments is not None and not isinstance(arguments, Mapping):
            raise CapabilityArgumentsError(CapabilityArgumentsError.code)
        binding = capability.runtime_binding or {"kind": capability.execution_kind}
        kind = binding.get("kind")
        if kind == "gateway_primitive":
            return await self._run_gateway_primitive(
                owner_id, projection, binding, self._python_arguments(capability, arguments)
            )
        if kind not in {"python_callable", "python_callable_v1", "context_python_callable"}:
            raise CapabilityExecutionFailureError()
        if capability.executor_path is None or capability.callable_name is None:
            raise CapabilityExecutionFailureError()
        return self._runner.run(projection, capability, self._python_arguments(capability, arguments))

    async def _require_eligible(self, owner_id: str, projection, capability) -> None:
        """Enforce current MABEL control-plane eligibility immediately before dispatch."""
        if not (await self._controls.for_projection(owner_id, projection, capability)).eligible:
            raise CapabilityExecutionFailureError()

    async def _run_gateway_primitive(
        self, owner_id: str, projection, binding: Mapping[str, object], arguments: Mapping[str, object] | None
    ) -> dict[str, object]:
        """Dispatch a context-declared binding to a Gateway-owned primitive.

        The capability id is deliberately not part of this dispatch.  Context
        declares which supported primitive it needs; the Gateway decides
        whether that primitive exists and executes it with its own credentials.
        """
        primitive = binding.get("primitive")
        if primitive == "notification_delivery" and self._notifications is not None:
            return (await self._notifications.send(arguments)).as_dict()
        if primitive == "amie_context_repository" and self._amie_context is not None:
            operation = binding.get("operation")
            if not isinstance(operation, str):
                raise CapabilityExecutionFailureError()
            return await self._amie_context.run(owner_id, operation, arguments)
        if primitive == "workflow_execution" and self._workflow_execution is not None and arguments is not None:
            return await self._workflow_execution.run(owner_id, arguments, projection)
        raise CapabilityExecutionFailureError()

    @staticmethod
    def _uses_gateway_primitive(capability) -> bool:
        binding = capability.runtime_binding or {"kind": capability.execution_kind}
        return binding.get("kind") == "gateway_primitive"

    @staticmethod
    def _python_arguments(capability, arguments: Mapping[str, object] | None) -> dict[str, object] | None:
        """Validate declared new-format contracts while preserving legacy invocation behavior."""
        if capability.input_contract is None:
            if arguments:
                raise CapabilityArgumentsError(CapabilityArgumentsError.code)
            return None
        candidate: object = {} if arguments is None else arguments
        validate_capability_contract(capability.input_contract, candidate, "input_contract")
        if capability.invocation == "no_arguments":
            return None
        return dict(candidate)

    async def _projection(self, owner_id: str, operation: str):
        try:
            projection = await self._projections.synchronize(owner_id)
        except ContextProjectionError as error:
            log_capability_diagnostic(operation, "context_projection_failed", error)
            raise CapabilityServiceError() from error
        log_capability_diagnostic(
            operation,
            "context_projection_loaded",
            commit_sha=projection.commit_sha,
            context_root=projection.root,
        )
        return projection
