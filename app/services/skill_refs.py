"""Cross-reference extraction + the dangling-reference gate.

fdeloop_0808 Phase A (folded-in Phase E).

A published SKILL.md that says "see obviously-awesome" when no such slug exists
sends a reader to a 404. Live on prod 2026-08-08: ``hundred-million-offers``
does exactly that, twice. The portal already enforces this class of invariant
for its own links via ``audit-links.mjs``; the catalog had no equivalent.

**Offline by construction.** The gate compares readme text against the set of
published slugs the caller supplies. It performs no network I/O, so it can run
in CI, in a pre-publish hook, or against a database snapshot identically. A
gate that needs the live site to answer is a gate that goes yellow whenever the
site is slow, and a gate that goes yellow gets muted.

**Conservative by design.** Only two forms count as references:

* ``see <slug>`` — the prose convention already used in the corpus
* ``[label](/skills/<slug>)`` — the portal's own link shape

Bare mentions of a word do not. A gate that fires on "see the docs" is a gate
someone disables within a week, so the extractor requires a slug-shaped token
(lowercase, hyphenated, >=2 segments) and skips URLs.
"""

from __future__ import annotations

import re

# A slug: lowercase alnum segments joined by single hyphens, at least two
# segments. The two-segment floor is what keeps "see also", "see below" and
# "see https://..." out of the result set without a stopword list.
_SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)+"

# "see <slug>" / "See `<slug>`" — optional backticks, optional trailing punctuation.
_SEE_RE = re.compile(rf"\bsee\s+`?({_SLUG})`?", re.IGNORECASE)

# Markdown link into the skills namespace: [label](/skills/<slug>) and the
# absolute variant.
_LINK_RE = re.compile(rf"\]\((?:https?://[^)/]+)?/skills/({_SLUG})[)#/?]")

# Fenced code blocks are examples, not claims — a snippet showing `see foo-bar`
# should not fail a publish.
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


def extract_skill_references(text: str | None) -> set[str]:
    """Return every skill slug ``text`` points a reader at.

    Case-insensitive on the ``see`` keyword; the slug itself is returned
    lowercased because slugs are lowercase by definition.
    """
    if not text:
        return set()
    body = _FENCE_RE.sub(" ", text)
    refs = {m.group(1).lower() for m in _SEE_RE.finditer(body)}
    refs |= {m.group(1).lower() for m in _LINK_RE.finditer(body + " ")}
    return refs


def find_dangling_references(readmes: dict[str, str | None], published: set[str]) -> dict[str, set[str]]:
    """Map each skill slug to the references it makes that do not resolve.

    ``readmes``   — {slug: readme text} for the corpus being checked
    ``published`` — every slug a reader could actually reach

    Skills with no dangling references are omitted, so an empty dict IS the
    pass condition and the caller needs no separate boolean.

    A skill referencing ITSELF is never dangling even if the caller forgot to
    include it in ``published`` — self-reference is prose ("as described above,
    see foo-bar"), not navigation.
    """
    out: dict[str, set[str]] = {}
    for slug, text in readmes.items():
        refs = extract_skill_references(text)
        dangling = {r for r in refs if r not in published and r != slug}
        if dangling:
            out[slug] = dangling
    return out


def format_dangling_report(dangling: dict[str, set[str]]) -> str:
    """Human-readable failure text for CI output."""
    if not dangling:
        return "No dangling skill references."
    lines = [f"{len(dangling)} skill(s) reference slugs that do not resolve:"]
    for slug in sorted(dangling):
        for ref in sorted(dangling[slug]):
            lines.append(f"  {slug} -> {ref}  (no such published skill)")
    return "\n".join(lines)
