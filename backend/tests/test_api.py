import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_health():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "online"
        assert data["service"] == "archer-core"


@pytest.mark.asyncio
async def test_list_plugins():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/plugins")
        assert resp.status_code == 200
        data = resp.json()
        assert "plugins" in data
        plugin_names = [p["name"] for p in data["plugins"]]
        assert "read_approved_file" in plugin_names
        assert "personal_notes" in plugin_names
        assert "web_search" in plugin_names


@pytest.mark.asyncio
async def test_models_status():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/models/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "active_provider" in data
        assert "ollama" in data
