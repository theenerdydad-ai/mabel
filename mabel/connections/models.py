"""Generic, owner-scoped MABEL connection records."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class Connection:
    """Persistent metadata for one registered external connection."""

    id: UUID
    owner_id: str
    display_name: str
    connection_type: str
    transport: str
    endpoint: str
    # This is an internal relationship to encrypted AMIE bearer material.  It
    # is deliberately not an opaque value supplied by a browser client.
    credential_id: UUID | None
    enabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ConnectionTestResult:
    """Bounded evidence that an AMIE endpoint accepted the read handshake."""

    ok: bool
    connection_id: UUID
    bootstrap_loaded: bool
    bootstrap_sha: str | None
    root_manifest_loaded: bool
    collection_count: int | None
    error_code: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "ok": self.ok,
            "connection_id": str(self.connection_id),
        }
        if self.ok:
            result["bootstrap"] = {"loaded": self.bootstrap_loaded, "sha": self.bootstrap_sha}
            result["root_manifest"] = {
                "loaded": self.root_manifest_loaded,
                "collection_count": self.collection_count,
            }
        else:
            result["error_code"] = self.error_code
        return result
