import json
import subprocess

from mcpgt.provenance import CaptureProvenance, SourceProvenance, git_state, tree_sha256


def test_tree_digest_ignores_listing_order_and_tracks_content(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.toml").write_text("k = 2\n")
    files = [tmp_path / "a.py", tmp_path / "b.toml"]
    before = tree_sha256(tmp_path, files)
    assert tree_sha256(tmp_path, reversed(files)) == before
    (tmp_path / "a.py").write_text("x = 2\n")
    assert tree_sha256(tmp_path, files) != before


def test_tree_digest_tracks_renames(tmp_path):
    (tmp_path / "a.py").write_text("x")
    before = tree_sha256(tmp_path, [tmp_path / "a.py"])
    (tmp_path / "a.py").rename(tmp_path / "b.py")
    assert tree_sha256(tmp_path, [tmp_path / "b.py"]) != before


def test_git_state_reports_sha_and_dirtiness(tmp_path):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    assert git_state(tmp_path) == (None, None)
    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    assert git_state(tmp_path) == (None, None)  # no commits yet
    (tmp_path / "f").write_text("1")
    git("add", "f")
    git("commit", "-qm", "c")
    sha, dirty = git_state(tmp_path)
    assert len(sha) == 40 and dirty is False
    (tmp_path / "f").write_text("2")
    assert git_state(tmp_path) == (sha, True)


def test_capture_provenance_serializes():
    src = SourceProvenance("fixtures/x", "a" * 40, False, None, None, "b" * 64)
    cap = CaptureProvenance(src, src, '{"jsonrpc":"2.0"}', "2026-10-13T00:00:00+00:00", "CPython 3.11.9")
    data = json.loads(json.dumps(cap.to_json()))
    assert data["server"]["git_sha"] == "a" * 40
    assert data["initialize_response"] == '{"jsonrpc":"2.0"}'
