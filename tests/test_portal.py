from hashlib import sha256
from pathlib import Path
import subprocess

from httpx import ASGITransport, AsyncClient

from mabel.app import create_app
from mabel.config import Settings


async def test_portal_serves_a_thin_status_and_connections_shell(tmp_path) -> None:
    app = create_app(settings=Settings(state_database_path=tmp_path / "state.db"))

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            portal = await client.get("/portal")
            stylesheet = await client.get("/portal/static/portal.css")
            script = await client.get("/portal/static/portal.js")
            mark = await client.get("/portal/static/assets/mabel-mark-square.png")
            favicon = await client.get("/portal/static/assets/favicon.ico")

    assert portal.status_code == 200
    assert "MABEL status" in portal.text
    assert "Operational checkout" in portal.text
    assert 'data-view-target="workspace" aria-current="page"' in portal.text
    assert "management-columns" in portal.text
    assert "AMIE connections" in portal.text
    assert "AMIE API token" in portal.text
    assert 'href="https://www.theenerdydad.com/ace/app"' in portal.text
    assert "Opaque credential reference" not in portal.text
    assert "API &amp; MCP access" in portal.text
    assert "/portal/static/portal.js" in portal.text
    assert 'src="/portal/static/assets/mabel-mark-square.png"' in portal.text
    assert 'href="/portal/static/assets/favicon.ico"' in portal.text
    assert stylesheet.status_code == 200
    assert "--brand-primary: #944CAB" in stylesheet.text
    assert "--brand-gradient:" in stylesheet.text
    assert ".nav-link svg" in stylesheet.text
    assert ".management-columns" in stylesheet.text
    assert script.status_code == 200
    assert mark.status_code == 200
    assert mark.headers["content-type"] == "image/png"
    assert favicon.status_code == 200
    assert favicon.headers["content-type"] == "image/x-icon"
    assert 'api("/health")' in script.text
    assert 'api("/api/connections")' in script.text
    assert 'api("/api/managed-repositories")' in script.text
    assert 'api("/api/access/token-status")' in script.text
    assert 'api("/api/access/token", { method: "POST" })' in script.text
    assert "connection_type: \"amie\"" in script.text
    assert "amie_api_token" in script.text
    assert "credential_reference" not in script.text
    assert "Fine-grained GitHub token" in portal.text
    assert 'data-view-target="workflow-executions"' in portal.text
    assert "Workflow Executions" in portal.text


def test_capabilities_view_uses_a_fingerprinted_loader_and_render_path() -> None:
    """Keep the portal asset cache key and Capability Control Center path in sync."""
    web_root = Path(__file__).resolve().parents[1] / "mabel" / "web"
    portal = (web_root / "index.html").read_text(encoding="utf-8")
    script = (web_root / "portal.js").read_text(encoding="utf-8")
    fingerprint = sha256(script.encode("utf-8")).hexdigest()[:12]

    assert f'/portal/static/portal.js?v={fingerprint}' in portal
    assert 'data-view-target="capabilities"' in portal
    assert 'if (view === "capabilities") refreshCapabilities();' in script
    assert 'api("/api/portal/capabilities")' in script
    assert "state.capabilities = response.capabilities; renderCapabilities();" in script
    assert "state.capabilities.forEach((capability) => list.append(renderCapability(capability)));" in script
    assert 'api("/api/portal/workflow-executions")' in script


def test_capabilities_navigation_executes_the_frontend_loader() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["node", "--test", "tests/portal_capabilities.test.mjs"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
