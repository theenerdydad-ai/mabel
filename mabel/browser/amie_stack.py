"""Trusted server-to-server client for AMIE's deliberately small stack handoff."""

from dataclasses import dataclass
from typing import Protocol

import httpx


class AmieStackError(RuntimeError):
    """A safe failure from the AMIE stack-auth transport."""


@dataclass(frozen=True)
class AmieStackIdentity:
    session: str
    subject: str
    expires_at: int


class AmieStackClient(Protocol):
    async def consume(self, code: str, state: str) -> AmieStackIdentity: ...
    async def introspect(self, session: str) -> AmieStackIdentity: ...
    async def revoke(self, session: str) -> None: ...


class AmieHttpStackClient:
    """Calls private AMIE endpoints with a server-only shared secret."""

    def __init__(self, base_url: str, internal_secret: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-AMIE-MABEL-Internal": internal_secret}

    async def consume(self, code: str, state: str) -> AmieStackIdentity:
        return await self._identity("/_internal/mabel-stack/consume", {"code": code, "state": state})

    async def introspect(self, session: str) -> AmieStackIdentity:
        return await self._identity("/_internal/mabel-stack/introspect", {"session": session})

    async def revoke(self, session: str) -> None:
        await self._request("/_internal/mabel-stack/revoke", {"session": session})

    async def _identity(self, path: str, payload: dict[str, str]) -> AmieStackIdentity:
        data = await self._request(path, payload)
        try:
            return AmieStackIdentity(
                session=str(data["session"]), subject=str(data["subject"]), expires_at=int(data["expires_at"])
            )
        except (KeyError, TypeError, ValueError) as error:
            raise AmieStackError("AMIE returned an invalid stack-identity response.") from error

    async def _request(self, path: str, payload: dict[str, str]) -> dict[str, object]:
        try:
            async with httpx.AsyncClient(base_url=self._base_url, timeout=5.0) as client:
                response = await client.post(path, json=payload, headers=self._headers)
        except httpx.HTTPError as error:
            raise AmieStackError("AMIE stack authentication is unavailable.") from error
        if response.status_code != 200:
            raise AmieStackError("AMIE did not accept the stack session.")
        try:
            data = response.json()
        except ValueError as error:
            raise AmieStackError("AMIE returned an invalid stack-auth response.") from error
        if not isinstance(data, dict):
            raise AmieStackError("AMIE returned an invalid stack-auth response.")
        return data
