"""mcpgt init: a starting target.toml that refuses to load until completed."""

import sys
import tomllib
from pathlib import Path

import pytest

from mcpgt import fixture as fixtures
from mcpgt import scaffold
from mcpgt.cli import main
from mcpgt.contracts import parse_tool
from mcpgt.sandbox import IsolationUnavailable, isolation


def isolation_available() -> bool:
    try:
        isolation()
    except IsolationUnavailable:
        return False
    return True


needs_isolation = pytest.mark.skipif(not isolation_available(), reason="no network isolation on this host")

FIXTURES = Path(__file__).parent.parent / "fixtures"
SERVER = FIXTURES / "honest-idempotent" / "server.py"

TOOLS = (
    {
        "name": "add_note",
        "description": "Add a note.\nSecond line.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "The title"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "priority": {"enum": ["low", "high"], "default": "low"},
            },
            "required": ["title"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {"name": "list_notes", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}},
    {"name": "odd name", "inputSchema": {"type": "object", "properties": {"a b": {"type": ["string", "null"]}}}},
    {"name": "scribble", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": "yes"}},
)


def discovered(tools=TOOLS):
    return scaffold.Discovered(
        server_info={"name": "notes", "version": "1.2.3"},
        protocol_version="2025-11-25",
        tools=tools,
        contracts={t["name"]: parse_tool(t) for t in tools},
        isolation=None,
    )


def rendered(tmp_path, env=None):
    text = scaffold.render("notes", ["{python}", "{target}/server.py"], env or {}, discovered(), ["python", "x"])
    return scaffold.write(tmp_path / "t", text), text


def test_render_lists_every_tool_with_declared_hints_as_comments(tmp_path):
    _, text = rendered(tmp_path)
    data = tomllib.loads(text)
    assert [t["name"] for t in data["tools"]] == ["add_note", "list_notes", "odd name", "scribble"]
    hints = "readOnlyHint=false destructiveHint=false idempotentHint=(absent) openWorldHint=(absent)"
    assert f"#   declared: {hints}" in text
    assert "#   declared: readOnlyHint=malformed" in text  # never shown as false
    assert "#   anomaly: non_boolean_hint at annotations.readOnlyHint" in text
    assert "# add_note: Add a note. Second line." in text
    assert data["target"]["command"] == ["{python}", "{target}/server.py"]


def test_arguments_come_from_the_input_schema(tmp_path):
    _, text = rendered(tmp_path)
    data = tomllib.loads(text)
    add = data["tools"][0]["arguments"]
    assert add == {"title": "TODO(mcpgt): string, required. The title"}  # only required properties are live
    assert '# tags = "TODO(mcpgt): array of string, optional"' in text
    assert '# priority = "TODO(mcpgt): value, optional, one of \\"low\\", \\"high\\", default \\"low\\""' in text
    assert data["tools"][1]["arguments"] == {}
    assert '# "a b" = "TODO(mcpgt): string|null, optional"' in text


def test_state_adapters_classification_and_masks_are_never_guessed(tmp_path):
    _, text = rendered(tmp_path)
    data = tomllib.loads(text)
    (adapter,) = data["adapters"]
    for key in ("kind", "label", "business", "append_only", "mask"):
        assert adapter[key].startswith(fixtures.TODO_MARKER), key
    assert data["state"]["locations"][0].startswith(fixtures.TODO_MARKER)
    assert "seed" not in adapter


def test_unanswered_todos_make_the_target_fail_to_load(tmp_path):
    path, _ = rendered(tmp_path)
    with pytest.raises(fixtures.FixtureError) as err:
        fixtures.load(path.parent)
    message = str(err.value)
    assert "7 unanswered TODO(mcpgt) placeholders" in message
    assert "state.locations[0]: TODO(mcpgt)" in message
    assert "adapters[0].mask: TODO(mcpgt)" in message
    assert "tools[0](add_note).arguments.title: TODO(mcpgt): string, required" in message


def test_a_completed_target_loads(tmp_path):
    path, text = rendered(tmp_path)
    answers = {
        '["TODO(mcpgt): every state location, e.g. sqlite:app.db"]': '["sqlite:notes.db"]',
        '"TODO(mcpgt): sqlite, fs or json"': '"sqlite"',
        '"TODO(mcpgt): path inside the state directory, matching a location"': '"notes.db"',
        '"TODO(mcpgt): string, required. The title"': '"Groceries"',
    }
    for old, new in answers.items():
        assert old in text
        text = text.replace(old, new)
    lines = []
    for line in text.splitlines():
        key = line.split(" = ", 1)[0]
        lines.append(f"{key} = []" if key in ("business", "append_only", "mask") else line)
    path.write_text("\n".join(lines) + "\n")
    target = fixtures.load(path.parent)
    assert target.is_target and [t.name for t in target.tools] == ["add_note", "list_notes", "odd name", "scribble"]
    assert target.tools[0].arguments == {"title": "Groceries"}


def test_todo_marker_is_specific(tmp_path):
    # A real argument may well start with "TODO"; only the marker blocks loading.
    assert fixtures.unanswered_todos({"tools": [{"name": "x", "arguments": {"t": "TODO buy milk"}}]}) == []


def test_portable_command_rewrites_file_arguments(tmp_path):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "server.py").touch()
    (project / "my-target").mkdir()
    (project / "my-target" / "local.py").touch()
    command = ["python", "src/server.py", "my-target/local.py", "--db", "{state}/app.db", "-v", "/usr/bin/env"]
    assert scaffold.portable_command(command, project / "my-target", cwd=project) == [
        "python",  # not a file here: left to PATH
        "{target}/../src/server.py",
        "{target}/local.py",
        "--db",
        "{state}/app.db",
        "-v",
        "/usr/bin/env",  # shares only the filesystem root: kept absolute
    ]
    assert scaffold.portable_command([sys.executable], project) == ["{python}"]


def test_target_and_state_placeholders_expand_in_the_command(tmp_path):
    path, _ = rendered(tmp_path)
    fx = fixtures.Fixture(
        name="t",
        root=path.parent,
        description="",
        command=("{target}/server.py", "--db", "{state}/app.db"),
        locations=(),
        adapters=(),
        tools=(),
        is_target=True,
    )
    state = tmp_path / "state"
    assert fx.server_command(None, state) == [f"{path.parent}/server.py", "--db", f"{state.resolve()}/app.db"]


def test_cli_init_writes_a_target_from_a_running_server(tmp_path, capsys):
    out = tmp_path / "upsert"
    assert main(["init", str(out), "--no-sandbox", "--env", "LOG=1", "--", sys.executable, str(SERVER)]) == 0
    printed = capsys.readouterr().out
    assert "1 tool, 8 TODO(mcpgt) placeholders" in printed
    data = tomllib.loads((out / "target.toml").read_text())
    assert data["target"]["name"] == "upsert"
    assert data["target"]["env"] == {"LOG": "1"}
    assert data["target"]["command"][0] == "{python}"
    assert data["target"]["command"][1].startswith("/")  # the fixture shares no project dir with tmp_path
    (tool,) = data["tools"]
    assert tool["name"] == "upsert_item"
    assert tool["arguments"] == {"key": "TODO(mcpgt): string, required", "value": "TODO(mcpgt): string, required"}
    text = (out / "target.toml").read_text()
    assert "declared: readOnlyHint=false destructiveHint=true idempotentHint=true openWorldHint=false" in text
    assert "# Server: honest-idempotent" in text


@needs_isolation
def test_cli_init_runs_the_server_in_the_audit_sandbox(tmp_path, capsys):
    assert main(["init", str(tmp_path / "t"), "--", sys.executable, str(SERVER)]) == 0
    assert "(sandbox: " in capsys.readouterr().out


def test_cli_init_refuses_to_overwrite_and_needs_a_command(tmp_path, capsys):
    out = tmp_path / "t"
    assert main(["init", str(out), "--no-sandbox", "--", sys.executable, str(SERVER)]) == 0
    assert main(["init", str(out), "--no-sandbox", "--", sys.executable, str(SERVER)]) == 2
    assert "already exists" in capsys.readouterr().err
    assert main(["init", str(out), "--no-sandbox", "--force", "--", sys.executable, str(SERVER)]) == 0
    assert main(["init", str(tmp_path / "u")]) == 2
    assert "give the server command after --" in capsys.readouterr().err
    assert main(["init", str(tmp_path / "u"), "--env", "NOEQUALS", "--", "x"]) == 2


def test_cli_init_reports_a_server_that_fails_to_start(tmp_path, capsys):
    crash = [sys.executable, "-c", "import sys; sys.stderr.write('no database configured'); sys.exit(3)"]
    assert main(["init", str(tmp_path / "t"), "--no-sandbox", "--", *crash]) == 2
    err = capsys.readouterr().err
    assert "no database configured" in err
    assert not (tmp_path / "t" / "target.toml").exists()
