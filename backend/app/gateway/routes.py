"""API Gateway routes for Archer V1."""

import asyncio
import json
import uuid

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.cognition.models import ModelAdapter
from app.config import settings
from app.journal.service import journal
from app.memory.extractor import extractor
from app.plugins.registry import registry
from app.storage.database import get_db

router = APIRouter()


# Schemas
class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    provider: str | None = None


class NoteCreate(BaseModel):
    title: str = "Untitled Note"
    content: str = ""
    tags: list[str] = Field(default_factory=list)


class NoteUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    tags: list[str] | None = None


class MemoryCorrection(BaseModel):
    new_content: str


# In V1 local development, default to Sri's owner account if no auth header
def get_current_account(x_account_id: str | None = Header(default="acc_sri_owner")) -> str:
    return x_account_id or "acc_sri_owner"


@router.get("/health")
async def health_check():
    """System health check and status."""
    return {
        "status": "online",
        "service": "archer-core",
        "version": "0.1.0",
        "environment": settings.env,
        "active_provider": settings.model_provider,
        "database": "sqlite-connected",
    }


@router.get("/models/status")
async def models_status():
    """Live check of DGX Spark model services (Ollama & SGLang router)."""
    return await ModelAdapter.get_available_models()


@router.get("/plugins")
async def list_plugins():
    """List approved plugins and their input schemas."""
    return {"plugins": registry.list_plugins()}


# ------------------------------------------------------------------------------
# Chat & Streaming
# ------------------------------------------------------------------------------
@router.post("/chat/stream")
async def chat_stream(request: ChatRequest, account_id: str = Header(default="acc_sri_owner")):
    """Server-Sent Events streaming chat endpoint with tool detection and action journaling."""
    conv_id = request.conversation_id or f"conv_{uuid.uuid4().hex[:8]}"

    async def event_generator():
        # Store user message
        async with get_db() as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO conversations (id, account_id, title, provider)
                VALUES (?, ?, ?, ?)
                """,
                (conv_id, account_id, request.message[:30], settings.model_provider),
            )
            await db.execute(
                """
                INSERT INTO messages (id, conversation_id, account_id, role, content)
                VALUES (?, ?, ?, 'user', ?)
                """,
                (f"msg_{uuid.uuid4().hex[:8]}", conv_id, account_id, request.message),
            )
            await db.commit()

        # Emit conversation ID
        yield {
            "event": "meta",
            "data": json.dumps({"conversation_id": conv_id, "provider": settings.model_provider}),
        }

        # Check for tool commands (e.g. search, read file, note)
        lower_msg = request.message.lower()
        if "search for" in lower_msg or "look up" in lower_msg or lower_msg.startswith("search"):
            query = request.message.replace("search for", "").replace("search", "").strip()
            # Log action start
            await journal.log_action(
                account_id=account_id,
                tool_name="web_search",
                arguments={"query": query},
                permission_decision="allowed",
                conversation_id=conv_id,
            )
            res = await registry.dispatch("web_search", account_id, {"query": query})
            yield {"event": "tool_call", "data": json.dumps({"tool": "web_search", "result": res})}

        # Stream assistant response
        full_response = []
        messages = [{"role": "user", "content": request.message}]
        async for token in ModelAdapter.stream_chat(messages, provider=request.provider):
            full_response.append(token)
            yield {"event": "token", "data": json.dumps({"token": token})}

        assistant_text = "".join(full_response)

        # Save assistant message
        async with get_db() as db:
            await db.execute(
                """
                INSERT INTO messages (id, conversation_id, account_id, role, content)
                VALUES (?, ?, ?, 'assistant', ?)
                """,
                (f"msg_{uuid.uuid4().hex[:8]}", conv_id, account_id, assistant_text),
            )
            await db.commit()

        # Extract preference if user expressed one (e.g., "I prefer", "always", "remember that")
        if "i prefer" in lower_msg or "remember that" in lower_msg:
            await extractor.extract_and_save_preference(
                account_id=account_id,
                content=request.message,
                evidence=f"From user message in {conv_id}: '{request.message}'",
                origin="inferred",
            )
            yield {"event": "memory_learned", "data": json.dumps({"content": request.message})}

        yield {"event": "done", "data": "[DONE]"}

    return EventSourceResponse(event_generator())


# ------------------------------------------------------------------------------
# Notes
# ------------------------------------------------------------------------------
@router.get("/notes")
async def get_notes(account_id: str = Header(default="acc_sri_owner")):
    plugin = registry.get("personal_notes")
    res = await plugin.execute(account_id=account_id, arguments={"action": "list"})
    return res.get("notes", [])


@router.post("/notes")
async def create_note(note: NoteCreate, account_id: str = Header(default="acc_sri_owner")):
    plugin = registry.get("personal_notes")
    res = await plugin.execute(
        account_id=account_id,
        arguments={"action": "create", "title": note.title, "content": note.content, "tags": note.tags},
    )
    return res


@router.delete("/notes/{note_id}")
async def delete_note(note_id: str, account_id: str = Header(default="acc_sri_owner")):
    plugin = registry.get("personal_notes")
    res = await plugin.execute(account_id=account_id, arguments={"action": "delete", "id": note_id})
    return res


# ------------------------------------------------------------------------------
# Learned Memories
# ------------------------------------------------------------------------------
@router.get("/memories")
async def get_memories(account_id: str = Header(default="acc_sri_owner")):
    return await extractor.get_memories(account_id)


@router.put("/memories/{memory_id}")
async def correct_memory(
    memory_id: str,
    correction: MemoryCorrection,
    account_id: str = Header(default="acc_sri_owner"),
):
    success = await extractor.correct_memory(account_id, memory_id, correction.new_content)
    return {"success": success}


@router.delete("/memories/{memory_id}")
async def delete_memory(memory_id: str, account_id: str = Header(default="acc_sri_owner")):
    success = await extractor.delete_memory(account_id, memory_id)
    return {"success": success}


# ------------------------------------------------------------------------------
# Action Journal
# ------------------------------------------------------------------------------
@router.get("/journal")
async def get_journal_history(
    limit: int = Query(default=30, le=100),
    account_id: str = Header(default="acc_sri_owner"),
):
    return await journal.get_history(account_id=account_id, limit=limit)


@router.get("/journal/stream")
async def stream_journal():
    """SSE real-time stream of all tool actions as they happen."""
    q = journal.subscribe()

    async def event_generator():
        try:
            while True:
                entry = await q.get()
                yield {"event": "action", "data": json.dumps(entry)}
        except asyncio.CancelledError:
            journal.unsubscribe(q)

    return EventSourceResponse(event_generator())
