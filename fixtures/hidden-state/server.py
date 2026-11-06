"""hidden-state: every tool passes the state-based tests, because the database
never changes. Two tools still change what list_notes returns, through state
kept in server memory, which no state adapter can observe.

- list_notes  readOnlyHint:true   notes, with what memory says about them (the probe)
- get_note    readOnlyHint:true   honest: reads one note
- peek_note   readOnlyHint:true   LIES: marks the note as seen, in memory
- bump        idempotentHint:true LIES: increments an in-memory counter
- pin         idempotentHint:true honest: sets an in-memory flag; a repeat changes nothing
"""

import contextlib
import json
import os
import sqlite3
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

DB = Path(os.environ["MCPGT_STATE_DIR"]) / "state.db"
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
IDEMPOTENT = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

server = MCPServer("hidden-state")
seen: set[int] = set()
memory = {"bumps": 0, "pinned": False}


def notes() -> list[tuple[int, str]]:
    with contextlib.closing(sqlite3.connect(DB)) as conn:
        return conn.execute("SELECT id, text FROM notes ORDER BY id").fetchall()


@server.tool(annotations=ToolAnnotations.model_validate(READ_ONLY))
def list_notes() -> str:
    """Every note, whether it has been seen, and the server's counters."""
    rows = [{"id": i, "text": t, "seen": i in seen} for i, t in notes()]
    # served_at differs on every call; the fixture's [probes].ignore removes it.
    return json.dumps({"notes": rows, **memory, "served_at": time.time()})


@server.tool(annotations=ToolAnnotations.model_validate(READ_ONLY))
def get_note(id: int) -> str:
    """The text of one note."""
    return dict(notes()).get(id, "")


@server.tool(annotations=ToolAnnotations.model_validate(READ_ONLY))
def peek_note(id: int) -> str:
    """The text of one note. Also marks it as seen, which a read-only tool must not do."""
    seen.add(id)
    return dict(notes()).get(id, "")


@server.tool(annotations=ToolAnnotations.model_validate(IDEMPOTENT))
def bump() -> str:
    """Count a visit. Each call adds one, so a repeat is not a no-op."""
    time.sleep(0.2)  # long enough for concurrent attempts to overlap
    memory["bumps"] += 1
    return "ok"


@server.tool(annotations=ToolAnnotations.model_validate(IDEMPOTENT))
def pin() -> str:
    """Pin the list. Pinning twice is the same as pinning once."""
    time.sleep(0.2)
    memory["pinned"] = True
    return "ok"


if __name__ == "__main__":
    server.run("stdio")
