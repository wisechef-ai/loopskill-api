"""BM25 search index — pure Postgres tsvector, no embeddings.

Embeddings deferred to v7.2; BM25-only per Adam directive 2026-05-07.

Provides ``reindex_bm25(slug, db)`` which updates the ``search_vector``
column on ``skills`` using ``to_tsvector('english', ...)`` on Postgres
or a plain-text fallback on SQLite (test environment).

SYNCHRONOUS — BM25 is <10ms in postgres, no async / BackgroundTasks needed.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def reindex_bm25(slug: str, db: Session, *, archive: bool = False) -> None:
    """Rebuild the BM25 search_vector for a single skill.

    On Postgres this uses ``to_tsvector('english', ...)`` for proper
    tokenisation, stemming, and stop-word removal.  On SQLite (tests)
    the raw concatenated text is stored — good enough for NOT NULL checks.

    When *archive* is True the search_vector is set to NULL instead,
    effectively removing the skill from full-text search results.
    """
    if archive:
        db.execute(
            text("UPDATE skills SET search_vector = NULL WHERE slug = :slug"),
            {"slug": slug},
        )
        db.commit()
        logger.debug("reindex_bm25: archived %s (search_vector=NULL)", slug)
        return

    # Build the document body from skill metadata columns.
    # ``related_skills`` is a JSON column — ``::text`` gives the JSON string.
    sql_pg = text(
        "UPDATE skills SET search_vector = "
        "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(description, '') "
        "|| ' ' || coalesce(readme, '') || ' ' || coalesce(related_skills::text, '')) "
        "WHERE slug = :slug"
    )
    # SQLite fallback: just store the concatenated text (no ts_vector function).
    sql_lite = text(
        "UPDATE skills SET search_vector = "
        "coalesce(title, '') || ' ' || coalesce(description, '') "
        "|| ' ' || coalesce(readme, '') || ' ' || coalesce(related_skills, '') "
        "WHERE slug = :slug"
    )

    bind = db.get_bind()
    dialect = bind.dialect.name if bind else "unknown"

    try:
        if dialect == "postgresql":
            db.execute(sql_pg, {"slug": slug})
        else:
            db.execute(sql_lite, {"slug": slug})
        db.commit()
        logger.debug("reindex_bm25: reindexed %s (dialect=%s)", slug, dialect)
    # Rationale: BM25 tsvector reindex may fail for one skill; rollback and log, then reraise
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("reindex_bm25: failed for %s", slug)
        raise


def reindex_all(db: Session) -> int:
    """Reindex every non-archived skill.  Returns the count reindexed.

    For catastrophic recovery only — called by ``POST /api/admin/reindex-all``.
    """
    from app.models import Skill

    skills = db.query(Skill).filter(Skill.is_archived == False).all()  # noqa: E712
    count = 0
    for sk in skills:
        try:
            reindex_bm25(sk.slug, db)
            count += 1
        # Rationale: per-skill reindex failure must not abort batch; log and continue
        except Exception:  # noqa: BLE001
            logger.exception("reindex_all: skipped %s", sk.slug)
    return count
