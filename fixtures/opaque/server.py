"""opaque: keeps its real state in a journal file no adapter observes.

`record` appends a journal line per call (so it is NOT idempotent, despite
declaring so), and `peek` declares readOnlyHint:true yet also appends. The
observed SQLite database never changes. With the journal invisible, the
harness must not PASS either tool.
"""

import json
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

JOURNAL = Path(os.environ["MCPGT_STATE_DIR"]) / "journal.jsonl"

server = MCPServer("opaque")


def append(entry: dict) -> None:
    with JOURNAL.open("a") as f:
        f.write(json.dumps(entry) + "\n")


@server.tool(
    annotations=ToolAnnotations.model_validate(
        {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
    )
)
def record(event: str) -> str:
    """Record `event`."""
    append({"event": event})
    return "ok"


@server.tool(
    annotations=ToolAnnotations.model_validate(
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
    )
)
def peek(event: str) -> str:
    """Report whether `event` was recorded."""
    seen = JOURNAL.exists() and any(json.loads(line).get("event") == event for line in JOURNAL.open())
    append({"peeked": event})
    return "yes" if seen else "no"


if __name__ == "__main__":
    server.run("stdio")
