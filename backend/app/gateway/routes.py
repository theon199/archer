"""API Gateway routes for Archer V1."""

import asyncio
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from app.auth import current_account
from app.cognition.models import ModelAdapter
from app.config import settings
from app.journal.service import journal
from app.memory.extractor import extractor
from app.plugins.registry import registry
from app.runs.service import submit_run

router = APIRouter()


# Schemas
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=32000)
    client_operation_id: str | None = Field(default=None, max_length=128)
    conversation_id: str | None = None
    provider: str | None = None


class NoteCreate(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    title: str = "Untitled Note"
    content: str = ""
    tags: list[str] = Field(default_factory=list)


class NoteUpdate(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    expected_revision: int = Field(ge=1)
    title: str | None = None
    content: str | None = None
    tags: list[str] | None = None


class MemoryCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    new_content: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)


class LearningSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    learning_enabled: bool
    retrieval_enabled: bool
    expected_epoch: int = Field(ge=0)


@router.get("/health")
async def health_check():
    """System health check and status."""
    return {
        "status": "online",
        "service": "archer-core",
        "version": "0.1.0",
        "environment": settings.env,
        "active_provider": settings.model_provider,
        "database": "not_checked; use /api/ready",
    }


@router.get("/models/status")
async def models_status(account_id: str = Depends(current_account)):
    """Live check of DGX Spark model services (Ollama & SGLang router)."""
    return await ModelAdapter.get_available_models()


@router.get("/plugins")
async def list_plugins(account_id: str = Depends(current_account)):
    """List approved plugins and their input schemas."""
    return {"plugins": registry.list_plugins(account_id)}


# ------------------------------------------------------------------------------
# Chat & Streaming
# ------------------------------------------------------------------------------
@router.post("/chat/stream")
async def chat_stream(request: ChatRequest, account_id: str = Depends(current_account)):
    """Compatibility submission; execution belongs to the separate durable worker.

    New clients submit /runs and then GET /runs/{id}/events to reconnect.
    """
    run = await submit_run(
        account_id,
        request.message,
        request.conversation_id,
        request.provider,
        request.client_operation_id or uuid.uuid4().hex,
    )
    return {"run_id": run["id"], "conversation_id": run["conversation_id"], "status": run["status"]}


# ------------------------------------------------------------------------------
# Notes
# ------------------------------------------------------------------------------
def note_response(result):
    if not result.get("success"):
        raise HTTPException(
            {"denied": 403, "not_found": 404, "conflict": 409, "needs_reconciliation": 409}.get(
                result.get("status"), 400
            ),
            result.get("error", "Note operation failed"),
        )
    return result


@router.get("/notes")
async def get_notes(account_id: str = Depends(current_account)):
    return note_response(await registry.dispatch("personal_notes", account_id, {"action": "list"}))["notes"]


@router.get("/notes/{note_id}")
async def get_note(note_id: str, account_id: str = Depends(current_account)):
    return note_response(await registry.dispatch("personal_notes", account_id, {"action": "get", "id": note_id}))


@router.get("/notes/{note_id}/history")
async def note_history(note_id: str, account_id: str = Depends(current_account)):
    return note_response(await registry.dispatch("personal_notes", account_id, {"action": "history", "id": note_id}))


@router.post("/notes")
async def create_note(
    note: NoteCreate,
    idempotency_key: str = Header(min_length=1, max_length=128),
    account_id: str = Depends(current_account),
):
    return note_response(
        await registry.dispatch(
            "personal_notes",
            account_id,
            {"action": "create", **note.model_dump()},
            operation_id="api:" + idempotency_key,
        )
    )


@router.put("/notes/{note_id}")
async def update_note(
    note_id: str,
    note: NoteUpdate,
    idempotency_key: str = Header(min_length=1, max_length=128),
    account_id: str = Depends(current_account),
):
    return note_response(
        await registry.dispatch(
            "personal_notes",
            account_id,
            {"action": "update", "id": note_id, **note.model_dump(exclude_none=True)},
            operation_id="api:" + idempotency_key,
        )
    )


@router.delete("/notes/{note_id}")
async def delete_note(
    note_id: str,
    expected_revision: int = Query(ge=1),
    idempotency_key: str = Header(min_length=1, max_length=128),
    account_id: str = Depends(current_account),
):
    return note_response(
        await registry.dispatch(
            "personal_notes",
            account_id,
            {"action": "delete", "id": note_id, "expected_revision": expected_revision},
            operation_id="api:" + idempotency_key,
        )
    )


# ------------------------------------------------------------------------------
# Learned Memories
# ------------------------------------------------------------------------------
@router.get("/memory/settings")
async def learning_settings(account_id: str = Depends(current_account)):
    return await extractor.settings(account_id)


@router.put("/memory/settings")
async def update_learning_settings(body: LearningSettings, account_id: str = Depends(current_account)):
    return await extractor.change_settings(
        account_id, body.learning_enabled, body.retrieval_enabled, body.expected_epoch
    )


@router.get("/memories")
async def get_memories(account_id: str = Depends(current_account)):
    return await extractor.get_memories(account_id)


@router.put("/memories/{memory_id}")
async def correct_memory(
    memory_id: str,
    correction: MemoryCorrection,
    account_id: str = Depends(current_account),
):
    success = await extractor.correct_memory(
        account_id, memory_id, correction.new_content, correction.expected_revision
    )
    return {"success": success}


@router.delete("/memories/{memory_id}")
async def delete_memory(
    memory_id: str, expected_revision: int = Query(ge=1), account_id: str = Depends(current_account)
):
    success = await extractor.delete_memory(account_id, memory_id, expected_revision)
    return {"success": success}


# ------------------------------------------------------------------------------
# Action Journal
# ------------------------------------------------------------------------------
@router.get("/journal")
async def get_journal_history(
    limit: int = Query(default=30, ge=1, le=100),
    account_id: str = Depends(current_account),
):
    return await journal.get_history(account_id=account_id, limit=limit)


@router.get("/journal/stream")
async def stream_journal(
    account_id: str = Depends(current_account),
    authorization: str = Header(default=""),
    last_event_id: str | None = Header(default=None),
    after: int = Query(default=0, ge=0),
):
    """Replay persisted journal transitions across gateway/worker processes."""
    if last_event_id is not None:
        try:
            after = int(last_event_id)
            if after < 0:
                raise ValueError
        except ValueError:
            raise HTTPException(422, "Invalid journal event cursor") from None

    async def event_generator():
        cursor = after
        while True:
            try:
                await current_account(authorization)
            except HTTPException:
                yield {"event": "auth_expired", "data": "{}"}
                return
            for row in await journal.events_after(account_id, cursor):
                cursor = row["seq"]
                yield {"event": "action", "id": str(cursor), "data": row["data"]}
            await asyncio.sleep(0.25)

    return EventSourceResponse(event_generator(), send_timeout=30, headers={"Cache-Control": "no-store"})
