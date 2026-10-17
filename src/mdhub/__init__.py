"""market-data-hub: loaders, schedule and PostgreSQL store for RUB market data.

See ``docs/DESIGN.md`` for the architecture and ``docs/DEVELOPING.md`` for how to extend it.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("market-data-hub")
    """Installed version, derived from git tags at build time (hatch-vcs, ``pyproject.toml``)."""
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"

USER_AGENT = f"market-data-hub/{__version__}"
"""Honest User-Agent sent with every outbound HTTP request (DESIGN §2)."""

__all__ = ["USER_AGENT", "__version__"]
