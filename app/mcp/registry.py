"""MCP tool registry — _tool_definitions() returns the advertised types.Tool list.

Naming convention (post loopskill rename):
* PRIMARY names are ``loopskill_*`` (new canonical names advertised to MCP clients).
* BACK-COMPAT aliases are ``recipes_*`` — kept so existing agents that hard-code
  the old names continue to work.  Dispatch normalisation in
  ``app/mcp/_alias_map.py`` routes both names to the same handler.
"""

from __future__ import annotations

import mcp.types as types
from app.mcp._alias_map import LOOPSKILL_TO_RECIPES
from app.mcp._registry_bundle import _bundle_tools
from app.mcp._registry_d import _phase_d_tools, _phase_e_tools
from app.mcp._registry_j import _phase_j_tools
from app.mcp._registry_k import _fleet_tools, _publish_tools, _share_tools, _tailor_tools
from app.mcp._registry_loopskill import _loopskill_catalog_tools


def _core_tools() -> list[types.Tool]:
    """Core loopskill_* tools: search, install, bundle-install, list, recall, etc."""
    return [
        types.Tool(
            name="loopskill_search",
            description="Full-text search across the public skill catalog.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "category": {"type": "string"},
                    "tier": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
            },
        ),
        types.Tool(
            name="loopskill_install",
            description="Return a signed tarball URL + manifest for a skill slug.",
            inputSchema={
                "type": "object",
                "required": ["slug"],
                "properties": {"slug": {"type": "string"}},
            },
        ),
        types.Tool(
            name="loopskill_bundle_install",
            description=(
                "Install all skills from a cookbook (bulk) or one skill by slug. "
                "cbt_token callers may omit cookbook_id — it defaults to the "
                "token's scoped cookbook. user/master callers must pass "
                "cookbook_id. The single-skill payload mirrors loopskill_install; "
                "the bulk payload mirrors POST /api/cookbooks/{id}/install."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "cookbook_id": {
                        "type": "string",
                        "description": (
                            "Bundle UUID. Optional for cbt_token (defaults "
                            "to token's bundle_scope); required otherwise."
                        ),
                    },
                    "slug": {
                        "type": "string",
                        "description": (
                            "Optional single-skill filter. Omit to bulk-install "
                            "every active skill in the cookbook."
                        ),
                    },
                },
            },
        ),
        types.Tool(
            name="loopskill_list_bundle",
            description="List the caller's cookbook and its skill provenance rows.",
            inputSchema={
                "type": "object",
                "properties": {"cookbook_id": {"type": "string"}},
            },
        ),
        types.Tool(
            name="loopskill_recall",
            description="Hybrid (vector + BM25) skill recall ranked for the caller's tier.",
            inputSchema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "local_context_summary": {"type": "string"},
                    "tier_filter": {
                        "type": "array",
                        # Canonical: free|pro|pro_plus. Legacy aliases cook|operator accepted until 2026-06-10.
                        "items": {
                            "type": "string",
                            "enum": ["free", "pro", "pro_plus", "cook", "operator"],
                        },  # cook|operator = legacy aliases
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                },
            },
        ),
        types.Tool(
            name="loopskill_recipify",
            description=(
                "Convert a SKILL.md draft into a CookbookSkill row: validates "
                "YAML frontmatter, classifies the category, infers related "
                "skills via embedding cosine, writes the skill to the caller's "
                "cookbook."
            ),
            inputSchema={
                "type": "object",
                "required": ["slug", "content"],
                "properties": {
                    "slug": {"type": "string"},
                    "content": {"type": "string"},
                    "target_cookbook_id": {"type": "string"},
                    "visibility": {
                        "type": "string",
                        "enum": ["private", "public_pending_review"],
                        "default": "private",
                    },
                    "target_subrecipe_id": {"type": "string"},
                    "tier": {
                        "type": "string",
                        # Canonical: free|pro|pro_plus. Legacy aliases cook|operator accepted until 2026-06-10.
                        "enum": [
                            "free",
                            "pro",
                            "pro_plus",
                            "cook",
                            "operator",
                        ],  # cook|operator = legacy aliases
                        "default": "pro",
                    },
                    "is_public": {"type": "boolean"},
                },
            },
        ),
        types.Tool(
            name="loopskill_carousel_today",
            description="Today's curated carousel of skills.",
            inputSchema={"type": "object"},
        ),
        types.Tool(
            name="loopskill_subrecipe_resolve",
            description="Phase C stub — resolve a sub-recipe key to a scope.",
            inputSchema={"type": "object"},
        ),
        types.Tool(
            name="loopskill_doctor",
            description="Audit a local skill install directory for missing files and hardcoded paths.",
            inputSchema={
                "type": "object",
                "required": ["install_dir"],
                "properties": {"install_dir": {"type": "string"}},
            },
        ),
        types.Tool(
            name="loopskill_seeker",
            description=(
                "Probe local vendor skill directories (Claude / Codex / "
                "Hermes / OpenCode) and diff against the public catalog. "
                "READ-ONLY — never mutates vendor dirs."
            ),
            inputSchema={"type": "object"},
        ),
        types.Tool(
            name="loopskill_sync",
            description=(
                "Synchronise a cookbook's skills to their latest published "
                "versions. By default (dry_run=false) this APplies updates "
                "immediately. Pass dry_run=true to preview the diff without "
                "mutating state."
            ),
            inputSchema={
                "type": "object",
                "required": ["cookbook_id"],
                "properties": {
                    "cookbook_id": {
                        "type": "string",
                        "description": "UUID of the cookbook to synchronise.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "If true, return the diff without applying changes. "
                            "Default is false (apply immediately)."
                        ),
                    },
                },
            },
        ),
        types.Tool(
            name="loopskill_feedback",
            description=(
                "Send feedback about recipes.wisechef.ai. Use when the "
                "user says 'write feedback that...', 'give feedback...', "
                "'report that...', or expresses frustration with the platform "
                "UX, search, billing, or docs. Auto-creates a labelled GitHub "
                "issue. Rate limited per 24h."
            ),
            inputSchema={
                "type": "object",
                "required": ["category", "message"],
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["ux", "search", "billing", "docs", "install", "other"],
                    },
                    "message": {"type": "string"},
                    "context": {"type": "object"},
                    "agent_id": {"type": "string"},
                    "force": {"type": "boolean", "default": False},
                    "confirmation": {"type": "string"},
                    "provenance_id": {
                        "type": "string",
                        "description": "Install-provenance token; routes to the right creator repo (Ph E).",
                    },
                },
            },
        ),
        types.Tool(
            name="loopskill_request_recipe",
            description=(
                "Request a new recipe (skill). Use when the user says "
                "'recipify X', 'please add X to recipes', "
                "'we need a recipe for X'. Creates a GitHub wishlist issue."
            ),
            inputSchema={
                "type": "object",
                "required": ["target_name", "why_useful"],
                "properties": {
                    "target_name": {"type": "string"},
                    "why_useful": {"type": "string"},
                    "suggested_sources": {"type": "array", "items": {"type": "string"}},
                    "agent_id": {"type": "string"},
                },
            },
        ),
        types.Tool(
            name="loopskill_propose_skill_patch",
            description=(
                "Submit a working patch (draft PR) to a recipes-marketplace skill "
                "on wisechef-ai/recipes-api. Use when you have ALREADY fixed a skill "
                "locally during install or use and want to ship the fix back so other "
                "agents do not hit the same bug. Allowed file paths: SKILL.md, "
                "references/*.md, templates/*.{yml,yaml,sh,env,md}. Script changes "
                "(scripts/*, install.sh, recipe.yaml) are NOT allowed here — describe "
                "those as a comment on the skill-error issue body instead. Hard limits: "
                "3 files max, 200 lines per file, 600 lines total. Rate limited to "
                "1 patch per 24h per (agent, skill). Returns dedup_hash and (eventually) pr_url."
            ),
            inputSchema={
                "type": "object",
                "required": ["slug", "base_version", "files", "rationale"],
                "properties": {
                    "slug": {"type": "string"},
                    "base_version": {"type": "string"},
                    "files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["path", "content"],
                            "properties": {
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                        },
                    },
                    "rationale": {"type": "string"},
                    "evidence_install_id": {"type": "string"},
                    "agent_id_anon": {"type": "string"},
                },
            },
        ),
    ]


def _tool_definitions() -> list[types.Tool]:
    primary: list[types.Tool] = [
        *_bundle_tools(),  # Phase 3+4 new-vocab tools (bundle_list, bundle_install)
        *_core_tools(),  # loopskill_search, install, recall, recipify, etc.
        *_share_tools(),  # Phase D share-token tools (loopskill_share_*)
        *_fleet_tools(),  # Phase E fleet tools (loopskill_fleet_*)
        *_publish_tools(),  # loopskill_publish_request
        *_tailor_tools(),  # loopskill_fork_list / tailor / tailor_version / bundle_attach/handoff
        *_phase_d_tools(),  # spotify_0608 Ph D (install_from_bundle, compose_bundle_from_links, etc.)
        *_phase_e_tools(),  # spotify_0608 Ph E (report_skill_error)
        *_phase_j_tools(),  # loopclose_3005 Phase J (configure_feedback)
        *_loopskill_catalog_tools(),  # loopskill_0622 Phase 8 — loops + personalities
    ]

    # ── Back-compat aliases: also advertise recipes_* names ─────────────────
    # Built dynamically from the alias map so new loopskill_* entries auto-get
    # a compat alias once they're added to LOOPSKILL_TO_RECIPES.
    primary_by_name = {t.name: t for t in primary}
    compat_aliases: list[types.Tool] = [
        types.Tool(
            name=recipes_name,
            description=primary_by_name[ls_name].description,
            inputSchema=primary_by_name[ls_name].inputSchema,
        )
        for ls_name, recipes_name in LOOPSKILL_TO_RECIPES.items()
        if ls_name in primary_by_name
    ]

    return [*primary, *compat_aliases]
