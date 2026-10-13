"""honest-readonly: declares readOnlyHint:true and genuinely only reads."""

import contextlib
import os
import sqlite3
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

DB = Path(os.environ["MCPGT_STATE_DIR"]) / "state.db"

server = MCPServer("honest-readonly")


@server.tool(
    annotations=ToolAnnotations.model_validate(
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
    )
)
def get_item(key: str) -> str:
    """Return the value stored under `key`, or an empty string."""
    with contextlib.closing(sqlite3.connect(DB)) as conn:
        row = conn.execute("SELECT value FROM items WHERE key = ?", (key,)).fetchone()
    return "" if row is None else row[0]


if __name__ == "__main__":
    server.run("stdio")
