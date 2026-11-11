"""Real owned adversarial subprocesses, not a third-party connector integration."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import anyio
import pytest
from mcp.client.stdio import StdioServerParameters

from app.plugins import mcp_transport as transport

FIXTURE = Path(__file__).parent / "fixtures/mcp_wire_adversary.py"


def parameters(mode, path):
    return StdioServerParameters(
        command=sys.executable, args=[str(FIXTURE), mode, str(path)], env={"PATH": "/usr/bin:/bin"}
    )


async def wait_pid(path):
    for _ in range(100):
        if path.exists() and path.read_text():
            return int(path.read_text())
        await asyncio.sleep(0.01)
    raise AssertionError("Owned fixture did not start")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["oversized", "unterminated", "partial", "invalid-json", "invalid-utf8", "one-over", "flood-ignore-term"]
)
async def test_bad_wire_rejected_and_reaped_before_payload_logging(mode, tmp_path, caplog):
    pid_path = tmp_path / "pid"
    async with asyncio.timeout(3):
        async with transport.bounded_stdio_client(parameters(mode, pid_path)) as (read, write):
            error = await read.receive()
            assert isinstance(error, transport.MCPTransportError)
            assert "SYNTHETIC_SECRET" not in str(error)
    assert not Path(f"/proc/{int(pid_path.read_text())}").exists()
    assert "SYNTHETIC_SECRET" not in caplog.text


@pytest.mark.asyncio
async def test_oversized_frame_never_reaches_decoder(tmp_path, monkeypatch):
    def forbidden(frame):
        raise AssertionError("Decoder must not see oversized bytes")

    monkeypatch.setattr(transport, "decode_frame", forbidden)
    async with transport.bounded_stdio_client(parameters("oversized", tmp_path / "pid")) as (read, write):
        assert "limit" in str(await read.receive())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [("frames", 128), ("bytes", 16)])
async def test_invocation_wire_budgets_bound_notification_flood(mode, expected, tmp_path):
    received = 0
    async with asyncio.timeout(3):
        async with transport.bounded_stdio_client(parameters(mode, tmp_path / "pid")) as (read, write):
            while True:
                message = await read.receive()
                if isinstance(message, Exception):
                    assert "limits" in str(message)
                    break
                received += 1
    assert received == expected
    assert not Path(f"/proc/{int((tmp_path / 'pid').read_text())}").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["exact", "fragmented"])
async def test_exact_boundary_and_fragmented_utf8_survive_real_transport(mode, tmp_path):
    async with transport.bounded_stdio_client(parameters(mode, tmp_path / "pid")) as (read, write):
        message = await read.receive()
        assert not isinstance(message, Exception)
        assert message.message.method == "fixture"
        if mode == "fragmented":
            assert message.message.params["value"] == "你好"
        with pytest.raises(anyio.EndOfStream):
            await read.receive()


@pytest.mark.asyncio
async def test_clean_eof_closes_stream(tmp_path):
    async with transport.bounded_stdio_client(parameters("eof", tmp_path / "pid")) as (read, write):
        with pytest.raises(anyio.EndOfStream):
            await read.receive()


@pytest.mark.asyncio
async def test_repeated_cancellation_reaps_sigterm_resistant_owned_child(tmp_path):
    pid_path = tmp_path / "pid"

    async def invocation():
        async with transport.bounded_stdio_client(parameters("ignore-term", pid_path)) as (read, write):
            await read.receive()

    task = asyncio.create_task(invocation())
    pid = await wait_pid(pid_path)
    task.cancel()
    await asyncio.sleep(0.1)
    task.cancel()
    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert time.monotonic() - started < 2
    assert not Path(f"/proc/{pid}").exists()


@pytest.mark.asyncio
async def test_outgoing_frame_limit_is_sanitized_and_child_reaped(tmp_path):
    from mcp.shared.message import SessionMessage

    frame = json.dumps(
        {"jsonrpc": "2.0", "method": "fixture", "params": {"value": "x" * (transport.MAX_FRAME_BYTES + 1)}}
    ).encode()
    message = transport.decode_frame(frame)  # Construct a valid oversized outbound object, test-only.
    assert isinstance(message, SessionMessage)
    async with transport.bounded_stdio_client(parameters("ignore-term", tmp_path / "pid")) as (read, write):
        await wait_pid(tmp_path / "pid")
        await write.send(message)
        error = await read.receive()
        assert isinstance(error, transport.MCPTransportError) and "outgoing" in str(error)
    assert not Path(f"/proc/{int((tmp_path / 'pid').read_text())}").exists()


@pytest.mark.asyncio
async def test_real_sdk_timeout_closes_owned_server(tmp_path):
    from mcp import ClientSession, types
    from mcp.shared.exceptions import MCPError

    pid_path = tmp_path / "sdk-pid"
    server = FIXTURE.parent / "mcp_development_server.py"
    params = StdioServerParameters(command=sys.executable, args=[str(server), str(pid_path)], env={})
    started = time.monotonic()
    async with transport.bounded_stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=2) as session:
            await session.initialize()
            with pytest.raises(MCPError) as failure:
                await session.call_tool("inspect_value", {"value": "slow"}, read_timeout_seconds=0.2)
            assert failure.value.code == types.REQUEST_TIMEOUT
    assert time.monotonic() - started < 3
    assert not Path(f"/proc/{int(pid_path.read_text())}").exists()


@pytest.mark.asyncio
async def test_repeated_sessions_do_not_accumulate_descriptors_or_io_tasks(tmp_path):
    async def invoke(index):
        async with transport.bounded_stdio_client(parameters("exact", tmp_path / f"pid-{index}")) as (read, write):
            await read.receive()
        await asyncio.sleep(0)

    await invoke(0)  # Warm the subprocess backend before measuring retained handles.
    descriptors = len(os.listdir("/proc/self/fd"))
    tasks = {task for task in asyncio.all_tasks() if not task.done()}
    for index in range(1, 6):
        await invoke(index)
    assert len(os.listdir("/proc/self/fd")) <= descriptors
    assert {task for task in asyncio.all_tasks() if not task.done()} == tasks
