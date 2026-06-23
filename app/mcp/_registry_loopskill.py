"""LoopSkill Phase 8 tool definitions — split out to keep registry.py under 600 lines.

loopskill_0622 Phase 8: MCP discovery tools for the runnable catalog types
(loops + personalities). Mirrors the _registry_d / _registry_j extraction pattern.
"""

from __future__ import annotations

import mcp.types as types


def _loopskill_catalog_tools() -> list[types.Tool]:
    """MCP discovery tools for the runnable catalog types (loops, personalities)."""
    return [
        types.Tool(
            name="loopskill_search_loops",
            description=(
                "Search the public registry of runnable, safety-bounded agentic "
                "loops. Each result carries its bounds (max_turns, budget, "
                "tool_allowlist) so you see the safety envelope before pulling."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "category": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                },
            },
        ),
        types.Tool(
            name="loopskill_get_loop",
            description=(
                "Pull a single loop's full safety-bounded execution contract "
                "(success_condition, verification_script, stopping_criteria, "
                "max_turns, tool_allowlist, system_prompt) by slug."
            ),
            inputSchema={
                "type": "object",
                "required": ["slug"],
                "properties": {"slug": {"type": "string"}},
            },
        ),
        types.Tool(
            name="loopskill_search_personalities",
            description="Search the public registry of deployable personalities (SOULs).",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "category": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                },
            },
        ),
        types.Tool(
            name="loopskill_get_personality",
            description="Pull a personality's system prompt + config by slug.",
            inputSchema={
                "type": "object",
                "required": ["slug"],
                "properties": {"slug": {"type": "string"}},
            },
        ),
    ]
