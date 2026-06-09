"""spotify_0608 Ph D — "streaming" MCP verbs for cookbook composition.

The product thesis (D2/D3): *streaming = MCP-orchestrated cookbook
composition*. Three verbs make an agent able to consume the catalogue the way
a listener consumes Spotify — on tap, from a link, in ONE call:

  1. ``recipes_install_from_cookbook`` — "install from ``<link>``": resolve a
     PUBLIC cookbook by its slug and hand back every skill's install line in
     one shot. Anonymous-reachable (a public cookbook's skills are streamable
     by anyone, exactly like a public playlist's tracks). This is the
     top-of-funnel verb the public cookbook page surfaces as its one-line
     clone instruction (GTM build-plan mod #1).

  2. ``recipes_pick_best_from_cookbook`` — "pick best from ``<link>``": given a
     public cookbook link and an optional ``need`` description, return the
     single best-matching skill (ranked by real 7d installs then total, with a
     keyword relevance pre-filter when ``need`` is supplied) + its install
     line. Lets an agent say "give me the right tool for X from this stack."

  3. ``recipes_compose_cookbook_from_links`` — "compose new from
     ``<l1> <l2> …>``": resolve N links (public cookbooks, internal catalogue
     skills, and/or external federated skills) into the union of their skills
     and mint a NEW cookbook owned by the caller containing all of them. The
     verb that turns three links into a working, shareable stack in one call.

Link grammar (``_parse_link``) — deliberately forgiving so a human or an agent
can paste almost anything:

  ``cookbook://<slug>`` / ``cookbook:<slug>``   → a public cookbook
  ``skill://<slug>``    / ``recipes:<slug>``    → an internal catalogue skill
  ``<source>:<slug>``   (source ∈ wired federation namespaces, e.g.
                         ``clawhub:web-scraper``, ``hermes-hub:pr-draft``)
                                                  → an external federated skill
  bare ``<slug>``                                → tried as a public cookbook
                                                   first, then an internal skill

Any ``?ref=<creator>`` attribution query is stripped before parsing (the
public page appends it; the verb tolerates it).

Authorization model: these verbs read only PUBLIC cookbooks (visibility
=='public'). A public cookbook's membership IS the install grant — the same
contract the already-shipped public cookbook page (``GET
/api/cookbooks/public/{slug}``) exposes, which lists every member skill. We do
NOT re-gate each member through ``authz.can_install`` because that would wrongly
strip the curator's intentionally-public external skills (materialized rows are
``is_public=False`` by the federation isolation wall, reachable only via
cookbook membership). Composition (verb 3) writes into a NEW cookbook owned by
the authenticated caller and respects the tier cookbook cap.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.mcp.tools.cookbook_install import (
    CookbookInstallError,
    _make_install_url,
    _resolve_version,
)
from app.models import Cookbook, CookbookSkill, Skill
from app.services.cookbook_external import (
    install_descriptor_for,
    is_external_skill,
    known_external_source,
    materialize_external_skill,
)
from app.tier_labels import cookbook_limit

logger = logging.getLogger(__name__)

# ── Link parsing ───────────────────────────────────────────────────────────

# Kinds returned by _parse_link.
LINK_COOKBOOK = "cookbook"
LINK_SKILL = "skill"
LINK_EXTERNAL = "external"
LINK_BARE = "bare"


def _strip_ref(link: str) -> str:
    """Drop any ``?...`` / ``#...`` suffix (e.g. ``?ref=<creator>``)."""
    for sep in ("?", "#"):
        idx = link.find(sep)
        if idx != -1:
            link = link[:idx]
    return link.strip()


def _parse_link(link: str) -> tuple[str, ...]:
    """Parse a composition link into a typed tuple.

    Returns one of:
      ("cookbook", slug)
      ("skill", slug)
      ("external", source, external_slug)
      ("bare", token)            — caller resolves cookbook-then-skill

    Raises CookbookInstallError("bad_link", ...) on an empty/garbage token.
    """
    if not link or not isinstance(link, str):
        raise CookbookInstallError("bad_link", "empty link", status=422)
    raw = _strip_ref(link)
    if not raw:
        raise CookbookInstallError("bad_link", "empty link", status=422)

    # scheme:// forms
    for scheme, kind in (("cookbook://", LINK_COOKBOOK), ("skill://", LINK_SKILL)):
        if raw.lower().startswith(scheme):
            rest = raw[len(scheme) :].strip("/").strip()
            if not rest:
                raise CookbookInstallError("bad_link", f"empty slug after {scheme}", status=422)
            return (kind, rest)

    # scheme:token forms (no slashes)
    if ":" in raw:
        head, tail = raw.split(":", 1)
        head_l = head.lower().strip()
        tail = tail.strip().lstrip("/").strip()
        if not tail:
            raise CookbookInstallError("bad_link", f"empty slug after {head_l}:", status=422)
        if head_l == LINK_COOKBOOK:
            return (LINK_COOKBOOK, tail)
        if head_l in (LINK_SKILL, "recipes"):
            return (LINK_SKILL, tail)
        # external federation source?
        if known_external_source(head_l):
            return (LINK_EXTERNAL, head_l, tail)
        # Unknown scheme prefix → treat the whole thing as a bare token so a
        # slug that legitimately contains a colon still resolves.
        return (LINK_BARE, raw)

    return (LINK_BARE, raw)


def _resolve_public_cookbook(db: Session, slug: str) -> Cookbook:
    """Return a PUBLIC cookbook by slug or raise 404. Never leaks private ones."""
    cb = db.query(Cookbook).filter(Cookbook.slug == slug).first()
    if cb is None or cb.visibility != "public":
        raise CookbookInstallError("cookbook_not_found", "cookbook_not_found", status=404)
    return cb


def _cookbook_member_rows(db: Session, cookbook_id: Any) -> list[tuple[CookbookSkill, Skill]]:
    """Active (non-disabled) member rows of a cookbook, ordered deterministically."""
    return (
        db.query(CookbookSkill, Skill)
        .join(Skill, Skill.id == CookbookSkill.skill_id)
        .filter(
            CookbookSkill.cookbook_id == cookbook_id,
            CookbookSkill.source != "disabled",
        )
        .order_by(CookbookSkill.added_at.asc())
        .all()
    )


def _skill_install_entry(
    db: Session,
    cb: Cookbook,
    cs: CookbookSkill,
    skill: Skill,
) -> dict[str, Any]:
    """Build one install entry for a skill in a public-cookbook stream.

    Internal skills get a signed tarball install URL (salt-parity preserved via
    the shared ``_make_install_url``). External skills get the cheap cached
    descriptor (no origin fetch in the listing path — isolation wall #2).
    """
    if is_external_skill(skill):
        return install_descriptor_for(str(cb.id), skill)
    version = _resolve_version(db, skill, cs.pinned_version)
    return {
        "slug": skill.slug,
        "title": skill.title,
        "external": False,
        "version": version.semver if version else None,
        "tarball_url": _make_install_url(skill.slug, version.id, version.semver) if version else None,
        "checksum_sha256": version.checksum_sha256 if version else None,
        "source": cs.source,
    }


# ── Verb 1: install-from-cookbook ──────────────────────────────────────────


def recipes_install_from_cookbook(
    db: Session,
    *,
    link: str,
    ctx: AuthContext | None = None,
    request: Any = None,
) -> dict[str, Any]:
    """Install every skill in a PUBLIC cookbook referenced by ``link``.

    "Streaming" verb #1 — the one-line clone the public cookbook page surfaces.
    ``link`` may be ``cookbook://<slug>``, ``cookbook:<slug>``, or a bare slug.
    Anonymous-reachable: a public cookbook's skills are streamable by anyone.

    Returns ``{cookbook, name, slug, skills: [...], clone_line}``. Records an
    InstallEvent per internal skill so the install shows up in transparency
    stats (test/CI keys don't inflate the public counter — Ph B §4.2).
    """
    kind = _parse_link(link)
    if kind[0] == LINK_EXTERNAL or kind[0] == LINK_SKILL:
        raise CookbookInstallError(
            "not_a_cookbook_link",
            "install_from_cookbook needs a cookbook link (cookbook://<slug>).",
            status=422,
        )
    cb = _resolve_public_cookbook(db, kind[1])

    rows = _cookbook_member_rows(db, cb.id)
    skills_payload: list[dict[str, Any]] = []
    to_record: list[tuple[Skill, str]] = []
    for cs, skill in rows:
        entry = _skill_install_entry(db, cb, cs, skill)
        skills_payload.append(entry)
        if not entry.get("external") and entry.get("version"):
            to_record.append((skill, entry["version"]))

    from app._skill_helpers import _record_install_event

    for skill, semver in to_record:
        _record_install_event(db, skill=skill, version_semver=semver, request=request, source="mcp")
    if to_record:
        db.commit()

    ref_q = f"?ref={cb.cookbook_owner}" if cb.cookbook_owner else ""
    return {
        "cookbook": str(cb.id),
        "name": cb.name,
        "slug": cb.slug,
        "skills": skills_payload,
        "clone_line": f'recipes_install_from_cookbook from "cookbook://{cb.slug}{ref_q}"',
    }


# ── Verb 2: pick-best-from-cookbook ────────────────────────────────────────


def _relevance(skill: Skill, need: str) -> int:
    """Cheap keyword relevance score for ``need`` against a skill.

    2 = need appears in the title or slug, 1 = in the description, 0 = no match.
    Used only to PRE-FILTER candidates before the install-popularity ranking;
    deliberately simple (the catalogue is small and this stays dependency-free).
    """
    n = need.lower().strip()
    if not n:
        return 0
    title = (skill.title or "").lower()
    slug = (skill.slug or "").lower()
    desc = (skill.description or "").lower()
    if n in title or n in slug:
        return 2
    # token overlap on the title/slug counts as a title hit
    tokens = [t for t in n.replace("/", " ").split() if len(t) > 2]
    if tokens and any(t in title or t in slug for t in tokens):
        return 2
    if n in desc or (tokens and any(t in desc for t in tokens)):
        return 1
    return 0


def recipes_pick_best_from_cookbook(
    db: Session,
    *,
    link: str,
    need: str | None = None,
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """Return the single best skill from a PUBLIC cookbook for ``need``.

    "Streaming" verb #2. Ranking: when ``need`` is given, keep the highest
    relevance tier that has any candidate, then break ties by real installs
    (7d, then total — test/CI excluded). With no ``need``, rank the whole
    cookbook by installs. Returns ``{picked: {...}, ranked: [...]}`` where
    ``picked`` carries the install line so the agent can install in the next
    call. ``picked`` is None only when the cookbook is empty.
    """
    kind = _parse_link(link)
    if kind[0] in (LINK_EXTERNAL, LINK_SKILL):
        raise CookbookInstallError(
            "not_a_cookbook_link",
            "pick_best_from_cookbook needs a cookbook link (cookbook://<slug>).",
            status=422,
        )
    cb = _resolve_public_cookbook(db, kind[1])
    rows = _cookbook_member_rows(db, cb.id)
    if not rows:
        return {"picked": None, "ranked": [], "cookbook": str(cb.id), "slug": cb.slug}

    from app._skill_helpers import _install_counts_for

    skill_ids = [skill.id for _cs, skill in rows]
    counts = _install_counts_for(db, skill_ids)

    need_s = (need or "").strip()
    scored: list[tuple[int, int, int, CookbookSkill, Skill]] = []
    for cs, skill in rows:
        rel = _relevance(skill, need_s) if need_s else 1
        total, last7 = counts.get(skill.id, (0, 0))
        scored.append((rel, last7, total, cs, skill))

    # When a need is supplied, drop zero-relevance candidates UNLESS that leaves
    # nothing (then fall back to popularity over the whole cookbook).
    if need_s:
        relevant = [s for s in scored if s[0] > 0]
        pool = relevant if relevant else scored
    else:
        pool = scored

    pool.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)

    def _entry(item: tuple[int, int, int, CookbookSkill, Skill]) -> dict[str, Any]:
        rel, last7, total, cs, skill = item
        e = _skill_install_entry(db, cb, cs, skill)
        e["installs_7d"] = last7
        e["installs_total"] = total
        e["relevance"] = rel
        return e

    ranked = [_entry(it) for it in pool]
    picked = dict(ranked[0])
    picked["install_line"] = (
        f'recipes_install_from_cookbook from "cookbook://{cb.slug}"  # then keep: {picked["slug"]}'
    )
    return {
        "picked": picked,
        "ranked": ranked,
        "cookbook": str(cb.id),
        "slug": cb.slug,
        "need": need_s or None,
    }


# ── Verb 3: compose-new-cookbook-from-links ────────────────────────────────


def _resolve_one_link_to_skills(db: Session, link: str) -> list[Skill]:
    """Resolve a single composition link to the Skill rows it contributes.

    - cookbook link  → every active member skill of that PUBLIC cookbook
    - skill link     → the one internal catalogue Skill
    - external link  → the materialized (private pointer) Skill for that source
    - bare token     → public cookbook if one matches, else internal skill

    Raises CookbookInstallError on an unresolvable link so the caller can report
    exactly which link failed.
    """
    kind = _parse_link(link)

    if kind[0] == LINK_COOKBOOK:
        cb = _resolve_public_cookbook(db, kind[1])
        return [skill for _cs, skill in _cookbook_member_rows(db, cb.id)]

    if kind[0] == LINK_SKILL:
        skill = db.query(Skill).filter(Skill.slug == kind[1]).first()
        if skill is None or not skill.is_public:
            raise CookbookInstallError("skill_not_found", f"skill_not_found: {kind[1]}", status=404)
        return [skill]

    if kind[0] == LINK_EXTERNAL:
        _, source, ext_slug = kind
        skill = materialize_external_skill(db, source, ext_slug)
        if skill is None:
            raise CookbookInstallError(
                "external_skill_not_found", f"external_skill_not_found: {source}:{ext_slug}", status=404
            )
        return [skill]

    # bare token: cookbook first, then internal skill
    token = kind[1]
    cb = db.query(Cookbook).filter(Cookbook.slug == token, Cookbook.visibility == "public").first()
    if cb is not None:
        return [skill for _cs, skill in _cookbook_member_rows(db, cb.id)]
    skill = db.query(Skill).filter(Skill.slug == token, Skill.is_public.is_(True)).first()
    if skill is not None:
        return [skill]
    raise CookbookInstallError("link_unresolvable", f"link_unresolvable: {token}", status=404)


def recipes_compose_cookbook_from_links(
    db: Session,
    *,
    links: list[str],
    name: str | None = None,
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """Compose a NEW cookbook owned by the caller from N links, in one call.

    "Streaming" verb #3 — the share/compose primitive. Each link is resolved
    (public cookbook → its members, internal slug → that skill, external
    ``source:slug`` → a materialized pointer) and the de-duplicated union
    becomes the membership of a freshly-created cookbook owned by ``ctx.user_id``.

    Requires user scope (master/anonymous/cbt cannot own a composed cookbook).
    Honors the tier cookbook cap. Returns the new cookbook + its skill list +
    the one-line clone instruction (the cookbook is private by default; the
    owner publishes it to make it discoverable).
    """
    if ctx is None or ctx.scope != "user" or ctx.user_id is None:
        raise CookbookInstallError(
            "auth_required",
            "compose_cookbook_from_links requires an authenticated user (it creates a cookbook you own).",
            status=401,
        )
    if not links or not isinstance(links, list):
        raise CookbookInstallError("no_links", "provide at least one link to compose from.", status=422)
    # Guardrail: keep the verb a single bounded call, not a catalogue slurp.
    if len(links) > 25:
        raise CookbookInstallError("too_many_links", "compose accepts at most 25 links per call.", status=422)

    # Tier cookbook cap (free=1, pro=10, pro+=200; None=unlimited).
    limit = cookbook_limit(ctx.tier)
    if limit is not None:
        existing = db.query(Cookbook).filter(Cookbook.cookbook_owner == ctx.user_id).count()
        if existing >= limit:
            raise CookbookInstallError(
                "cookbook_limit",
                f"cookbook limit reached for tier '{ctx.tier}' (max {limit}). Upgrade to compose more.",
                status=403,
            )

    # Resolve every link, collecting per-link errors but failing the call only
    # if NOTHING resolved (partial success is the useful behavior — one dead
    # link shouldn't sink a 3-link compose; we report which failed).
    resolved: list[Skill] = []
    seen_ids: set = set()
    per_link: list[dict[str, Any]] = []
    for link in links:
        try:
            skills = _resolve_one_link_to_skills(db, link)
            added = 0
            for sk in skills:
                if sk.id in seen_ids:
                    continue
                seen_ids.add(sk.id)
                resolved.append(sk)
                added += 1
            per_link.append({"link": link, "ok": True, "skills_added": added})
        except CookbookInstallError as exc:
            per_link.append({"link": link, "ok": False, "error": exc.code, "detail": exc.message})

    if not resolved:
        raise CookbookInstallError(
            "no_skills_resolved",
            "none of the supplied links resolved to an installable skill.",
            status=404,
        )

    cb_name = (name or "").strip() or f"Composed stack ({len(resolved)} skills)"
    cb = Cookbook(
        id=uuid4(),
        name=cb_name,
        description="Composed via recipes_compose_cookbook_from_links.",
        is_base=False,
        cookbook_owner=ctx.user_id,
    )
    db.add(cb)
    db.flush()  # need cb.id before adding members

    member_out: list[dict[str, Any]] = []
    for sk in resolved:
        db.add(CookbookSkill(cookbook_id=cb.id, skill_id=sk.id, source="custom-added"))
        member_out.append({"slug": sk.slug, "title": sk.title, "external": bool(is_external_skill(sk))})
    db.commit()
    db.refresh(cb)

    return {
        "cookbook": str(cb.id),
        "name": cb.name,
        "visibility": cb.visibility,
        "skill_count": len(member_out),
        "skills": member_out,
        "links": per_link,
        "next": (
            "Publish this cookbook (set visibility='public' + a slug) to get a shareable "
            "cookbook:// link, then recipes_install_from_cookbook installs the whole stack in one call."
        ),
    }
