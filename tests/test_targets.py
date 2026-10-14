"""Audit targets: definitions, pinned sources, the sandbox, and audit mode."""

import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from mcpgt import fixture as fixtures
from mcpgt.cli import main
from mcpgt.sandbox import IsolationUnavailable, isolated, isolation
from mcpgt.sources import SourceError, SourceSpec, checkout_dir, prepare, verify

FIXTURES = Path(__file__).parent.parent / "fixtures"


def isolation_available() -> bool:
    try:
        isolation()
    except IsolationUnavailable:
        return False
    return True


needs_isolation = pytest.mark.skipif(not isolation_available(), reason="no network isolation on this host")


# --- Definitions ------------------------------------------------------------------

TARGET = """
[target]
name = "t"
command = ["{python}", "SERVER"]
env = { DATA = "{state}/data" }

[state]
locations = ["fs:.", "json:graph.jsonl"]

[[adapters]]
kind = "fs"
label = "."

[[adapters]]
kind = "json"
format = "jsonl"
partition_by = "type"
explode = { observations = ["name"] }
label = "graph.jsonl"
business = ["entity", "entity.observations"]

[[tools]]
name = "read"
arguments = { q = "x" }
"""


def write_target(root: Path, text: str = TARGET) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "target.toml").write_text(text)
    return root


def test_target_loads(tmp_path):
    fx = fixtures.load(write_target(tmp_path / "t"))
    assert fx.is_target and fx.isolated and fx.kind == "target"
    assert fx.server_env(tmp_path / "s") == {"DATA": str((tmp_path / "s").resolve()) + "/data"}
    (fs, _json) = fx.build_adapters(tmp_path / "s")
    assert fs.delegated == {"graph.jsonl": "json:graph.jsonl"}  # type: ignore[attr-defined]
    assert fixtures.load(FIXTURES / "liar-idempotent").isolated is False


@pytest.mark.parametrize(
    "old, new, message",
    [
        (
            'arguments = { q = "x" }',
            'arguments = { q = "x" }\n[tools.expected.read_only]\nverdict = "PASS"',
            "never carry",
        ),
        ('kind = "fs"\nlabel = "."', 'kind = "sqlite"\nlabel = "."', "whole state directory"),
        ('format = "jsonl"', 'format = "yaml"', "format must be"),
        ('kind = "fs"\nlabel = "."', 'kind = "fs"\nlabel = "../escape"', "relative path"),
        ('command = ["{python}", "SERVER"]', 'command = ["node", "{source}/x.js"]', r"no \[source\]"),
    ],
)
def test_invalid_targets_are_rejected(tmp_path, old, new, message):
    assert old in TARGET
    with pytest.raises(fixtures.FixtureError, match=message):
        fixtures.load(write_target(tmp_path / "t", TARGET.replace(old, new)))


def test_fixtures_cannot_have_a_source(tmp_path):
    root = tmp_path / "fx"
    shutil.copytree(FIXTURES / "honest-readonly", root)
    with (root / "fixture.toml").open("a") as f:
        f.write('\n[source]\nrepository = "x"\ncommit = "' + "a" * 40 + '"\nbuild = []\n')
    with pytest.raises(fixtures.FixtureError, match="unknown keys"):
        fixtures.load(root)


def test_json_options_only_on_json_adapters(tmp_path):
    text = TARGET.replace('kind = "fs"\nlabel = "."', 'kind = "fs"\nlabel = "."\nformat = "jsonl"')
    with pytest.raises(fixtures.FixtureError, match="json adapters only"):
        fixtures.load(write_target(tmp_path / "t", text))


# --- Pinned sources (a local repository; no network) ---------------------------------


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def upstream(tmp_path):
    repo = tmp_path / "upstream"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "t")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "package.json").write_text(json.dumps({"name": "demo", "version": "1.2.3"}))
    (repo / ".gitignore").write_text("dist/\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "c1")
    return repo, git(repo, "rev-parse", "HEAD").strip()


def spec_for(repo, commit, build=(("mkdir", "-p", "pkg/dist"), ("touch", "pkg/dist/index.js"))):
    return SourceSpec(repo.as_uri(), commit, tuple(build), "pkg", ("pkg/dist",))


def test_prepare_then_verify(upstream, tmp_path):
    repo, commit = upstream
    spec = spec_for(repo, commit)
    prepared = prepare(spec, base=tmp_path / "cache", log=lambda _: None)
    assert prepared.path == checkout_dir(spec, tmp_path / "cache")
    assert (prepared.package_name, prepared.package_version) == ("demo", "1.2.3")
    assert verify(spec, base=tmp_path / "cache").content_sha256 == prepared.content_sha256


def test_verify_refuses_unprepared_modified_or_rebuilt_sources(upstream, tmp_path):
    repo, commit = upstream
    base = tmp_path / "cache"
    spec = spec_for(repo, commit)
    with pytest.raises(SourceError, match="not prepared"):
        verify(spec, base=base)
    prepared = prepare(spec, base=base, log=lambda _: None)
    changed_build = spec_for(repo, commit, build=(("mkdir", "-p", "pkg/dist"),))
    with pytest.raises(SourceError, match="build commands changed"):
        verify(changed_build, base=base)
    (prepared.path / "pkg" / "package.json").write_text("{}")
    with pytest.raises(SourceError, match="modified"):
        verify(spec, base=base)


def test_commit_must_be_a_full_sha():
    with pytest.raises(ValueError, match="40-character"):
        SourceSpec("x", "main", (), None, ())


# --- Sandbox ----------------------------------------------------------------------


@needs_isolation
def test_sandbox_denies_network(tmp_path):
    probe = "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)"
    proc = subprocess.run(isolated([sys.executable, "-c", probe], tmp_path), capture_output=True, text=True)
    assert proc.returncode != 0
    assert "Operation not permitted" in proc.stderr or "unreachable" in proc.stderr.lower()


@needs_isolation
@pytest.mark.skipif(sys.platform != "darwin", reason="write confinement is macOS-only")
def test_sandbox_confines_writes_to_the_state_directory(tmp_path):
    state, outside = tmp_path / "state", tmp_path / "outside"
    state.mkdir()
    outside.mkdir()
    script = f"open({str(state / 'ok')!r}, 'w').write('1'); open({str(outside / 'no')!r}, 'w').write('1')"
    proc = subprocess.run(isolated([sys.executable, "-c", script], state), capture_output=True, text=True)
    assert (state / "ok").exists()
    assert not (outside / "no").exists()
    assert "Operation not permitted" in proc.stderr


def test_sandbox_still_allows_local_pipes(tmp_path):
    if not isolation_available():
        pytest.skip("no network isolation on this host")
    proc = subprocess.run(isolated([sys.executable, "-c", "print('ok')"], tmp_path), capture_output=True, text=True)
    assert proc.stdout.strip() == "ok"
    socket.gethostname()  # the harness itself is unaffected


# --- Audit mode end to end ------------------------------------------------------------


@pytest.fixture
def liar_target(tmp_path):
    """liar-idempotent, redeclared as an audit target (absolute server path, no ground truth)."""
    root = tmp_path / "liar-target"
    root.mkdir()
    shutil.copy(FIXTURES / "liar-idempotent" / "server.py", root / "server.py")
    shutil.copy(FIXTURES / "liar-idempotent" / "seed.sql", root / "seed.sql")
    (root / "target.toml").write_text(
        f"""
[target]
name = "liar-target"
command = ["{{python}}", "{root / "server.py"}"]

[state]
locations = ["fs:.", "sqlite:state.db"]

[[adapters]]
kind = "fs"
label = "."

[[adapters]]
kind = "sqlite"
label = "state.db"
seed = "seed.sql"
business = ["items"]
mask = [{{ region = "items", column = "id", kind = "autoincrement" }}]

[[tools]]
name = "add_item"
arguments = {{ key = "c", value = "3" }}
"""
    )
    return root


def test_targets_require_audit_mode(liar_target, tmp_path):
    assert main(["run", str(liar_target), "--runs-dir", str(tmp_path / "runs")]) == 2


@needs_isolation
def test_audit_exits_zero_and_reports_without_expectations(liar_target, tmp_path, capsys):
    runs = tmp_path / "runs"
    args = ["run", str(liar_target), "--audit", "--concurrent-attempts", "2", "--runs-dir", str(runs)]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "AUDIT" in out and "VIOLATION" in out
    (report_dir,) = runs.iterdir()
    report = json.loads((report_dir / "report.json").read_text())
    assert report["mode"] == "audit" and report["kind"] == "target"
    assert report["expectations"]["checked"] == 0
    assert all("expected" not in v for t in report["tools"] for v in t["verdicts"])
    assert report["provenance"]["runtime"]["isolation"]["network"] == "denied"
    seq = next(v for v in report["tools"][0]["verdicts"] if v["test"] == "sequential_idempotence")
    assert seq["outcome"] == "VIOLATION"


def test_cli_prepare_needs_a_source(capsys):
    assert main(["prepare", str(FIXTURES / "honest-readonly")]) == 2
    assert "no [source]" in capsys.readouterr().err
