"""app/skill_title.py — shared Skill.title derivation logic.

Extracted from app/scripts/backfill_skill_titles.py (issue #155) so the same
derivation used by the one-time backfill script is also enforced at publish
time — a title-less (title == slug) skill should never be creatable going
forward, not just patched after the fact.

Derivation priority:
  1. SKILL.md frontmatter top-level `title:` scalar (if present and != slug)
  2. SKILL.md frontmatter top-level `name:` scalar (if present and != slug)
  3. slug -> Title Case with acronym preservation. Each hyphen-separated word
     either stays uppercase if it's a known acronym (LLM, CI, MCP, API, CLI,
     etc.) or is capitalized. CLI-tool-style slugs whose FIRST token is short
     and lowercase (e.g. `gh-fix-ci`, `npm-audit-watch`) are preserved as
     "<tool> <Verb> <Object>" with the tool name kept lowercase.
"""

from __future__ import annotations

import re

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Acronyms that should stay uppercase in title-case derivation. Lowercased on
# input. Order doesn't matter; this is a membership check.
ACRONYMS = frozenset(
    {
        "ai",
        "ci",
        "cd",
        "cli",
        "api",
        "ui",
        "ux",
        "id",
        "os",
        "pr",
        "qa",
        "ml",
        "llm",
        "mcp",
        "rest",
        "sdk",
        "tts",
        "stt",
        "rag",
        "rl",
        "db",
        "fs",
        "gpu",
        "cpu",
        "io",
        "url",
        "uri",
        "http",
        "https",
        "json",
        "yaml",
        "xml",
        "js",
        "ts",
        "css",
        "html",
        "sql",
        "vps",
        "dns",
        "tls",
        "ssl",
        "ssh",
        "rss",
        "fyi",
        "ack",
        "pm",
        "wip",
        "tdd",
        "bdd",
        "dx",
    }
)

# Known CLI-tool prefixes. If slug starts with one of these followed by `-`,
# we preserve the tool name lowercase and treat the rest as the action.
CLI_TOOLS = frozenset({"gh", "git", "npm", "pip", "uv", "pnpm", "yarn", "kubectl", "docker"})


def parse_frontmatter_field(readme: str | None, field: str) -> str | None:
    """Regex-based frontmatter parser. Returns the value of `field` or None.

    PyYAML would refuse multi-line unhappy_paths blocks; a regex on a simple
    top-level scalar is robust enough for this use case.
    """
    if not readme:
        return None
    m = _FRONTMATTER_RE.match(readme)
    if not m:
        return None
    block = m.group(1)
    line_re = re.compile(rf"^{re.escape(field)}:\s*(.+?)\s*$", re.MULTILINE)
    match = line_re.search(block)
    if not match:
        return None
    val = match.group(1).strip()
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        val = val[1:-1]
    return val or None


def _cap_word(w: str) -> str:
    """Capitalize a word, preserving acronyms."""
    if not w:
        return w
    return w.upper() if w.lower() in ACRONYMS else w.capitalize()


def slug_to_title(slug: str) -> str:
    """Hyphens -> spaces, capitalize per word, but preserve known acronyms
    and known CLI tool prefixes."""
    parts = [p for p in slug.replace("_", "-").split("-") if p]
    if not parts:
        return slug

    out: list[str] = []
    for i, p in enumerate(parts):
        # First-token CLI-tool exception: keep lowercase tool name verbatim
        if i == 0 and p.lower() in CLI_TOOLS and len(parts) >= 2:
            out.append(p.lower())
        else:
            out.append(_cap_word(p))
    return " ".join(out)


def derive_title(slug: str, readme: str | None) -> str | None:
    """Return the best display title for `slug`, or None if no improvement
    over the slug itself is possible.

    `readme` is the skill's SKILL.md content (may be None/empty).
    """
    fm_title = parse_frontmatter_field(readme, "title")
    if fm_title and fm_title != slug:
        return fm_title

    fm_name = parse_frontmatter_field(readme, "name")
    if fm_name and fm_name != slug:
        return fm_name

    proposed = slug_to_title(slug)
    if proposed and proposed != slug:
        return proposed

    return None


def resolve_title_for_new_skill(slug: str, manifest_name: str | None, readme: str | None) -> str | None:
    """Title to assign when publishing creates a brand-new Skill row.

    issue #155: a manifest `name` that IS the slug (creator never set a real
    title) must not become the row's title verbatim — fall back to
    derive_title() so no newly-created skill lands title-less.
    """
    manifest_name = (manifest_name or "").strip()
    if manifest_name and manifest_name != slug:
        return manifest_name
    return derive_title(slug, readme) or manifest_name or None


def resolve_title_for_republish(
    slug: str, existing_title: str | None, manifest_name: str | None, readme: str | None
) -> str | None:
    """Title to assign when publishing updates an EXISTING Skill row.

    issue #155: the pre-fix behaviour blindly copied manifest_name onto the
    row on every publish, which REGRESSED a good editorial title back to a
    slug-shaped value whenever a later publish's manifest `name` happened to
    equal the slug. Fix: only accept manifest_name as a genuine update when
    it is not slug-shaped itself; otherwise, only backfill via derive_title()
    when the EXISTING title is ALSO title-less. A good existing title is
    never downgraded. Returns None when no change should be made.
    """
    existing_title = (existing_title or "").strip()
    manifest_name = (manifest_name or "").strip()
    if manifest_name and manifest_name != slug and manifest_name != existing_title:
        return manifest_name
    if not existing_title or existing_title == slug:
        return derive_title(slug, readme)
    return None
