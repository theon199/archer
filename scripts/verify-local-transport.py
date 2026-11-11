#!/usr/bin/env python3
"""Development-only actual Ollama forwarding/fault relay; synthetic private data.

Never supplies a generated response or fake provider result. Only truncates its
own forwarded HTTP stream. Does not stop/reconfigure the existing Ollama service.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import socket
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
EVIDENCE = ROOT / "docs/evidence/checkpoint-16-local-transport.json"
UPSTREAM = "http://127.0.0.1:11434"
DIGEST = "08ae7ec1744bd7f451c4a530afb39d2673ad9d07a8369b8a33a3613b41212a68"


async def main(scenario="truncate"):
    from app.auth import Login, login, provision_owner
    from app.cognition.models import ModelAdapter
    from app.config import settings
    from app.main import app
    from app.runs.service import events_after, get_run, submit_run
    from app.runs.worker import Worker
    from app.storage.database import get_db

    EVIDENCE.write_text(json.dumps({"passed": False, "state": "in_progress"}) + "\n")
    checks = {}
    stats = {
        "structured_requests": 0,
        "stream_requests": 0,
        "real_content_frames_forwarded": 0,
        "done_frames_observed": 0,
        "truncated": False,
        "held_after_real_prefix": False,
        "fault_forwarder_closed": False,
    }
    fault = True
    relay = FastAPI()

    @relay.get("/api/tags")
    async def tags():
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            response = await client.get(UPSTREAM + "/api/tags")
            return Response(response.content, status_code=response.status_code, media_type="application/json")

    @relay.post("/api/chat")
    async def chat(request: Request):
        payload = await request.json()
        if not payload.get("stream"):
            stats["structured_requests"] += 1
            async with httpx.AsyncClient(trust_env=False, timeout=90) as client:
                response = await client.post(UPSTREAM + "/api/chat", json=payload)
                return Response(response.content, status_code=response.status_code, media_type="application/json")
        stats["stream_requests"] += 1

        async def forward():
            injecting = fault
            upstream_response = None
            upstream_client = None
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=90) as client:
                    upstream_client = client
                    async with client.stream("POST", UPSTREAM + "/api/chat", json=payload) as response:
                        upstream_response = response
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line:
                                continue
                            frame = json.loads(line)
                            if frame.get("done") is True:
                                stats["done_frames_observed"] += 1
                            content = frame.get("message", {}).get("content", "")
                            yield (line + "\n").encode()
                            if content.strip():
                                stats["real_content_frames_forwarded"] += 1
                                if fault and frame.get("done") is not True:
                                    if scenario == "truncate":
                                        stats["truncated"] = True
                                        return  # Real prefix, intentionally no completion marker.
                                    stats["held_after_real_prefix"] = True
                                    await asyncio.sleep(90)
                                    return
            finally:
                if injecting:
                    stats["fault_forwarder_closed"] = bool(
                        upstream_response is not None
                        and upstream_response.is_closed
                        and upstream_client is not None
                        and upstream_client.is_closed
                    )

        return StreamingResponse(forward(), media_type="application/x-ndjson")

    with tempfile.TemporaryDirectory(prefix="archer-local-transport-") as directory:
        settings.data_dir = Path(directory)
        settings.model_provider = "local-ollama"
        settings.ollama_model = "gemma4:26b"
        settings.ollama_num_ctx = 4096
        settings.expected_model_digest = DIGEST
        settings.run_timeout_seconds = 10 if scenario == "timeout" else 90
        settings.host = "127.0.0.1"
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        settings.ollama_base_url = f"http://127.0.0.1:{port}"
        server = uvicorn.Server(uvicorn.Config(relay, host="127.0.0.1", port=port, log_level="error", access_log=False))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if server.started:
                    break
                if serving.done():
                    raise RuntimeError("Owned loopback relay failed to start")
                await asyncio.sleep(0.05)
            else:
                raise RuntimeError("Owned loopback relay startup deadline")
            await provision_owner("synthetic-transport-owner", "synthetic-only-transport-password")
            async with get_db() as db:
                account = (await (await db.execute("SELECT id FROM accounts WHERE is_owner=1")).fetchone())[0]
                await db.execute(
                    "INSERT INTO tool_grants(account_id,tool_name,action,tool_version,enabled) VALUES (?,'personal_notes','create','1',1)",
                    (account,),
                )
                await db.commit()
            saved = await submit_run(
                account,
                "Create a personal note titled Synthetic Transport Note with content Synthetic transport evidence. Use personal_notes to save it, then explain in detail what was saved.",
                None,
                "local-ollama",
                "transport-write-once",
            )
            expected_status = "canceled" if scenario == "cancel" else "failed"
            run_started = time.monotonic()
            with Worker() as worker:
                executing = asyncio.create_task(worker.run_one())
                try:
                    if scenario == "cancel":
                        deadline = time.monotonic() + 60
                        while time.monotonic() < deadline:
                            observed = await get_run(account, saved["id"])
                            if stats["held_after_real_prefix"] and observed["output"].strip():
                                break
                            if executing.done():
                                raise RuntimeError("Run finished before active-response cancellation")
                            await asyncio.sleep(0.05)
                        else:
                            raise RuntimeError("No real persisted prefix before cancellation deadline")
                        auth = await login(
                            Login(username="synthetic-transport-owner", password="synthetic-only-transport-password")
                        )
                        async with httpx.AsyncClient(
                            transport=httpx.ASGITransport(app=app),
                            base_url="http://test",
                            headers={"Authorization": "Bearer " + auth["access_token"]},
                        ) as api:
                            canceled = await api.post(f"/api/v1/runs/{saved['id']}/cancel")
                            checks["authenticated_active_cancel_accepted"] = (
                                canceled.status_code == 200
                                and canceled.json()["status"] in {"cancel_requested", "canceled"}
                            )
                    assert await asyncio.wait_for(executing, 105)
                finally:
                    if not executing.done():
                        executing.cancel()
                        await asyncio.gather(executing, return_exceptions=True)
                run_elapsed = time.monotonic() - run_started
                deadline = time.monotonic() + 5
                while not stats["fault_forwarder_closed"] and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                checks["fault_forwarder_http_context_closed"] = stats["fault_forwarder_closed"]
                run = await get_run(account, saved["id"])
                checks["real_prefix_fault_exercised"] = (
                    stats["truncated"] if scenario == "truncate" else stats["held_after_real_prefix"]
                ) and stats["real_content_frames_forwarded"] > 0
                checks["run_has_expected_terminal_state"] = run["status"] == expected_status
                checks["durable_partial_output"] = bool(run["output"].strip())
                checks["sanitized_error"] = (
                    run["error"]
                    == (
                        {
                            "truncate": "Execution failed; check local provider availability or tool outcome",
                            "timeout": "Run time limit exceeded",
                            "cancel": None,
                        }[scenario]
                    )
                )
                initial_events = await events_after(account, run["id"], 0)
                checks["terminal_event_durable"] = any(
                    event["event"] == "done" and json.loads(event["data"]).get("status") == expected_status
                    for event in initial_events
                )
                authentication = await login(
                    Login(username="synthetic-transport-owner", password="synthetic-only-transport-password")
                )
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                    headers={"Authorization": "Bearer " + authentication["access_token"]},
                ) as api:
                    token_events = [event for event in initial_events if event["event"] == "token"]
                    cursor = token_events[-1]["seq"] if token_events else 0
                    response = await api.get(f"/api/v1/runs/{run['id']}/events?after={cursor}")
                    checks["reconnect_after_partial_replays_terminal_without_duplicate_token"] = (
                        response.status_code == 200
                        and "event: done" in response.text
                        and expected_status in response.text
                        and "event: token" not in response.text
                    )
                    history = await api.get(f"/api/v1/conversations/{run['conversation_id']}/history")
                    assistant = history.json()["items"][-1]
                    checks["history_retains_terminal_partial_output"] = (
                        assistant["status"] == expected_status and assistant["content"] == run["output"]
                    )
                before = dict(stats)
                checks["no_automatic_run_retry"] = not await worker.run_one() and stats == before
                # Submission replay uses original null conversation identity.
                replay = await submit_run(account, saved["message"], None, "local-ollama", "transport-write-once")
                checks["submission_replay_returns_terminal_run"] = (
                    replay["id"] == run["id"] and replay["status"] == expected_status
                )
                checks["replay_did_not_call_provider"] = stats == before
            async with get_db() as db:
                notes = await (
                    await db.execute("SELECT title,content FROM notes WHERE account_id=?", (account,))
                ).fetchall()
                checks["real_model_selected_one_committed_note"] = (
                    len(notes) == 1 and notes[0]["title"] == "Synthetic Transport Note"
                )
                operations = await (
                    await db.execute("SELECT status FROM tool_operations WHERE account_id=?", (account,))
                ).fetchall()
                checks["confirmed_tool_outcome_preserved"] = (
                    len(operations) == 1 and operations[0]["status"] == "succeeded"
                )
                journals = await (
                    await db.execute(
                        "SELECT outcome FROM journal_entries WHERE account_id=? AND tool_name='personal_notes'",
                        (account,),
                    )
                ).fetchall()
                checks["journal_reports_confirmed_note_success"] = (
                    len(journals) == 1 and json.loads(journals[0][0]).get("success") is True
                )
            fault = False
            recovered = "".join(
                [
                    token
                    async for token in ModelAdapter.stream_chat(
                        [{"role": "user", "content": "Reply with exactly TRANSPORT_RECOVERED and nothing else."}]
                    )
                ]
            )
            checks["real_stream_recovers_after_fault"] = recovered.strip() == "TRANSPORT_RECOVERED"
            checks["real_completion_frame_observed"] = stats["done_frames_observed"] > 0
            result = {
                "passed": all(checks.values()),
                "checks": checks,
                "scenario": scenario,
                "terminal_status": run["status"],
                "terminal_error": run["error"],
                "configured_run_timeout_seconds": settings.run_timeout_seconds,
                "run_elapsed_seconds": round(run_elapsed, 3),
                "stats": stats,
                "checked_at": datetime.now(UTC).isoformat(),
                "model": "gemma4:26b",
                "expected_digest": DIGEST,
                "partial_output_sha256": hashlib.sha256(run["output"].encode()).hexdigest(),
                "recovered_output": recovered,
                "scope": "Actual local Ollama generation and model-selected note tool through real worker/SQLite. Development-only loopback relay forwards actual responses and truncates or pauses only its own final stream. HTTP context closure does not prove GPU execution stops. Worker runs in-process, not restarted. No mock provider, third-party MCP, upstream GPU termination or host/network outage claim.",
            }
            EVIDENCE.write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result))
            return 0 if result["passed"] else 1
        finally:
            server.should_exit = True
            try:
                await asyncio.wait_for(serving, 10)
            except TimeoutError:
                serving.cancel()
                await asyncio.gather(serving, return_exceptions=True)
            listener.close()


if __name__ == "__main__":
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["truncate", "timeout", "cancel"], default="truncate")
    scenario = parser.parse_args().scenario
    if scenario != "truncate":
        EVIDENCE = ROOT / f"docs/evidence/checkpoint-17-local-{scenario}.json"
    try:
        status = asyncio.run(asyncio.wait_for(main(scenario), timeout=240))
    except Exception as error:
        failure = {
            "passed": False,
            "error_type": type(error).__name__,
            "checked_at": datetime.now(UTC).isoformat(),
            "scope": "Real local transport drill failed; no fake integration substituted",
        }
        EVIDENCE.write_text(json.dumps(failure, indent=2) + "\n")
        print(json.dumps(failure))
        status = 1
    raise SystemExit(status)
