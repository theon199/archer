"""Local owner provisioning and opaque, revocable bearer sessions.

Browser clients must keep tokens in memory, not URLs or persistent web storage.
No cookie authentication is accepted, so ambient browser credentials cannot write.
"""

import asyncio
import getpass
import hashlib
import secrets
import time
import uuid

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.storage.database import get_db, init_db

router = APIRouter()
hasher = PasswordHasher()
# Equal-cost verification for missing users. This hash contains no account credential.
_dummy_hash = hasher.hash(secrets.token_urlsafe(32))


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def current_account(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Authentication required")
    async with get_db() as db:
        row = await (
            await db.execute(
                "SELECT account_id FROM sessions WHERE token = ? AND expires_at > ?",
                (token_digest(authorization[7:]), int(time.time())),
            )
        ).fetchone()
    if row is None:
        raise HTTPException(401, "Invalid or expired session")
    return row["account_id"]


@router.post("/login")
async def login(request: Login):
    # Durable global admission limit is appropriate for this single-owner service.
    now = int(time.time())
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (now - 300,))
        count = await (await db.execute("SELECT COUNT(*) FROM login_attempts")).fetchone()
        if count[0] >= 10:
            raise HTTPException(429, "Login rate limited; retry in five minutes")
        await db.execute("INSERT INTO login_attempts (attempted_at) VALUES (?)", (now,))
        row = await (
            await db.execute("SELECT id, password_hash FROM accounts WHERE username = ?", (request.username,))
        ).fetchone()
        await db.commit()
    try:
        await asyncio.to_thread(hasher.verify, row["password_hash"] if row else _dummy_hash, request.password)
    except (VerificationError, InvalidHashError):
        raise HTTPException(401, "Invalid credentials") from None
    if row is None:
        raise HTTPException(401, "Invalid credentials")
    token = secrets.token_urlsafe(32)
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        # A password reset may commit while Argon2 verification runs off-thread.
        # Never resurrect a revoked credential with a newly minted session.
        current = await (await db.execute("SELECT password_hash FROM accounts WHERE id = ?", (row["id"],))).fetchone()
        if current is None or current["password_hash"] != row["password_hash"]:
            raise HTTPException(401, "Credentials changed; sign in again")
        await db.execute(
            "INSERT INTO sessions (id, account_id, token, expires_at) VALUES (?, ?, ?, ?)",
            (uuid.uuid4().hex, row["id"], token_digest(token), now + 43200),
        )
        await db.commit()
    return {"access_token": token, "token_type": "bearer", "expires_in": 43200}


@router.post("/logout")
async def logout(account_id: str = Depends(current_account), authorization: str = Header()):
    async with get_db() as db:
        await db.execute(
            "DELETE FROM sessions WHERE token = ? AND account_id = ?", (token_digest(authorization[7:]), account_id)
        )
        await db.commit()
    return {"success": True}


async def provision_owner(username: str, password: str) -> None:
    if not 1 <= len(username) <= 128 or not 12 <= len(password) <= 1024:
        raise ValueError("Username must be 1–128 characters; password must be 12–1024 characters")
    password_hash = await asyncio.to_thread(hasher.hash, password)
    await init_db()
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        # The scaffold's known bypass is never a valid provisioned owner.
        await db.execute(
            "DELETE FROM sessions WHERE account_id IN (SELECT id FROM accounts WHERE password_hash = 'local_dev_bypass')"
        )
        await db.execute("UPDATE accounts SET is_owner = 0 WHERE password_hash = 'local_dev_bypass'")
        row = await (await db.execute("SELECT id FROM accounts WHERE is_owner = 1")).fetchone()
        if row:
            raise ValueError("Owner already provisioned")
        owner_id = f"acc_{uuid.uuid4().hex}"
        await db.execute(
            "INSERT INTO accounts (id, username, password_hash, is_owner) VALUES (?, ?, ?, 1)",
            (owner_id, username, password_hash),
        )
        from app.plugins.registry import apply_owner_default_grants

        await apply_owner_default_grants(db, owner_id)
        await db.commit()


if __name__ == "__main__":
    owner_username = input("Owner username (use a new name if a legacy account exists): ")
    owner_password = getpass.getpass("Owner password (at least 12 characters): ")
    if owner_password != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords differ")
    asyncio.run(provision_owner(owner_username, owner_password))
    print("Owner provisioned. Legacy data remains preserved under its original account.")
