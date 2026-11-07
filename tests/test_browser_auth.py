"""Focused browser-auth tests; no browser-supplied owner can select MABEL data."""

import time
from uuid import uuid4

from fastapi.testclient import TestClient

from mabel.app import create_app
from mabel.browser.amie_stack import AmieStackError, AmieStackIdentity
from mabel.config import Settings


class FakeAmieStackClient:
    def __init__(self, subject: str) -> None:
        self.subject = subject
        self.sessions: set[str] = set()
        self.consumed: list[tuple[str, str]] = []
        self.revoked: list[str] = []

    async def consume(self, code: str, state: str) -> AmieStackIdentity:
        if code != "one-time-code" or any(previous[0] == code for previous in self.consumed):
            raise AmieStackError("replayed")
        self.consumed.append((code, state))
        self.sessions.add("opaque-mabel-session")
        return AmieStackIdentity("opaque-mabel-session", self.subject, int(time.time()) + 600)

    async def introspect(self, session: str) -> AmieStackIdentity:
        if session not in self.sessions:
            raise AmieStackError("revoked")
        return AmieStackIdentity(session, self.subject, int(time.time()) + 600)

    async def revoke(self, session: str) -> None:
        self.revoked.append(session)
        self.sessions.discard(session)


def _settings(tmp_path, subject: str) -> Settings:
    return Settings(
        state_database_path=tmp_path / "state.db",
        context_projection_root=tmp_path / "projections",
        browser_auth_enabled=True,
        amie_public_url="https://amie.example",
        amie_portal_url="https://amie.example/ace/app",
        amie_internal_url="http://127.0.0.1:8000",
        public_url="https://mabel.example",
        amie_mabel_internal_secret="shared-secret-not-exposed-to-browser",
        portal_cookie_secure=True,
        amie_owner_bindings={subject: "existing-production-owner"},
        development_owner_id=None,
    )


def test_browser_handoff_is_state_bound_owner_scoped_and_csrf_protected(tmp_path) -> None:
    subject = str(uuid4())
    fake = FakeAmieStackClient(subject)
    app = create_app(settings=_settings(tmp_path, subject), amie_stack_client=fake)
    with TestClient(app, base_url="https://mabel.example") as client:
        portal = client.get("/portal", follow_redirects=False)
        assert portal.status_code == 303
        assert portal.headers["location"] == "https://mabel.example/auth/amie/login"

        start = client.get("/auth/amie/login", follow_redirects=False)
        assert start.status_code == 303
        assert start.headers["location"].startswith("https://amie.example/stack/mabel?state=")
        state = client.cookies.get("mabel_handoff_state")
        assert state

        callback = client.get(
            "/auth/amie/callback", params={"code": "one-time-code", "state": state}, follow_redirects=False
        )
        assert callback.status_code == 303
        assert callback.headers["location"] == "https://mabel.example/portal"
        cookies = callback.headers.get_list("set-cookie")
        session_cookie = next(value for value in cookies if value.startswith("mabel_stack_session="))
        assert "HttpOnly" in session_cookie and "Secure" in session_cookie and "SameSite=lax" in session_cookie
        assert "Domain=" not in session_cookie

        status = client.get("/api/access/token-status")
        assert status.status_code == 200
        assert status.json() == {"has_token": False}
        session = client.get("/api/portal/session")
        assert session.json()["amie_url"] == "https://amie.example/ace/app"
        execution_list = client.get("/api/portal/workflow-executions")
        assert execution_list.status_code == 200
        assert execution_list.json()["executions"] == []
        # Capability controls use the same browser-derived owner and CSRF
        # boundary as the rest of the MABEL Portal.
        capability_list = client.get("/api/portal/capabilities")
        assert capability_list.status_code == 409
        capability_missing_csrf = client.patch("/api/portal/capabilities/echo", json={"enabled": False})
        assert capability_missing_csrf.status_code == 403
        missing_csrf = client.post("/api/access/token")
        assert missing_csrf.status_code == 403
        issued = client.post("/api/access/token", headers={"X-MABEL-CSRF": client.cookies.get("mabel_csrf")})
        assert issued.status_code == 200
        assert issued.json()["token"].startswith("mabel_")

        replay = client.get(
            "/auth/amie/callback", params={"code": "one-time-code", "state": state}, follow_redirects=False
        )
        assert replay.status_code == 400

        # AMIE remains authoritative: a revoked ACE/stack session fails on the
        # next protected MABEL request without a MABEL-side cache window.
        fake.sessions.clear()
        revoked_upstream = client.get("/api/access/token-status")
        assert revoked_upstream.status_code == 401
        assert revoked_upstream.json()["detail"]["login_url"] == "https://mabel.example/auth/amie/login"
        revoked_executions = client.get("/api/portal/workflow-executions")
        assert revoked_executions.status_code == 401
        fake.sessions.add("opaque-mabel-session")

        logout = client.post("/auth/logout", headers={"X-MABEL-CSRF": client.cookies.get("mabel_csrf")})
        assert logout.status_code == 204
        assert fake.revoked == ["opaque-mabel-session"]
        unauthenticated = client.get("/api/access/token-status")
        assert unauthenticated.status_code == 401
        assert unauthenticated.json()["detail"]["login_url"] == "https://mabel.example/auth/amie/login"


def test_unmapped_or_revoked_amie_subject_never_resolves_to_an_owner(tmp_path) -> None:
    mapped_subject = str(uuid4())
    fake = FakeAmieStackClient(str(uuid4()))
    app = create_app(settings=_settings(tmp_path, mapped_subject), amie_stack_client=fake)
    with TestClient(app, base_url="https://mabel.example") as client:
        client.get("/auth/amie/login", follow_redirects=False)
        rejected = client.get(
            "/auth/amie/callback",
            params={"code": "one-time-code", "state": client.cookies.get("mabel_handoff_state")},
            follow_redirects=False,
        )
        assert rejected.status_code == 403
