"""Bounded stdio framing before decoding for the pinned MCP SDK.

One subprocess per invocation. No shell, ambient environment, payload logs or retries.
The Docker caller separately owns and removes its exact container.
"""

import asyncio
import os
import signal
from contextlib import asynccontextmanager, suppress

import anyio
from mcp import types
from mcp.shared.message import SessionMessage

MAX_FRAME_BYTES = 128 * 1024  # Includes newline, checked before UTF-8/JSON decode.
MAX_TOTAL_BYTES = 2 * 1024 * 1024  # Per direction, per invocation.
MAX_MESSAGES = 128  # Per direction, includes notifications.


class MCPTransportError(RuntimeError):
    """Payload-free transport failure."""


def decode_frame(frame):
    try:
        return SessionMessage(types.jsonrpc_message_adapter.validate_json(frame, by_name=False))
    except Exception:
        raise MCPTransportError("MCP protocol frame is invalid") from None


@asynccontextmanager
async def bounded_stdio_client(parameters, errlog=None):
    """SDK-compatible bounded streams; caller supplies the complete clean env."""
    process = await anyio.open_process(
        [parameters.command, *parameters.args],
        env=parameters.env if parameters.env is not None else {},
        cwd=parameters.cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
        umask=0o077,
    )
    incoming_send, incoming_receive = anyio.create_memory_object_stream(1)
    outgoing_send, outgoing_receive = anyio.create_memory_object_stream(1)

    def stop(sig):
        if process.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, sig)

    async def fail(message):
        stop(signal.SIGTERM)
        with suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
            await incoming_send.send(MCPTransportError(message))

    async def reader():
        total = count = 0
        buffer = bytearray()
        try:
            while True:
                try:
                    chunk = await process.stdout.receive(4096)
                except anyio.EndOfStream:
                    if buffer:
                        await fail("MCP protocol frame is incomplete")
                    return
                buffer.extend(chunk)
                while (end := buffer.find(b"\n")) >= 0:
                    size = end + 1
                    total += size
                    count += 1
                    if size > MAX_FRAME_BYTES or total > MAX_TOTAL_BYTES or count > MAX_MESSAGES:
                        await fail("MCP protocol traffic exceeds limits")
                        return
                    frame = bytes(buffer[:size])
                    del buffer[:size]
                    try:
                        message = decode_frame(frame)
                    except MCPTransportError:
                        await fail("MCP protocol frame is invalid")
                        return
                    await incoming_send.send(message)
                if len(buffer) >= MAX_FRAME_BYTES:
                    # A full buffer without newline cannot become a valid frame.
                    await fail("MCP protocol frame exceeds byte limit")
                    return
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            pass
        except Exception:
            await fail("MCP protocol read failed")
        finally:
            await incoming_send.aclose()

    async def writer():
        total = count = 0
        try:
            async with outgoing_receive:
                async for message in outgoing_receive:
                    frame = (message.message.model_dump_json(by_alias=True, exclude_unset=True) + "\n").encode()
                    total += len(frame)
                    count += 1
                    if len(frame) > MAX_FRAME_BYTES or total > MAX_TOTAL_BYTES or count > MAX_MESSAGES:
                        await fail("MCP outgoing traffic exceeds limits")
                        return
                    await process.stdin.send(frame)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            pass
        except Exception:
            await fail("MCP protocol write failed")

    tasks = [asyncio.create_task(reader()), asyncio.create_task(writer())]

    async def cleanup():
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        stop(signal.SIGTERM)
        # Close the bounded receive pipe before waiting: a paused pipe reader
        # must not prevent reaping a server that floods stdout.
        await process.stdout.aclose()
        try:
            await asyncio.wait_for(process.wait(), 1)
        except TimeoutError:
            stop(signal.SIGKILL)
            await asyncio.wait_for(process.wait(), 1)
        # Process.aclose closes the underlying pipe transports, beyond stream wrappers.
        await process.aclose()
        await incoming_send.aclose()
        await incoming_receive.aclose()
        await outgoing_send.aclose()
        await outgoing_receive.aclose()

    try:
        yield incoming_receive, outgoing_send
    finally:
        cleanup_task = asyncio.create_task(cleanup())
        interrupted = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                interrupted = True
        cleanup_task.result()
        if interrupted:
            raise asyncio.CancelledError
