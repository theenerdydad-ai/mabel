from httpx import ASGITransport, AsyncClient

from mabel.app import create_app


async def test_health_returns_shared_service_status() -> None:
    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "service": "mabel"}
