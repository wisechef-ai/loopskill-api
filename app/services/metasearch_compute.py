"""The ONE unified-metasearch compute, shared by the REST route and MCP search.

Before this module the compute lived as a closure inside
``metasearch_routes.metasearch``, deliberately not importable, because the MCP
search path was forbidden to fan out (a cold fan-out once measured >90s). That
cost came from ClawHub owner lookups — one live HTTP call per row — and was
removed by ``prime_clawhub_owner_cache`` (issue #148) and by ClawHub search hits
carrying ``ownerHandle`` inline. Re-measured on prod 2026-10-04: a cold fan-out
over every source finishes in ~2s (per-source deadline 1.2s, run in parallel).

What the cache-only rule cost in exchange: ``loopskill_search`` answered
``federated: cold`` with ZERO federated rows for every query no REST caller had
warmed in the last 15 minutes. Verified live: ``loopskill_search("ste100")``
returned nothing while ``/api/skills/metasearch?q=ste100`` returned 30 rows.

Contract (fed1004, after two adversarial review rounds):

- ONE cache key per query and ONE per-source deadline for every caller, so a
  cold query fans out once per process no matter which surface asks first, and
  no caller ever waits longer than the web UI's own compute.
- **Late-merge.** A source still running at the deadline (ClawHub's search
  route: p50 ~1.25s from prod vs a 1.2s deadline) is not lost: its future keeps
  running, and when it lands within ``LATE_GRACE_S`` its rows are merged into
  the cached entry by compare-and-set. Nobody waits for it; the NEXT read gets
  it. Breaker state is never touched by a late result — the owning fan-out loop
  already recorded the timeout (stragglers never mutate shared health).
- A background compute uses its OWN database session; the late-merge needs none.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app._skill_helpers import _install_counts_for, _skill_to_out
from app.models import Skill

logger = logging.getLogger(__name__)

CURATED_CAP = 50  # curated candidates pulled before the merge caps the page

# How long a late source may take after the deadline and still be merged. The
# HTTP timeout under it is 12s; past this grace the straggler is discarded.
LATE_GRACE_S = 6.0
# Late-merge threads at once. Past it, late rows are simply dropped (the next
# compute for the query tries again) — never queued.
MAX_LATE_MERGES = 8
_late_slots = threading.BoundedSemaphore(MAX_LATE_MERGES)
# How long the late-merge waits for the compute's own cache write to land.
_ENTRY_WAIT_S = 2.0


def curated_candidates(db: Session, q: str | None, limit: int) -> list[dict]:
    """Pull curated (internal, public) skill rows matching the query, as
    _skill_to_out dicts. Mirrors the literal-match pass of /api/skills/search but
    only the public catalog (the federation wall: never surface private skills)."""
    query = (
        db.query(Skill)
        .options(joinedload(Skill.versions), joinedload(Skill.creator))
        .filter(Skill.is_public == True, Skill.is_archived == False)  # noqa: E712
    )
    if q:
        like = f"%{q}%"
        query = query.filter(
            Skill.title.ilike(like)
            | Skill.description.ilike(like)
            | Skill.category.ilike(like)
            | Skill.readme.ilike(like)
        )
    rows = query.limit(limit).all()
    if not rows:
        return []
    counts = _install_counts_for(db, [s.id for s in rows])
    out = []
    for s in rows:
        skill_out = _skill_to_out(s, *counts.get(s.id, (0, 0)))
        d = skill_out.model_dump() if hasattr(skill_out, "model_dump") else dict(skill_out)
        # unify_curated reads install_count + slug/title/description/updated_at
        d["install_count"] = d.get("install_count_total", 0)
        out.append(d)
    return out


def build_unified(db: Session, q: str | None) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Build the unified ranked result against ``db``.

    Returns ``(contracted_skills, sources_ok, sources_degraded)`` — the exact
    triple ``HotQueryCache.get_or_compute`` stores. Sources that missed the
    deadline are handed to the late-merge. Module attributes are read at call
    time (``_fanout.fan_out``) so a test patch on the fan-out module is honoured
    here exactly as it is in the route.
    """
    from app.services import metasearch_fanout as _fanout
    from app.services.clawhub_owner_prime import prime_clawhub_owner_cache
    from app.services.metasearch import unify_curated, unify_external

    curated = [unify_curated(r) for r in curated_candidates(db, q, CURATED_CAP)]
    # issue #148: seed the ClawHub owner cache from the persisted snapshot BEFORE
    # fanning out, so no browse row pays a live owner lookup on a worker thread.
    prime_clawhub_owner_cache(db)
    sources = tuple(_fanout.DEFAULT_FANOUT_SOURCES)
    fanout = _fanout.fan_out(q or "", sources=_fanout.DEFAULT_FANOUT_SOURCES)
    external = _unify_pairs(unify_external, fanout.pairs)
    result = _merge(q, curated, external, ["recipes", *fanout.sources_ok], list(fanout.sources_degraded))
    if fanout.late:
        schedule_late_merge(q, sources, curated, external, fanout)
    return result


def _merge(
    q: str | None, curated: list, external: list, sources_ok: list[str], sources_degraded: list[str]
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    from app.services.metasearch import merge_unified
    from app.services.metasearch_card_contract import apply_card_contract

    payload = merge_unified(
        curated, external, query=q, sources_ok=sources_ok, sources_degraded=sources_degraded
    ).to_dict()
    return (
        apply_card_contract(payload["skills"]),
        payload.get("sources_ok", []),
        payload.get("sources_degraded", []),
    )


def _unify_pairs(unify_external: Any, pairs: Any) -> list:
    out = []
    for skill, raw in pairs:
        unified = _safe_unify(unify_external, skill, raw)
        if unified is not None:
            out.append(unified)
    return out


def build_unified_own_session(q: str | None) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """``build_unified`` on a fresh session — for any compute that may outlive
    the request that started it (SWR refresh, MCP warm)."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        return build_unified(db, q)
    finally:
        db.close()


def _safe_unify(unify_external: Any, skill: Any, raw: Any) -> Any:
    """One external row → UnifiedSkill, or None when the row is malformed.

    A single upstream row with a wrong-typed field (a list where a title
    belongs) used to raise inside the merge and fail the WHOLE compute — every
    source's rows lost for one bad hit (fed1004 R1). The row is dropped and
    logged instead; the other rows ship. ``origin_url`` and ``description`` may
    be None: the card contract normalises both, and a fetch-origin row without
    a page URL is still installable.
    """
    try:
        unified = unify_external(skill, raw_row=raw)
    # Rationale: one malformed upstream row must never fail the whole fan-out.
    except Exception:  # noqa: BLE001
        logger.warning(
            "dropping malformed federated row from %s", getattr(skill, "source", "?"), exc_info=True
        )
        return None
    for field in ("slug", "title", "source", "install_ref"):
        if not isinstance(getattr(unified, field, None), str):
            logger.warning(
                "dropping federated row with non-string %s from %s", field, getattr(unified, "source", "?")
            )
            return None
    for field in ("origin_url", "description"):
        if not isinstance(getattr(unified, field, None), (str, type(None))):
            logger.warning(
                "dropping federated row with non-string %s from %s", field, getattr(unified, "source", "?")
            )
            return None
    return unified


def _late_rows(fanout: Any, done: set) -> tuple[list[str], list[tuple[Any, dict]]]:
    landed: list[str] = []
    pairs: list[tuple[Any, dict]] = []
    for fut in done:
        try:
            res = fut.result()
        # Rationale: a straggler that raised is simply not merged.
        except Exception:  # noqa: BLE001
            continue
        if not getattr(res, "ok", False):
            continue
        landed.append(fanout.late[fut])
        rows = res.raw_rows
        pairs += [(skill, rows[i] if i < len(rows) else {}) for i, skill in enumerate(res.skills)]
    return landed, pairs


def merge_late_results(
    q: str | None, sources: tuple[str, ...], curated: list, external: list, fanout: Any
) -> bool:
    """Wait (bounded) for the late sources, then CAS-merge their rows into the
    cached entry for ``(q, sources)``. Returns True iff the cache was upgraded.

    The upgrade lands only if the cached entry still lists a landed source as
    degraded (an entry that already has it — a newer compute — is left alone)
    and only through ``put_if_current`` against that entry's seq.
    """
    from app.services.metasearch import unify_external
    from app.services.metasearch_cache import get_cache

    done, _ = concurrent.futures.wait(list(fanout.late), timeout=LATE_GRACE_S)
    landed, pairs = _late_rows(fanout, done)
    if not landed:
        return False
    ok = ["recipes", *fanout.sources_ok, *landed]
    degraded = [src for src in fanout.sources_degraded if src not in landed]
    skills, ok, degraded = _merge(q, curated, external + _unify_pairs(unify_external, pairs), ok, degraded)
    cache = get_cache()
    deadline = time.monotonic() + _ENTRY_WAIT_S
    lookup = cache.get_entry(q or "", sources, _count=False)
    while lookup.entry is None and time.monotonic() < deadline:
        time.sleep(0.05)
        lookup = cache.get_entry(q or "", sources, _count=False)
    entry = lookup.entry
    if entry is None or not set(landed) & set(entry.sources_degraded):
        return False
    return cache.put_if_current(
        q or "", sources, skills, expected_seq=entry.seq, sources_ok=ok, sources_degraded=degraded
    )


def schedule_late_merge(
    q: str | None, sources: tuple[str, ...], curated: list, external: list, fanout: Any
) -> bool:
    """Run ``merge_late_results`` on a daemon thread holding a late slot.
    False when every slot is busy or the thread cannot start (slot released)."""
    if not _late_slots.acquire(blocking=False):
        return False

    def _run() -> None:
        try:
            merge_late_results(q, sources, curated, external, fanout)
        # Rationale: the late-merge is a best-effort upgrade; failure only means
        # the late rows arrive with the next compute instead.
        except Exception:  # noqa: BLE001
            logger.warning("metasearch late-merge failed for %r", q, exc_info=True)
        finally:
            _late_slots.release()

    try:
        threading.Thread(target=_run, name="metasearch-late-merge", daemon=True).start()
    except BaseException:  # noqa: BLE001 — RuntimeError("can't start new thread")
        _late_slots.release()
        return False
    return True


def warm(q: str | None, sources: tuple[str, ...]) -> None:
    """Fill or refresh the cache entry for ``(q, sources)`` ON THE CALLING THREAD.

    - fresh entry → nothing to do;
    - stale entry → ``refresh_now``: synchronous, per-key guarded, CAS-stored;
    - miss → ``get_or_compute``: synchronous single-flight with any other
      caller of the same key in this process.

    Synchronous on purpose: a caller that bounds its concurrency (MCP warm
    slots) holds its slot for exactly as long as the fan-out runs.
    """
    from app.services.metasearch_cache import get_cache

    cache = get_cache()

    def _compute() -> tuple[list[dict[str, Any]], list[str], list[str]]:
        return build_unified_own_session(q)

    lookup = cache.get_entry(q or "", sources, _count=False)
    if lookup.entry is not None and lookup.entry.fresh:
        return
    if lookup.entry is not None and lookup.entry.stale:
        cache.refresh_now((q or "", sources), _compute, expected_seq=lookup.entry.seq)
        return
    cache.get_or_compute((q or "", sources), _compute, refresh_fn=_compute)
