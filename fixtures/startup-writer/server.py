"""startup-writer: appends one boot-log row every time the server starts.

Both tools are honest. `read_setting` only reads; `set_setting` upserts by
key. The startup write is absorbed by the control session for the read-only
test, but it makes two-process concurrent attempts incomparable: two
instances write two boot rows.
"""

import contextlib
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

DB = Path(os.environ["MCPGT_STATE_DIR"]) / "state.db"

server = MCPServer("startup-writer")


@server.tool(
    annotations=ToolAnnotations.model_validate(
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
    )
)
def read_setting(key: str) -> str:
    """Return setting `key`, or an empty string."""
    with contextlib.closing(sqlite3.connect(DB)) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return "" if row is None else row[0]


@server.tool(
    annotations=ToolAnnotations.model_validate(
        {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False}
    )
)
def set_setting(key: str, value: str) -> str:
    """Set setting `key` to `value`."""
    with contextlib.closing(sqlite3.connect(DB)) as conn, conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    return "ok"


def record_boot() -> None:
    with contextlib.closing(sqlite3.connect(DB)) as conn, conn:
        conn.execute("INSERT INTO boot_log (at) VALUES (?)", (datetime.now(UTC).isoformat(),))


if __name__ == "__main__":
    record_boot()
    server.run("stdio")
