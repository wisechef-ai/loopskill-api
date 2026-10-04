"""Local search over the ingested Hermes Hub snapshot — bounded and multi-word.

The MCP search floor (fed1004) answers from ``federation_hub_skills`` while a
live fan-out is still running. Two properties the generic hub adapter search
does not give, and this module does:

- **Bounded.** On Postgres the query runs under ``SET LOCAL statement_timeout``,
  so a slow or locked table cannot hold an agent's search open; the caller also
  waits for this thread with its own deadline.
- **Multi-word recall.** The adapter matches the whole query as ONE phrase, so
  ``code review`` never finds a row slugged ``code-review``. Here every token
  must appear somewhere (slug, title, identifier or description), then the
  shared relevance ladder orders the matches.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT_MS = 800
MAX_TOKENS = 6
_TOKEN_SPLIT = re.compile(r"[\s\-_/.]+")


def query_tokens(query: str | None) -> list[str]:
    """Lower-cased search tokens. ``-``/``_``/``/``/``.`` split like spaces, so
    ``code-review`` and ``code review`` search the same tokens."""
    tokens = [t for t in _TOKEN_SPLIT.split((query or "").lower()) if t]
    return tokens[:MAX_TOKENS]


def _like(token: str) -> str:
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_hub_index(query: str | None, *, limit: int) -> list[Any]:
    """``ExternalSkill`` rows from the hub index that contain every query token,
    most relevant first. Empty query → ``[]``. Raises on DB failure (the caller
    owns degradation)."""
    tokens = query_tokens(query)
    if not tokens:
        return []
    from sqlalchemy import and_, or_, text

    from app.database import SessionLocal
    from app.models import FederationHubSkill as M
    from app.services.federation_adapters import HermesHubAdapter
    from app.services.federation_relevance import relevance_order_clauses

    db = SessionLocal()
    try:
        if db.get_bind().dialect.name == "postgresql":
            # SET LOCAL cannot take a bind parameter; the value is an int constant.
            db.execute(text(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}"))
        match_all = and_(
            *[
                or_(
                    M.slug.ilike(_like(t), escape="\\"),
                    M.title.ilike(_like(t), escape="\\"),
                    M.identifier.ilike(_like(t), escape="\\"),
                    M.description.ilike(_like(t), escape="\\"),
                )
                for t in tokens
            ]
        )
        q = " ".join(tokens)
        rows = (
            db.query(M).filter(match_all).order_by(*relevance_order_clauses(M, q), M.title).limit(limit).all()
        )
        adapter = HermesHubAdapter()
        return [adapter._map_hub_skill(r) for r in rows]
    finally:
        try:
            db.rollback()
        finally:
            db.close()
