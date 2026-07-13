"""MCP tool-name normalisation.

lsrename_0713 Phase A: the legacy ``recipes_*`` → ``loopskill_*`` back-compat
alias layer has been REMOVED. ``loopskill_*`` names are now the ONLY names
the MCP server advertises and dispatches. A caller that still sends an old
``recipes_*`` name gets ``unknown tool`` — dispatch does NOT fall through to
any handler for it. There is no alias map, no compat registration, nothing to
maintain going forward for this rename.

The only mapping that remains here is unrelated to the recipes→loopskill
rename: activate_0701 Phase A1's legacy ``loop_*`` verifier tool names, which
predate this sprint and are out of scope for lsrename_0713.
"""

from __future__ import annotations

# activate_0701 Phase A1: old loop_* tool names → new canonical verifier names.
# Legacy ``loopskill_search_loops`` / ``loopskill_get_loop`` resolve to the new
# canonical verifier names at dispatch time.  # compat-alias
LOOP_TO_VERIFIER: dict[str, str] = {
    "loopskill_search_loops": "loopskill_search_verifiers",
    "loopskill_get_loop": "loopskill_get_verifier",
}


def normalize_tool_name(name: str) -> str:
    """Map a tool name to its canonical dispatch name.

    Only the Phase A1 loop→verifier legacy mapping remains (unrelated to the
    lsrename_0713 recipes→loopskill cutover, which dropped its alias layer
    entirely — see module docstring). Every other name, including any legacy
    ``recipes_*`` name, passes through unchanged so ``_dispatch`` falls
    through to ``raise ValueError(f"unknown tool: {name}")``.
    """
    return LOOP_TO_VERIFIER.get(name, name)
