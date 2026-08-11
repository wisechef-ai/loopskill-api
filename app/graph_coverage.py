"""Skill-graph honest coverage reporting (bundles0811 P6).

The graph builder (`app.edge_builder`) existed but was NEVER run in
production — `skill_derived_edges` measured 0 rows on 2026-08-11. This
module answers the question the plan actually gates on: *of everything that
COULD be an edge, how much IS one, per signal type* — and refuses to report
a number it did not compute.

Per-edge-type scope decisions (all deliberate; see this phase's PR body for
the full reasoning):

  - ``tag_overlap`` / ``category_sibling``: LOCAL-only. `build_edges` only
    ever queries `Skill` (the local catalog — 64 public rows @ 2026-08-11).
    Federated tracks (90,605 rows in `federation_hub_skills`) are never
    scanned for these signals — widening the existing O(N^2) builder to
    that set is 8.2 BILLION comparisons and is explicitly NOT attempted
    here (see `app.edge_builder` module docstring).

  - ``related_skills`` (declared cross-refs): LOCAL-only, same reason —
    SKILL.md frontmatter is read from `skill_versions.skill_toml`, which
    only exists for local skills. Federated cross-ref extraction (reading
    frontmatter from licence-fetchable / `fetch_origin` federated origins)
    is DEFERRED — see the PR body. The eligible-origin count for that
    deferred slice (real, computed) is reported here so the gap is honest
    rather than silent.

  - ``co_install``: LOCAL-ONLY, and not by choice — by CONTRACT.
    `InstallEvent.skill_id` is a NOT NULL foreign key to `skills.id`; the
    install-event pipeline physically cannot record a federated install
    with a federated identity. Reporting co-install coverage over the
    federated set would fabricate identities the DB never captured.

  - ``bundle_co_membership``: the one edge type that DOES span federated
    identities honestly. `bundle_skills.federated_source` /
    `.federated_slug` already carry federated track identity (spotify_2607
    Phase A) — no new fetch, no body ever read. Eligible/covered are a
    direct GROUP BY over `bundle_skills`, bounded by
    sum(C(bundle_size, 2)) across all bundles (small — nowhere near the
    O(N^2) trap; 16 bundles, largest observed 53 members => ~1.4k pairs).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import BundleSkill, FederationHubSkill, InstallEvent, Skill, SkillDerivedEdge

# Edge types this module reports coverage for. Kept separate from
# `app.graph_extension.EDGE_TYPES` (the HTTP-surfaced set) because coverage
# reporting includes deferred/aspirational types (federated cross-refs) that
# have no live query endpoint yet.
COVERAGE_EDGE_TYPES = (
    "tag_overlap",
    "category_sibling",
    "co_install",
    "related_skills",
    "bundle_co_membership",
)


def _local_public_slugs(db: Session) -> set[str]:
    rows = db.query(Skill.slug).filter(Skill.is_public == True).all()  # noqa: E712
    return {r[0] for r in rows}


def _last_built_at(db: Session) -> datetime | None:
    return db.query(func.max(SkillDerivedEdge.last_built_at)).scalar()


def _signal_coverage(db: Session, signal_key: str, eligible: set[str]) -> tuple[int, int]:
    """Count distinct source_slugs with a nonzero `signal_key` in `signals`.

    Restricted to `eligible` (public local slugs) so a stale row for a
    since-unpublished skill can't inflate coverage.
    """
    if not eligible:
        return 0, 0
    rows = (
        db.query(SkillDerivedEdge.source_slug, SkillDerivedEdge.signals)
        .filter(SkillDerivedEdge.source_slug.in_(eligible))
        .all()
    )
    covered: set[str] = set()
    for slug, signals in rows:
        sig = signals or {}
        if float(sig.get(signal_key) or 0.0) > 0.0:
            covered.add(slug)
    return len(eligible), len(covered)


def _pct(covered: int, eligible: int) -> float:
    if eligible <= 0:
        return 0.0
    return round(100.0 * covered / eligible, 2)


def _tag_overlap_coverage(db: Session, eligible: set[str], built_at) -> dict:
    e, c = _signal_coverage(db, "jaccard", eligible)
    return {
        "eligible_nodes": e,
        "covered_nodes": c,
        "coverage_pct": _pct(c, e),
        "last_built_at": built_at.isoformat() if built_at else None,
        "scope": "local-only",
        "note": (
            "Local public catalog only (64 skills @ 2026-08-11). Widening to the "
            "90,605-row federation_hub_skills set is an O(N^2) trap — see "
            "app.edge_builder module docstring."
        ),
    }


def _category_sibling_coverage(db: Session, eligible: set[str], built_at) -> dict:
    e, c = _signal_coverage(db, "category", eligible)
    return {
        "eligible_nodes": e,
        "covered_nodes": c,
        "coverage_pct": _pct(c, e),
        "last_built_at": built_at.isoformat() if built_at else None,
        "scope": "local-only",
        "note": "Local public catalog only — same scope reasoning as tag_overlap.",
    }


def _co_install_coverage(db: Session, eligible: set[str], built_at) -> dict:
    # Eligible = local public skills that have >=1 recorded install event.
    # InstallEvent.skill_id is a NOT NULL FK to skills.id, so this can never
    # include a federated identity — reported explicitly as LOCAL-ONLY.
    installed_slugs = {
        r[0]
        for r in db.query(InstallEvent.skill_slug)
        .filter(InstallEvent.skill_slug.isnot(None))
        .filter(InstallEvent.skill_slug.in_(eligible))
        .distinct()
        .all()
    }
    e_count, c = _signal_coverage(db, "coinstall", installed_slugs)
    return {
        "eligible_nodes": e_count,
        "covered_nodes": c,
        "coverage_pct": _pct(c, e_count),
        "last_built_at": built_at.isoformat() if built_at else None,
        "scope": "local-only",
        "note": (
            "LOCAL-ONLY by CONTRACT, not choice: InstallEvent.skill_id is a NOT "
            "NULL FK to skills.id — the install-event pipeline cannot record a "
            "federated install identity. Reporting this over the federated set "
            "would fabricate identities the DB never captured."
        ),
    }


def _related_skills_coverage(db: Session, eligible: set[str]) -> dict:
    # Declared cross-refs are read live off Skill.related_skills — not a
    # "build" step, so last_built_at is genuinely None here.
    skills = db.query(Skill.slug, Skill.related_skills).filter(Skill.slug.in_(eligible)).all()
    covered = {slug for slug, rel in skills if rel}
    e = len(eligible)
    c = len(covered)

    # Real, computed eligible-origin count for the DEFERRED federated slice —
    # licence-fetchable (install_path='fetch_origin') federated rows. Zero
    # bodies are fetched to produce this number; it's a plain COUNT(*).
    deferred_eligible = (
        db.query(func.count(FederationHubSkill.id))
        .filter(FederationHubSkill.install_path == "fetch_origin")
        .scalar()
        or 0
    )

    return {
        "eligible_nodes": e,
        "covered_nodes": c,
        "coverage_pct": _pct(c, e),
        "last_built_at": None,
        "scope": "local-only",
        "note": (
            "Local public catalog only (declared `related_skills` frontmatter). "
            "Federated cross-ref extraction is DEFERRED this phase — "
            f"{deferred_eligible} licence-fetchable (install_path='fetch_origin') "
            "federated origins are eligible for a future pass, 0 covered today, "
            "0 bodies fetched to produce this count."
        ),
        "deferred_federated_eligible_origins": deferred_eligible,
    }


def _bundle_co_membership_coverage(db: Session) -> dict:
    """The one edge type that spans federated identities honestly.

    Identity = local skill_id (as `str(uuid)`) OR `f"{federated_source}:{federated_slug}"`
    (matches the convention already used by BundleSkill/SkillLike elsewhere
    in this codebase). Eligible = any identity appearing in >=1 bundle.
    Covered = any identity that shares a bundle with >=1 OTHER identity
    (i.e. actually produces a co-membership pair). Pure GROUP BY over
    `bundle_skills` — no O(N^2) risk (bounded by bundle size, not catalog
    size) and zero federated bodies ever read.
    """
    rows = db.query(
        BundleSkill.bundle_id,
        BundleSkill.skill_id,
        BundleSkill.federated_source,
        BundleSkill.federated_slug,
    ).all()

    by_bundle: dict = {}
    all_identities: set[str] = set()
    for bundle_id, skill_id, fed_source, fed_slug in rows:
        if skill_id is not None:
            identity = str(skill_id)
        elif fed_source and fed_slug:
            identity = f"{fed_source}:{fed_slug}"
        else:
            continue  # malformed row (shouldn't happen — CHECK constraint enforces XOR)
        all_identities.add(identity)
        by_bundle.setdefault(bundle_id, set()).add(identity)

    covered: set[str] = set()
    for members in by_bundle.values():
        if len(members) >= 2:
            covered |= members

    e = len(all_identities)
    c = len(covered)
    return {
        "eligible_nodes": e,
        "covered_nodes": c,
        "coverage_pct": _pct(c, e),
        "last_built_at": None,  # live GROUP BY, not a batch-built table
        "scope": "all-indexed-identities (local + federated)",
        "note": (
            f"Computed live over {len(by_bundle)} bundles via GROUP BY on "
            "bundle_skills — no O(N^2) scan (bounded by per-bundle size), zero "
            "federated bodies fetched."
        ),
    }


def compute_coverage(db: Session) -> dict[str, dict]:
    """Return the honest per-edge-type coverage table.

    Every field is computed from a live query against the bound session —
    nothing here is a placeholder or an estimate.
    """
    eligible = _local_public_slugs(db)
    built_at = _last_built_at(db)
    return {
        "tag_overlap": _tag_overlap_coverage(db, eligible, built_at),
        "category_sibling": _category_sibling_coverage(db, eligible, built_at),
        "co_install": _co_install_coverage(db, eligible, built_at),
        "related_skills": _related_skills_coverage(db, eligible),
        "bundle_co_membership": _bundle_co_membership_coverage(db),
    }
