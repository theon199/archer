"""Durable execution tests use explicitly mocked model/agent output, never real inference."""

import asyncio
import json
import socket
import struct
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app
from app.runs.routes import stream_events
from app.runs.service import append_event, cancel_run, claim, events_after, get_run, recover, submit_run
from app.runs.worker import Worker
from app.storage.database import get_db, init_db

ACCOUNT = "acc_sri_owner"
HEADERS = {"Authorization": "Bearer test-token"}


async def submit(op="op", conv=None):
    return await submit_run(ACCOUNT, "A test-only daily plan", conv, "local-ollama", op)


async def model_reply(*args, **kwargs):
    return {"content": "Mocked daily plan", "tool_calls": []}


@pytest.mark.asyncio
async def test_atomic_idempotency_conflict_and_queue_limit():
    one, two = await asyncio.gather(submit(), submit())
    assert one["id"] == two["id"]
    with pytest.raises(HTTPException) as conflict:
        await submit_run(ACCOUNT, "different", None, "local-ollama", "op")
    assert conflict.value.status_code == 409
    async with get_db() as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM messages")).fetchone())[0] == 1
    for i in range(19):
        await submit(str(i))
    with pytest.raises(HTTPException) as full:
        await submit("overflow")
    assert full.value.status_code == 429
    assert (await submit())["id"] == one["id"]


@pytest.mark.asyncio
async def test_durable_success_replay_snapshot_and_foreign_account(monkeypatch):
    monkeypatch.setattr("app.cognition.models.ModelAdapter.chat_with_tools_step", model_reply)
    run = await submit()
    with Worker() as worker:
        assert await worker.run_one()
    await init_db()  # New connections and repeat startup preserve durable state.
    row = await get_run(ACCOUNT, run["id"])
    assert row["status"] == "succeeded"
    assert row["output"] == "Mocked daily plan"
    events = await events_after(ACCOUNT, run["id"], 0)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=HEADERS) as client:
        replay = await client.get(f"/api/v1/runs/{run['id']}/events", headers={"Last-Event-ID": "2"})
        assert replay.status_code == 200
        assert "id: 1\r\n" not in replay.text and "id: 2\r\n" not in replay.text
        assert "event: token" in replay.text and "event: done" in replay.text
        assert "lease_owner" not in (await client.get(f"/api/v1/runs/{run['id']}")).json()
        assert (
            await client.get(f"/api/v1/runs/{run['id']}/events", headers={"Last-Event-ID": "bad"})
        ).status_code == 422
        history = (await client.get(f"/api/v1/conversations/{run['conversation_id']}/messages")).json()
        assert [m["role"] for m in history] == ["user", "assistant"]
    with pytest.raises(HTTPException) as denied:
        await events_after("foreign-account", run["id"], 0)
    assert denied.value.status_code == 404
    with pytest.raises(HTTPException):
        await cancel_run("foreign-account", run["id"])


@pytest.mark.asyncio
async def test_queued_cancel_is_idempotent_and_never_executes(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Canceled queued job must never reach inference")

    monkeypatch.setattr("app.cognition.models.ModelAdapter.chat_with_tools_step", forbidden)
    run = await submit()
    assert (await cancel_run(ACCOUNT, run["id"]))["status"] == "canceled"
    assert (await cancel_run(ACCOUNT, run["id"]))["status"] == "canceled"
    with Worker() as worker:
        assert not await worker.run_one()
    assert len(await events_after(ACCOUNT, run["id"], 0)) == 3


@pytest.mark.asyncio
async def test_running_cancel_stops_mock_provider(monkeypatch):
    started, stopped = asyncio.Event(), asyncio.Event()

    async def blocking(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr("app.cognition.models.ModelAdapter.chat_with_tools_step", blocking)
    run = await submit()
    with Worker() as worker:
        task = asyncio.create_task(worker.run_one())
        await asyncio.wait_for(started.wait(), 2)
        await cancel_run(ACCOUNT, run["id"])
        await asyncio.wait_for(task, 2)
    assert stopped.is_set()
    assert (await get_run(ACCOUNT, run["id"]))["status"] == "canceled"


@pytest.mark.asyncio
async def test_unknown_tool_outcome_survives_restart_without_retry(monkeypatch):
    run = await submit()
    queued = await submit("still-queued")
    with Worker() as worker:
        active = await claim(worker.owner)
        await append_event(active, "tool_call", {"tool": "test-only-write", "arguments": {}})
        # Simulate abrupt process loss after durable intent but before result.
    with Worker():
        await recover()
    assert (await get_run(ACCOUNT, run["id"]))["status"] == "needs_reconciliation"
    assert (await get_run(ACCOUNT, queued["id"]))["status"] == "queued"
    # Old generation/owner cannot append after recovery.
    with pytest.raises(RuntimeError):
        await append_event(active, "token", {"token": "stale"})


@pytest.mark.asyncio
async def test_worker_lock_and_generation_fencing():
    await submit()
    with Worker() as worker:
        with pytest.raises(RuntimeError, match="Another Archer worker"):
            with Worker():
                pass
        active = await claim(worker.owner)
        assert await claim("other-worker") is None
        forged = dict(active, generation=active["generation"] + 1)
        with pytest.raises(RuntimeError):
            await append_event(forged, "token", {"token": "stale"})
        async with get_db() as db:
            await db.execute("UPDATE runs SET lease_until = ?", (time.time() - 1,))
            await db.commit()
        with pytest.raises(RuntimeError):
            await append_event(active, "token", {"token": "expired"})


@pytest.mark.asyncio
async def test_stream_disconnect_does_not_cancel_job_and_revocation_is_rechecked():
    run = await submit()
    response = await stream_events(
        run["id"], after=0, last_event_id=None, authorization="Bearer test-token", account_id=ACCOUNT
    )
    iterator = response.body_iterator
    event = await anext(iterator)
    assert event["event"] == "state"
    await iterator.aclose()  # Simulated reader disconnect; no execution is attached.
    assert (await get_run(ACCOUNT, run["id"]))["status"] == "queued"
    response = await stream_events(
        run["id"], after=0, last_event_id=None, authorization="Bearer test-token", account_id=ACCOUNT
    )
    iterator = response.body_iterator
    await anext(iterator)
    async with get_db() as db:
        await db.execute("DELETE FROM sessions")
        await db.commit()
    event = await asyncio.wait_for(anext(iterator), 2)
    assert event["event"] == "auth_expired"
    await iterator.aclose()


@pytest.mark.asyncio
async def test_timeout_and_provider_failures_are_failed_not_success(monkeypatch):
    async def unavailable(*args, **kwargs):
        raise RuntimeError("a secret provider error body must not be stored")

    monkeypatch.setattr("app.cognition.models.ModelAdapter.chat_with_tools_step", unavailable)
    run = await submit()
    with Worker() as worker:
        await worker.run_one()
    failed = await get_run(ACCOUNT, run["id"])
    assert failed["status"] == "failed" and failed["output"] == ""
    assert "secret" not in failed["error"]

    async def slow(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("app.cognition.models.ModelAdapter.chat_with_tools_step", slow)
    monkeypatch.setattr(settings, "run_timeout_seconds", 0.01)
    timeout = await submit("timeout")
    with Worker() as worker:
        await worker.run_one()
    assert (await get_run(ACCOUNT, timeout["id"]))["error"] == "Run time limit exceeded"


@pytest.mark.asyncio
async def test_provider_failure_flushes_short_buffered_output(monkeypatch):
    async def interrupted(_run):
        yield {"event": "token", "data": json.dumps({"token": "short partial"})}
        raise RuntimeError("synthetic provider failure")

    monkeypatch.setattr("app.runs.worker.execute", interrupted)
    monkeypatch.setattr("app.runs.worker.time", SimpleNamespace(monotonic=lambda: 0, time=time.time))
    run = await submit("short-buffer-failure")
    with Worker() as worker:
        assert await worker.run_one()
    failed = await get_run(ACCOUNT, run["id"])
    assert failed["status"] == "failed"
    assert failed["output"] == "short partial"
    events = await events_after(ACCOUNT, run["id"], 0)
    assert [json.loads(event["data"])["token"] for event in events if event["event"] == "token"] == ["short partial"]
    assert json.loads(events[-1]["data"])["status"] == "failed"


@pytest.mark.asyncio
async def test_loopback_provider_outage_and_recovery(monkeypatch):
    # Reserve an actual port without listening, then start a synthetic provider
    # on that same port. No real model service or adapter is replaced.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    monkeypatch.setattr(settings, "ollama_base_url", f"http://127.0.0.1:{sock.getsockname()[1]}")
    monkeypatch.setattr(settings, "expected_model_digest", "")
    requests = []

    async def respond(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(line.split(b":", 1)[1])
                for line in headers.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            requests.append(json.loads(await reader.readexactly(length)))
            body = json.dumps({"message": {"content": "Synthetic recovery response", "tool_calls": []}}).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = None
    try:
        failed_run = await submit("provider-outage")
        with Worker() as worker:
            assert await worker.run_one()
            failed = await get_run(ACCOUNT, failed_run["id"])
            assert failed["status"] == "failed"
            assert failed["output"] == ""
            assert failed["error"] == "Execution failed; check local provider availability or tool outcome"

            sock.listen()
            server = await asyncio.start_server(respond, sock=sock)
            assert (await submit("provider-outage"))["id"] == failed_run["id"]
            assert not await worker.run_one()
            assert requests == []

            recovered_run = await submit("provider-recovered")
            assert await worker.run_one()
            recovered = await get_run(ACCOUNT, recovered_run["id"])
            assert recovered["status"] == "succeeded"
            assert recovered["output"] == "Synthetic recovery response"
            assert len(requests) == 1
            assert (await get_run(ACCOUNT, failed_run["id"]))["status"] == "failed"
    finally:
        if server:
            server.close()
            await server.wait_closed()
        else:
            sock.close()


@pytest.mark.asyncio
async def test_provider_socket_reset_does_not_retry_and_new_run_recovers(monkeypatch):
    requests = []

    async def respond(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(line.split(b":", 1)[1])
                for line in headers.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            requests.append(json.loads(await reader.readexactly(length)))
            if len(requests) == 1:
                # An incomplete response followed by a TCP reset, not a model reply.
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n{")
                await writer.drain()
                sock = writer.get_extra_info("socket")
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                writer.transport.abort()
                return
            body = json.dumps({"message": {"content": "Synthetic recovery response", "tool_calls": []}}).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(settings, "ollama_base_url", f"http://127.0.0.1:{port}")
        monkeypatch.setattr(settings, "expected_model_digest", "")
        failed_run = await submit("provider-reset")
        with Worker() as worker:
            assert await worker.run_one()
            failed = await get_run(ACCOUNT, failed_run["id"])
            assert failed["status"] == "failed"
            assert failed["output"] == ""
            assert failed["error"] == "Execution failed; check local provider availability or tool outcome"
            assert len(requests) == 1
            assert (await submit("provider-reset"))["id"] == failed_run["id"]
            assert not await worker.run_one()
            assert len(requests) == 1

            recovered_run = await submit("provider-reset-recovered")
            assert await worker.run_one()
            recovered = await get_run(ACCOUNT, recovered_run["id"])
            assert recovered["status"] == "succeeded"
            assert recovered["output"] == "Synthetic recovery response"
            assert len(requests) == 2
            assert (await get_run(ACCOUNT, failed_run["id"]))["status"] == "failed"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["P" * 600, "short partial"])
async def test_stream_socket_reset_preserves_persisted_output_and_replays_failure(monkeypatch, prefix):
    requests = []
    stream_started = asyncio.Event()
    reset_stream = asyncio.Event()
    token_buffered = asyncio.Event()

    async def respond(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(line.split(b":", 1)[1])
                for line in headers.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            request = json.loads(await reader.readexactly(length))
            requests.append(request)
            if request["stream"]:
                frame = json.dumps({"message": {"content": prefix}, "done": False}).encode() + b"\n"
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\nContent-Length: 10000\r\n\r\n" + frame
                )
                await writer.drain()
                stream_started.set()
                await asyncio.wait_for(reset_stream.wait(), 5)
                writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                writer.transport.abort()
                return
            content = "" if len(requests) == 1 else "Synthetic recovery response"
            body = json.dumps({"message": {"content": content, "tool_calls": []}}).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(settings, "ollama_base_url", f"http://127.0.0.1:{port}")
        monkeypatch.setattr(settings, "expected_model_digest", "")
        if len(prefix) < 512:
            from app.runs.execution import execute as real_execute

            async def observe_execution(run):
                async for item in real_execute(run):
                    yield item
                    if item["event"] == "token":
                        token_buffered.set()

            monkeypatch.setattr("app.runs.worker.execute", observe_execution)
            monkeypatch.setattr("app.runs.worker.time", SimpleNamespace(monotonic=lambda: 0, time=time.time))
        run = await submit("stream-reset")
        with Worker() as worker:
            task = asyncio.create_task(worker.run_one())
            try:
                await asyncio.wait_for(stream_started.wait(), 5)
                if len(prefix) >= 512:
                    async with asyncio.timeout(5):
                        while (await get_run(ACCOUNT, run["id"]))["output"] != prefix:
                            await asyncio.sleep(0.01)
                else:
                    # Observe the worker requesting the next token while its
                    # frozen batching clock keeps this prefix unflushed.
                    await asyncio.wait_for(token_buffered.wait(), 5)
                    assert (await get_run(ACCOUNT, run["id"]))["output"] == ""
                reset_stream.set()
                assert await asyncio.wait_for(task, 5)
            finally:
                reset_stream.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

            failed = await get_run(ACCOUNT, run["id"])
            assert failed["status"] == "failed" and failed["output"] == prefix
            events = await events_after(ACCOUNT, run["id"], 0)
            tokens = [event for event in events if event["event"] == "token"]
            assert len(tokens) == 1 and json.loads(tokens[0]["data"])["token"] == prefix
            assert json.loads(events[-1]["data"])["status"] == "failed"
            assert (await submit("stream-reset"))["id"] == run["id"]
            assert not await worker.run_one()
            assert len(requests) == 2
            next_run = await submit("stream-reset-recovered")
            assert await worker.run_one()
            assert (await get_run(ACCOUNT, next_run["id"]))["output"] == "Synthetic recovery response"
            assert (await get_run(ACCOUNT, next_run["id"]))["status"] == "succeeded"
            assert len(requests) == 3
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_queued_turns_include_prior_answer_but_not_future_input(monkeypatch):
    first = await submit("first")
    second = await submit_run(ACCOUNT, "second turn", first["conversation_id"], "local-ollama", "second")
    await submit_run(ACCOUNT, "future turn", first["conversation_id"], "local-ollama", "third")
    prompts = []

    async def record(messages, **kwargs):
        prompts.append(messages)
        return await model_reply()

    monkeypatch.setattr("app.cognition.models.ModelAdapter.chat_with_tools_step", record)
    with Worker() as worker:
        await worker.run_one()
        await worker.run_one()
    assert [m["role"] for m in prompts[1]] == ["user", "assistant", "user"]
    assert prompts[1][-1]["content"] == "second turn"
    assert (await get_run(ACCOUNT, second["id"]))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_actual_process_loss_releases_lock_and_preserves_unknown_intent():
    """Crash a test-only subprocess after intent commit, then recover the same DB."""
    import os
    import subprocess
    import sys

    run = await submit()
    code = """
import asyncio, os
from app.runs.worker import Worker
from app.runs.service import claim, append_event
async def crash():
    with Worker() as worker:
        run = await claim(worker.owner)
        await append_event(run, "tool_call", {"tool": "test-only-no-dispatch", "arguments": {}})
        os._exit(23)
asyncio.run(crash())
"""
    env = dict(os.environ, ARCHER_DATA_DIR=str(settings.data_dir))
    process = await asyncio.to_thread(
        subprocess.run, [sys.executable, "-c", code], env=env, capture_output=True, timeout=5, check=False
    )
    assert process.returncode == 23, process.stderr.decode()
    with Worker():
        await recover()
    result = await get_run(ACCOUNT, run["id"])
    assert result["status"] == "needs_reconciliation"
    assert "no automatic retry" in result["error"]


@pytest.mark.asyncio
async def test_recovery_audit_failure_rolls_back_terminal_decision(monkeypatch):
    """Real SQL recovery must not commit its terminal state without its audit."""
    from app.plugins import registry

    run = await submit("recovery-audit-rollback")
    with Worker() as worker:
        active = await claim(worker.owner)
        await append_event(active, "tool_call", {"tool": "test-only-no-dispatch", "arguments": {}})
    original_record = registry.record

    async def fail_after_audit(*args, **kwargs):
        await original_record(*args, **kwargs)
        raise RuntimeError("Explicit test-only recovery audit fault")

    monkeypatch.setattr(registry, "record", fail_after_audit)
    with Worker():
        with pytest.raises(RuntimeError, match="test-only recovery audit fault"):
            await recover()
    assert (await get_run(ACCOUNT, run["id"]))["status"] == "generating"
    assert not any(event["event"] == "done" for event in await events_after(ACCOUNT, run["id"], 0))
    async with get_db() as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM journal_entries")).fetchone())[0] == 0
        assert (await (await db.execute("SELECT COUNT(*) FROM journal_events")).fetchone())[0] == 0
    monkeypatch.setattr(registry, "record", original_record)
    with Worker():
        await recover()
        await recover()
    assert (await get_run(ACCOUNT, run["id"]))["status"] == "needs_reconciliation"
    async with get_db() as db:
        assert (
            await (await db.execute("SELECT COUNT(*) FROM journal_entries WHERE tool_name='run_recovery'")).fetchone()
        )[0] == 1
        assert (await (await db.execute("SELECT COUNT(*) FROM journal_events")).fetchone())[0] == 1
    assert sum(event["event"] == "done" for event in await events_after(ACCOUNT, run["id"], 0)) == 1
