"""LEGO price tracker for the Indian market."""

__version__ = "2.0.0"

__all__ = ["Database", "PoliteSession"]


def __getattr__(name):
    """Import the heavy modules only when something actually asks for them.

    `from legotracker import PoliteSession` still works exactly as before. What
    changed is that merely importing the package no longer drags in `requests`
    — which matters because `legotracker.site` builds the public website inside
    GitHub Actions with no dependencies installed at all. Eager imports here
    would make `import legotracker.site` fail there, and it would fail on the
    first deploy rather than anywhere near this line.

    PEP 562 module-level __getattr__, Python 3.7+.
    """
    if name == "Database":
        from .db import Database
        return Database
    if name == "PoliteSession":
        from .http_client import PoliteSession
        return PoliteSession
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
