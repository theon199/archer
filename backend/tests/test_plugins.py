from pathlib import Path

import pytest

from app.plugins.files import ApprovedFilesPlugin
from app.plugins.notes import PersonalNotesPlugin
from app.plugins.web_search import WebSearchPlugin
from app.storage.database import get_db, init_db


@pytest.mark.asyncio
async def test_approved_files_sandboxing(tmp_path: Path):
    # Setup test directory
    safe_file = tmp_path / "hello.txt"
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
    del_res = await plugin.execute(account_id="user_test_1", arguments={"action": "delete", "id": note_id})
    assert del_res["success"] is True


@pytest.mark.asyncio
async def test_web_search():
    plugin = WebSearchPlugin()
    res = await plugin.execute(account_id="acc_sri_owner", arguments={"query": "NVIDIA DGX Spark"})
    assert res["success"] is True
    assert len(res["results"]) > 0
