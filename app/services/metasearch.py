"""Metasearch — the unified query-time router (metasearch_0710 P0).

REPLACES the crawl (`federation_index_cache` + `scripts/federation_reindex.py`)
with a *live* fan-out. The requirement was never "own an index" — it was "let
fleet runners find + deploy skills". The sources own the index; we own the
experience: one ranked, unified list a fleet runner searches once and deploys from.

North-star framing (see obsidian `north_star_plan.md`): metasearch is a FEEDER
to the fleet-deploy motion, not the product. The success metric is
external-search → fleet-deploy → cap-hit, never catalog size.

This module ships the **normalisation + rank + dedupe** half of P0 (the "intact
seam"). The concurrent fan-out orchestrator + token bucket land in
`metasearch_fanout.py` (council condition 1). The two are split for the
600-line-per-file discipline (AGENTS.md).

Design decisions grounded in the 2026-07-10 dual-model council
(COUNCIL_TERRA/SOL in the vault) + Adam's condition answers:

- **C5 fix — retain popularity + canonical identity.** The existing per-source
  mappers (`SkillsShAdapter._map`, `ClawHubAdapter._map`) DISCARD `installs` /
  `stats`. `UnifiedSkill` retains them as `popularity` plus a `canonical_id`
  (normalised repo+path or source-scoped slug) so ranking + dedupe have real
  inputs. `origin_url` (a source *page*) is NOT the dedupe key — `canonical_id`
  is (council C5: page URLs are not artifact identities).
- **Ranking (§10.1):** per-source percentile of `popularity` (missing-signal →
  neutral 0.5 prior), curated gets a fixed above-the-fold boost, ties fall back
  to source priority then title. No cross-source raw-number comparison.
- **Dedupe (§10.2):** collapse on `canonical_id`; keep the highest-priority
  source's row (curated > skills-sh > github > well-known/browse-sh > lobehub >
  clawhub). The kept row inherits the max popularity seen (so a github skill
  also indexed on skills.sh keeps its install signal).
- **Condition 2(b):** ClawHub is `deployable=False` here (searchable + ad-hoc
  install only, never the "Deploy to fleet" button). `deployable` is derived
  from the install path + the source's fleet-deploy eligibility, NOT invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.federation import ExternalSkill, InstallPath, route_install
from app.services.federation_relevance import relevance_tier

# ── Source priority (dedupe tie-break + rank prior) ──────────────────────────
# Lower number = higher priority. Curated ("recipes") always wins. Order below
# the curated row mirrors trust + popularity-signal richness: skills.sh carries
# real install counts; github taps are license-verified OSS; clawhub is the
# supply-chain-unvetted long tail (ClawHavoc incident — see ClawHubAdapter).
_SOURCE_PRIORITY: dict[str, int] = {
    "recipes": 0,  # curated / internal — always above the fold
    "skills-sh": 10,
    "github-oss": 20,
    "well-known": 30,
    "browse-sh": 30,
    "hermes-hub": 25,
    "lobehub": 40,
    "clawhub": 50,
}
_DEFAULT_PRIORITY = 35  # unknown github facet taps land mid-pack

# ── Fleet-deploy eligibility (Adam condition 2b + 3, 2026-07-10) ─────────────
# A source is fleet-deployable only when its artifact can be resolved + pinned
# server-side for reconcile delivery. ClawHub is EXCLUDED in v1 (decision #6 not
# reversed — searchable + ad-hoc install only). skills.sh + github + well-known
# + browse-sh resolve to a real SKILL.md and are eligible. This is the ONLY
# place the v1 fleet-deploy allow-list is expressed.
_FLEET_DEPLOYABLE_SOURCES: frozenset[str] = frozenset(
    {"recipes", "skills-sh", "github-oss", "well-known", "browse-sh", "hermes-hub"}
)


def _source_priority(source: str) -> int:
    if source in _SOURCE_PRIORITY:
        return _SOURCE_PRIORITY[source]
    # GitHub provider facets (superset_0606) share github-oss trust.
    if source.startswith("github"):
        return _SOURCE_PRIORITY["github-oss"]
    return _DEFAULT_PRIORITY


@dataclass(frozen=True)
class UnifiedSkill:
    """One normalised skill card — the schema every source maps into BEFORE
    ranking, so downstream (rank, dedupe, render) never branches on source.

    This is the ``§4.1 UnifiedSkill normaliser`` from the plan, made concrete.
    Curated internal skills and external skills both become this shape; a
    ``source`` of ``"recipes"`` marks curated.
    """

    canonical_id: str
    """Stable cross-source artifact identity for dedupe. For github-backed
    skills: normalised ``owner/repo/path``. For source-scoped rows: ``source:slug``.
    NOT the origin_url (a page). Two rows with the same canonical_id are the same
    skill discovered via different sources."""

    slug: str
    title: str
    description: str
    source: str
    """Namespace label: 'recipes' (curated) | 'skills-sh' | 'clawhub' | 'github:*' …"""

    origin_url: str
    install_ref: str
    """Opaque token the install route resolves (source-specific: skills.sh id,
    clawhub slug, github tree ref, or curated slug)."""

    quality: str  # "curated" | "community"
    deployable: bool
    """True iff this card may show the 'Deploy to fleet' action (v1: not clawhub)."""

    install_path: str  # InstallPath value — fetch_origin | register_mcp | deep_link
    popularity: int | None = None
    """Raw popularity signal (skills.sh installs / github stars / clawhub
    downloads). None when the source exposes none. Normalised to a percentile at
    rank time — never compared raw across sources."""

    license: str | None = None
    updated_at: str | None = None
    # Populated during ranking; not part of the source mapping.
    rank_score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "canonical_id": self.canonical_id,
            "slug": self.slug,
            "title": self.title,
            "description": self.description,
            "source": self.source,
            "origin_url": self.origin_url,
            "install_ref": self.install_ref,
            "quality": self.quality,
            "deployable": self.deployable,
            "install_path": self.install_path,
            "popularity": self.popularity,
            "license": self.license,
            "updated_at": self.updated_at,
            "rank_score": round(self.rank_score, 4),
        }


# ── Canonical identity derivation ────────────────────────────────────────────


def _canonical_id_for_external(
    skill: ExternalSkill, *, popularity_repo: str | None = None, raw_id: str | None = None
) -> str:
    """Derive a stable cross-source identity for an external skill.

    The goal (council C5): a github skill that appears BOTH on skills.sh and as a
    github tap must collapse to ONE canonical id so dedupe works. We key on the
    underlying github ``owner/repo`` when we can recover it, else fall back to a
    source-scoped slug (which never false-merges across sources).

    ``raw_id`` is the source row's UNESCAPED id (skills.sh ``id``/``skillId``) —
    preferred over the slug because the adapter escapes every ``/`` to ``--``,
    which is lossy when a github owner/repo/path legitimately contains ``--``
    (council finding 4: reversing the escaped slug both false-merges and
    mis-splits). ``popularity_repo`` is skills.sh's ``source`` ("owner/repo").
    """
    src = skill.source
    slug = skill.slug
    # skills.sh: prefer the raw, unescaped id ("owner/repo/skill") over the
    # lossy escaped slug. Only fall back to the slug when no raw id is present.
    if src == "skills-sh":
        ident = (raw_id or "").strip() or slug.replace("--", "/")
        parts = [p for p in ident.split("/") if p]
        if len(parts) >= 3:
            return f"gh:{parts[0]}/{parts[1]}/{'/'.join(parts[2:])}"
        if popularity_repo and "/" in popularity_repo:
            return f"gh:{popularity_repo}/{parts[-1] if parts else slug}"
        # No recoverable github identity → source-scoped on the RAW id (not the
        # escaped slug) so two distinct ids don't collapse.
        return f"skills-sh:{ident or slug}"
    # github taps / oss: origin_url is a github tree/repo URL.
    if src.startswith("github"):
        url = skill.origin_url
        if "github.com/" in url:
            tail = url.split("github.com/", 1)[1].strip("/")
            return f"gh:{_normalise_gh_path(tail)}"
        return f"{src}:{slug}"
    # everything else: source-scoped (never false-merges).
    return f"{src}:{slug}"


def _normalise_gh_path(tail: str) -> str:
    """Normalise a github path tail to owner/repo[/subpath], dropping the
    ``/tree/<branch>/`` or ``/blob/<branch>/`` ref segment.

    Council C5: branch-name URLs are not pins — the branch must NOT be part of
    the canonical identity, or the same skill discovered via skills.sh (which
    yields ``owner/repo/skill``) and via a github tap (which yields
    ``owner/repo/tree/main/skill``) will fail to dedupe.
    ``owner/repo/tree/main/skill`` → ``owner/repo/skill``.
    ``owner/repo/blob/v1.2/path/x`` → ``owner/repo/path/x``.
    """
    parts = [p for p in tail.split("/") if p]
    if len(parts) >= 4 and parts[2] in ("tree", "blob"):
        # drop parts[2] (tree|blob) and parts[3] (the branch/ref)
        parts = parts[:2] + parts[4:]
    return "/".join(parts)


def _popularity_of(row: dict | None) -> int | None:
    """Recover a raw popularity signal from the source's ORIGINAL row dict.

    The adapters discard this (council C5), so callers pass the pre-map row.
    skills.sh: ``installs``. clawhub: ``stats.downloads``. github: ``stars`` when
    present. None when absent."""
    if not isinstance(row, dict):
        return None
    if "installs" in row:
        try:
            return int(row["installs"])
        except (TypeError, ValueError):
            return None
    stats = row.get("stats")
    if isinstance(stats, dict):
        for key in ("downloads", "installs", "stars"):
            if key in stats:
                try:
                    return int(stats[key])
                except (TypeError, ValueError):
                    continue
    for key in ("stars", "stargazers_count"):
        if key in row:
            try:
                return int(row[key])
            except (TypeError, ValueError):
                return None
    return None


def unify_external(skill: ExternalSkill, *, raw_row: dict | None = None) -> UnifiedSkill:
    """Normalise one ExternalSkill (+ its original source row for popularity)
    into a UnifiedSkill. ``raw_row`` is the pre-map source dict so we can recover
    the popularity signal the adapter dropped (council C5)."""
    popularity_repo = None
    raw_id = None
    if isinstance(raw_row, dict):
        if skill.source == "skills-sh":
            popularity_repo = raw_row.get("source")
            # Prefer the UNESCAPED id/skillId over the --escaped slug (finding 4).
            raw_id = str(raw_row.get("id") or raw_row.get("skillId") or "") or None
    canonical = _canonical_id_for_external(skill, popularity_repo=popularity_repo, raw_id=raw_id)
    # deployable = the source is on the v1 fleet allow-list AND the install router
    # permits a real (non-deep-link) install. ClawHub fails BOTH gates.
    installable = route_install(skill).allowed
    deployable = installable and skill.source in _FLEET_DEPLOYABLE_SOURCES
    return UnifiedSkill(
        canonical_id=canonical,
        slug=skill.slug,
        title=skill.title,
        description=skill.description,
        source=skill.source,
        origin_url=skill.origin_url,
        install_ref=f"{skill.source}:{skill.slug}",
        quality="community",
        deployable=deployable,
        install_path=skill.install_path.value,
        popularity=_popularity_of(raw_row),
        license=skill.license,
    )


def unify_curated(row: dict) -> UnifiedSkill:
    """Normalise a curated internal skill (a ``_skill_to_out`` dict) into a
    UnifiedSkill. Curated is always ``quality='curated'``, always deployable,
    and carries its internal install_count as the popularity signal."""
    slug = str(row.get("slug", ""))
    return UnifiedSkill(
        canonical_id=f"recipes:{slug}",
        slug=slug,
        title=str(row.get("title", slug)),
        description=str(row.get("description", "") or ""),
        source="recipes",
        origin_url=row.get("origin_url", "") or f"/skills/{slug}",
        install_ref=f"recipes:{slug}",
        quality="curated",
        deployable=True,
        install_path=InstallPath.FETCH_ORIGIN.value,
        popularity=_curated_popularity(row),
        license=row.get("license"),
        updated_at=str(row.get("updated_at")) if row.get("updated_at") else None,
    )


def _curated_popularity(row: dict) -> int | None:
    for key in ("install_count", "installs", "total_installs"):
        if key in row and row[key] is not None:
            try:
                return int(row[key])
            except (TypeError, ValueError):
                continue
    return None


# ── Rank + dedupe ────────────────────────────────────────────────────────────

_CURATED_BOOST = 1.0
"""Fixed above-the-fold boost so a curated skill always outranks an external one
of equal normalised popularity (plan §5.4: curated wins ties, honesty ≠ burying
our own gate)."""


def _percentiles_within_source(skills: list[UnifiedSkill]) -> dict[int, float]:
    """Map each skill (by id()) to its popularity percentile WITHIN its own
    source. Missing-signal → neutral 0.5 prior (council C5: a source with no
    popularity field must not produce an undefined distribution). Single-item or
    all-equal cohorts → 0.5 for every member (no false spread)."""
    by_source: dict[str, list[UnifiedSkill]] = {}
    for s in skills:
        by_source.setdefault(s.source, []).append(s)

    out: dict[int, float] = {}
    for _source, group in by_source.items():
        rated = [s for s in group if s.popularity is not None]
        if len(rated) < 2:
            # Not enough signal to rank within source → neutral for all.
            for s in group:
                out[id(s)] = 0.5
            continue
        # All-equal cohort → no real spread; every member is neutral (council
        # finding 3: guarding only len<2 wrongly gave 3 equal values 0/0.5/1,
        # letting a source mint arbitrary within-source winners via duplicate
        # signals). Distinct-value check is the correct guard.
        if len({s.popularity for s in rated}) == 1:
            for s in group:
                out[id(s)] = 0.5
            continue
        ordered = sorted(rated, key=lambda s: s.popularity or 0)
        n = len(ordered)
        # Tied-rank percentile: members with the SAME popularity share one
        # percentile (council R2: plain enumeration gave [10,10,100] the values
        # 0.0/0.5/1.0, so equal signals got different ranks — a source could
        # still mint a within-tie winner). Assign each distinct value the mean of
        # the index positions it occupies, normalised to [0,1].
        pct: dict[int, float] = {}
        i = 0
        while i < n:
            j = i
            while j < n and ordered[j].popularity == ordered[i].popularity:
                j += 1
            # positions [i, j) all share this value → mean index, normalised.
            shared = (sum(range(i, j)) / (j - i)) / (n - 1)
            for k in range(i, j):
                pct[id(ordered[k])] = shared
            i = j
        for s in group:
            out[id(s)] = pct.get(id(s), 0.5)  # unrated members → neutral prior
    return out


def rank(skills: list[UnifiedSkill], *, query: str | None = None) -> list[UnifiedSkill]:
    """Assign rank_score and return a new list sorted best-first.

    score = popularity_percentile_within_source
            + (curated ? _CURATED_BOOST : 0)

    fdeloop0808 Phase B2 (2026-08-10): the score above is a POPULARITY signal
    only — it has no idea whether a row actually matches what the user typed.
    PR #209 fixed per-source SQL truncation so the right row always survives
    into this merged set, but this cross-source sort had no lexical-relevance
    key at all, so a popular/curated row with ZERO relevance could (and did,
    live) outrank an exact-slug/exact-title match from a less popular source.

    Verified on prod 2026-08-10:
      - q=seo: ``hundred-million-offers`` (curated, +1.0 boost, 5 installs,
        description contains NO occurrence of "seo" — verified against the
        live API body) ranked ABOVE the hermes-hub row slugged exactly
        ``seo``, purely on the boost + percentile.
      - q="code review": three curated skills with higher install counts
        (ruthless-mentor, clean-code, critical-code-reviewer) outranked the
        skill slugged exactly ``code-review``, for the same reason.

    Fix: reuse the SAME tier ladder PR #209 built for the SQL layer
    (``federation_relevance.relevance_tier`` — slug-prefix > slug-contains >
    title-prefix > title-contains > identifier > description > no-match) as
    the PRIMARY sort key, computed against each row's already-unified
    slug/title/description. Popularity + curated boost remain the score used
    to break ties WITHIN a tier (so curated still wins a genuine tie — e.g.
    two exact-slug matches from different sources), and the shortest-slug
    tiebreak from PR #209 is carried into this layer too (`len(slug)`) so a
    same-tier, same-score group of same-source rows (e.g. three unrated
    ``polymarket*`` hermes-hub rows for q=polymark) doesn't fall back to an
    alphabetical-by-title ordering that has no relationship to intent — the
    exact class of bug PR #209 fixed one layer down.

    With no query (``query`` omitted/empty — a browse, or any pre-existing
    caller that has none), the ordering is BYTE-IDENTICAL to before this fix:
    tiers and slug-length are never computed, and the tiebreak stays
    (source priority, title-alpha) exactly as previously.
    """
    pctile = _percentiles_within_source(skills)
    scored: list[UnifiedSkill] = []
    for s in skills:
        base = pctile.get(id(s), 0.5)
        boost = _CURATED_BOOST if s.quality == "curated" else 0.0
        scored.append(_with_score(s, base + boost))

    q = (query or "").strip()
    if not q:
        # Backward-compatible path: unchanged since before Phase B2.
        scored.sort(
            key=lambda s: (
                -s.rank_score,
                _source_priority(s.source),
                s.title.lower(),
            )
        )
        return scored

    tiers = {
        id(s): relevance_tier(q, slug=s.slug, title=s.title, description=s.description) for s in scored
    }
    scored.sort(
        key=lambda s: (
            tiers[id(s)],
            -s.rank_score,
            _source_priority(s.source),
            len(s.slug),
            s.title.lower(),
        )
    )
    return scored


def _with_score(s: UnifiedSkill, score: float) -> UnifiedSkill:
    # frozen dataclass → rebuild with the score set.
    return UnifiedSkill(
        canonical_id=s.canonical_id,
        slug=s.slug,
        title=s.title,
        description=s.description,
        source=s.source,
        origin_url=s.origin_url,
        install_ref=s.install_ref,
        quality=s.quality,
        deployable=s.deployable,
        install_path=s.install_path,
        popularity=s.popularity,
        license=s.license,
        updated_at=s.updated_at,
        rank_score=score,
    )


def dedupe(skills: list[UnifiedSkill]) -> list[UnifiedSkill]:
    """Collapse rows sharing a canonical_id, keeping the highest-priority source.

    The kept row inherits the MAX popularity seen across duplicates (so a github
    skill also on skills.sh keeps its install signal even if we keep the github
    row). Order of first appearance is otherwise preserved."""
    best: dict[str, UnifiedSkill] = {}
    order: list[str] = []
    for s in skills:
        cid = s.canonical_id
        if cid not in best:
            best[cid] = s
            order.append(cid)
            continue
        incumbent = best[cid]
        max_pop = _max_pop(incumbent.popularity, s.popularity)
        # Keep the higher-priority source; carry the max popularity forward.
        winner = incumbent if _source_priority(incumbent.source) <= _source_priority(s.source) else s
        if winner.popularity != max_pop:
            winner = _with_popularity(winner, max_pop)
        best[cid] = winner
    return [best[cid] for cid in order]


def _max_pop(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _with_popularity(s: UnifiedSkill, pop: int | None) -> UnifiedSkill:
    return UnifiedSkill(
        canonical_id=s.canonical_id,
        slug=s.slug,
        title=s.title,
        description=s.description,
        source=s.source,
        origin_url=s.origin_url,
        install_ref=s.install_ref,
        quality=s.quality,
        deployable=s.deployable,
        install_path=s.install_path,
        popularity=pop,
        license=s.license,
        updated_at=s.updated_at,
        rank_score=s.rank_score,
    )


@dataclass
class MetasearchResult:
    """One unified ranked list + honest per-query provenance (no stored count —
    Spotify model, Adam Q2). ``sources_ok`` / ``sources_degraded`` power the
    §8 reachability predicate and the per-query 'N results across M sources'."""

    skills: list[UnifiedSkill] = field(default_factory=list)
    sources_ok: list[str] = field(default_factory=list)
    sources_degraded: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "skills": [s.to_dict() for s in self.skills],
            "result_count": len(self.skills),
            "sources_ok": self.sources_ok,
            "sources_degraded": self.sources_degraded,
            "source_count": len(self.sources_ok),
        }


def merge_unified(
    curated: list[UnifiedSkill],
    external: list[UnifiedSkill],
    *,
    query: str | None = None,
    sources_ok: list[str] | None = None,
    sources_degraded: list[str] | None = None,
) -> MetasearchResult:
    """The intact seam: ONE ranked list from curated + external (no second-class
    namespace — this is what replaces `federation.merge_search`'s isolation wall).

    dedupe first (so a github skill on skills.sh doesn't double-render), then
    rank the merged set. Curated always sorts above an external row of equal
    normalised popularity via _CURATED_BOOST — but only WITHIN the same
    lexical-relevance tier when ``query`` is supplied (fdeloop0808 Phase B2):
    an unrelated curated row must never outrank an exact-slug/title match from
    a less-popular source. ``query`` is optional and defaults to the
    pre-Phase-B2 popularity-only ordering for backward compatibility.
    """
    merged = dedupe([*curated, *external])
    ranked = rank(merged, query=query)
    return MetasearchResult(
        skills=ranked,
        sources_ok=sources_ok or [],
        sources_degraded=sources_degraded or [],
    )
