from datetime import datetime, timezone
from uuid import uuid4

from mabel.repositories.models import ManagedRepository
from mabel.services.context_bindings import ContextBindingCandidateService, candidate_failure
from mabel.services.managed_repositories import ManagedRepositoryNotFoundError


class FakeRepositories:
    def __init__(self, repository=None):
        self.repository = repository

    def resolve_active_context_repository(self, owner_id):
        if self.repository is None:
            raise ManagedRepositoryNotFoundError("missing")
        return self.repository


def _repository() -> ManagedRepository:
    now = datetime.now(timezone.utc)
    return ManagedRepository(
        id=uuid4(), owner_id="mabel-owner", provider="github", purpose="mabel_context_repository",
        display_name="Context", provider_repository_id="12345", canonical_locator="owner/context",
        default_branch="main", credential_id=uuid4(), status="connected", enabled=True,
        created_at=now, updated_at=now,
    )


def test_candidate_exposes_only_identity_and_immutable_repository_proof() -> None:
    repository = _repository()
    service = ContextBindingCandidateService(FakeRepositories(repository))

    result = candidate_failure("mabel-owner", service)

    assert result == {
        "ok": True,
        "mabel_owner_id": "mabel-owner",
        "context_repository": {
            "id": str(repository.id), "provider": "github", "provider_repository_id": "12345",
            "canonical_locator": "owner/context", "status": "connected", "enabled": True,
        },
    }
    assert "credential" not in str(result).lower()


def test_candidate_returns_bounded_setup_failures() -> None:
    assert candidate_failure("owner", ContextBindingCandidateService(FakeRepositories())) == {
        "ok": False, "error": {"code": "context_repository_unavailable"}
    }
