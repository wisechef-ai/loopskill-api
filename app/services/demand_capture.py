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


def _upsert(db: Session, q: str, *, user_id=None, is_probe: bool = False) -> None:
    """Increment the (lower(q), today) row, inserting it if absent.

    Split out from ``record_missing_skill_query`` so the swallow-everything
    boundary is one function up: a test can make THIS raise and assert the
    caller stays quiet.

    **The conflict target must be spelled exactly as the index.** The unique
    index (topshelf_2605_h) is FUNCTIONAL — ``(lower(query), day)`` — so
    Postgres only infers it when ON CONFLICT names the same expression.
    Handing SQLAlchemy the ORM column renders ``ON CONFLICT (lower(query), day)``
    against the INSERT's own column reference, which Postgres declines to match
    to the index; the statement then affects zero rows. Combined with the
    fire-and-forget guard around this call, the write vanishes **silently**.

    Caught by the postgres CI matrix on 2026-08-08: 4 tests green on SQLite,
    zero rows written on Postgres — which is what prod runs. The SQLite branch
    below cannot catch this class of bug (the same migration gives SQLite a
    plain, non-functional unique index), so the postgres matrix is the only
    thing standing between this and silent data loss in production.

    ``index_where=None`` + an explicit text() target is the spelling that
    matches; it is written literally rather than built from ORM constructs so
    it can be diffed against the migration by eye.

    ``is_probe`` (coldstart_0609/A) is only stamped on the INSERT branch — a
    probe hitting an already-existing row still increments ``count`` (the
    row's origin doesn't change retroactively); on Postgres the same is true
    of the ON CONFLICT DO UPDATE, which intentionally omits is_probe.
    """
    today = date.today()
    bind = db.get_bind()

    if bind.dialect.name == "postgresql":
        from sqlalchemy import text

        db.execute(
            text(
                """
                INSERT INTO missing_skill_queries (id, query, user_id, day, count, is_probe)
                VALUES (gen_random_uuid(), :q, :uid, :day, 1, :is_probe)
                ON CONFLICT (lower(query), day)
                DO UPDATE SET count = missing_skill_queries.count + 1
                """
            ),
            {"q": q, "uid": user_id, "day": today, "is_probe": is_probe},
        )
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
            db.add(MissingSkillQuery(query=q, user_id=user_id, day=today, count=1, is_probe=is_probe))
    db.commit()


def record_missing_skill_query(
    db: Session,
    q: str | None,
    *,
    user_id=None,
    api_key_id=None,
    client_ip: str | None = None,
) -> bool:
    """Record one zero-result search. Returns True if a signal was written.

    Fire-and-forget by contract: callers do not check the return value in
    production; it exists so tests can assert the skip cases without reading
    the table. An empty/whitespace query is a BROWSE, not demand, and is
    deliberately not recorded — otherwise every homepage visit would mint a
    row and drown the real signal.

    ``api_key_id`` / ``client_ip`` (coldstart_0609/A) are optional so
    existing callers with no request context keep working unchanged; when
    supplied they are run through the single probe-detection seam
    (``app.services.probe_detection.is_probe_request``) to stamp
    ``is_probe`` on the row.
    """
    query = normalise_query(q)
    if not query:
        return False
    from app.services.probe_detection import is_probe_request

    is_probe = is_probe_request(db, api_key_id=api_key_id, client_ip=client_ip)
    try:
        _upsert(db, query, user_id=user_id, is_probe=is_probe)
        return True
    # Rationale: VOC logging must never break the search response it rides on.
    except Exception:  # noqa: BLE001
        logger.debug("missing_skill_query upsert failed — ignored", exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            logger.debug("missing_skill_query rollback also failed", exc_info=True)
        return False


# ── Federation-fulfilled demand (unisearch_0709 P2) ──────────────────────────
#
# A DIFFERENT signal, and therefore a DIFFERENT store. ``missing_skill_queries``
# above means "zero TOTAL results" — the catalog gap the weekly digest and
# ``/api/admin/demand-brief`` read as "nobody, anywhere, had this". A query the
# MCP search answered only from federation is the opposite finding: somebody had
# it, it just wasn't us. Writing those rows into the same table would inflate the
# gap count with queries that were in fact fulfilled and corrupt the VOC signal
# the brief exists to produce. The existing writers above are untouched.
#
# Why a TelemetryEvent row rather than a new table:
#   * No alembic migration. unisearch_0709 locked "no migrations this sprint"
#     precisely because one changes the rollback story of every phase.
#   * ``telemetry_events.event_type`` is indexed and is already where this exact
#     funnel lives (``metasearch.query``, ``metasearch.install_intent``). A
#     dedicated event_type is a namespace no ``missing_skill_queries`` reader
#     can accidentally sweep up.
#   * The signal is per-EVENT, not a per-day counter. The actionable content is
#     the (query -> install_ref) pair: "agents keep asking for X and we keep
#     handing them somebody else's X" is the curation prompt. A
#     (query, day, count) aggregate row would throw the install_ref away.
FEDERATED_FULFILLED_EVENT = "federated_fulfilled_queries"


def record_federated_fulfilled_query(
    db: Session,
    q: str | None,
    *,
    rows: list[dict] | None = None,
    freshness: str = "cold",
    api_key_id=None,
    client_ip: str | None = None,
) -> bool:
    """Record one search that ONLY federation could answer. Returns True if written.

    Callers must have already established the precondition (zero native hits AND
    at least one federated row); this function does not re-derive it, it only
    refuses an empty query — a browse is not demand, same rule as
    ``record_missing_skill_query``.

    Fire-and-forget by contract: every failure is swallowed and logged. A demand
    row is worth strictly less than the search response it rides along with.
    """
    import json

    from app.models import TelemetryEvent
    from app.services.probe_detection import is_probe_request

    query = normalise_query(q)
    if not query or not rows:
        return False
    try:
        payload = {
            "query": query,
            "federated_count": len(rows),
            "top_install_ref": rows[0].get("install_ref"),
            "deployable_count": sum(1 for r in rows if r.get("deployable")),
            "freshness": freshness,
            # coldstart_0609/A: fleet dogfooding must not read as customer demand.
            "is_probe": is_probe_request(db, api_key_id=api_key_id, client_ip=client_ip),
        }
        db.add(
            TelemetryEvent(
                event_type=FEDERATED_FULFILLED_EVENT,
                skill_slug=None,
                payload=json.dumps(payload),
                client_ip=client_ip,
            )
        )
        db.commit()
        return True
    # Rationale: VOC logging must never break the search response it rides on.
    except Exception:  # noqa: BLE001
        logger.debug("federated_fulfilled_query write failed — ignored", exc_info=True)
        try:
            db.rollback()
        # Rationale: rollback itself can raise on a broken session; still never surface.
        except Exception:  # noqa: BLE001
            logger.debug("federated_fulfilled_query rollback also failed", exc_info=True)
        return False
