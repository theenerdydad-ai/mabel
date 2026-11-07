"""Read-only proof material for ACE ↔ MABEL context bindings."""

from dataclasses import dataclass

from mabel.services.managed_repositories import (
    ManagedRepositoryConflictError,
    ManagedRepositoryNotFoundError,
    ManagedRepositoryService,
)


@dataclass(frozen=True)
class ContextBindingCandidate:
    """Non-secret evidence ACE needs to bind one account-owned context repo."""

    owner_id: str
    managed_repository_id: str
    provider: str
    provider_repository_id: str
    canonical_locator: str
    status: str
    enabled: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": True,
            "mabel_owner_id": self.owner_id,
            "context_repository": {
                "id": self.managed_repository_id,
                "provider": self.provider,
                "provider_repository_id": self.provider_repository_id,
                "canonical_locator": self.canonical_locator,
                "status": self.status,
                "enabled": self.enabled,
            },
        }


class ContextBindingCandidateService:
    """Expose only the currently designated context repository to ACE."""

    def __init__(self, repositories: ManagedRepositoryService) -> None:
        self._repositories = repositories

    def candidate(self, owner_id: str) -> ContextBindingCandidate:
        repository = self._repositories.resolve_active_context_repository(owner_id)
        return ContextBindingCandidate(
            owner_id=owner_id,
            managed_repository_id=str(repository.id),
            provider=repository.provider,
            provider_repository_id=repository.provider_repository_id,
            canonical_locator=repository.canonical_locator,
            status=repository.status,
            enabled=repository.enabled,
        )


def candidate_failure(owner_id: str, service: ContextBindingCandidateService | None) -> dict[str, object]:
    """Return a bounded, credential-free MCP result for expected setup gaps."""
    if service is None:
        code = "binding_unavailable"
    else:
        try:
            return service.candidate(owner_id).as_dict()
        except ManagedRepositoryNotFoundError:
            code = "context_repository_unavailable"
        except ManagedRepositoryConflictError:
            code = "context_repository_ambiguous"
    return {"ok": False, "error": {"code": code}}
