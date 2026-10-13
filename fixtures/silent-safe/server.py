"""silent-safe: declares nothing at all; genuinely idempotent (upsert by name)."""

import contextlib
import os
import sqlite3
from pathlib import Path

from mcp.server.mcpserver import MCPServer

DB = Path(os.environ["MCPGT_STATE_DIR"]) / "state.db"

server = MCPServer("silent-safe")


@server.tool()
def set_flag(name: str, value: str) -> str:
    """Set flag `name` to `value`."""
    with contextlib.closing(sqlite3.connect(DB)) as conn, conn:
        conn.execute(
            "INSERT INTO flags (name, value) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (name, value),
        )
    return "ok"


if __name__ == "__main__":
    server.run("stdio")
