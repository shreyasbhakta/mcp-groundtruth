"""notes: a tiny MCP server used by docs/your-server.md.

Keeps notes in a SQLite database at $NOTES_DB and logs every write in an
activity table. It has one deliberate annotation bug: get_note declares
readOnlyHint: true but counts views, so reading a note changes stored state.

Run it with an interpreter that has the MCP Python SDK (mcpgt's own does):

    NOTES_DB=/tmp/notes.db python server.py
"""

import contextlib
import os
import sqlite3
from datetime import UTC, datetime

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

DB = os.environ["NOTES_DB"]
SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    views INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    note_id INTEGER,
    at TEXT NOT NULL
);
"""

server = MCPServer("notes")


def connect() -> contextlib.closing[sqlite3.Connection]:
    return contextlib.closing(sqlite3.connect(DB))


def log(conn: sqlite3.Connection, action: str, note_id: int | None) -> None:
    conn.execute(
        "INSERT INTO activity (action, note_id, at) VALUES (?, ?, ?)",
        (action, note_id, datetime.now(UTC).isoformat()),
    )


def hints(read_only: bool, destructive: bool, idempotent: bool) -> ToolAnnotations:
    return ToolAnnotations.model_validate(
        {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "idempotentHint": idempotent,
            "openWorldHint": False,
        }
    )


@server.tool(annotations=hints(read_only=False, destructive=False, idempotent=False))
def add_note(title: str, body: str) -> int:
    """Add a note and return its id."""
    with connect() as conn, conn:
        cur = conn.execute(
            "INSERT INTO notes (title, body, created_at) VALUES (?, ?, ?)",
            (title, body, datetime.now(UTC).isoformat()),
        )
        log(conn, "add", cur.lastrowid)
        return int(cur.lastrowid or 0)


@server.tool(annotations=hints(read_only=True, destructive=False, idempotent=True))
def list_notes() -> list[str]:
    """List note titles."""
    with connect() as conn:
        return [row[0] for row in conn.execute("SELECT title FROM notes ORDER BY id")]


@server.tool(annotations=hints(read_only=True, destructive=False, idempotent=True))
def get_note(note_id: int) -> str:
    """Return a note's body."""
    with connect() as conn, conn:
        row = conn.execute("SELECT body FROM notes WHERE id = ?", (note_id,)).fetchone()
        if row is None:
            raise ValueError(f"no note {note_id}")
        conn.execute("UPDATE notes SET views = views + 1 WHERE id = ?", (note_id,))  # the bug
        return str(row[0])


@server.tool(annotations=hints(read_only=False, destructive=True, idempotent=True))
def rename_note(note_id: int, title: str) -> str:
    """Set a note's title."""
    with connect() as conn, conn:
        conn.execute("UPDATE notes SET title = ? WHERE id = ?", (title, note_id))
        log(conn, "rename", note_id)
        return title


@server.tool(annotations=hints(read_only=False, destructive=True, idempotent=True))
def delete_note(note_id: int) -> bool:
    """Delete a note. Deleting a missing note does nothing."""
    with connect() as conn, conn:
        deleted = conn.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount > 0
        log(conn, "delete", note_id)
        return deleted


if __name__ == "__main__":
    with connect() as conn:
        conn.executescript(SCHEMA)
    server.run("stdio")
