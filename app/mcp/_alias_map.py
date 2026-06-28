"""Canonical loopskill_* → back-compat recipes_* alias map for MCP tool dispatch.

Strategy
--------
* MCP **discovery** (tools/list) advertises ``loopskill_*`` as PRIMARY names.
  Back-compat ``recipes_*`` entries are also registered so existing agents
  that hard-code the old names keep working.

* MCP **dispatch** normalises incoming names at the very top of ``_dispatch``
  via :func:`normalize_tool_name`, so the internal if-chain (which still tests
  ``if name == "recipes_search":`` etc.) is touched once and only once.
"""

from __future__ import annotations

import mcp.types as types

# Canonical loopskill_* name  →  back-compat recipes_* dispatch name
# NOTE: ``cookbook`` → ``bundle`` applies for tools that operate on bundles.
LOOPSKILL_TO_RECIPES: dict[str, str] = {
    "loopskill_search": "recipes_search",
    "loopskill_install": "recipes_install",
    "loopskill_bundle_install": "recipes_cookbook_install",
    "loopskill_install_from_bundle": "recipes_install_from_cookbook",
    "loopskill_list_bundle": "recipes_list_cookbook",
    "loopskill_compose_bundle_from_links": "recipes_compose_cookbook_from_links",
    "loopskill_pick_best_from_bundle": "recipes_pick_best_from_cookbook",
    "loopskill_recall": "recipes_recall",
    "loopskill_recipify": "recipes_recipify",
    "loopskill_carousel_today": "recipes_carousel_today",
    "loopskill_report_skill_error": "recipes_report_skill_error",
    "loopskill_configure_feedback": "recipes_configure_feedback",
    "loopskill_subrecipe_resolve": "recipes_subrecipe_resolve",
    "loopskill_sync": "recipes_sync",
    "loopskill_doctor": "recipes_doctor",
    "loopskill_seeker": "recipes_seeker",
    "loopskill_feedback": "recipes_feedback",
    "loopskill_request_recipe": "recipes_request_recipe",
    "loopskill_propose_skill_patch": "recipes_propose_skill_patch",
    "loopskill_share_create": "recipes_share_create",
    "loopskill_share_list": "recipes_share_list",
    "loopskill_share_revoke": "recipes_share_revoke",
    "loopskill_share_rotate": "recipes_share_rotate",
    "loopskill_fleet_create": "recipes_fleet_create",
    "loopskill_fleet_subscribe": "recipes_fleet_subscribe",
    "loopskill_fleet_sync": "recipes_fleet_sync",
    "loopskill_fleet_list": "recipes_fleet_list",
    "loopskill_publish_request": "recipes_publish_request",
    "loopskill_fork_list": "recipes_fork_list",
    "loopskill_tailor": "recipes_tailor",
    "loopskill_tailor_version": "recipes_tailor_version",
    "loopskill_bundle_attach": "recipes_cookbook_attach",
    "loopskill_bundle_handoff": "recipes_cookbook_handoff",
}

# Inverse: recipes_* → loopskill_*  (informational; not used in dispatch)
RECIPES_TO_LOOPSKILL: dict[str, str] = {v: k for k, v in LOOPSKILL_TO_RECIPES.items()}


def normalize_tool_name(name: str) -> str:
    """Map a loopskill_* canonical name to its recipes_* dispatch name.

    Back-compat aliases (``recipes_*``) pass through unchanged so the existing
    ``if name == "recipes_..."`` chain in ``_dispatch`` continues to work for
    old agents that call the legacy names directly.
    """
    return LOOPSKILL_TO_RECIPES.get(name, name)


def make_compat_alias_tools(primary_tools: list[types.Tool]) -> list[types.Tool]:
    """Return ``recipes_*`` compat-alias Tool entries for every ``loopskill_*`` tool.

    Only tools whose name appears in :data:`LOOPSKILL_TO_RECIPES` get an alias;
    other tools (``bundle_*``, ``loopskill_search_loops``, etc.) are skipped.

    Schemas are shared by reference — mutations to the primary schema would
    affect the alias, but registry schemas are treated as read-only at runtime.
    """
    aliases: list[types.Tool] = []
    for tool in primary_tools:
        recipes_name = LOOPSKILL_TO_RECIPES.get(tool.name)
        if recipes_name is not None:
            aliases.append(
                types.Tool(
                    name=recipes_name,
                    description=tool.description,
                    inputSchema=tool.inputSchema,
                )
            )
    return aliases
