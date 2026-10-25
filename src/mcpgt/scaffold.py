"""``mcpgt init``: a starting target.toml for someone else's server.

The server is started once, sandboxed like an audit, and asked for its tools.
What it declares is written down; what only the author knows is not guessed.
State locations, adapters, classification and masks are written as
``TODO(mcpgt)`` placeholders, and the target refuses to load while any remain
(see ``fixture.unanswered_todos``). Tool arguments get one placeholder per
property of the tool's input schema: required properties as values to fill
in, optional ones as comments.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcpgt import __version__
from mcpgt.contracts import Annotation, DeclaredContract, Hint, parse_listing
from mcpgt.fixture import TODO_MARKER
from mcpgt.provenance import now_utc
from mcpgt.runner import server_env
from mcpgt.sandbox import isolated, isolation
from mcpgt.wire import PROTOCOL_VERSION, WireClient

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_PLACEHOLDER = re.compile(r"\{(python|state|target)\}")
_BUSINESS = 'regions holding user data, e.g. ["notes"], or []'
_APPEND_ONLY = 'regions that only grow, such as logs, e.g. ["activity_log"], or []'
_MASK = '[{ region = "notes", column = "created_at", kind = "timestamp" }], or []'


class InitError(RuntimeError):
    """The server could not be started or listed, or the directory is in use."""


@dataclass(frozen=True, slots=True)
class Discovered:
    server_info: Mapping[str, Any]
    protocol_version: str | None
    tools: tuple[Mapping[str, Any], ...]  # raw tool objects, in listing order
    contracts: Mapping[str, DeclaredContract]
    isolation: str | None  # sandbox mechanism used, or None


def todo(text: str) -> str:
    return f"{TODO_MARKER}: {text}"


def portable_command(command: Sequence[str], target_dir: Path, cwd: Path | None = None) -> list[str]:
    """The command as written to target.toml.

    Audits run with the state directory as working directory, so arguments
    naming existing files are rewritten: relative to ``{target}`` when the
    file and the target directory share a project directory, absolute
    otherwise. mcpgt's own interpreter becomes ``{python}``. Everything else
    is kept verbatim.
    """
    cwd = Path.cwd() if cwd is None else cwd
    target = target_dir.resolve()
    out = []
    for part in command:
        path = cwd / part
        if not part or part.startswith("-") or _PLACEHOLDER.search(part) or not path.exists():
            out.append(part)
            continue
        if os.path.abspath(path) == os.path.abspath(sys.executable):
            out.append("{python}")
            continue
        # Resolve the directory but not the file itself: a virtualenv's python
        # is a symlink, and following it would lose the environment.
        file = path.parent.resolve() / path.name
        common = Path(os.path.commonpath([file, target]))
        if common in (Path(common.anchor), Path.home().resolve()):
            out.append(str(file))  # nothing shared but the filesystem root or home directory
            continue
        rel = Path(os.path.relpath(file, target)).as_posix()
        out.append("{target}" if rel == "." else "{target}/" + rel)
    return out


def _expand(part: str, state_dir: Path, target_dir: Path) -> str:
    values = {"python": sys.executable, "state": str(state_dir.resolve()), "target": str(target_dir.resolve())}
    expanded = _PLACEHOLDER.sub(lambda m: values[m.group(1)], part)
    # The target directory may not exist yet, so "{target}/../x" is normalized
    # lexically rather than left for the OS to traverse.
    return os.path.normpath(expanded) if part.startswith("{target}") else expanded


async def discover(
    command: Sequence[str],
    target_dir: Path,
    env: Mapping[str, str],
    sandbox: bool = True,
    timeout: float = 30.0,
) -> Discovered:
    """Start the server in an empty state directory and list its tools."""
    with tempfile.TemporaryDirectory(prefix="mcpgt-init-") as tmp:
        state_dir = Path(tmp) / "state"
        state_dir.mkdir()
        argv = [_expand(p, state_dir, target_dir) for p in command]
        full_env = {**server_env(state_dir), **{k: _expand(v, state_dir, target_dir) for k, v in env.items()}}
        mechanism = None
        if sandbox:
            mechanism = isolation().mechanism
            argv = isolated(argv, state_dir)
        try:
            client = await WireClient.start(argv, env=full_env, cwd=state_dir, timeout=timeout)
        except OSError as exc:
            raise InitError(f"could not start the server: {exc}") from exc
        try:
            init = await client.initialize()
            pages = await client.list_tools()
        except Exception as exc:
            stderr = client.stderr.strip()
            detail = f"\nserver stderr:\n{stderr}" if stderr and stderr not in str(exc) else ""
            raise InitError(f"{exc}{detail}") from exc
        finally:
            await client.close()
    texts = [p.text for p in pages]
    listing = parse_listing(texts)
    tools: list[Mapping[str, Any]] = []
    for page in pages:
        result = page.result or {}
        tools.extend(t for t in result.get("tools", []) if isinstance(t, Mapping))
    result = init.result or {}
    info = result.get("serverInfo")
    return Discovered(
        server_info=info if isinstance(info, Mapping) else {},
        protocol_version=result.get("protocolVersion") if isinstance(result.get("protocolVersion"), str) else None,
        tools=tuple(tools),
        contracts=listing.contracts,
        isolation=mechanism,
    )


# --- Rendering -------------------------------------------------------------------


def _s(value: str) -> str:
    """A TOML basic string. JSON string escapes are valid TOML, except a raw DEL."""
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _key(name: str) -> str:
    return name if _BARE_KEY.match(name) else _s(name)


def _comment(text: Any, limit: int = 100) -> str:
    one_line = _CONTROL.sub(" ", str(text)).strip()
    return one_line if len(one_line) <= limit else one_line[: limit - 3].rstrip() + "..."


def _hints(contract: DeclaredContract) -> str:
    malformed = contract.malformed_hints
    parts = []
    for annotation in Annotation:
        hint = contract.hint(annotation)
        value = "malformed" if annotation in malformed else ("(absent)" if hint is Hint.ABSENT else hint.value)
        parts.append(f"{annotation.wire_name}={value}")
    return " ".join(parts)


def _schema_type(schema: Mapping[str, Any]) -> str:
    kind = schema.get("type")
    items = schema.get("items")
    if kind == "array" and isinstance(items, Mapping):
        return f"array of {_schema_type(items)}"
    if isinstance(kind, str):
        return kind
    if isinstance(kind, list):
        return "|".join(str(k) for k in kind)
    for combinator in ("anyOf", "oneOf"):
        options = schema.get(combinator)
        if isinstance(options, list):
            return "|".join(_schema_type(o) for o in options if isinstance(o, Mapping)) or "value"
    if "$ref" in schema or "properties" in schema:
        return "object"
    return "value"


def placeholder(name: str, schema: Any, required: bool) -> str:
    """A TODO describing one input-schema property, for the author to replace."""
    schema = schema if isinstance(schema, Mapping) else {}
    parts = [_schema_type(schema), "required" if required else "optional"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        parts.append("one of " + ", ".join(json.dumps(v, ensure_ascii=False) for v in enum))
    if "default" in schema:
        parts.append("default " + json.dumps(schema["default"], ensure_ascii=False))
    text = ", ".join(parts)
    description = schema.get("description")
    if isinstance(description, str) and description.strip():
        text += ". " + _comment(description, 80)
    return todo(text)


def _arguments(tool: Mapping[str, Any]) -> list[str]:
    schema = tool.get("inputSchema")
    schema = schema if isinstance(schema, Mapping) else {}
    properties = schema.get("properties")
    required = schema.get("required")
    required = {r for r in required if isinstance(r, str)} if isinstance(required, list) else set()
    if not isinstance(properties, Mapping):
        if required or schema.get("additionalProperties") not in (None, False):
            return [f"arguments = {_s(todo('the input schema lists no properties; write the arguments as a table'))}"]
        return ["arguments = {}"]
    if not properties:
        return ["arguments = {}"]
    lines = ["", "[tools.arguments]"]
    for name, prop in properties.items():
        line = f"{_key(name)} = {_s(placeholder(name, prop, name in required))}"
        lines.append(line if name in required else "# " + line)
    return lines


def render(
    name: str,
    command: Sequence[str],
    env: Mapping[str, str],
    found: Discovered,
    original_command: Sequence[str],
) -> str:
    info = found.server_info
    server = " ".join(_comment(info.get(k, ""), 60) for k in ("name", "version") if info.get(k)) or "unnamed server"
    out = [
        f"# Generated by mcpgt {__version__} init on {now_utc()} from:",
        f"#   {_comment(' '.join(original_command), 200)}",
        f"# Server: {server}; protocol {found.protocol_version or 'unknown'} "
        f"(client sent {PROTOCOL_VERSION}); {len(found.tools)} tools.",
        "#",
        f'# Every value starting with "{TODO_MARKER}" is a decision only you can',
        "# make. The target refuses to load until none are left.",
        "# Guide: https://github.com/shreyasbhakta/mcp-groundtruth/blob/main/docs/your-server.md",
        "",
        "[target]",
        f"name = {_s(name)}",
        f"description = {_s(server)}",
        "# Audits run with the state directory as working directory. {target} is",
        "# this directory, {state} the state directory, {python} mcpgt's Python.",
        "command = [" + ", ".join(_s(p) for p in command) + "]",
        "# The server runs with a minimal environment (PATH, HOME). Configure it",
        "# here, or in the command, to keep all of its state inside {state}.",
        "env = {" + ", ".join(f"{_key(k)} = {_s(v)}" for k, v in env.items()) + "}" if env else "env = {}",
        '# node = "24"  # require a Node major version on PATH',
        "",
        "[state]",
        '# Every place the server keeps state, as "<kind>:<label>": "sqlite:app.db",',
        '# "json:data.json", "fs:uploads", or "fs:." for the whole state directory.',
        "# Labels are paths inside {state}. A location no adapter observes makes",
        "# coverage partial, and verdicts that depend on it INCONCLUSIVE.",
        f"locations = [{_s(todo('every state location, e.g. sqlite:app.db'))}]",
        "",
        "# One [[adapters]] block per location; copy this one as needed.",
        "[[adapters]]",
        f"kind = {_s(todo('sqlite, fs or json'))}",
        f"label = {_s(todo('path inside the state directory, matching a location'))}",
        '# seed = "seed/app.sql"  # the state every session starts from: a SQL script',
        "#                         # (sqlite), a directory (fs) or a file (json)",
        "# Regions are tables (sqlite), files (fs) or partitions (json). A region",
        "# listed in neither business nor append_only is unclassified: changes to",
        "# it make idempotence and destructive verdicts INCONCLUSIVE.",
        f"business = {_s(todo(_BUSINESS))}",
        f"append_only = {_s(todo(_APPEND_ONLY))}",
        "# Columns whose values differ on every run (timestamps, UUIDs, ids), masked",
        "# before comparison. Kinds: timestamp, uuid, autoincrement, random_token.",
        f"mask = {_s(todo(_MASK))}",
        "",
        "# Optional: also test what each tool tries to reach on the network.",
        "# Guide: https://github.com/shreyasbhakta/mcp-groundtruth/blob/main/docs/network.md",
        "# [network]",
        '# policy = "deny"    # deny | allowlist (with allow = ["host:port"]) | permitted',
        "# capture = true     # a local endpoint that records requests and forwards nothing (macOS)",
        "",
        "# --- Tools --------------------------------------------------------------",
        "# One [[tools]] entry per argument set. Choose arguments that do real work",
        "# against the seeded state. Delete an entry to leave a tool untested; copy",
        '# one and add variant = "<name>" to test another argument set.',
    ]
    for tool in found.tools:
        tool_name = str(tool.get("name"))
        contract = found.contracts.get(tool_name)
        out.append("")
        description = tool.get("description")
        out.append(f"# {_comment(tool_name, 60)}" + (f": {_comment(description)}" if description else ""))
        if contract is not None:
            out.append(f"#   declared: {_hints(contract)}")
            for anomaly in contract.anomalies:
                out.append(f"#   anomaly: {anomaly.kind.value} at {anomaly.location} = {_comment(anomaly.raw, 60)}")
        out.append("[[tools]]")
        out.append(f"name = {_s(tool_name)}")
        out.extend(_arguments(tool))
    return "\n".join(out) + "\n"


def write(target_dir: Path, text: str, force: bool = False) -> Path:
    path = target_dir / "target.toml"
    if (target_dir / "fixture.toml").exists():
        raise InitError(f"{target_dir} already holds a fixture")
    if path.exists() and not force:
        raise InitError(f"{path} already exists (use --force to overwrite)")
    target_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path
