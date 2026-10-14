"""examples/notes, the server docs/your-server.md walks through."""

import json
import sys
import time
import tomllib
from pathlib import Path

import pytest

from mcpgt import fixture as fixtures
from mcpgt.cli import main
from mcpgt.sandbox import IsolationUnavailable, isolation

EXAMPLE = Path(__file__).parent.parent / "examples" / "notes"


def isolation_available() -> bool:
    try:
        isolation()
    except IsolationUnavailable:
        return False
    return True


needs_isolation = pytest.mark.skipif(not isolation_available(), reason="no network isolation on this host")


def test_the_completed_example_loads_and_resets(capsys):
    target = fixtures.load(EXAMPLE)
    assert target.is_target and target.source is None
    assert main(["verify-reset", str(EXAMPLE)]) == 0
    assert "reset verified" in capsys.readouterr().out


def test_init_on_the_example_lists_the_tools_the_guide_fills_in(tmp_path, capsys):
    out = tmp_path / "my-notes"
    command = ["init", str(out), "--no-sandbox", "--env", "NOTES_DB={state}/notes.db", "--", sys.executable]
    assert main([*command, str(EXAMPLE / "server.py")]) == 0
    assert "5 tools, 12 TODO(mcpgt) placeholders" in capsys.readouterr().out
    generated = tomllib.loads((out / "target.toml").read_text())
    completed = tomllib.loads((EXAMPLE / "target.toml").read_text())
    assert [t["name"] for t in generated["tools"]] == [t["name"] for t in completed["tools"]]
    for gen, done in zip(generated["tools"], completed["tools"], strict=True):
        assert set(gen["arguments"]) == set(done["arguments"]), gen["name"]
    assert generated["target"]["env"] == completed["target"]["env"]


@needs_isolation
def test_audit_finds_exactly_the_documented_violation(tmp_path, capsys):
    started = time.monotonic()
    assert main(["run", str(EXAMPLE), "--audit", "--runs-dir", str(tmp_path)]) == 0
    assert time.monotonic() - started < 300
    (report_dir,) = tmp_path.iterdir()
    report = json.loads((report_dir / "report.json").read_text())
    outcomes = {(t["tool"], v["test"]): v["outcome"] for t in report["tools"] for v in t["verdicts"]}
    flagged = {k: o for k, o in outcomes.items() if o in ("VIOLATION", "INCONCLUSIVE")}
    assert flagged == {("get_note", "read_only"): "VIOLATION"}
    (get_note,) = [
        v for t in report["tools"] if t["tool"] == "get_note" for v in t["verdicts"] if v["test"] == "read_only"
    ]
    (delta,) = get_note["observed"]["runs"][0]["deltas"][0]["by_category"]["business"]
    assert delta["region"] == "notes"
    assert [c[1] for c in delta["removed"][0]][-1] == 0 and [c[1] for c in delta["added"][0]][-1] == 1  # views
    for passed in [("rename_note", "concurrent_idempotence"), ("delete_note", "sequential_idempotence")]:
        assert outcomes[passed] == "PASS"
