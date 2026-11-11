"""Owned preference evidence and user controls, using real isolated SQLite."""

import pytest

from app.memory.extractor import extractor
from app.storage.database import get_db


@pytest.mark.asyncio
async def test_memory_extraction_and_correction():
    async with get_db() as db:
        await db.execute(
            "INSERT INTO conversations (id,account_id,title,provider) VALUES ('test-conv','acc_sri_owner','test','local-ollama')"
        )
        await db.execute(
            "INSERT INTO messages (id,conversation_id,account_id,role,content) VALUES ('source','test-conv','acc_sri_owner','user','I prefer concise answers.')"
        )
        await extractor.enqueue_in_transaction(db, "acc_sri_owner", "source")
        await db.commit()
    assert await extractor.process_one()
    mem = (await extractor.get_memories("acc_sri_owner"))[0]
    assert mem["origin"] == "inferred"
    assert mem["sources"][0]["source_message_id"] == "source"
    await extractor.correct_memory("acc_sri_owner", mem["id"], "detailed", 1)
    updated = (await extractor.get_memories("acc_sri_owner"))[0]
    assert updated["origin"] == "corrected" and updated["value"] == "detailed"
    await extractor.delete_memory("acc_sri_owner", mem["id"], 2)
    assert await extractor.get_memories("acc_sri_owner") == []
    assert await extractor.preferences_for_context("acc_sri_owner") == []
