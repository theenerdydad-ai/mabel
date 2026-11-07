"""Temporary local owner context; this is deliberately not authentication."""

from fastapi import HTTPException, status


class DevelopmentOwnerContext:
    """Supply an explicitly named local-development owner, if configured."""

    def __init__(self, owner_id: str | None) -> None:
        self._owner_id = owner_id

    def require_owner_id(self) -> str:
        if self._owner_id is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Connection management requires an authenticated owner context.",
            )
        return self._owner_id
