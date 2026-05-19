"""Graph extension (Phase B.5) — three new edge types on top of G15-G17.

The Stage 1-3 graph already exposes:
  - `related_skills`      — declared in SKILL.md frontmatter
  - `tag_overlap`         — Jaccard-of-tags signal in skill_derived_edges
  - `co_install`          — same-api-key co-installs signal
  - `category_sibling`    — same-category signal (also in skill_derived_edges)

This module adds three more, all surfaced through GET /api/graph/related:

  - `failed_after`        — skill A run, skill B run within 5 min, B failed.
                            Derived from `incident_reports`. The table is
                            created in a sibling task (B.1 auto-improve); we
                            check existence at runtime so the endpoint
                            degrades to [] when the data isn't there yet.

  - `arch_compatible_with`— skills that succeed on the same host fingerprint.
                            Derived from `install_events.host_fingerprint`,
                            which lands in A.9. Degrades to [] until then.

  - `replaced_by`         — manual curator edits (skill_replacements) +
                            auto-detected pending review (replacement_candidates,
                            populated by `sweep_replacement_candidates`).

Public access (no auth) — keep the API consistent with /api/skills/{slug}/graph.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.models import (
    InstallEvent,
    ReplacementCandidate,
    Skill,
    SkillDerivedEdge,
    SkillReplacement,
)

# ── Tunables (B.5) ────────────────────────────────────────────────────────

# `failed_after`: window between skill A's run and skill B's failure for the
# pair to count as a co-occurrence. 5 minutes per spec.
FAILED_AFTER_WINDOW = timedelta(minutes=5)

# Replacement-candidate sweep: lookback for incident counts.
REPLACEMENT_LOOKBACK = timedelta(days=30)
# Min share of recent incidents in the source skill to flag it.
INCIDENT_SHARE_MIN = 0.5
# Min co_invoked edge weight (we reuse co_install as the co-invoked proxy).
CO_INVOKED_MIN = 0.3


# ── Edge-type registry ────────────────────────────────────────────────────

EDGE_TYPES = {
    "related_skills",
    "tag_overlap",
    "co_install",
    "category_sibling",
    "failed_after",
    "arch_compatible_with",
    "replaced_by",
}


# ── Defensive table/column probes ─────────────────────────────────────────


def _table_exists(db: Session, table_name: str) -> bool:
    """Return True iff the given table exists in the bound database.

    Works on both Postgres and SQLite. We use SQLAlchemy's inspector so we
    don't have to special-case `to_regclass` vs `sqlite_master`.
    """
    bind = db.get_bind()
    insp = inspect(bind)
    try:
        return insp.has_table(table_name)
    # Rationale: table existence probe; older DBs may not have all tables yet
    except Exception:  # noqa: BLE001
        return False


def _column_exists(db: Session, table_name: str, column_name: str) -> bool:
    bind = db.get_bind()
    insp = inspect(bind)
    try:
        cols = {c["name"] for c in insp.get_columns(table_name)}
        return column_name in cols
    # Rationale: column existence probe; any SQLAlchemy inspector error → assume absent
    except Exception:  # noqa: BLE001
        return False


def failed_after_edges(
    db: Session,
    skill_slug: str,
    *,
    min_weight: float = 0.0,
) -> list[dict]:
    """For target_skill = `skill_slug`, count incident rows where the run was
    preceded by another skill within FAILED_AFTER_WINDOW.

    Weight = co-occurrence count / total incidents for `skill_slug` in window.
    Returns rows shaped as the public endpoint contract:
        [{skill_slug, edge_type, weight, evidence_count}]

    Defensive: if `incident_reports` table doesn't exist (B.1 not landed),
    returns []. Same for missing rows.
    """
    if not _table_exists(db, "incident_reports"):
        return []

    # We expect incident_reports columns:
    #   skill_slug, signature, occurred_at  (per B.1 spec)
    # Defensive: tolerate either `failed_at` or `occurred_at`.
    ts_col = (
        "occurred_at"
        if _column_exists(db, "incident_reports", "occurred_at")
        else "failed_at"
        if _column_exists(db, "incident_reports", "failed_at")
        else "created_at"
    )

    # Pull rows into Python and do the windowed join in memory. The corpus
    # is small (incidents per skill ≪ 10k) and this avoids dialect
    # differences in interval arithmetic between Postgres and SQLite (the
    # latter has no native INTERVAL type).
    try:
        incident_rows = db.execute(
            text(f"SELECT {ts_col} FROM incident_reports WHERE skill_slug = :slug"),
            {"slug": skill_slug},
        ).fetchall()
    # Rationale: incident_reports table may not exist on older schema; return empty list
    except Exception:  # noqa: BLE001
        return []

    if not incident_rows:
        return []

    incidents = [r[0] for r in incident_rows if r[0] is not None]
    if not incidents:
        return []

    # Materialise incidents as datetimes (SQLite returns strings; PG returns
    # datetime objects). Normalise to naive UTC so timedelta arithmetic is
    # portable across dialects with mixed tz-awareness.
    def _naive(dt: datetime) -> datetime:
        if dt.tzinfo is not None:
            return dt.astimezone(UTC).replace(tzinfo=None)
        return dt

    norm: list[datetime] = []
    for t in incidents:
        if isinstance(t, datetime):
            norm.append(_naive(t))
        else:
            try:
                norm.append(_naive(datetime.fromisoformat(str(t))))
            except ValueError:
                continue
    if not norm:
        return []

    # Pull install events that could possibly precede any incident. Bound
    # the lower edge so we don't drag the whole table on busy skills.
    earliest = min(norm) - FAILED_AFTER_WINDOW
    latest = max(norm)
    install_rows = (
        db.query(InstallEvent.skill_slug, InstallEvent.created_at)
        .filter(InstallEvent.skill_slug.isnot(None))
        .filter(InstallEvent.skill_slug != skill_slug)
        .filter(InstallEvent.created_at >= earliest)
        .filter(InstallEvent.created_at <= latest)
        .all()
    )

    # For each predecessor slug, count distinct incidents that had at least
    # one install of that slug within the preceding window.
    counts: dict[str, int] = defaultdict(int)
    for incident_at in norm:
        seen_for_incident: set[str] = set()
        window_start = incident_at - FAILED_AFTER_WINDOW
        for predecessor_slug, ie_at in install_rows:
            if ie_at is None:
                continue
            ie_dt = ie_at if isinstance(ie_at, datetime) else (datetime.fromisoformat(str(ie_at)))
            ie_dt = _naive(ie_dt)
            if window_start <= ie_dt <= incident_at and predecessor_slug not in seen_for_incident:
                counts[predecessor_slug] += 1
                seen_for_incident.add(predecessor_slug)

    total = len(norm)
    out: list[dict] = []
    for predecessor_slug, hits in counts.items():
        weight = float(hits) / float(total) if total else 0.0
        if weight < min_weight:
            continue
        out.append(
            {
                "skill_slug": predecessor_slug,
                "edge_type": "failed_after",
                "weight": round(weight, 4),
                "evidence_count": int(hits),
            }
        )
    out.sort(key=lambda e: e["weight"], reverse=True)
    return out


def arch_compatible_edges(
    db: Session,
    skill_slug: str,
    *,
    min_weight: float = 0.0,
) -> list[dict]:
    """Skills that share host fingerprints with the given skill.

    Weight = jaccard of host fingerprints. Returns [] when the
    `host_fingerprint` column doesn't exist on `install_events` (A.9 hasn't
    landed). Once the column lands and rows accumulate, this fires
    automatically.
    """
    if not _column_exists(db, "install_events", "host_fingerprint"):
        return []

    sql = text(
        """
        SELECT skill_slug, host_fingerprint
        FROM install_events
        WHERE host_fingerprint IS NOT NULL
          AND skill_slug IS NOT NULL
        """
    )
    try:
        rows = db.execute(sql).fetchall()
    # Rationale: co-install query uses install_events table which may not exist on all DBs
    except Exception:  # noqa: BLE001
        return []

    by_slug: dict[str, set] = defaultdict(set)
    for slug, fp in rows:
        by_slug[slug].add(fp)

    target = by_slug.get(skill_slug)
    if not target:
        return []

    out: list[dict] = []
    for other_slug, fps in by_slug.items():
        if other_slug == skill_slug:
            continue
        inter = target & fps
        union = target | fps
        if not union:
            continue
        weight = len(inter) / len(union)
        if weight < min_weight or weight <= 0:
            continue
        out.append(
            {
                "skill_slug": other_slug,
                "edge_type": "arch_compatible_with",
                "weight": round(weight, 4),
                "evidence_count": len(inter),
            }
        )
    out.sort(key=lambda e: e["weight"], reverse=True)
    return out


def replaced_by_edges(
    db: Session,
    skill_slug: str,
    *,
    min_weight: float = 0.0,
    include_candidates: bool = True,
) -> list[dict]:
    """Manual `skill_replacements` rows + pending `replacement_candidates`.

    Manual edges carry weight=1.0 (curator-confirmed); candidate edges carry
    a weight derived from the evidence (capped at 0.9 so manual always
    sorts first). Candidates with status != 'pending' are excluded.
    """
    src = db.query(Skill).filter(Skill.slug == skill_slug).first()
    if not src:
        return []

    out: list[dict] = []

    # Manual replacements
    manual_rows = (
        db.query(SkillReplacement, Skill)
        .join(Skill, Skill.id == SkillReplacement.target_id)
        .filter(SkillReplacement.source_id == src.id)
        .all()
    )
    for _repl, tgt in manual_rows:
        if 1.0 < min_weight:
            continue
        out.append(
            {
                "skill_slug": tgt.slug,
                "edge_type": "replaced_by",
                "weight": 1.0,
                "evidence_count": 1,
            }
        )

    # Auto-detected (pending) candidates
    if include_candidates:
        cand_rows = (
            db.query(ReplacementCandidate, Skill)
            .join(Skill, Skill.id == ReplacementCandidate.target_id)
            .filter(
                ReplacementCandidate.source_id == src.id,
                ReplacementCandidate.status == "pending",
            )
            .all()
        )
        for cand, tgt in cand_rows:
            ev = cand.evidence_json or {}
            # Confidence proxy — average of incident_share and co_invoke_weight,
            # capped at 0.9 so a confirmed manual edge always wins.
            inc = float(ev.get("incident_share", 0.0))
            cow = float(ev.get("co_invoke_weight", 0.0))
            weight = round(min(0.9, (inc + cow) / 2.0), 4)
            if weight < min_weight:
                continue
            out.append(
                {
                    "skill_slug": tgt.slug,
                    "edge_type": "replaced_by",
                    "weight": weight,
                    "evidence_count": int(ev.get("incident_count", 0)),
                }
            )

    out.sort(key=lambda e: e["weight"], reverse=True)
    return out


# ── Existing-edge surfacers (project the G15-G17 storage onto the new contract)


def declared_edges(db: Session, skill_slug: str, min_weight: float = 0.0) -> list[dict]:
    """Project Skill.related_skills (G15) onto the public contract."""
    skill = db.query(Skill).filter(Skill.slug == skill_slug).first()
    if not skill or not skill.related_skills:
        return []
    out: list[dict] = []
    raw = skill.related_skills or []
    # Manual declarations are full-confidence — weight=1.0
    for slug in raw:
        if not isinstance(slug, str) or not slug.strip() or slug == skill_slug:
            continue
        if 1.0 < min_weight:
            continue
        out.append(
            {
                "skill_slug": slug.strip().lower(),
                "edge_type": "related_skills",
                "weight": 1.0,
                "evidence_count": 1,
            }
        )
    return out


def _signal_edges(
    db: Session,
    skill_slug: str,
    signal_key: str,
    edge_type: str,
    min_weight: float,
) -> list[dict]:
    """Surface a single sub-signal from skill_derived_edges as its own edge type.

    The G16 builder writes one row per directed pair with three signals
    bundled in `signals`. We split them out here so the new endpoint can
    answer queries like "edges based on tag_overlap only".
    """
    rows = db.query(SkillDerivedEdge).filter(SkillDerivedEdge.source_slug == skill_slug).all()
    out: list[dict] = []
    for r in rows:
        sig = r.signals or {}
        weight = float(sig.get(signal_key) or 0.0)
        if weight <= 0 or weight < min_weight:
            continue
        out.append(
            {
                "skill_slug": r.target_slug,
                "edge_type": edge_type,
                "weight": round(weight, 4),
                "evidence_count": 1,
            }
        )
    out.sort(key=lambda e: e["weight"], reverse=True)
    return out


def tag_overlap_edges(db: Session, skill_slug: str, min_weight: float = 0.0):
    """Return tag-overlap edges for a skill."""
    return _signal_edges(db, skill_slug, "jaccard", "tag_overlap", min_weight)


def co_install_edges(db: Session, skill_slug: str, min_weight: float = 0.0):
    """Return co-install edges for a skill."""
    return _signal_edges(db, skill_slug, "coinstall", "co_install", min_weight)


def category_sibling_edges(
    db: Session,
    skill_slug: str,
    min_weight: float = 0.0,
) -> list[dict]:
    """Same-category neighbours.

    Primary path: split out the `category` sub-signal from skill_derived_edges
    (the same path used for tag_overlap / co_install).

    Fallback: when nothing matches that path (e.g. an isolated skill that
    fell below the combined-edge threshold but still has same-category
    siblings), drop to the Cognee semantic-graph if it's importable, else
    to a plain DB query of skills sharing this category. Cognee is treated
    as an optional dependency — `import` is wrapped because it isn't a
    runtime dep here.
    """
    primary = _signal_edges(db, skill_slug, "category", "category_sibling", min_weight)
    if primary:
        return primary

    skill = db.query(Skill).filter(Skill.slug == skill_slug).first()
    if not skill or not skill.category:
        return []

    # Try Cognee first (optional dep — if unavailable, use the DB fallback).
    try:
        import cognee  # type: ignore  # noqa: F401
        # If Cognee is wired into this deployment, plug its query in here.
        # We don't ship the integration in this PR — empty the result and
        # fall through to the DB path so the contract stays consistent.
    # Rationale: Cognee is an optional dependency; ImportError expected on most deployments
    except Exception:  # noqa: BLE001
        pass

    siblings = (
        db.query(Skill)
        .filter(
            Skill.category == skill.category,
            Skill.slug != skill_slug,
            Skill.is_public == True,  # noqa: E712
        )
        .all()
    )
    out: list[dict] = []
    fallback_weight = 0.2  # category-only signal weight (matches G16's W_CATEGORY)
    if fallback_weight < min_weight:
        return []
    for s in siblings:
        out.append(
            {
                "skill_slug": s.slug,
                "edge_type": "category_sibling",
                "weight": fallback_weight,
                "evidence_count": 1,
            }
        )
    return out


# ── Dispatch ──────────────────────────────────────────────────────────────


def edges_for(
    db: Session,
    skill_slug: str,
    edge_type: str,
    min_weight: float = 0.0,
) -> list[dict]:
    """Return the canonical edge list for one (skill, edge_type) pair."""
    if edge_type == "related_skills":
        return declared_edges(db, skill_slug, min_weight)
    if edge_type == "tag_overlap":
        return tag_overlap_edges(db, skill_slug, min_weight)
    if edge_type == "co_install":
        return co_install_edges(db, skill_slug, min_weight)
    if edge_type == "category_sibling":
        return category_sibling_edges(db, skill_slug, min_weight)
    if edge_type == "failed_after":
        return failed_after_edges(db, skill_slug, min_weight=min_weight)
    if edge_type == "arch_compatible_with":
        return arch_compatible_edges(db, skill_slug, min_weight=min_weight)
    if edge_type == "replaced_by":
        return replaced_by_edges(db, skill_slug, min_weight=min_weight)
    raise ValueError(f"unknown edge_type {edge_type!r}")


# ── Replacement-candidate sweep (cron entry point) ────────────────────────


def sweep_replacement_candidates(db: Session) -> int:
    """Walk recent incidents and propose replacement candidates for review.

    Logic per B.5 spec:
      - For each skill X, count incidents in the last 30 days.
      - If X's incidents are >50% of all incidents for X-or-similar skills
        in the window AND there exists a co_invoked-edge neighbour Y with
        weight > 0.3 AND Y has a lower incident rate → propose X→Y.

    Returns the number of new pending candidates inserted. Existing
    candidates (matched by source/target pair) are left untouched, so this
    is idempotent across reruns.
    """
    if not _table_exists(db, "incident_reports"):
        return 0

    since = datetime.now(UTC) - REPLACEMENT_LOOKBACK

    # Per-slug recent incident counts. Defensive about the timestamp column.
    ts_col = (
        "occurred_at"
        if _column_exists(db, "incident_reports", "occurred_at")
        else "failed_at"
        if _column_exists(db, "incident_reports", "failed_at")
        else "created_at"
    )
    try:
        rows = db.execute(
            text(
                f"SELECT skill_slug, COUNT(*) FROM incident_reports "
                f"WHERE {ts_col} >= :since GROUP BY skill_slug"
            ),
            {"since": since},
        ).fetchall()
    # Rationale: incident_reports table may not exist on older DBs; return 0 count
    except Exception:  # noqa: BLE001
        return 0
    incident_counts: dict[str, int] = {slug: int(n) for slug, n in rows if slug}
    if not incident_counts:
        return 0
    total = sum(incident_counts.values())

    inserted = 0
    skills_by_slug = {
        s.slug: s for s in db.query(Skill).filter(Skill.slug.in_(list(incident_counts.keys()))).all()
    }
    for slug, count in incident_counts.items():
        share = count / total if total else 0.0
        if share <= INCIDENT_SHARE_MIN:
            continue
        src = skills_by_slug.get(slug)
        if not src:
            continue

        # Find a co_invoked neighbour with weight > 0.3 and lower incident rate.
        edges = (
            db.query(SkillDerivedEdge)
            .filter(SkillDerivedEdge.source_slug == slug)
            .order_by(SkillDerivedEdge.weight.desc())
            .all()
        )
        for e in edges:
            cow = float((e.signals or {}).get("coinstall") or 0.0)
            if cow <= CO_INVOKED_MIN:
                continue
            other_count = incident_counts.get(e.target_slug, 0)
            if other_count >= count:
                continue
            tgt = db.query(Skill).filter(Skill.slug == e.target_slug).first()
            if not tgt:
                continue

            # Skip if a candidate or manual replacement already exists.
            existing = (
                db.query(ReplacementCandidate)
                .filter(
                    ReplacementCandidate.source_id == src.id,
                    ReplacementCandidate.target_id == tgt.id,
                )
                .first()
            )
            if existing:
                continue
            existing_manual = (
                db.query(SkillReplacement)
                .filter(
                    SkillReplacement.source_id == src.id,
                    SkillReplacement.target_id == tgt.id,
                )
                .first()
            )
            if existing_manual:
                continue

            db.add(
                ReplacementCandidate(
                    id=uuid4(),
                    source_id=src.id,
                    target_id=tgt.id,
                    evidence_json={
                        "incident_count": count,
                        "incident_share": round(share, 4),
                        "co_invoke_weight": round(cow, 4),
                        "alternative_incident_count": other_count,
                        "window_days": REPLACEMENT_LOOKBACK.days,
                    },
                    status="pending",
                )
            )
            inserted += 1
            break  # one candidate per source per sweep — keep review queue tractable
    db.flush()
    return inserted
