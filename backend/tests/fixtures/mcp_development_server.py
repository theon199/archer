"""Explicitly synthetic development-only MCP server; not a third-party integration."""

import asyncio
import os
import sys
from pathlib import Path

from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict

server = MCPServer("archer-development-fixture")


class Inspection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str
    pid: int
    ambient_secret: str


@server.tool()
async def inspect_value(value: str) -> Inspection:
    """Return synthetic text, test-only PID and absence of ambient credentials."""
    if value == "slow":
        await asyncio.sleep(30)
    if value == "failure":
        raise ValueError("SYNTHETIC_SECRET_DO_NOT_LOG")
    return Inspection(value=value, pid=os.getpid(), ambient_secret=os.getenv("ARCHER_TEST_AMBIENT_SECRET", "absent"))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(str(os.getpid()))
    server.run(transport="stdio")
