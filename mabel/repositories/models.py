"""Owner-scoped managed repository records."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class ManagedRepository:
    id: UUID
    owner_id: str
    provider: str
    purpose: str
    display_name: str
    provider_repository_id: str
    canonical_locator: str
    default_branch: str
    credential_id: UUID
    status: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class RepositoryVerification:
    provider_repository_id: str
    canonical_locator: str
    default_branch: str


@dataclass(frozen=True)
class ManagedRepositoryTestResult:
    ok: bool
    repository_id: UUID
    provider_repository_id: str | None = None
    canonical_locator: str | None = None
    default_branch: str | None = None
    error_code: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"ok": self.ok, "repository_id": str(self.repository_id)}
        if self.ok:
            result["repository"] = {
                "provider_repository_id": self.provider_repository_id,
                "canonical_locator": self.canonical_locator,
                "default_branch": self.default_branch,
            }
        else:
            result["error_code"] = self.error_code
        return result
