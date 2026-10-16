"""market-data-hub: loaders, schedule and PostgreSQL store for RUB market data.

See ``docs/DESIGN.md`` for the architecture and ``docs/DEVELOPING.md`` for how to extend it.
"""

__version__ = "0.1.0"

USER_AGENT = f"market-data-hub/{__version__}"
"""Honest User-Agent sent with every outbound HTTP request (DESIGN §2)."""

__all__ = ["USER_AGENT", "__version__"]
