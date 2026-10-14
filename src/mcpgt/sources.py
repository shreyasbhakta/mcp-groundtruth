"""Pinned third-party server sources: fetch, build, and check before every run.

``mcpgt prepare <target>`` is the only step that touches the network: it
fetches exactly one commit of the target's repository into a local cache and
runs the target's build commands there. Every run then verifies, offline,
that the checkout is still at that commit, that no tracked file has been
modified, and that it was built with the commands now in the target config.
The server is always started from this checkout, never from a registry.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcpgt.provenance import now_utc, tree_sha256

SOURCES_DIR = Path(".mcpgt") / "sources"
_SHA = re.compile(r"^[0-9a-f]{40}$")


class SourceError(RuntimeError):
    """The pinned source is missing, at the wrong commit, modified, or unbuilt."""


@dataclass(frozen=True, slots=True)
class SourceSpec:
    repository: str
    commit: str
    build: tuple[tuple[str, ...], ...]
    package: str | None  # directory with the package manifest, relative to the checkout
    artifacts: tuple[str, ...]  # built paths whose content is digested into provenance

    def __post_init__(self) -> None:
        if not _SHA.match(self.commit):
            raise ValueError(f"commit must be a full 40-character SHA, got {self.commit!r}")

    @property
    def build_digest(self) -> str:
        return hashlib.sha256(json.dumps([list(c) for c in self.build]).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PreparedSource:
    path: Path
    spec: SourceSpec
    package_name: str | None
    package_version: str | None
    content_sha256: str
    prepared_at: str
    toolchain: dict[str, str]


def checkout_dir(spec: SourceSpec, base: Path = SOURCES_DIR) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", spec.repository.removesuffix(".git")).strip("-")[-60:]
    return (base / slug / spec.commit).resolve()


def _marker(checkout: Path) -> Path:
    return checkout.with_name(checkout.name + ".prepared.json")


def _git(checkout: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SourceError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


@dataclass(frozen=True, slots=True)
class NodeRuntime:
    path: str
    version: str  # e.g. "v24.21.0"

    @property
    def major(self) -> str:
        return self.version.lstrip("v").split(".", 1)[0]


def node_runtime(required_major: str) -> NodeRuntime:
    """The ``node`` first on PATH, which must be the required major version."""
    exe = shutil.which("node", path=os.environ.get("PATH"))
    if exe is None:
        raise SourceError(f"Node {required_major} required, but no node on PATH")
    version = subprocess.run([exe, "--version"], capture_output=True, text=True, check=False).stdout.strip()
    runtime = NodeRuntime(str(Path(exe).resolve()), version)
    if runtime.major != required_major:
        raise SourceError(
            f"Node {required_major} required, but {exe} is {version or 'unknown'}; "
            f"put Node {required_major} first on PATH"
        )
    return runtime


def _toolchain() -> dict[str, str]:
    versions: dict[str, str] = {}
    for tool in ("node", "npm"):
        exe = shutil.which(tool)
        if exe is not None:
            proc = subprocess.run([exe, "--version"], capture_output=True, text=True, check=False)
            versions[tool] = proc.stdout.strip()
    return versions


def prepare(
    spec: SourceSpec, base: Path = SOURCES_DIR, log: Any = print, node_major: str | None = None
) -> PreparedSource:
    """Fetch the pinned commit and build it. Uses the network."""
    if node_major is not None:
        node_runtime(node_major)  # build only under the pinned Node major
    checkout = checkout_dir(spec, base)
    if not (checkout / ".git").is_dir():
        checkout.mkdir(parents=True, exist_ok=True)
        _git(checkout, "init", "-q")
        _git(checkout, "remote", "add", "origin", spec.repository)
    log(f"fetching {spec.repository} @ {spec.commit}")
    _git(checkout, "fetch", "-q", "--depth", "1", "origin", spec.commit)
    _git(checkout, "checkout", "-q", "--detach", spec.commit)
    for command in spec.build:
        log("$ " + " ".join(command))
        proc = subprocess.run(list(command), cwd=checkout, check=False)
        if proc.returncode != 0:
            raise SourceError(f"build step failed ({proc.returncode}): {' '.join(command)}")
    _marker(checkout).write_text(
        json.dumps(
            {"commit": spec.commit, "build_digest": spec.build_digest, "prepared_at": now_utc(), **_toolchain()},
            indent=2,
        )
        + "\n"
    )
    return verify(spec, base, node_major)


def verify(spec: SourceSpec, base: Path = SOURCES_DIR, node_major: str | None = None) -> PreparedSource:
    """Offline check that the prepared checkout is exactly what the spec pins."""
    checkout = checkout_dir(spec, base)
    if not (checkout / ".git").is_dir():
        raise SourceError(f"source not prepared: run `mcpgt prepare` (expected {checkout})")
    head = _git(checkout, "rev-parse", "HEAD").strip()
    if head != spec.commit:
        raise SourceError(f"checkout is at {head}, target pins {spec.commit}")
    modified = _git(checkout, "status", "--porcelain", "--untracked-files=no").strip()
    if modified:
        raise SourceError(f"tracked files modified in {checkout}:\n{modified}")
    try:
        marker = json.loads(_marker(checkout).read_text())
    except (OSError, ValueError) as exc:
        raise SourceError("source fetched but not built: run `mcpgt prepare`") from exc
    if marker.get("commit") != spec.commit or marker.get("build_digest") != spec.build_digest:
        raise SourceError("build commands changed since the source was prepared: run `mcpgt prepare`")
    if node_major is not None and str(marker.get("node", "")).lstrip("v").split(".", 1)[0] != node_major:
        raise SourceError(
            f"source was built with Node {marker.get('node', 'unknown')}, target pins Node {node_major}: "
            "run `mcpgt prepare` under that version"
        )
    files: list[Path] = []
    for artifact in spec.artifacts:
        path = checkout / artifact
        if not path.exists():
            raise SourceError(f"build artifact missing: {artifact}")
        files += [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
    name = version = None
    if spec.package is not None:
        manifest = json.loads((checkout / spec.package / "package.json").read_text())
        name, version = manifest.get("name"), manifest.get("version")
    return PreparedSource(
        path=checkout,
        spec=spec,
        package_name=name,
        package_version=version,
        content_sha256=tree_sha256(checkout, files),
        prepared_at=str(marker.get("prepared_at")),
        toolchain={k: str(marker[k]) for k in ("node", "npm") if k in marker},
    )


def build_commands(value: Sequence[Sequence[str]]) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(c) for c in value)
