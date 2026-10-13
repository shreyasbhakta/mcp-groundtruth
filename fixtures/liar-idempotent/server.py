"""liar-idempotent: declares idempotentHint:true but INSERTs a new row per call.

Additive (destructiveHint:false is honest); the idempotence claim is the lie.
"""

import contextlib
import os
import sqlite3
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

DB = Path(os.environ["MCPGT_STATE_DIR"]) / "state.db"

server = MCPServer("liar-idempotent")


@server.tool(
    annotations=ToolAnnotations.model_validate(
        {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
    )
)
def add_item(key: str, value: str) -> str:
    """Store `value` under `key`."""
    with contextlib.closing(sqlite3.connect(DB)) as conn, conn:
        conn.execute("INSERT INTO items (key, value) VALUES (?, ?)", (key, value))
    return "ok"


if __name__ == "__main__":
    server.run("stdio")
