"""Known skill-client install roots, detected by PATH — never by asking.

An absent client directory (e.g. ~/.cursor/skills on a machine that has
never installed Cursor) is a normal state, not an error: every function
here treats "does not exist" as "0 skills for this client", full stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: client_id -> path relative to $HOME. Order is the display/scan order.
#: Extend this table (not the scan logic) to support a new client.
KNOWN_CLIENT_PATHS: dict[str, str] = {
    "claude": ".claude/skills",
    "hermes": ".hermes/skills",
    "codex": ".codex/skills",
    "cursor": ".cursor/skills",
}


@dataclass(frozen=True)
class ClientRoot:
    """One detected (or not) skill-client root."""

    client_id: str
    path: Path
    exists: bool


def known_clients(home: Path | None = None) -> list[ClientRoot]:
    """Return every known client, each stamped with whether its root exists.

    ``home`` is injectable for tests; defaults to ``Path.home()``. This is
    the ONLY function that decides client identity — everything downstream
    (import, diff, apply) consumes its output rather than re-deriving paths.
    """
    base = home if home is not None else Path.home()
    out: list[ClientRoot] = []
    for client_id, rel in KNOWN_CLIENT_PATHS.items():
        p = (base / rel).expanduser()
        out.append(ClientRoot(client_id=client_id, path=p, exists=p.is_dir()))
    return out


def present_clients(home: Path | None = None) -> list[ClientRoot]:
    """Return only the clients whose root actually exists on this machine."""
    return [c for c in known_clients(home) if c.exists]


def resolve_client_root(client_id: str, home: Path | None = None) -> ClientRoot:
    """Resolve a single client by id. Raises KeyError for an unknown id."""
    for c in known_clients(home):
        if c.client_id == client_id:
            return c
    raise KeyError(f"Unknown client id: {client_id!r}. Known: {sorted(KNOWN_CLIENT_PATHS)}")
