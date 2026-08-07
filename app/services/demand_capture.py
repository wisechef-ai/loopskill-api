"""Voice-of-customer demand capture — one writer for every search path.

fdeloop_0808 Phase A.

``MissingSkillQuery`` (topshelf_2605/H) records searches that returned nothing.
Until this module, the upsert lived inline in ``skill_routes.search_skills``
and therefore fired ONLY on the first-party path — 55 curated skills. A
zero-result search across the ~91k federated catalog (``/api/skills/external``,
which the portal's library and browse pages call, and ``/api/skills/metasearch``,
the agent-facing route) recorded nothing at all. The larger surface by three
orders of magnitude was throwing its demand signal away.

Two properties this module exists to guarantee:

**One normalisation.** The unique index is on ``(lower(query), day)``. Two
callers normalising differently produce either a constraint violation or a
split count that under-reports demand. Normalisation lives here, once.

**Never breaks a search.** Every failure mode is swallowed and logged. A demand
row is worth strictly less than the search response it rides along with.
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import MissingSkillQuery

logger = logging.getLogger(__name__)

# Long queries are almost always a paste accident, and the column is indexed on
# lower(query) — an unbounded key would bloat the index for zero signal.
_MAX_QUERY_LEN = 200


def normalise_query(q: str | None) -> str:
    """The single normalisation every caller shares: strip, collapse, truncate.

    Case is preserved in the stored value (the brief reads better with the
    user's own capitalisation); the unique index lowercases, and the upsert
    below matches on ``lower(query)``, so ``Copywriting`` and ``copywriting``
    increment ONE row.
    """
    if not q:
        return ""
    return " ".join(q.split())[:_MAX_QUERY_LEN]


def _upsert(db: Session, q: str, *, user_id=None) -> None:
    """Increment the (lower(q), today) row, inserting it if absent.

    Split out from ``record_missing_skill_query`` so the swallow-everything
    boundary is one function up: a test can make THIS raise and assert the
    caller stays quiet.
    """
    today = date.today()
    bind = db.get_bind()

    if bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(MissingSkillQuery).values(query=q, user_id=user_id, day=today, count=1)
        stmt = stmt.on_conflict_do_update(
            index_elements=[func.lower(MissingSkillQuery.query), MissingSkillQuery.day],
            set_={"count": MissingSkillQuery.count + 1},
        )
        db.execute(stmt)
    else:
        # SQLite (tests): no functional-index upsert support — SELECT then write.
        existing = (
            db.query(MissingSkillQuery)
            .filter(
                func.lower(MissingSkillQuery.query) == q.lower(),
                MissingSkillQuery.day == today,
            )
            .first()
        )
        if existing:
            existing.count += 1
        else:
            db.add(MissingSkillQuery(query=q, user_id=user_id, day=today, count=1))
    db.commit()


def record_missing_skill_query(db: Session, q: str | None, *, user_id=None) -> bool:
    """Record one zero-result search. Returns True if a signal was written.

    Fire-and-forget by contract: callers do not check the return value in
    production; it exists so tests can assert the skip cases without reading
    the table. An empty/whitespace query is a BROWSE, not demand, and is
    deliberately not recorded — otherwise every homepage visit would mint a
    row and drown the real signal.
    """
    query = normalise_query(q)
    if not query:
        return False
    try:
        _upsert(db, query, user_id=user_id)
        return True
    # Rationale: VOC logging must never break the search response it rides on.
    except Exception:  # noqa: BLE001
        logger.debug("missing_skill_query upsert failed — ignored", exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            logger.debug("missing_skill_query rollback also failed", exc_info=True)
        return False
