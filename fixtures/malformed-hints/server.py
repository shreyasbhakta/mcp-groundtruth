"""malformed-hints: a hand-written MCP server over stdio, no SDK.

The SDK server cannot emit malformed annotations, so this one writes its
tools/list bytes by hand. Each tool is malformed in a different way:

  bump      idempotentHint "true" (string), destructiveHint 1 (number)
  peek      readOnlyHint null; idempotentHint true (well-formed)
  dup_peek  readOnlyHint twice in one object
  listy     annotations is a list, not an object

The listing is split over two pages to exercise nextCursor. (An SDK client
would see no tools at all from this server: `listy` fails its validation.)
"""

import contextlib
import json
import os
import sqlite3
import sys
from pathlib import Path

DB = Path(os.environ["MCPGT_STATE_DIR"]) / "state.db"
SCHEMA = '{"type":"object","properties":{"key":{"type":"string"}},"required":["key"]}'

# Raw JSON text, so duplicate keys and odd values reach the wire unchanged.
PAGES = {
    None: (
        '{"tools":['
        '{"name":"bump","inputSchema":%s,"annotations":{"readOnlyHint":false,"idempotentHint":"true","destructiveHint":1}},'
        '{"name":"peek","inputSchema":%s,"annotations":{"readOnlyHint":null,"idempotentHint":true}}'
        '],"nextCursor":"page-2"}' % (SCHEMA, SCHEMA)
    ),
    "page-2": (
        '{"tools":['
        '{"name":"dup_peek","inputSchema":%s,"annotations":{"readOnlyHint":true,"readOnlyHint":true}},'
        '{"name":"listy","inputSchema":%s,"annotations":["readOnly"]}'
        "]}" % (SCHEMA, SCHEMA)
    ),
}


def call(name: str, key: str) -> str:
    with contextlib.closing(sqlite3.connect(DB)) as conn, conn:
        if name == "bump":
            conn.execute(
                "INSERT INTO items (key, value) VALUES (?, 'bumped') "
                "ON CONFLICT(key) DO UPDATE SET value = 'bumped'",
                (key,),
            )
            return "bumped"
        row = conn.execute("SELECT value FROM items WHERE key = ?", (key,)).fetchone()
        return "" if row is None else row[0]


def result(message_id, result_json: str) -> str:
    return '{"jsonrpc":"2.0","id":%s,"result":%s}' % (json.dumps(message_id), result_json)


def error(message_id, code: int, text: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text}})


def handle(msg: dict) -> str | None:
    if "id" not in msg:
        return None  # notification
    method, params, mid = msg.get("method"), msg.get("params") or {}, msg["id"]
    if method == "initialize":
        return result(
            mid,
            json.dumps(
                {
                    "protocolVersion": params.get("protocolVersion", "2025-11-25"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "malformed-hints", "version": "0.1"},
                }
            ),
        )
    if method == "ping":
        return result(mid, "{}")
    if method == "tools/list":
        page = PAGES.get(params.get("cursor"))
        return error(mid, -32602, "bad cursor") if page is None else result(mid, page)
    if method == "tools/call":
        name = params.get("name")
        if name not in ("bump", "peek", "dup_peek", "listy"):
            return error(mid, -32602, f"unknown tool {name!r}")
        text = call(name, params.get("arguments", {}).get("key", ""))
        return result(mid, json.dumps({"content": [{"type": "text", "text": text}], "isError": False}))
    return error(mid, -32601, f"method not found: {method}")


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        reply = handle(json.loads(line))
        if reply is not None:
            sys.stdout.write(reply + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
