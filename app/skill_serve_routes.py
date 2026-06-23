"""Canonical /skill serve route — loopclose_3005 Phase B.

`/skill` is the install command printed on every public surface (hero, the four
integrations cards, the per-skill pages). It MUST serve the current, clean,
in-repo SKILL.md directly — NOT 302-redirect to the `wisechef-ai/recipes-skill`
GitHub mirror, which drifts (it advertised 10 tools when the server had 24/26),
names a phantom CLI, and carries mirror-bot leak headers exposing internal
repo paths.

This route reads `docs/recipes-skill/SKILL.md` from the repo (the SSOT the
drift-check gate validates against `app/mcp/registry.py`) and serves it as
`text/plain`, 200, no redirect. As defence-in-depth it strips any
mirror-bot leak headers (`auto-mirrored from…`, `DO NOT EDIT…`,
`last sync: commit…`) should they ever appear on the served path — the in-repo
source is already clean, but the served copy must never leak internal paths.

ROOT-level route (no /api prefix) so the bare `/skill` URL works. Mounted in
main.py alongside utm_router.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

skill_serve_router = APIRouter(tags=["skills"])

# docs/recipes-skill/SKILL.md lives two levels up from app/.
SKILL_MD_PATH = Path(__file__).resolve().parent.parent / "docs" / "recipes-skill" / "SKILL.md"

# Mirror-bot leak headers to strip from the served copy (defence-in-depth;
# the in-repo source is clean). Matches HTML comment lines the sync bot injects.
_LEAK_HEADER_RE = re.compile(
    r"^<!--\s*(?:auto-mirrored from|DO NOT EDIT|last sync:).*?-->\s*$",
    re.IGNORECASE,
)


def _strip_leak_headers(text: str) -> str:
    """Remove mirror-bot leak-header comment lines from the served body.

    The bot injects a leading block of HTML comments
    (``<!-- auto-mirrored… -->`` etc.). We drop any such comment line wherever
    it appears, then trim leading blank lines so a stripped file starts at real
    content. A clean source (no headers) is returned unchanged apart from a
    possible leading-blank trim.
    """
    kept = [line for line in text.splitlines() if not _LEAK_HEADER_RE.match(line)]
    body = "\n".join(kept).lstrip("\n")
    return body + ("\n" if text.endswith("\n") and not body.endswith("\n") else "")


@lru_cache(maxsize=1)
def _canonical_skill_md() -> str:
    """Read + clean the canonical SKILL.md once (cached for process lifetime)."""
    raw = SKILL_MD_PATH.read_text(encoding="utf-8")
    return _strip_leak_headers(raw)


@skill_serve_router.get("/skill", include_in_schema=False)
@skill_serve_router.get("/skill/", include_in_schema=False)
@skill_serve_router.get("/SKILL.md", include_in_schema=False)
def serve_canonical_skill() -> PlainTextResponse:
    """Serve the canonical, clean SKILL.md as text/plain (no redirect).

    Mounted at /skill, /skill/, and /SKILL.md — the same three paths the old
    Caddy 302-to-GitHub rule covered, so every existing link keeps working.
    An agent runs `curl -sL https://recipes.wisechef.ai/skill -o SKILL.md` and
    gets a file it can load directly with the correct MCP tool names.
    """
    body = _canonical_skill_md()
    return PlainTextResponse(
        content=body,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )
