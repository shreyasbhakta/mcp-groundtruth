"""A full run of honest-idempotent through runner, verdicts and report."""

import asyncio
import json
from pathlib import Path

import pytest

from mcpgt import fixture as fixtures
from mcpgt import report as reports
from mcpgt.cli import main
from mcpgt.runner import run_fixture
from mcpgt.verdict import Outcome, TestKind, derive

FX = fixtures.load(Path(__file__).parent.parent / "fixtures" / "honest-idempotent")


def full_run(tmp_path):
    run = asyncio.run(run_fixture(FX, tmp_path))
    verdicts = derive(run)
    return run, verdicts, reports.build(run, verdicts, "test-run")


@pytest.fixture(scope="module")
def first(tmp_path_factory):
    return full_run(tmp_path_factory.mktemp("first"))


def test_expected_verdicts(first):
    _, verdicts, report = first
    by_test = {v.test: v.outcome for v in verdicts}
    assert by_test == {
        TestKind.SEQUENTIAL_IDEMPOTENCE: Outcome.PASS,
        TestKind.READ_ONLY: Outcome.NOT_APPLICABLE,
        TestKind.DESTRUCTIVE: Outcome.NOT_APPLICABLE,
    }
    assert report["expectations"]["checked"] == 3
    assert report["expectations"]["mismatches"] == []
    assert report["expectations"]["not_run"] == ["upsert_item/concurrent_idempotence"]


def test_control_sessions_are_stable_and_inert(first):
    run, _, report = first
    assert run.control_stable
    assert len(run.controls) == 3
    assert not any(o.deltas[0].regions for o in run.control_effect)


def test_report_carries_raw_listing_and_provenance(first):
    run, _, report = first
    (listing,) = report["listing"]["listings"]
    assert len(listing["sessions"]) == 3 + 3 * 2
    assert listing["raw_pages"][0].startswith('{"jsonrpc":"2.0"')
    assert report["listing"]["stable"]
    prov = report["provenance"]
    assert prov["server"]["source"] == "fixtures/honest-idempotent"
    assert prov["server"]["package_version"] == "2.3.0"
    assert len(prov["server"]["content_sha256"]) == 64
    assert '"serverInfo"' in prov["initialize_response"]


def test_tool_responses_are_kept_verbatim(first):
    _, _, report = first
    (tool,) = report["tools"]
    once = tool["sessions"][0]
    assert once["label"] == "upsert_item/once-1"
    assert once["responses"][0].startswith('{"jsonrpc":"2.0"')


def test_report_is_json_serializable(first):
    _, _, report = first
    assert json.loads(json.dumps(report)) == report


def test_same_fixture_same_verdicts(first, tmp_path):
    _, verdicts_a, _ = first
    _, verdicts_b, _ = full_run(tmp_path)
    assert [v.to_json() for v in verdicts_a] == [v.to_json() for v in verdicts_b]


def test_cli_run_and_report(tmp_path, capsys):
    runs_dir = tmp_path / "runs"
    assert main(["run", str(FX.root), "--runs-dir", str(runs_dir)]) == 0
    out = capsys.readouterr().out
    assert "3/3 matched" in out
    (run_dir,) = runs_dir.iterdir()
    assert main(["report", run_dir.name, "--runs-dir", str(runs_dir)]) == 0
    assert "sequential_idempotence" in capsys.readouterr().out
    assert main(["report", run_dir.name, "--runs-dir", str(runs_dir), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["fixture"] == "honest-idempotent"


def test_cli_rejects_too_few_runs(tmp_path):
    assert main(["run", str(FX.root), "--runs", "2", "--runs-dir", str(tmp_path)]) == 2
