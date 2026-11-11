import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from app.config import settings
from app.plugins.files import ApprovedFilesPlugin
from app.plugins.notes import PersonalNotesPlugin
from app.plugins.web_search import WebSearchPlugin
from app.storage.database import get_db, init_db


@pytest.mark.asyncio
async def test_approved_files_sandboxing(tmp_path: Path):
    # Setup test directory
    (tmp_path / "acc_sri_owner").mkdir()
    safe_file = tmp_path / "acc_sri_owner" / "hello.txt"
    safe_file.write_text("Hello Archer")

    plugin = ApprovedFilesPlugin(root_dir=tmp_path)

    # 1. Successful read
    read_res = await plugin.execute(account_id="acc_sri_owner", arguments={"action": "read", "path": "hello.txt"})
    assert read_res["success"] is True
    assert read_res["content"] == "Hello Archer"

    # 2. Block path traversal outside root
    escape_res = await plugin.execute(
        account_id="acc_sri_owner", arguments={"action": "read", "path": "../../etc/passwd"}
    )
    assert escape_res["success"] is False
    assert "Access denied" in escape_res["error"]


@pytest.mark.asyncio
async def test_personal_notes_crud():
    await init_db()
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO accounts (id, username, password_hash) VALUES (?, ?, ?)",
            ("user_test_1", "tester1", "hash"),
        )
        for action in ["create", "list", "get", "delete"]:
            await db.execute(
                "INSERT INTO tool_grants (account_id,tool_name,action,tool_version,enabled) VALUES (?, ?, ?, ?, 1)",
                ("user_test_1", "personal_notes", action, "1"),
            )
        await db.commit()

    plugin = PersonalNotesPlugin()

    # Create
    create_res = await plugin.execute(
        account_id="user_test_1",
        arguments={"action": "create", "title": "Meeting Notes", "content": "Discuss architecture"},
    )
    assert create_res["success"] is True
    note_id = create_res["note_id"]

    # List
    list_res = await plugin.execute(account_id="user_test_1", arguments={"action": "list"})
    assert list_res["success"] is True
    assert len(list_res["notes"]) >= 1

    # Get
    get_res = await plugin.execute(account_id="user_test_1", arguments={"action": "get", "id": note_id})
    assert get_res["success"] is True
    assert get_res["note"]["title"] == "Meeting Notes"

    # Delete
    del_res = await plugin.execute(
        account_id="user_test_1", arguments={"action": "delete", "id": note_id, "expected_revision": 1}
    )
    assert del_res["success"] is True


@pytest.mark.asyncio
async def test_web_search():
    plugin = WebSearchPlugin()
    res = await plugin.execute(account_id="acc_sri_owner", arguments={"query": "NVIDIA DGX Spark"})
    assert res["success"] is False
    assert res["status"] == "unavailable"


@pytest.mark.asyncio
async def test_search_uses_bounded_direct_http_and_returns_citations(monkeypatch, tmp_path):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"results": [{"title": "Example", "url": "https://example.com/", "content": "Excerpt"}]},
        )

    original = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs.pop("trust_env") is False
        return original(transport=httpx.MockTransport(respond), **kwargs)

    key = tmp_path / "key"
    key.write_text("synthetic-test-key")
    key.chmod(0o600)
    monkeypatch.setattr(settings, "tavily_api_key_file", key)
    monkeypatch.setattr("app.plugins.web_search.httpx.AsyncClient", client)
    result = await WebSearchPlugin().execute("acc_sri_owner", {"query": "example", "count": 1})
    assert result["results"] == [{"title": "Example", "url": "https://example.com/", "snippet": "Excerpt"}]
    assert requests[0].headers["Authorization"] == "Bearer synthetic-test-key"
    assert requests[0].url.host == "api.tavily.com" and requests[0].method == "POST"
    assert json.loads(requests[0].content) == {
        "query": "example",
        "search_depth": "basic",
        "max_results": 1,
        "include_answer": False,
        "include_raw_content": False,
        "auto_parameters": False,
    }


@pytest.mark.asyncio
async def test_search_rejects_oversized_provider_response(monkeypatch, tmp_path):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * (1024 * 1024 + 1)))
    key = tmp_path / "key"
    key.write_text("synthetic-test-key")
    key.chmod(0o600)
    monkeypatch.setattr(settings, "tavily_api_key_file", key)
    monkeypatch.setattr(
        "app.plugins.web_search.httpx.AsyncClient", lambda **kwargs: original(transport=transport, **kwargs)
    )
    result = await WebSearchPlugin().execute("acc_sri_owner", {"query": "example"})
    assert result == {"success": False, "error": "Search response exceeded size limit"}


@pytest.mark.asyncio
async def test_search_refuses_unprivate_and_symlinked_keys(monkeypatch, tmp_path):
    key = tmp_path / "key"
    key.write_text("synthetic-test-key")
    key.chmod(0o644)
    monkeypatch.setattr(settings, "tavily_api_key_file", key)
    plugin = WebSearchPlugin()
    assert (await plugin.execute("acc_sri_owner", {"query": "example"}))["status"] == "unavailable"
    key.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(key)
    monkeypatch.setattr(settings, "tavily_api_key_file", link)
    assert (await plugin.execute("acc_sri_owner", {"query": "example"}))["status"] == "unavailable"


@pytest.mark.asyncio
async def test_search_error_never_returns_provider_body(monkeypatch, tmp_path):
    key = tmp_path / "key"
    key.write_text("synthetic-test-key")
    key.chmod(0o600)
    monkeypatch.setattr(settings, "tavily_api_key_file", key)
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(429, text="synthetic secret provider body"))
    monkeypatch.setattr(
        "app.plugins.web_search.httpx.AsyncClient", lambda **kwargs: original(transport=transport, **kwargs)
    )
    result = await WebSearchPlugin().execute("acc_sri_owner", {"query": "example"})
    assert result == {"success": False, "error": "Tavily API returned status 429"}


@pytest.mark.asyncio
async def test_search_rejects_invalid_result_payload(monkeypatch, tmp_path):
    key = tmp_path / "key"
    key.write_text("synthetic-test-key")
    key.chmod(0o600)
    monkeypatch.setattr(settings, "tavily_api_key_file", key)
    original = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"results": [{"title": "Bad", "url": "javascript:alert(1)", "content": "x"}]}
        )
    )
    monkeypatch.setattr(
        "app.plugins.web_search.httpx.AsyncClient", lambda **kwargs: original(transport=transport, **kwargs)
    )
    result = await WebSearchPlugin().execute("acc_sri_owner", {"query": "example"})
    assert result == {"success": False, "error": "Search response was invalid"}


@pytest.mark.asyncio
async def test_search_verifier_with_synthetic_provider(monkeypatch, tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "verify-tavily-search.py"
    spec = importlib.util.spec_from_file_location("verify_tavily_search", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"results": [{"title": "SQLite", "url": "https://www.sqlite.org/wal.html", "content": "SQLite"}]},
        )

    original_client = httpx.AsyncClient
    original_data_dir = settings.data_dir
    key = tmp_path / "key"
    key.write_text("synthetic-verifier-key")
    key.chmod(0o600)
    monkeypatch.setattr(settings, "tavily_api_key_file", key)
    monkeypatch.setattr(
        "app.plugins.web_search.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    try:
        result = await module.main()
    finally:
        settings.data_dir = original_data_dir
    assert result["passed"] and all(result["checks"].values())
    assert len(requests) == 1
