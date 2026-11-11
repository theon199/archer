import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app
from app.readiness import validate_runtime
from app.runs.worker import Worker
from app.storage.database import get_db


@pytest.mark.asyncio
async def test_readiness_requires_worker_and_real_dependency_status(monkeypatch):
    async def available():
        return True

    monkeypatch.setattr("app.readiness.model_ready", available)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/ready")
        assert response.status_code == 503 and not response.json()["worker"]
        with Worker() as worker:
            await worker.heartbeat()
            assert (await client.get("/api/ready")).status_code == 200
        async with get_db() as db:
            await db.execute("UPDATE worker_status SET heartbeat=?", (time.time() - 6,))
            await db.commit()
        assert (await client.get("/api/ready")).status_code == 503
        assert (await client.get("/api/notes")).headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_static_frontend_served_without_api_or_path_escape(tmp_path, monkeypatch):
    static = tmp_path / "dist"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text('<!doctype html><div id="root"></div>')
    (static / "assets/main.js").write_text('console.log("synthetic static test")')
    (tmp_path / "secret").write_text("private")
    (static / "assets/escape").symlink_to(tmp_path / "secret")
    monkeypatch.setattr(settings, "static_dir", static)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert "<!doctype html>" in (await client.get("/")).text
        assert (await client.get("/assets/main.js")).status_code == 200
        assert (await client.get("/assets/escape")).status_code == 404
        assert (await client.get("/assets/%2e%2e/%2e%2e/secret")).status_code == 404
        assert (await client.get("/api/notes")).status_code == 401


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("host", "0.0.0.0"),
        ("model_provider", "deepseek"),
        ("ollama_base_url", "https://example.com"),
        ("ollama_base_url", "http://secret@127.0.0.1:11434"),
    ],
)
def test_private_runtime_rejects_cloud_and_public_bindings(monkeypatch, attribute, value):
    monkeypatch.setattr(settings, "model_provider", "local-ollama")
    monkeypatch.setattr(settings, attribute, value)
    with pytest.raises(ValueError):
        validate_runtime()


def test_digest_pin_cannot_silently_apply_to_sglang(monkeypatch):
    monkeypatch.setattr(settings, "expected_model_digest", "synthetic-digest")
    with pytest.raises(ValueError, match="only for Ollama"):
        validate_runtime("local-sglang")


@pytest.mark.asyncio
async def test_model_metadata_digest_mismatch_fails_closed(monkeypatch):
    import httpx

    from app.readiness import model_ready

    original = httpx.AsyncClient
    requested = []

    def respond(request):
        requested.append(str(request.url))
        return httpx.Response(200, json={"models": [{"name": "synthetic-test-model", "digest": "actual-test-digest"}]})

    transport = httpx.MockTransport(respond)  # Explicit deterministic test-only provider metadata.
    monkeypatch.setattr("app.readiness.httpx.AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    monkeypatch.setattr(settings, "model_provider", "local-ollama")
    monkeypatch.setattr(settings, "ollama_base_url", "http://127.0.0.1:11434")
    monkeypatch.setattr(settings, "ollama_model", "synthetic-test-model")
    monkeypatch.setattr(settings, "expected_model_digest", "different-test-digest")
    assert not await model_ready()
    monkeypatch.setattr(settings, "expected_model_digest", "actual-test-digest")
    assert await model_ready()
    assert requested == ["http://127.0.0.1:11434/api/tags"] * 2


@pytest.mark.parametrize("version", [(3, 45, 1), (3, 51, 2), (3, 50, 6), (3, 44, 5)])
def test_runtime_rejects_sqlite_without_wal_reset_fix(monkeypatch, version):
    monkeypatch.setattr("app.readiness.sqlite3.sqlite_version_info", version)
    with pytest.raises(ValueError, match="WAL-reset fix"):
        validate_runtime()


@pytest.mark.parametrize("version", [(3, 51, 3), (3, 54, 1), (3, 50, 7), (3, 44, 6)])
def test_runtime_accepts_official_sqlite_fix_lines(monkeypatch, version):
    monkeypatch.setattr("app.readiness.sqlite3.sqlite_version_info", version)
    monkeypatch.setattr("app.readiness.sqlite3.threadsafety", 3)
    validate_runtime()
