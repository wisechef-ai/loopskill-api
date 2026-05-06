"""Recipify service — Phase G.

Turns a SKILL.md draft (YAML frontmatter + body) into a CookbookSkill row.

Pipeline:
    1. validate_frontmatter — strict YAML lint, required fields, slug regex.
    2. classify_skill — keyword classifier across the 10 canonical categories
       (docs/taxonomy.md). Litellm-based variant is a future enhancement; the
       deterministic fallback documented here is the v7 path.
    3. infer_related_skills — embed via app.embeddings.embed_text and cosine
       against the cookbook's existing skills; return top-K slugs.
    4. write_cookbook_skill — upsert Skill (visibility=private keeps it
       user-scoped) + CookbookSkill provenance row.
"""

from __future__ import annotations

import math
import re
from typing import Iterable
from uuid import UUID, uuid4

import yaml
from sqlalchemy.orm import Session

from app.embeddings import embed_text, embed_skill, cosine
from app.models import Cookbook, CookbookSkill, Skill


CANONICAL_CATEGORIES = [
    "research",
    "dev-tools",
    "agency",
    "marketing",
    "content",
    "automation",
    "code-review",
    "productivity",
    "data",
    "ops",
]

SLUG_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


class ValidationError(ValueError):
    """Raised when SKILL.md frontmatter is missing or malformed."""


# ── 1) Frontmatter validation ────────────────────────────────────────────

def validate_frontmatter(text: str) -> dict:
    """Parse + validate YAML frontmatter from a SKILL.md string.

    Required: ``name`` (slug-shaped), ``description`` (non-empty string).
    Raises ValidationError on any failure.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("empty SKILL.md content")

    m = FRONTMATTER_RE.match(text.lstrip())
    if not m:
        raise ValidationError("missing YAML frontmatter (expected leading '---')")

    raw_yaml = m.group(1)
    try:
        meta = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        raise ValidationError(f"frontmatter YAML parse error: {exc}") from exc

    if not isinstance(meta, dict):
        raise ValidationError("frontmatter must be a YAML mapping")

    if "name" not in meta:
        raise ValidationError("frontmatter missing required field: name")
    if "description" not in meta:
        raise ValidationError("frontmatter missing required field: description")

    name = meta["name"]
    if not isinstance(name, str) or not SLUG_RE.match(name):
        raise ValidationError(
            "frontmatter 'name' must match ^[a-z0-9_-]{1,64}$ "
            f"(got: {name!r})"
        )

    desc = meta["description"]
    if not isinstance(desc, str) or not desc.strip():
        raise ValidationError("frontmatter 'description' must be a non-empty string")

    return meta


# ── 2) Classifier ────────────────────────────────────────────────────────

# Keyword tables tuned against docs/taxonomy.md mapping (legacy → canonical).
# Categories are checked in priority order; the first whose keyword set hits
# the highest count wins. Ties resolve to the earlier entry.
_CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("code-review", ["code review", "pr review", "pull request", "lint",
                     "static analysis", "audit", "security scan", "vulnerability",
                     "code-quality", "code quality"]),
    ("data", ["scrap", "etl", "scraping", "extract", "pipeline", "analytics",
              "ml ", "machine learning", "dataset", "ingest", "warehouse",
              "proxy rotation", "crawl"]),
    ("ops", ["devops", "deploy", "infra", "infrastructure", "kubernetes",
             "k8s", "monitoring", "observability", "ci/cd", "terraform",
             "docker", "platform"]),
    ("marketing", ["seo", "ad campaign", "ads ", "lead gen", "lead-gen",
                   "marketing", "newsletter", "growth"]),
    ("content", ["copywrit", "blog post", "creative", "image gen", "video",
                 "illustration", "creative writing", "content"]),
    ("agency", ["client deliverable", "proposal", "scoping", "consulting",
                "agency", "client report"]),
    ("automation", ["workflow", "scheduler", "cron", "bot ", "automation",
                    "orchestrat"]),
    ("research", ["research", "discovery", "literature", "knowledge harvest",
                  "knowledge base", "literature review"]),
    ("dev-tools", ["ide", "cli", "code generator", "scaffold", "developer tool",
                   "dev tool", "boilerplate", "linter config", "formatter"]),
    ("productivity", ["calendar", "email", "notes", "todo", "personal",
                      "productivity", "general utility", "communication"]),
]


def _tokenize_tags(text: str) -> list[str]:
    words = re.findall(r"[a-z][a-z0-9-]{2,}", text.lower())
    seen: list[str] = []
    for w in words:
        if w in {"the", "and", "for", "with", "from", "into", "this", "that",
                 "are", "was", "your", "our", "any", "all", "use", "uses"}:
            continue
        if w not in seen:
            seen.append(w)
        if len(seen) >= 5:
            break
    return seen


def classify_skill(text: str) -> dict:
    """Return ``{"category": <one of 10>, "tags": [..3-5..]}``.

    Deterministic keyword classifier (v7 fallback). The Litellm/Haiku variant
    is reserved for a future enhancement — see SUBAGENT_G_OUTPUT.md.
    """
    if not isinstance(text, str):
        text = ""
    haystack = text.lower()

    best_cat = "productivity"
    best_score = 0
    for cat, kws in _CATEGORY_KEYWORDS:
        score = sum(1 for kw in kws if kw in haystack)
        if score > best_score:
            best_score = score
            best_cat = cat

    tags = _tokenize_tags(text)
    # Pad to at least 3 to honour the 3-5 spec.
    while len(tags) < 3:
        tags.append(best_cat)

    return {"category": best_cat, "tags": tags[:5]}


# ── 3) Related-skills inference ──────────────────────────────────────────

def _decode_existing_embedding(raw) -> list[float] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        return [float(x) for x in raw]
    if isinstance(raw, str):
        import json as _json
        try:
            return [float(x) for x in _json.loads(raw)]
        except Exception:
            return None
    return None


def infer_related_skills(
    text: str,
    cookbook_id: UUID,
    db: Session,
    *,
    k: int = 5,
) -> list[str]:
    """Embed ``text`` and return up to ``k`` related slugs from the cookbook.

    Reuses ``embed_text`` from app.embeddings (same path the recall service
    walks). Skills whose stored embedding doesn't decode are re-embedded on
    the fly via title+description so a cold cookbook still gets ranked.
    """
    target = embed_text(text or "")
    rows = (
        db.query(CookbookSkill, Skill)
        .join(Skill, Skill.id == CookbookSkill.skill_id)
        .filter(CookbookSkill.cookbook_id == cookbook_id)
        .filter(CookbookSkill.source != "disabled")
        .all()
    )
    scored: list[tuple[float, str]] = []
    for _cs, skill in rows:
        vec = _decode_existing_embedding(skill.embedding)
        if vec is None:
            vec = embed_skill(skill)
        score = cosine(target, vec)
        if score > 0.0:
            scored.append((score, skill.slug))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [slug for _, slug in scored[:k]]


# ── 4) Write helpers ─────────────────────────────────────────────────────

def write_cookbook_skill(
    slug: str,
    content: str,
    target_cookbook_id: UUID,
    visibility: str,
    db: Session,
    *,
    classifier: dict | None = None,
    related: list[str] | None = None,
    owner_user_id: UUID | None = None,
) -> tuple[CookbookSkill, str]:
    """Upsert Skill + CookbookSkill rows. Returns (row, status).

    status is "created" the first time the slug appears in this cookbook;
    "updated" when it already existed (idempotent on slug). The catalog Skill
    row is created on first sight (with is_public derived from visibility).
    """
    if not SLUG_RE.match(slug):
        raise ValidationError(f"slug must match ^[a-z0-9_-]{{1,64}}$ (got {slug!r})")

    classifier = classifier or classify_skill(content)
    category = classifier.get("category", "productivity")

    skill = db.query(Skill).filter(Skill.slug == slug).first()
    if skill is None:
        skill = Skill(
            id=uuid4(),
            slug=slug,
            title=slug.replace("-", " ").replace("_", " ").title(),
            description=content[:512],
            category=category,
            readme=content,
            tier="cook",
            is_public=(visibility == "public_pending_review"),
            related_skills=related or [],
        )
        db.add(skill)
        db.flush()
    else:
        skill.readme = content
        if classifier.get("category"):
            skill.category = category
        if related is not None:
            skill.related_skills = related

    cb = db.query(Cookbook).filter(Cookbook.id == target_cookbook_id).first()
    if cb is None:
        raise ValidationError(f"cookbook not found: {target_cookbook_id}")

    cs = (
        db.query(CookbookSkill)
        .filter(
            CookbookSkill.cookbook_id == cb.id,
            CookbookSkill.skill_id == skill.id,
        )
        .first()
    )
    if cs is None:
        cs = CookbookSkill(
            cookbook_id=cb.id,
            skill_id=skill.id,
            source="custom-added",
        )
        db.add(cs)
        status = "created"
    else:
        cs.source = "custom-added"
        status = "updated"
    db.commit()
    db.refresh(cs)
    return cs, status
