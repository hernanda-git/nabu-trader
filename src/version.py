"""Package version.

The canonical version is a PLAIN INTEGER (e.g. 62) — not a git SHA and not a
"v"-prefixed string. It is the single source of truth for the bot's version;
it surfaces in the /version command, the startup banner, traded telemetry, and
the API server's reported version. Bump it on each release.
"""

__version__ = 67
