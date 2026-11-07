"""Owner-scoped capability eligibility and management state."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from mabel.context.models import CapabilityDefinition, CapabilitySummary
from mabel.context.projection import ContextProjection, ContextProjectionError
from mabel.services.amie_context import AmieContextOperationService
from mabel.services.notifications import NotificationService
from mabel.state.capability_controls import CapabilityControl, CapabilityControlStore


class CapabilityControlServiceError(ValueError):
    """A safe failure from the MABEL capability control plane."""


class CapabilityNotDeclaredError(CapabilityControlServiceError):
    """The requested capability is not in the current validated projection."""


@dataclass(frozen=True)
class CapabilityEligibility:
    capability: CapabilitySummary
    enabled: bool
    status: str
    reason_code: str | None
    validation_state: str
    validation_error_code: str | None
    last_validated_at: datetime | None

    @property
    def eligible(self) -> bool:
        return self.status == "active"

    def as_management_dict(self) -> dict[str, object]:
        return {
            "id": self.capability.id,
            "title": self.capability.title,
            "version": self.capability.version,
            "enabled": self.enabled,
            "status": self.status,
            "reason_code": self.reason_code,
            "validation": {
                "state": self.validation_state,
                "last_checked_at": self.last_validated_at.isoformat() if self.last_validated_at else None,
                "error_code": self.validation_error_code,
            },
        }


class ProjectionSynchronizer(Protocol):
    async def synchronize(self, owner_id: str) -> ContextProjection: ...


class CapabilityControlService:
    """Keep mutable activation state separate from governed declarations.

    The exact same eligibility calculation feeds the Portal management list,
    the machine-facing discovery index, and execution enforcement.
    """

    def __init__(
        self,
        projections: ProjectionSynchronizer,
        controls: CapabilityControlStore,
        notifications: NotificationService | None = None,
        amie_context: AmieContextOperationService | None = None,
    ) -> None:
        self._projections = projections
        self._controls = controls
        self._notifications = notifications
        self._amie_context = amie_context
        self._workflow_execution_available = False

    def configure_workflow_execution(self) -> None:
        """Mark the app-composed workflow dispatcher available for eligibility checks."""
        self._workflow_execution_available = True

    async def list(self, owner_id: str) -> dict[str, object]:
        projection = await self._projection(owner_id)
        capabilities = []
        for summary in projection.context.capabilities:
            try:
                capability = projection.discovery.load_capability(summary.id)
            except Exception:
                # The catalog summary is valid, but this one artifact is not.
                # Preserve the rest of the owner’s management surface.
                capabilities.append(self.for_malformed_summary(owner_id, summary).as_management_dict())
                continue
            capabilities.append((await self.for_projection(owner_id, projection, capability)).as_management_dict())
        return {"repository_commit": projection.commit_sha, "capabilities": capabilities}

    async def set_enabled(self, owner_id: str, capability_id: str, enabled: bool) -> dict[str, object]:
        projection, capability = await self._declared_capability(owner_id, capability_id)
        self._controls.set_enabled(owner_id, capability.summary.id, enabled, _now())
        return (await self.for_projection(owner_id, projection, capability)).as_management_dict()

    async def validate(self, owner_id: str, capability_id: str) -> dict[str, object]:
        projection, capability = await self._declared_capability(owner_id, capability_id)
        runtime_status, reason_code = await self._runtime_readiness(owner_id, capability)
        self._controls.record_validation(
            owner_id,
            capability.summary.id,
            "passed" if runtime_status == "active" else "failed",
            reason_code,
            _now(),
        )
        return (await self.for_projection(owner_id, projection, capability)).as_management_dict()

    async def for_projection(
        self, owner_id: str, projection: ContextProjection, capability: CapabilityDefinition
    ) -> CapabilityEligibility:
        control = self._controls.get(owner_id, capability.summary.id)
        enabled = True if control is None else control.enabled
        validation = _validation(control)
        if not enabled:
            return CapabilityEligibility(capability.summary, False, "inactive", "capability_disabled", *validation)
        status, reason_code = await self._runtime_readiness(owner_id, capability)
        return CapabilityEligibility(capability.summary, True, status, reason_code, *validation)

    def for_malformed_summary(self, owner_id: str, summary: CapabilitySummary) -> CapabilityEligibility:
        """Render a catalog-declared but invalid artifact without exposing details."""
        control = self._controls.get(owner_id, summary.id)
        enabled = True if control is None else control.enabled
        return CapabilityEligibility(
            summary, enabled, "error", "invalid_capability_artifact", *_validation(control)
        )

    async def _declared_capability(
        self, owner_id: str, capability_id: str
    ) -> tuple[ContextProjection, CapabilityDefinition]:
        projection = await self._projection(owner_id)
        try:
            capability = projection.discovery.load_capability(capability_id)
        except Exception as error:
            raise CapabilityNotDeclaredError("Capability is not declared by the current MABEL context.") from error
        return projection, capability

    async def _projection(self, owner_id: str) -> ContextProjection:
        try:
            return await self._projections.synchronize(owner_id)
        except ContextProjectionError as error:
            raise CapabilityControlServiceError("MABEL context projection is unavailable.") from error
        except Exception as error:
            raise CapabilityControlServiceError("MABEL context projection is unavailable.") from error

    async def _runtime_readiness(self, owner_id: str, capability: CapabilityDefinition) -> tuple[str, str | None]:
        binding = capability.runtime_binding or {"kind": capability.execution_kind}
        kind = binding.get("kind")
        if kind in {"python_callable", "python_callable_v1", "context_python_callable"}:
            if capability.executor_path is None or capability.callable_name is None:
                return "error", "invalid_runtime_binding"
            return "active", None
        if kind != "gateway_primitive":
            return "error", "unsupported_runtime_binding"
        primitive = binding.get("primitive")
        if primitive == "notification_delivery":
            if self._notifications is None:
                return "error", "unsupported_runtime_binding"
            reason = self._notifications.readiness_error_code()
            return ("active", None) if reason is None else ("needs_setup", reason)
        if primitive == "amie_context_repository":
            if self._amie_context is None:
                return "error", "unsupported_runtime_binding"
            reason = await self._amie_context.readiness_error_code(owner_id)
            return ("active", None) if reason is None else ("needs_setup", reason)
        if primitive == "workflow_execution":
            # The only workflow service is composed by the application before
            # it begins accepting work.  Its capability binding cannot carry
            # a provider, workspace, or repository selector.
            return (
                ("active", None)
                if self._workflow_execution_available
                else ("error", "unsupported_runtime_binding")
            )
        return "error", "unsupported_runtime_binding"


def _validation(control: CapabilityControl | None) -> tuple[str, str | None, datetime | None]:
    if control is None:
        return "not_run", None, None
    return control.validation_state, control.validation_error_code, control.last_validated_at


def _now() -> datetime:
    return datetime.now(timezone.utc)
