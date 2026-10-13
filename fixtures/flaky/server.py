"""flaky: writes a fresh random token on every call, so identical runs differ."""

import contextlib
import os
import sqlite3
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

DB = Path(os.environ["MCPGT_STATE_DIR"]) / "state.db"

server = MCPServer("flaky")


@server.tool(
    annotations=ToolAnnotations.model_validate(
        {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
    )
)
def refresh(key: str) -> str:
    """Refresh the session token for `key`."""
    token = os.urandom(8).hex()
    with contextlib.closing(sqlite3.connect(DB)) as conn, conn:
        conn.execute("UPDATE sessions SET token = ? WHERE key = ?", (token, key))
    return token


if __name__ == "__main__":
    server.run("stdio")
