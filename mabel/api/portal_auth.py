"""MABEL host-local browser session backed by AMIE stack introspection."""

import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response

from mabel.browser.amie_stack import AmieStackClient, AmieStackError
from mabel.services.owner_identities import AmieOwnerMappingService, UnmappedAmieIdentityError


@dataclass(frozen=True)
class PortalIdentity:
    owner_id: str
    subject: str


class BrowserPortalAuth:
    """Never accepts an owner from the browser; AMIE remains the session authority."""

    session_cookie = "mabel_stack_session"
    state_cookie = "mabel_handoff_state"
    csrf_cookie = "mabel_csrf"

    def __init__(
        self, *, amie_public_url: str, amie_portal_url: str, mabel_public_url: str, client: AmieStackClient,
        mappings: AmieOwnerMappingService, cookie_secure: bool,
    ) -> None:
        self._amie_public_url = amie_public_url.rstrip("/")
        self._amie_portal_url = amie_portal_url
        self._mabel_public_url = mabel_public_url.rstrip("/")
        self._client = client
        self._mappings = mappings
        self._cookie_secure = cookie_secure

    @property
    def login_url(self) -> str:
        return f"{self._mabel_public_url}/auth/amie/login"

    @property
    def amie_portal_url(self) -> str:
        return self._amie_portal_url

    @property
    def mabel_portal_url(self) -> str:
        return f"{self._mabel_public_url}/portal"

    async def require_owner_id(self, request: Request) -> str:
        return (await self.require_identity(request)).owner_id

    async def require_identity(self, request: Request) -> PortalIdentity:
        raw_session = request.cookies.get(self.session_cookie)
        if not raw_session:
            raise self._unauthenticated()
        try:
            identity = await self._client.introspect(raw_session)
            return PortalIdentity(owner_id=self._mappings.resolve(identity.subject), subject=identity.subject)
        except (AmieStackError, UnmappedAmieIdentityError, ValueError):
            raise self._unauthenticated() from None

    async def begin_login(self) -> RedirectResponse:
        state = secrets.token_urlsafe(32)
        destination = f"{self._amie_public_url}/stack/mabel?{urlencode({'state': state})}"
        response = RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            self.state_cookie, state, max_age=90, secure=self._cookie_secure,
            httponly=True, samesite="lax",
        )
        return response

    async def complete_callback(self, request: Request, code: str, state: str) -> RedirectResponse:
        expected_state = request.cookies.get(self.state_cookie)
        if not expected_state or not secrets.compare_digest(expected_state, state):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid MABEL sign-in state.")
        try:
            identity = await self._client.consume(code, state)
            self._mappings.resolve(identity.subject)
        except (AmieStackError, UnmappedAmieIdentityError, ValueError):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="AMIE sign-in could not be accepted.") from None
        remaining_seconds = max(1, identity.expires_at - int(time.time()))
        response = RedirectResponse(self.mabel_portal_url, status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            self.session_cookie, identity.session, max_age=remaining_seconds,
            secure=self._cookie_secure, httponly=True, samesite="lax",
        )
        response.set_cookie(
            self.csrf_cookie, secrets.token_urlsafe(32), max_age=remaining_seconds,
            secure=self._cookie_secure, httponly=False, samesite="lax",
        )
        response.delete_cookie(self.state_cookie)
        return response

    async def require_csrf(self, request: Request) -> None:
        cookie = request.cookies.get(self.csrf_cookie)
        provided = request.headers.get("X-MABEL-CSRF")
        if not cookie or not provided or not secrets.compare_digest(cookie, provided):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token.")

    async def logout(self, request: Request) -> Response:
        await self.require_csrf(request)
        raw_session = request.cookies.get(self.session_cookie)
        if raw_session:
            try:
                await self._client.revoke(raw_session)
            except AmieStackError:
                # The local browser session still must be cleared if AMIE is unavailable.
                pass
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(self.session_cookie)
        response.delete_cookie(self.csrf_cookie)
        response.delete_cookie(self.state_cookie)
        return response

    def session_response(self) -> dict[str, str]:
        return {"amie_url": self.amie_portal_url, "mabel_url": self.mabel_portal_url}

    def _unauthenticated(self) -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"message": "MABEL browser sign-in is required.", "login_url": self.login_url},
        )
