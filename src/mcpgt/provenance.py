"""What exactly was tested, so every claim is about a specific version.

Capture metadata (timestamps, SHAs) is evidence, not part of the verdict:
determinism is required of verdicts and observed deltas, not of these fields.
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    source: str  # repository URL, or local path for fixtures
    git_sha: str | None
    git_dirty: bool | None  # uncommitted changes at capture; a dirty SHA does not pin the code
    package: str | None
    package_version: str | None
    content_sha256: str | None  # digest of the files actually executed

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CaptureProvenance:
    server: SourceProvenance  # the code that ran as the server
    harness: SourceProvenance
    initialize_response: str | None  # verbatim, includes serverInfo and protocolVersion
    captured_at: str  # ISO-8601 UTC
    python_version: str
    definition: SourceProvenance | None = None  # the fixture.toml / target.toml directory
    runtime: dict[str, Any] | None = None  # toolchain versions, sandbox

    def to_json(self) -> dict[str, Any]:
        return {
            "server": self.server.to_json(),
            "definition": None if self.definition is None else self.definition.to_json(),
            "harness": self.harness.to_json(),
            "runtime": self.runtime,
            "initialize_response": self.initialize_response,
            "captured_at": self.captured_at,
            "python_version": self.python_version,
        }


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def python_version() -> str:
    return f"{platform.python_implementation()} {platform.python_version()}"


def git_state(path: Path) -> tuple[str | None, bool | None]:
    """(HEAD sha, dirty) for the repository containing ``path``; (None, None) if unknown."""

    def git(*args: str) -> str | None:
        try:
            proc = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=False)
        except OSError:
            return None
        return proc.stdout if proc.returncode == 0 else None

    sha = git("rev-parse", "--verify", "HEAD")
    if sha is None:
        return None, None
    status = git("status", "--porcelain", "--", ".")
    return sha.strip(), None if status is None else bool(status.strip())


def tree_sha256(root: Path, files: Iterable[Path]) -> str:
    """Digest over (relative path, content) pairs, independent of listing order."""
    digest = hashlib.sha256()
    for rel in sorted(f.resolve().relative_to(root.resolve()).as_posix() for f in files):
        content = (root / rel).read_bytes()
        digest.update(rel.encode() + b"\0" + hashlib.sha256(content).digest())
    return digest.hexdigest()
