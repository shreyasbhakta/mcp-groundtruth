"""The distribution is installable as mcp-groundtruth with the mcpgt command."""

import importlib.metadata
import tomllib
from pathlib import Path

import mcpgt

PYPROJECT = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())


def test_distribution_name_and_version():
    assert PYPROJECT["project"]["name"] == "mcp-groundtruth"
    assert mcpgt.__version__ == importlib.metadata.version("mcp-groundtruth") == PYPROJECT["project"]["version"]


def test_both_commands_run_the_cli():
    scripts = {ep.name: ep.value for ep in importlib.metadata.entry_points(group="console_scripts")}
    assert scripts["mcpgt"] == "mcpgt.cli:main"
    assert scripts["mcp-groundtruth"] == "mcpgt.cli:main"


def test_sdist_leaves_out_results_and_local_state():
    include = PYPROJECT["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    assert not {"/results", "/runs", "/.mcpgt"} & set(include)
