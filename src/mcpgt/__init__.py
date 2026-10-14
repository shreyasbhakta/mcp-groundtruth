"""mcp-groundtruth: behavioral conformance harness for MCP tool annotations."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mcpgt")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
