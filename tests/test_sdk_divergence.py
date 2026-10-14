"""Pins how the MCP Python SDK client reads tool annotations, versus mcpgt.

docs/methodology.md cites these results. If the `mcp` pin changes and a test
here fails, the methodology section must be revisited, not the test edited.
"""

import importlib.metadata
import json
import sys
import textwrap

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import Tool
from pydantic import ValidationError

from mcpgt.contracts import Hint, parse_listing

SDK_VERSION = importlib.metadata.version("mcp")


def sdk_read(annotations: dict):
    t = Tool.model_validate({"name": "t", "inputSchema": {"type": "object"}, "annotations": annotations})
    return t.annotations


def test_pinned_sdk_version():
    assert SDK_VERSION == "2.3.0", "re-run the divergence study for the new SDK version"


@pytest.mark.parametrize(
    "wire_value, sdk_value",
    [("true", True), ("yes", True), (1, True), ("false", False), ("no", False), (0, False)],
)
def test_sdk_coerces_non_boolean_hints(wire_value, sdk_value):
    assert sdk_read({"idempotentHint": wire_value}).idempotent_hint is sdk_value
    page = '{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"t","annotations":{"idempotentHint":%s}}]}}'
    contract = parse_listing([page % json.dumps(wire_value)]).contracts["t"]
    assert contract.idempotent is Hint.ABSENT


def test_sdk_attribute_access_conflates_null_and_missing():
    explicit_null, missing = sdk_read({"readOnlyHint": None}), sdk_read({})
    assert explicit_null.read_only_hint is None and missing.read_only_hint is None
    # The distinction survives only in pydantic's field-set bookkeeping.
    assert explicit_null.model_fields_set == {"read_only_hint"}
    assert missing.model_fields_set == set()


_RAW_SERVER = textwrap.dedent(
    """
    import sys, json
    tools = sys.argv[1]
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        if msg["method"] == "initialize":
            result = json.dumps({"protocolVersion": msg["params"]["protocolVersion"],
                                 "capabilities": {"tools": {}},
                                 "serverInfo": {"name": "raw", "version": "0"}})
        elif msg["method"] == "tools/list":
            result = '{"tools":%s}' % tools
        else:
            result = "{}"
        sys.stdout.write('{"jsonrpc":"2.0","id":%s,"result":%s}\\n' % (json.dumps(msg["id"]), result))
        sys.stdout.flush()
    """
)


def sdk_list_tools(tools_json: str):
    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-c", _RAW_SERVER, tools_json])
        async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            # Catch inside the session; escaping it wraps the error in an ExceptionGroup.
            try:
                return await session.list_tools()
            except Exception as exc:
                return exc

    return anyio.run(go)


def test_sdk_client_resolves_duplicate_keys_last_wins():
    result = sdk_list_tools(
        '[{"name":"t","inputSchema":{"type":"object"},"annotations":{"readOnlyHint":true,"readOnlyHint":false}}]'
    )
    assert result.tools[0].annotations.read_only_hint is False


def test_one_malformed_tool_loses_the_whole_listing_for_sdk_clients():
    tools = (
        '[{"name":"good","inputSchema":{"type":"object"},"annotations":{"readOnlyHint":true}},'
        '{"name":"bad","inputSchema":{"type":"object"},"annotations":["readOnly"]}]'
    )
    outcome = sdk_list_tools(tools)
    assert isinstance(outcome, ValidationError)
    assert "tools.1.annotations" in str(outcome)
    page = '{"jsonrpc":"2.0","id":1,"result":{"tools":%s}}' % tools
    contracts = parse_listing([page]).contracts
    assert contracts["good"].read_only is Hint.TRUE
    assert contracts["bad"].malformed_hints
