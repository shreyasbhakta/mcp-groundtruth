"""JSON reports and terminal summaries.

A report is self-contained evidence: provenance, the verbatim tools/list and
initialize responses, every session, the control sessions' own effect, and
for each verdict the declared contract, observed deltas by state category,
coverage and run count. For fixtures it also checks each verdict against the
expected verdict recorded in fixture.toml.
"""

from __future__ import annotations

import importlib.metadata
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import mcpgt
from mcpgt.fixture import Expectation, Fixture
from mcpgt.provenance import (
    CaptureProvenance,
    SourceProvenance,
    git_state,
    now_utc,
    portable_path,
    python_version,
    tree_sha256,
)
from mcpgt.runner import FixtureRun
from mcpgt.snapshot import StateCategory
from mcpgt.verdict import IMPLEMENTED, Outcome, Reason, Verdict, coherence

SCHEMA = "mcpgt.report/0.2"
RUNS_DIR = Path("runs")


def provenance(run: FixtureRun) -> CaptureProvenance:
    fx = run.fixture
    fx_sha, fx_dirty = git_state(fx.root)
    pkg_dir = Path(mcpgt.__file__).parent
    h_sha, h_dirty = git_state(pkg_dir)
    definition = SourceProvenance(
        source=f"{'targets' if fx.is_target else 'fixtures'}/{fx.root.name}",
        git_sha=fx_sha,
        git_dirty=fx_dirty,
        package=None,
        package_version=None,
        content_sha256=tree_sha256(fx.root, fx.source_files()),
    )
    source = None if run.launch is None else run.launch.source
    if source is not None:
        # Verified before the run: at the pinned commit, no tracked file modified.
        server = SourceProvenance(
            source=source.spec.repository,
            git_sha=source.spec.commit,
            git_dirty=False,
            package=source.package_name,
            package_version=source.package_version,
            content_sha256=source.content_sha256,
        )
    else:
        server = definition
    iso = None if run.launch is None else run.launch.isolation
    runtime: dict[str, Any] = {
        "mcp_python_sdk": importlib.metadata.version("mcp"),
        "isolation": None if iso is None else iso.to_json(),
    }
    node = None if run.launch is None else run.launch.node
    if node is not None:
        runtime["node"] = {"path": portable_path(node.path), "version": node.version, "required_major": fx.node}
    if source is not None:
        runtime["build_toolchain"] = source.toolchain
        runtime["prepared_at"] = source.prepared_at
        runtime["build_commands"] = [list(c) for c in source.spec.build]
    return CaptureProvenance(
        server=server,
        definition=definition,
        runtime=runtime,
        harness=SourceProvenance(
            source="mcpgt",
            git_sha=h_sha,
            git_dirty=h_dirty,
            package="mcpgt",
            package_version=importlib.metadata.version("mcpgt"),
            content_sha256=tree_sha256(pkg_dir, sorted(pkg_dir.glob("*.py"))),
        ),
        initialize_response=run.initialize_text,
        captured_at=now_utc(),
        python_version=python_version(),
    )


def _verdict_text(outcome: Outcome, reason: Reason | None) -> str:
    return outcome.value + (f":{reason.value}" if reason else "")


def expectation_check(verdict: Verdict, expected: Expectation) -> list[str]:
    """Differences between a verdict and its expectation; empty means it matched."""
    problems = []
    if verdict.outcome is not expected.outcome or verdict.reason is not expected.reason:
        problems.append(
            f"verdict {_verdict_text(verdict.outcome, verdict.reason)}, "
            f"expected {_verdict_text(expected.outcome, expected.reason)}"
        )
    if expected.secondary is not None and set(verdict.secondary_reasons) != set(expected.secondary):
        got = [r.value for r in verdict.secondary_reasons]
        problems.append(f"secondary reasons {got}, expected {[r.value for r in expected.secondary]}")
    if expected.behavior is not None and verdict.behavior is not expected.behavior:
        problems.append(f"behavior {verdict.behavior.value}, expected {expected.behavior.value}")
    if (
        expected.errored_with_side_effects is not None
        and verdict.errored_with_side_effects is not expected.errored_with_side_effects
    ):
        problems.append(f"errored_with_side_effects {verdict.errored_with_side_effects}")
    for flag, category in (
        ("business_changed", StateCategory.BUSINESS),
        ("append_only_changed", StateCategory.APPEND_ONLY),
    ):
        want = getattr(expected, flag)
        if want is None:
            continue
        seen = {o.changed(category) for o in verdict.observations}
        if seen != {want}:
            problems.append(f"{flag}: observed {sorted(seen) or 'nothing'}, expected {want}")
    return problems


def build(run: FixtureRun, verdicts: list[Verdict], run_id: str, audit: bool = False) -> dict[str, Any]:
    """The report. In audit mode no verdict is compared with anything: there is
    no ground truth for a third-party server, only measurements to review."""
    fx = run.fixture
    specs = {t.key: t for t in fx.tools}
    tools = []
    checked = matched = 0
    mismatches: list[dict[str, Any]] = []
    not_run: list[str] = []
    for ev in run.tools:
        tool_verdicts = []
        for v in (v for v in verdicts if v.tool == ev.tool):
            entry = v.to_json()
            expected = None if audit else specs[ev.tool].expected.get(v.test)
            if expected is not None:
                problems = expectation_check(v, expected)
                checked += 1
                matched += not problems
                entry["expected"] = {"problems": problems, "matched": not problems}
                if problems:
                    mismatches.append({"tool": ev.tool, "test": v.test.value, "problems": problems})
            tool_verdicts.append(entry)
        if not audit:
            not_run += [f"{ev.tool}/{t.value}" for t in specs[ev.tool].expected if t not in IMPLEMENTED]
        tools.append(
            {
                "tool": ev.tool,
                "name": specs[ev.tool].name,
                "variant": specs[ev.tool].variant,
                "arguments": specs[ev.tool].arguments,
                "contract": None if ev.contract is None else ev.contract.to_json(),
                "contract_instability": list(ev.contract_instability),
                "coherence": [] if ev.contract is None else [i.value for i in coherence(ev.contract)],
                "sessions": [s.to_json() for s in ev.once + ev.twice],
                "concurrent_sessions": [r.to_json() for r in ev.concurrent_raw],
                "verdicts": tool_verdicts,
            }
        )
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "fixture": fx.name,
        "mode": "audit" if audit else "validation",
        "kind": fx.kind,
        "scope": _scope(run, audit),
        "definition": {
            "locations": list(fx.locations),
            "adapters": [a.to_json() for a in fx.adapters],
            "tool_arguments": {t.key: t.arguments for t in fx.tools},
        },
        "run_count": run.runs,
        "provenance": provenance(run).to_json(),
        "reset_verification": run.reset.to_json(),
        "control": {
            "stable": run.control_stable,
            "error": run.control_error,
            "nondeterminism": [i.to_json() for i in run.control_instability],
            "sessions": [s.to_json() for s in run.controls],
            "effect_vs_seed": [o.to_json() for o in run.control_effect],
        },
        "listing": {
            "stable": len(run.listings) <= 1 and not run.listing_errors,
            "errors": list(run.listing_errors),
            "listings": [{"sessions": list(r.sessions), **r.listing.to_json()} for r in run.listings],
        },
        "tools": tools,
        "untested_tools": list(run.untested_tools),
        "expectations": {
            "mode": "none (audit)" if audit else "checked against fixture ground truth",
            "checked": checked,
            "matched": matched,
            "mismatches": mismatches,
            "not_run": not_run,
        },
    }


def _scope(run: FixtureRun, audit: bool) -> str:
    fx = run.fixture
    source = None if run.launch is None else run.launch.source
    if fx.is_target and source is not None:
        where = f"{source.spec.repository} at {source.spec.commit}"
        return f"third-party server {where}, run in a network-free sandbox; verdicts are unreviewed measurements"
    if audit:
        return "audit run; verdicts are unreviewed measurements"
    return "fixture server written for mcpgt; not a third-party server"


def new_run_id(fixture: Fixture) -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "-" + fixture.name


def write(report: dict[str, Any], runs_dir: Path = RUNS_DIR) -> Path:
    path = runs_dir / str(report["run_id"]) / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n")
    return path


def load(run_id: str, runs_dir: Path = RUNS_DIR) -> dict[str, Any]:
    path = Path(run_id)
    if not path.is_file():
        path = runs_dir / run_id / "report.json"
    if not path.is_file():
        raise FileNotFoundError(f"no report for run {run_id!r} under {runs_dir}")
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def render_text(report: dict[str, Any]) -> str:
    audit = report.get("mode") == "audit"
    title = "AUDIT  " if audit else ""
    lines = [f"{title}{report['fixture']}  run {report['run_id']}  ({report['run_count']} runs)"]
    lines.append(f"  scope: {report['scope']}")
    rv = report["reset_verification"]
    lines.append(f"  reset: {rv['status']}" + (f" ({rv['detail']})" if rv["detail"] else ""))
    cov = rv["coverage"]
    lines.append(f"  coverage: {'complete' if cov['complete'] else 'partial'}")
    lines.append(f"  control sessions: {'stable' if report['control']['stable'] else 'UNSTABLE'}")
    if not report["listing"]["stable"]:
        lines.append("  tools/list: UNSTABLE across sessions")
    header = f"  {'tool':<20} {'test':<24} {'declared':<10} {'verdict':<44} {'observed':<26} expected"
    lines += ["", header, "  " + "-" * (len(header) - 2)]
    for tool in report["tools"]:
        if tool["coherence"]:
            lines.append(f"  {tool['tool']}: incoherent declaration: {', '.join(tool['coherence'])}")
        for v in tool["verdicts"]:
            verdict = v["outcome"] + (f":{v['reason']}" if v["reason"] else "")
            observed = f"{v['observed']['property']}={v['observed']['behavior']}"
            exp = v.get("expected")
            mark = "-" if exp is None else ("ok" if exp["matched"] else "MISMATCH")
            lines.append(
                f"  {tool['tool']:<20} {v['test']:<24} {v['declared']:<10} {verdict:<44} {observed:<26} {mark}"
            )
            if v["secondary_reasons"]:
                lines.append(f"  {'':<20}   secondary: {', '.join(v['secondary_reasons'])}")
            conc = v.get("concurrency")
            if conc and conc["attempts"]:
                parts = []
                for mode, m in conc["modes"].items():
                    if m["attempts"]:
                        ov = m["overlap"]
                        parts.append(
                            f"{mode}: {m['diverged']}/{m['attempts']} diverged, overlap "
                            f"in_flight {ov['in_flight_overlap']} unverified {ov['unverified']} none {ov['none']}"
                        )
                by = ", ".join(conc["produced_by"]) or "-"
                lines.append(f"  {'':<20}   attempts: {'; '.join(parts)}; produced by: {by}")
    exp = report["expectations"]
    lines.append("")
    if audit:
        lines.append("  audit mode: no expected verdicts; every VIOLATION and INCONCLUSIVE needs review")
    else:
        lines.append(f"  expectations: {exp['matched']}/{exp['checked']} matched")
    for m in exp["mismatches"]:
        lines.append(f"    {m['tool']}/{m['test']}: {'; '.join(m['problems'])}")
    if exp["not_run"]:
        lines.append(f"    not run yet: {', '.join(exp['not_run'])}")
    if report["untested_tools"]:
        lines.append(f"  listed but untested (no arguments configured): {', '.join(report['untested_tools'])}")
    return "\n".join(lines)
