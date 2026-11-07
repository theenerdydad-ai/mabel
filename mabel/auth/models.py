"""Values used by MABEL's bearer-token boundary."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ApiIdentity:
    """The non-secret identity resolved from a valid MABEL API token."""

    owner_id: str
    authentication: str = "api_token"


@dataclass(frozen=True)
class IssuedApiToken:
    """A transient token value, returned only at issuance time."""

    token: str
    created_at: datetime


@dataclass(frozen=True)
class ApiTokenStatus:
    """Safe token-state summary for the browser management portal."""

    has_token: bool
