"""Graph extension (Phase B.5) — three new edge types on top of G15-G17.

The Stage 1-3 graph already exposes:
  - `related_skills`      — declared in SKILL.md frontmatter
  - `tag_overlap`         — Jaccard-of-tags signal in skill_derived_edges
  - `co_install`          — same-api-key co-installs signal
  - `category_sibling`    — same-category signal (also in skill_derived_edges)

This module adds three more, all surfaced through GET /api/graph/related:

  - `failed_after`        — skill A run, skill B run within 5 min, B failed.
                            Derived from the skill-error population of
                            `incident_reports` (report_type='skill_error'), not
                            from the whole table — a crash report from
                            POST /api/feedback/incident is LoopSkill's own tooling
                            dying and is not evidence about two marketplace
                            skills. The table is created in a sibling task
                            (B.1 auto-improve); we check existence at runtime so
                            the endpoint degrades to [] when the data isn't there
                            yet.

  - `arch_compatible_with`— skills that succeed on the same host fingerprint.
                            Derived from `install_events.host_fingerprint`,
                            which lands in A.9. Degrades to [] until then.

  - `replaced_by`         — manual curator edits (skill_replacements) +
                            pending `replacement_candidates`. The auto-detect
                            sweep that was meant to populate the candidates
                            table was deleted (t_e4c8c189): it hardcoded
                            `incident_reports.skill_slug`, a column only the
                            test scaffolding ever created — the shipped schema
                            keys by `skill_id` — so its bare `except` laundered
                            UndefinedColumn into "0 candidates" forever, and it
                            had no production caller (no cron, no script, no
                            unit). The read path stays: a candidate inserted
                            manually still surfaces here.

Public access (no auth) — keep the API consistent with /api/skills/{slug}/graph.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.models import (
    IncidentReport,
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
    #
    # bundles_0811 P6: `incident_reports` exists in TWO shapes and this query
    # only ever addressed one of them.
    #
    #   models.py IncidentReport (the REAL, shipped schema) keys by `skill_id`
    #   tests/test_graph_extension.py::_ensure_incident_reports_table drops that
    #   table and recreates a `skill_slug`-shaped one for derivation tests
    #
    # The query hardcoded `skill_slug`, so against the real schema it raised
    # UndefinedColumn — invisible since v1.0.0 because the bare `except` below
    # swallowed it and returned []. On Postgres a failed statement ABORTS THE
    # WHOLE TRANSACTION, so swallowing was not enough: every later query in the
    # same session died with InFailedSqlTransaction. That is why P6's
    # neighborhood endpoint — the first caller to reach this on the Postgres CI
    # leg — failed at an unrelated `db.query(Skill)` several frames away.
    # SQLite tolerates the sequence, which is why it never reproduced locally.
    #
    # Detect the shape rather than assuming either one, and roll back on failure
    # so a swallowed error can never poison the caller's session.
    #
    # t_0111f6c4 (2026-11-16): a THIRD dimension of that shape-split is
    # provenance. The real schema holds two populations under one table — agent
    # skill errors, and crash reports of LoopSkill's own tooling from
    # POST /api/feedback/incident (see the IncidentReport class docstring). A
    # crash report says LoopSkill's tooling died; it says nothing about a
    # `failed_after` relationship between two marketplace skills, and this feeds
    # the public no-auth GET /api/graph/related. So the real-shape branch filters
    # to skill errors. The test-shape branch cannot: that table has no
    # report_type column at all, which is why the filter is gated on
    # _column_exists the same way ts_col is rather than assumed. Provenance
    # filtering is therefore a property of the REAL schema; asserting it belongs
    # to tests that use models.IncidentReport, not to these derivation tests.
    _prov = ""
    if _column_exists(db, "incident_reports", "report_type"):
        _prov = " AND report_type = :prov"
    if _column_exists(db, "incident_reports", "skill_slug"):
        incident_sql = f"SELECT {ts_col} FROM incident_reports WHERE skill_slug = :slug{_prov}"
    elif _column_exists(db, "incident_reports", "skill_id"):
        incident_sql = (
            f"SELECT i.{ts_col} FROM incident_reports i "
            f"JOIN skills s ON s.id = i.skill_id WHERE s.slug = :slug{_prov}"
        )
    else:
        return []

    try:
        params: dict[str, object] = {"slug": skill_slug}
        if _prov:
            # Module-level import, deliberately NOT re-imported here: an import
            # placed inside this try would be laundered into [] by the same
            # blanket except that the P6 comment above exists to warn about.
            params["prov"] = IncidentReport.TYPE_SKILL_ERROR
        incident_rows = db.execute(text(incident_sql), params).fetchall()
    # Rationale: incident_reports may be absent or differently shaped on an older
    # schema. Roll back first — on Postgres the failed statement has already
    # aborted the transaction, and returning [] without a rollback hands the
    # caller a session in which every subsequent query fails.
    except Exception:  # noqa: BLE001
        db.rollback()
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
