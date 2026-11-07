"""Health behavior shared by HTTP and MCP."""

from typing import Final


class HealthService:
    """Provide the current process-level health response."""

    _SERVICE_NAME: Final[str] = "mabel"

    def status(self) -> dict[str, bool | str]:
        return {"ok": True, "service": self._SERVICE_NAME}
