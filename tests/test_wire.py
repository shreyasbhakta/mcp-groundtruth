import asyncio
import os
import sys

import pytest

from mcpgt.wire import WireClient, WireError

ENV = {"PATH": os.environ["PATH"]}


def run(coro):
    return asyncio.run(coro)


async def start(script, timeout=5.0):
    return await WireClient.start([sys.executable, "-c", script], env=ENV, timeout=timeout)


ECHO_SERVER = r"""
import sys, json
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg["method"] == "tools/list":
        cursor = (msg.get("params") or {}).get("cursor")
        page = {None: '{"tools":[{"name":"a"}],"nextCursor":"p2"}', "p2": '{"tools":[{"name":"b"}]}'}[cursor]
        sys.stdout.write('{"jsonrpc":"2.0","id":%d,"result":%s}\n' % (msg["id"], page))
    elif msg["method"] == "ask":
        # A server-to-client ping before answering.
        sys.stdout.write('{"jsonrpc":"2.0","id":"srv-1","method":"ping"}\n'); sys.stdout.flush()
        reply = json.loads(sys.stdin.readline())
        sys.stdout.write('{"jsonrpc":"2.0","id":%d,"result":{"pong":%s}}\n' % (msg["id"], json.dumps(reply)))
    else:
        sys.stdout.write('{"jsonrpc":"2.0","id":%d,"result":{ "spaced" : true }}\n' % msg["id"])
    sys.stdout.flush()
"""


def test_responses_are_kept_verbatim():
    async def go():
        async with await start(ECHO_SERVER) as client:
            return await client.request("anything")

    response = run(go())
    assert response.text == '{"jsonrpc":"2.0","id":1,"result":{ "spaced" : true }}'
    assert response.result == {"spaced": True}


def test_list_tools_follows_cursors():
    async def go():
        async with await start(ECHO_SERVER) as client:
            return await client.list_tools()

    pages = run(go())
    assert [p.result["tools"][0]["name"] for p in pages] == ["a", "b"]


def test_concurrent_requests_are_routed_by_id():
    async def go():
        async with await start(ECHO_SERVER) as client:
            return await asyncio.gather(*(client.request("x") for _ in range(5)))

    assert sorted(r.message["id"] for r in run(go())) == [1, 2, 3, 4, 5]


def test_server_ping_is_answered():
    async def go():
        async with await start(ECHO_SERVER) as client:
            return await client.request("ask")

    assert run(go()).result["pong"] == {"jsonrpc": "2.0", "id": "srv-1", "result": {}}


def test_server_exit_is_a_wire_error():
    async def go():
        async with await start("import sys; sys.stderr.write('boom'); sys.exit(3)") as client:
            await client.request("x")

    with pytest.raises(WireError, match="boom"):
        run(go())


def test_garbage_output_is_a_wire_error():
    async def go():
        async with await start("print('hello, not json', flush=True); import time; time.sleep(5)") as client:
            await asyncio.sleep(0.5)  # let the garbage arrive before the request
            await client.request("x")

    with pytest.raises(WireError, match="unreadable"):
        run(go())


def test_silence_times_out():
    async def go():
        async with await start("import time; time.sleep(5)", timeout=0.3) as client:
            await client.request("x")

    with pytest.raises(WireError, match="no response"):
        run(go())


def test_non_terminating_pagination_is_detected():
    loop_server = r"""
import sys, json
for line in sys.stdin:
    msg = json.loads(line)
    sys.stdout.write('{"jsonrpc":"2.0","id":%d,"result":{"tools":[],"nextCursor":"same"}}\n' % msg["id"])
    sys.stdout.flush()
"""

    async def go():
        async with await start(loop_server) as client:
            await client.list_tools()

    with pytest.raises(WireError, match="does not terminate"):
        run(go())
