"""Explicit synthetic transport/DNS cases; real public fetch is a separate receipt."""

import asyncio
import json
import socket

import httpcore
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.plugins import web_fetch
from app.plugins.registry import registry
from app.storage.database import get_db

ACCOUNT = "acc_sri_owner"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/",
        "https://user:secret@example.com/",
        "https://example.com:8443/",
        "https://example.com/#x",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://metadata.internal/",
        "https://a.local/",
        "https://example.com\\@localhost/",
        "https://%31%32%37.0.0.1/",
        "https://example.com/\r\nX: a",
        "https://example.com./",
        "https://a..com/",
    ],
)
def test_url_policy(url):
    with pytest.raises(ValueError):
        web_fetch.canonical_url(url)


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0",
        "127.0.0.1",
        "10.1.1.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "192.0.0.9",
        "192.88.99.1",
        "192.0.2.1",
        "224.0.0.1",
        "240.0.0.1",
        "::1",
        "::ffff:127.0.0.1",
        "fc00::1",
        "fe80::1",
        "64:ff9b::7f00:1",
        "2002:7f00:1::",
        "2001:db8::1",
        "2001::1",
        "3fff::1",
    ],
)
def test_special_addresses_rejected(address):
    assert not web_fetch.public_address(address)


@pytest.mark.asyncio
async def test_dns_mixed_answer_and_numeric_connect_pin(monkeypatch):
    loop = asyncio.get_running_loop()
    answers = ["93.184.215.14", "127.0.0.1"]
    lookups = []
    connected = []

    async def resolve(host, port, **kwargs):
        lookups.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in answers]

    async def connect(host, port, **kwargs):
        connected.append(host)
        return object()

    monkeypatch.setattr(loop, "getaddrinfo", resolve)
    backend = web_fetch.PublicNetworkBackend()
    monkeypatch.setattr(backend.backend, "connect_tcp", connect)
    with pytest.raises(web_fetch.FetchPolicyError):
        await backend.connect_tcp("example.com", 443)
    assert not connected
    answers[:] = ["93.184.215.14"]
    await backend.connect_tcp("example.com", 443)
    assert connected == ["93.184.215.14"] and lookups == ["example.com", "example.com"]
    answers[:] = ["169.254.169.254"]
    with pytest.raises(web_fetch.FetchPolicyError):
        await backend.connect_tcp("example.com", 443)
    assert connected == ["93.184.215.14"]
    with pytest.raises(web_fetch.FetchPolicyError):
        await backend.connect_unix_socket("/tmp/forbidden")


class SyntheticPool:
    def __init__(self, response):
        self.response = response
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def stream(self, *args, **kwargs):
        return self.response


class SyntheticResponse:
    def __init__(self, *, status=200, headers=None, chunks=None):
        self.status = status
        self.headers = headers or [(b"content-type", b"text/html")]
        self.chunks = (
            chunks
            if chunks is not None
            else [b"<title>Title</title><script>secret script</script><style>hidden style</style><p>Public text</p>"]
        )
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def aiter_stream(self):
        for chunk in self.chunks:
            yield chunk


@pytest.mark.asyncio
async def test_extraction_truncation_and_response_close(monkeypatch):
    response = SyntheticResponse()
    pool = SyntheticPool(response)
    monkeypatch.setattr(web_fetch.httpcore, "AsyncConnectionPool", lambda **kwargs: pool)
    result = await web_fetch.fetch_public("https://EXAMPLE.com/")
    assert result["success"] and result["trust"] == "untrusted_web_content"
    assert result["title"] == "Title" and result["url"] == "https://example.com/"
    assert "Public text" in result["text"] and "secret" not in result["text"] and "hidden" not in result["text"]
    assert pool.closed and response.closed
    response.chunks = [b"x" * (web_fetch.MAX_TEXT + 10)]
    result = await web_fetch.fetch_public("https://example.com/")
    assert result["truncated"] and len(result["text"]) == web_fetch.MAX_TEXT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        SyntheticResponse(status=302, headers=[(b"location", b"http://127.0.0.1/")]),
        SyntheticResponse(headers=[(b"content-type", b"application/pdf")]),
        SyntheticResponse(headers=[(b"content-type", b"text/plain"), (b"content-encoding", b"gzip")]),
        SyntheticResponse(headers=[(b"content-type", b"text/plain"), (b"content-length", b"99999999")]),
        SyntheticResponse(chunks=[b"x" * web_fetch.MAX_BODY, b"x"]),
        SyntheticResponse(headers=[(b"content-type", b"text/plain"), (b"x", b"x" * 17000)]),
    ],
)
async def test_response_policy_failure_closes_without_success(monkeypatch, response):
    pool = SyntheticPool(response)
    monkeypatch.setattr(web_fetch.httpcore, "AsyncConnectionPool", lambda **kwargs: pool)
    result = await web_fetch.WebFetchPlugin().execute(ACCOUNT, {"url": "https://example.com/"})
    assert not result["success"] and result["status"] == "failed"
    assert pool.closed and response.closed


@pytest.mark.asyncio
async def test_permission_owner_isolation_idempotency_and_journal(monkeypatch):
    calls = []

    async def synthetic_fetch(url):
        calls.append(url)
        return {"success": True, "text": "explicit synthetic result", "url": url}

    monkeypatch.setattr(web_fetch, "fetch_public", synthetic_fetch)
    denied = await registry.dispatch("web_fetch", ACCOUNT, {"url": "https://example.com/private-query?q=sensitive"})
    assert denied["status"] == "denied" and not calls
    async with get_db() as db:
        await db.execute(
            "INSERT INTO accounts(id,username,password_hash) VALUES ('ungranted-foreign','foreign-fetch-test','test-only-invalid')"
        )
        await db.execute("UPDATE accounts SET is_owner=1 WHERE id=?", (ACCOUNT,))
        await db.commit()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-token"}
    ) as client:
        assert (await client.put("/api/permissions/web_fetch/fetch", json={"enabled": True})).status_code == 200
        one = await registry.dispatch(
            "web_fetch", ACCOUNT, {"url": "https://example.com/private-query?q=sensitive"}, operation_id="fetch-once"
        )
        assert one["success"]
        two = await registry.dispatch(
            "web_fetch", ACCOUNT, {"url": "https://example.com/private-query?q=sensitive"}, operation_id="fetch-once"
        )
        assert one == two and len(calls) == 1
        foreign = await registry.dispatch("web_fetch", "ungranted-foreign", {"url": "https://example.com/"})
        assert foreign["status"] == "denied" and len(calls) == 1
        assert (await client.put("/api/permissions/web_fetch/fetch", json={"enabled": False})).status_code == 200
        assert (await registry.dispatch("web_fetch", ACCOUNT, {"url": "https://example.com/"}))["status"] == "denied"
    async with get_db() as db:
        entries = await (
            await db.execute(
                "SELECT arguments,outcome FROM journal_entries WHERE tool_name='web_fetch' AND permission_decision='allowed'"
            )
        ).fetchall()
    assert len(entries) == 1 and "sensitive" not in entries[0]["arguments"]
    assert json.loads(entries[0]["outcome"])["status"] == "succeeded"


@pytest.mark.asyncio
async def test_cancellation_and_transport_failure_are_not_success(monkeypatch):
    started = asyncio.Event()
    closed = asyncio.Event()

    async def pause(url):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(web_fetch, "fetch_public", pause)
    async with get_db() as db:
        await db.execute(
            "INSERT INTO tool_grants(account_id,tool_name,action,tool_version,enabled) VALUES (?,'web_fetch','fetch','1',1)",
            (ACCOUNT,),
        )
        await db.commit()
    task = asyncio.create_task(
        registry.dispatch("web_fetch", ACCOUNT, {"url": "https://example.com/"}, operation_id="cancel-fetch")
    )
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    async with get_db() as db:
        assert (
            await (await db.execute("SELECT status FROM tool_operations WHERE operation_id='cancel-fetch'")).fetchone()
        )[0] == "canceled"

    async def failed(url):
        raise httpcore.ConnectError("explicit synthetic secret connection error")

    monkeypatch.setattr(web_fetch, "fetch_public", failed)
    result = await web_fetch.WebFetchPlugin().execute(ACCOUNT, {"url": "https://example.com/"})
    assert not result["success"] and result["status"] == "unavailable" and "secret" not in result["error"]


@pytest.mark.asyncio
async def test_httpcore_preserves_origin_host_and_verified_tls_when_dialing_ip(monkeypatch):
    """Explicit synthetic wire stream exercises the real HTTPcore connection path."""
    import ssl

    trace = {"bytes": b"", "closed": False}

    class Wire(httpcore.AsyncNetworkStream):
        async def read(self, max_bytes, timeout=None):
            if trace.get("read"):
                return b""
            trace["read"] = True
            return b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 4\r\n\r\ntext"

        async def write(self, buffer, timeout=None):
            trace["bytes"] += buffer

        async def aclose(self):
            trace["closed"] = True

        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            trace["sni"] = server_hostname
            trace["tls_verified"] = ssl_context.verify_mode == ssl.CERT_REQUIRED and ssl_context.check_hostname
            return self

        def get_extra_info(self, info):
            return None

    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", port))]

    async def connect(self, host, port, **kwargs):
        trace["dial"] = host
        return Wire()

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    result = await web_fetch.fetch_public("https://example.com/document")
    assert result["text"] == "text"
    assert trace["dial"] == "93.184.215.14" and trace["sni"] == "example.com" and trace["tls_verified"]
    assert b"Host: example.com\r\n" in trace["bytes"]
    assert b"Authorization" not in trace["bytes"] and b"Cookie" not in trace["bytes"]
    assert trace["closed"]


@pytest.mark.asyncio
async def test_cancel_inside_actual_fetch_context_closes_stream_and_pool(monkeypatch):
    started = asyncio.Event()

    class PausedResponse(SyntheticResponse):
        async def aiter_stream(self):
            started.set()
            await asyncio.Event().wait()
            yield b"never"

    response = PausedResponse()
    pool = SyntheticPool(response)
    monkeypatch.setattr(web_fetch.httpcore, "AsyncConnectionPool", lambda **kwargs: pool)
    task = asyncio.create_task(web_fetch.fetch_public("https://example.com/"))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert response.closed and pool.closed


@pytest.mark.asyncio
async def test_whole_fetch_deadline_closes_contexts(monkeypatch):
    class PausedResponse(SyntheticResponse):
        async def aiter_stream(self):
            await asyncio.Event().wait()
            yield b"never"

    response = PausedResponse()
    pool = SyntheticPool(response)
    monkeypatch.setattr(web_fetch.httpcore, "AsyncConnectionPool", lambda **kwargs: pool)
    assert web_fetch.FETCH_TIMEOUT_SECONDS == 10
    monkeypatch.setattr(web_fetch, "FETCH_TIMEOUT_SECONDS", 0.01)
    result = await web_fetch.WebFetchPlugin().execute(ACCOUNT, {"url": "https://example.com/"})
    assert result["status"] == "unavailable" and not result["success"]
    assert response.closed and pool.closed
