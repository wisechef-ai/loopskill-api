"""LoopSkill Phase 8 tool definitions — split out to keep registry.py under 600 lines.

loopskill_0622 Phase 8: MCP discovery tools for the runnable catalog types
(loops + personalities). Mirrors the _registry_d / _registry_j extraction pattern.
"""

from __future__ import annotations

import mcp.types as types


def _loopskill_catalog_tools() -> list[types.Tool]:
    """MCP discovery tools for the runnable catalog types (verifiers, personalities).

    Phase A1 (activate_0701): ``loopskill_search_loops`` / ``loopskill_get_loop``
    are kept as legacy aliases; the canonical names are now
    ``loopskill_search_verifiers`` / ``loopskill_get_verifier``. Both sets are
    advertised so existing agents using old names keep working.  # compat-alias
    """
    _verifier_search_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "category": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        },
    }
    _verifier_get_schema = {
        "type": "object",
        "required": ["slug"],
        "properties": {"slug": {"type": "string"}},
    }
    _verifier_search_desc = (
        "Search the public registry of runnable, safety-bounded agentic "
        "verifiers. Each result carries its bounds (max_turns, budget, "
        "tool_allowlist) so you see the safety envelope before pulling."
    )
    _verifier_get_desc = (
        "Pull a single verifier's full safety-bounded execution contract "
        "(success_condition, verification_script, stopping_criteria, "
        "max_turns, tool_allowlist, system_prompt) by slug."
    )
    return [
        # ── Canonical verifier names (Phase A1) ───────────────────────────
        types.Tool(
            name="loopskill_search_verifiers",
            description=_verifier_search_desc,
            inputSchema=_verifier_search_schema,
        ),
        types.Tool(
            name="loopskill_get_verifier",
            description=_verifier_get_desc,
            inputSchema=_verifier_get_schema,
        ),
        # ── Legacy loop names (compat-alias — dual advertised) ────────────
        types.Tool(
            name="loopskill_search_loops",
            description=_verifier_search_desc,
            inputSchema=_verifier_search_schema,
        ),
        types.Tool(
            name="loopskill_get_loop",
            description=_verifier_get_desc,
            inputSchema=_verifier_get_schema,
        ),
        # ── Personality catalog (unchanged) ───────────────────────────────
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
        # ── Composite loop catalog (activate_0701 Phase A2 — NEW surface) ──
        types.Tool(
            name="loopskill_publish_composite_loop",
            description=(
                "Publish a composite loop — a deployable autonomous work unit "
                "(automation + skills + sub-agents + connectors + verifier gate + "
                "state seed). Validates the full manifest server-side."
            ),
            inputSchema={
                "type": "object",
                "required": ["slug", "title", "schedule", "verifier_slug", "prompt"],
                "properties": {
                    "slug": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "schedule": {"type": "string", "description": "cron expr or '30m' shorthand"},
                    "verifier_slug": {"type": "string"},
                    "prompt": {"type": "string"},
                    "skills": {"type": "array", "items": {"type": "object"}},
                    "connectors": {"type": "array", "items": {"type": "object"}},
                    "subagents_config": {"type": "object"},
                    "state_seed": {"type": "object"},
                    "budget_usd": {"type": "number"},
                    "tier": {"type": "string"},
                    "is_public": {"type": "boolean", "default": True},
                },
            },
        ),
        types.Tool(
            name="loopskill_get_composite_loop",
            description="Pull a composite loop's full composition by slug.",
            inputSchema={
                "type": "object",
                "required": ["slug"],
                "properties": {"slug": {"type": "string"}},
            },
        ),
        types.Tool(
            name="loopskill_search_composite_loops",
            description="Search the public registry of composite loops.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        types.Tool(
            name="loopskill_connector_publish",
            description=(
                "Publish a Connector (MCP-server config fragment) — create the "
                "connector if it doesn't exist, then mint a version with the "
                "config_template. The template uses ${VAR} env refs only; "
                "literal secrets are rejected at publish time. One call."
            ),
            inputSchema={
                "type": "object",
                "required": ["slug", "title", "connector_type", "semver", "config_template"],
                "properties": {
                    "slug": {"type": "string"},
                    "title": {"type": "string"},
                    "connector_type": {"type": "string", "enum": ["stdio", "http", "sse"]},
                    "semver": {"type": "string"},
                    "config_template": {"type": "object"},
                    "required_env": {"type": "array", "items": {"type": "string"}},
                    "description": {"type": "string"},
                    "residency_tag": {"type": "string"},
                    "changelog": {"type": "string"},
                },
            },
        ),
    ]
