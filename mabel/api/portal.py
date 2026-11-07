"""Static browser portal for the current MABEL management surface."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles


_WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


def mount_portal(app: FastAPI, browser_auth: object | None = None) -> None:
    """Serve MABEL's small API-driven portal without a frontend build system."""
    app.mount("/portal/static", StaticFiles(directory=_WEB_ROOT), name="portal-static")

    @app.get("/portal", include_in_schema=False)
    @app.get("/portal/", include_in_schema=False)
    async def portal(request: Request) -> Response:
        if browser_auth is not None:
            try:
                await browser_auth.require_identity(request)
            except Exception as error:
                if getattr(error, "status_code", None) == 401:
                    return RedirectResponse(browser_auth.login_url, status_code=303)
                raise
        return FileResponse(_WEB_ROOT / "index.html")

    if browser_auth is None:
        return

    @app.get("/auth/amie/login", include_in_schema=False)
    async def amie_login() -> Response:
        return await browser_auth.begin_login()

    @app.get("/auth/amie/callback", include_in_schema=False)
    async def amie_callback(request: Request, code: str, state: str) -> Response:
        return await browser_auth.complete_callback(request, code, state)

    @app.post("/auth/logout", include_in_schema=False, status_code=204)
    async def logout(request: Request) -> Response:
        return await browser_auth.logout(request)

    @app.get("/api/portal/session", include_in_schema=False)
    async def portal_session(request: Request) -> dict[str, str]:
        await browser_auth.require_identity(request)
        return browser_auth.session_response()
