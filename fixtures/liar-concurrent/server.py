"""liar-concurrent: declares idempotentHint:true and dedupes by check-then-insert.

There is no uniqueness constraint, and the check and the insert are separate
transactions with an await in between. Sequential calls see the earlier row
and skip the insert; concurrent calls can both pass the check.
"""

import contextlib
import os
import sqlite3
from pathlib import Path

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

DB = Path(os.environ["MCPGT_STATE_DIR"]) / "state.db"

server = MCPServer("liar-concurrent")


@server.tool(
    annotations=ToolAnnotations.model_validate(
        {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
    )
)
async def register(key: str, value: str) -> str:
    """Register `key` with `value` unless it is already registered."""
    with contextlib.closing(sqlite3.connect(DB)) as conn:
        exists = conn.execute("SELECT 1 FROM registrations WHERE key = ?", (key,)).fetchone()
    if exists:
        return "already registered"
    await anyio.sleep(0.2)  # e.g. validating against another service
    with contextlib.closing(sqlite3.connect(DB)) as conn, conn:
        conn.execute("INSERT INTO registrations (key, value) VALUES (?, ?)", (key, value))
    return "registered"


if __name__ == "__main__":
    server.run("stdio")
