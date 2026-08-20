"""AI coding plan usage tracker for MiniMax, GLM, Claude Code and Codex plans."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

try:
    # pyproject.toml is the single source of truth for the version.
    __version__ = _package_version("ai-coding-usage-tracker")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.2.1"  # keep in sync with pyproject.toml [project] version

from .cli import app  # noqa: E402  (version must resolve before cli imports it)

__all__ = ["app", "__version__"]
