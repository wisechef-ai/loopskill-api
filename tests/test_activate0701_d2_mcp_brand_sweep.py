"""Phase D2 (loopskill_activate_0701) — MCP tool-layer brand sweep + regression lock.

The MCP server block is correctly named ``loopskill`` and serves 78 tools, but a
recipes-era brand string ("recipes.wisechef.ai", "wisechef-ai/recipes-api",
"recipes-marketplace") lingered in the *user-facing tool descriptions* and a few
docstrings/comments under ``app/mcp/``. Agents read those descriptions verbatim,
so a stale brand string is a self-ID lie on the wire.

This locks the tool layer: no stale recipes-era brand strings may reappear in
``app/mcp/**.py``. The one legitimate exception is an *accurate rename-history*
note that documents the recipes-api -> loopskill-api rename, which lives in
``app/github_dispatch.py`` (outside app/mcp/) and is intentionally not swept.

Note: this is deliberately scoped to app/mcp/ (the tool surface agents see).
A brain-dead repo-wide ban would trip on legitimate history and the public
recipes.wisechef.ai product, which is a separate live site.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_MCP_DIR = Path(__file__).resolve().parent.parent / "app" / "mcp"

# Stale recipes-era brand strings that must NOT appear anywhere in the MCP tool layer.
_BANNED = (
    "recipes.wisechef.ai",
    "wisechef-ai/recipes-api",
    "recipes-marketplace",
    "recipes-api server process",
    "WiseRecipes",
)


def _mcp_py_files() -> list[Path]:
    return [p for p in _MCP_DIR.rglob("*.py") if "__pycache__" not in p.parts]


@pytest.mark.parametrize("banned", _BANNED)
def test_no_stale_recipes_brand_in_mcp_layer(banned: str):
    """No recipes-era brand string may appear in any app/mcp/**.py file."""
    offenders: list[str] = []
    for path in _mcp_py_files():
        text = path.read_text()
        if banned in text:
            for i, line in enumerate(text.splitlines(), 1):
                if banned in line:
                    rel = path.relative_to(_MCP_DIR.parent.parent)
                    offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, (
        f"Stale recipes-era brand string {banned!r} found in the MCP tool layer. "
        f"LoopSkill (app.loopskill.io / wisechef-ai/loopskill-api) is the current "
        f"product identity — agents read tool descriptions verbatim.\n" + "\n".join(offenders)
    )


def test_feedback_tool_description_says_loopskill():
    """The loopskill_feedback tool description must self-ID as LoopSkill (the string Adam saw)."""
    registry = (_MCP_DIR / "registry.py").read_text()
    assert "Send feedback about LoopSkill" in registry, (
        "loopskill_feedback description must say 'Send feedback about LoopSkill', not the recipes-era text."
    )


def test_pricing_link_points_at_loopskill():
    """The configure_feedback upgrade nudge must link app.loopskill.io/pricing (verified live 200)."""
    cfg = (_MCP_DIR / "tools" / "configure_feedback.py").read_text()
    assert "https://app.loopskill.io/pricing" in cfg
    assert "recipes.wisechef.ai/pricing" not in cfg
