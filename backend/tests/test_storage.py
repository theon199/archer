import pytest

from app.memory.extractor import extractor
from app.storage.database import init_db


@pytest.mark.asyncio
async def test_memory_extraction_and_correction():
    await init_db()

    # 1. Extract and save using initialized owner account
    mem = await extractor.extract_and_save_preference(
        account_id="acc_sri_owner",
        content="Prefers concise Python code with type annotations",
        evidence="From conversation: 'I prefer concise Python code with type hints'",
        memory_type="preference",
    )
    assert mem["id"].startswith("mem_")

    # 2. Retrieve
    mems = await extractor.get_memories("acc_sri_owner")
    found = [m for m in mems if m["id"] == mem["id"]]
    assert len(found) == 1
    assert found[0]["origin"] == "inferred"

    # 3. Explicit correction
    await extractor.correct_memory(
        account_id="acc_sri_owner",
        memory_id=mem["id"],
        new_content="Always use Python 3.12 syntax with strict type annotations",
    )

    updated_mems = await extractor.get_memories("acc_sri_owner")
    updated = [m for m in updated_mems if m["id"] == mem["id"]][0]
    assert updated["content"] == "Always use Python 3.12 syntax with strict type annotations"
    assert updated["origin"] == "corrected"

    # 4. Deletion
    await extractor.delete_memory("acc_sri_owner", mem["id"])
    active_mems = await extractor.get_memories("acc_sri_owner")
    assert not any(m["id"] == mem["id"] for m in active_mems)
