"""RED-proof regression for issue #157 Phase 1.

Adam (2026-07-30, #147/#157): "wipe the references to the recipes/cookbook
naming" on every agent-visible surface. Phase 1 scope = the MCP tool
*description* / inputSchema *description* strings that every agent reads on
every tool listing — pure copy, zero behaviour change, zero client contract
change.

Two literal strings are explicitly OUT of Phase 1 scope and whitelisted here,
because they are real wire-contract identifiers, not prose:
  - ``cookbook_id`` — the actual JSON-RPC field name (schema-breaking to
    rename; Phase 3/4 per the issue's phasing).
  - ``/api/cookbooks/{id}/install`` — the actual live HTTP route (the
    ``/api/cookbooks`` alias is still primary; see issue Phase 5).

Every OTHER occurrence of "cookbook" in a description is prose and must be
gone. On pre-fix `main` this test FAILS with 26 violations (verified in the
PR body's Breaker report).
"""

from __future__ import annotations

import re

from app.mcp.registry import _tool_definitions

_COOKBOOK_RE = re.compile(r"cookbook", re.IGNORECASE)
_ALLOWED_LITERALS = ("cookbook_id", "/api/cookbooks/{id}/install")


def _strip_allowed_literals(text: str) -> str:
    for literal in _ALLOWED_LITERALS:
        text = text.replace(literal, "")
    return text


def _walk_descriptions(obj, path=""):
    """Yield (path, string) for every string value under a description-ish key."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            new_path = f"{path}.{k}" if path else str(k)
            if k == "description" and isinstance(v, str):
                yield new_path, v
            else:
                yield from _walk_descriptions(v, new_path)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_descriptions(v, f"{path}[{i}]")


def test_no_mcp_tool_description_says_cookbook():
    tools = _tool_definitions()
    violations = []
    for tool in tools:
        tool_name = getattr(tool, "name", "?")
        desc = getattr(tool, "description", None)
        if isinstance(desc, str) and _COOKBOOK_RE.search(_strip_allowed_literals(desc)):
            violations.append(f"{tool_name}.description: {desc!r}")
        schema = getattr(tool, "inputSchema", None)
        if schema:
            for path, s in _walk_descriptions(schema):
                if _COOKBOOK_RE.search(_strip_allowed_literals(s)):
                    violations.append(f"{tool_name}.inputSchema.{path}: {s!r}")

    assert not violations, (
        f"{len(violations)} MCP description string(s) still say 'cookbook' "
        f"outside the whitelisted wire-contract literals (issue #157 Phase 1):\n"
        + "\n".join(violations)
    )
