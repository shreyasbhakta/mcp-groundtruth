"""honest-idempotent: upsert by key, truly idempotent over business state.

Declares readOnlyHint:false, destructiveHint:true (an upsert can overwrite),
idempotentHint:true, openWorldHint:false. Every call appends one audit row
with a timestamp and a random request id, so append-only state changes on
every call while business state converges.
"""

import contextlib
import os
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

DB = Path(os.environ["MCPGT_STATE_DIR"]) / "state.db"

server = MCPServer("honest-idempotent")


@server.tool(
    annotations=ToolAnnotations.model_validate(
        {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False}
    )
)
def upsert_item(key: str, value: str) -> str:
    """Set item `key` to `value`, creating it if needed."""
    with contextlib.closing(sqlite3.connect(DB)) as conn, conn:
        conn.execute(
            "INSERT INTO items (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.execute(
            "INSERT INTO audit (action, key, at, request_id) VALUES ('upsert', ?, ?, ?)",
            (key, datetime.now(UTC).isoformat(), str(uuid.uuid4())),
        )
    return f"{key}={value}"


if __name__ == "__main__":
    server.run("stdio")
