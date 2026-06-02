"""MCP tool registry — static list of all registered MCP tools.

This module owns _tool_definitions() which returns the full list of
types.Tool objects the server advertises to MCP clients.
"""

from __future__ import annotations

import mcp.types as types


def _tool_definitions() -> list[types.Tool]:
    return [
        types.Tool(
            name="recipes_search",
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
            name="recipes_install",
            description="Return a signed tarball URL + manifest for a skill slug.",
            inputSchema={
                "type": "object",
                "required": ["slug"],
                "properties": {"slug": {"type": "string"}},
            },
        ),
        types.Tool(
            name="recipes_cookbook_install",
            description=(
                "Install all skills from a cookbook (bulk) or one skill by slug. "
                "cbt_token callers may omit cookbook_id — it defaults to the "
                "token's scoped cookbook. user/master callers must pass "
                "cookbook_id. The single-skill payload mirrors recipes_install; "
                "the bulk payload mirrors POST /api/cookbooks/{id}/install."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "cookbook_id": {
                        "type": "string",
                        "description": (
                            "Cookbook UUID. Optional for cbt_token (defaults "
                            "to token's cookbook_scope); required otherwise."
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
            name="recipes_list_cookbook",
            description="List the caller's cookbook and its skill provenance rows.",
            inputSchema={
                "type": "object",
                "properties": {"cookbook_id": {"type": "string"}},
            },
        ),
        types.Tool(
            name="recipes_recall",
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
            name="recipes_recipify",
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
            name="recipes_carousel_today",
            description="Today's curated carousel of skills.",
            inputSchema={"type": "object"},
        ),
        types.Tool(
            name="recipes_subrecipe_resolve",
            description="Phase C stub — resolve a sub-recipe key to a scope.",
            inputSchema={"type": "object"},
        ),
        types.Tool(
            name="recipes_doctor",
            description="Audit a local skill install directory for missing files and hardcoded paths.",
            inputSchema={
                "type": "object",
                "required": ["install_dir"],
                "properties": {"install_dir": {"type": "string"}},
            },
        ),
        types.Tool(
            name="recipes_seeker",
            description=(
                "Probe local vendor skill directories (Claude / Codex / "
                "Hermes / OpenCode) and diff against the public catalog. "
                "READ-ONLY — never mutates vendor dirs."
            ),
            inputSchema={"type": "object"},
        ),
        types.Tool(
            name="recipes_sync",
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
            name="recipes_feedback",
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
                },
            },
        ),
        types.Tool(
            name="recipes_request_recipe",
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
                    "suggested_sources": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "agent_id": {"type": "string"},
                },
            },
        ),
        types.Tool(
            name="recipes_report_skill_error",
            description=(
                "Report that an installed recipe is broken, has wrong "
                "instructions, or fails on this host. Use when the user says "
                "'this skill is broken', 'report this skill', or when an "
                "install/run fails. Auto-creates a labelled GitHub issue with "
                "the failure signature."
            ),
            inputSchema={
                "type": "object",
                "required": ["slug", "signature", "summary"],
                "properties": {
                    "slug": {"type": "string"},
                    "signature": {"type": "string"},
                    "summary": {"type": "string"},
                    "details": {"type": "string"},
                    "agent_id": {"type": "string"},
                },
            },
        ),
        types.Tool(
            name="recipes_propose_skill_patch",
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
        # ── Phase D: share-token management tools ───────────────────────────
        types.Tool(
            name="recipes_share_create",
            description=(
                "Create a new share token for a cookbook. Returns the plaintext "
                "token (shown exactly once), prefix, scope, name, id, created_at, "
                "and config_blocks (Hermes YAML + Claude Desktop JSON snippets). "
                "Requires can_write_cookbook."
            ),
            inputSchema={
                "type": "object",
                "required": ["cookbook_id"],
                "properties": {
                    "cookbook_id": {"type": "string"},
                    "name": {"type": "string"},
                    "scope": {
                        "type": "string",
                        "enum": ["read", "edit", "install"],
                        "default": "install",
                    },
                },
            },
        ),
        types.Tool(
            name="recipes_share_list",
            description=(
                "List share tokens for a cookbook (metadata only, no plaintext). "
                "Returns {tokens: [{id, prefix, name, scope, is_active, created_at, "
                "last_used_at}]}. Requires can_write_cookbook."
            ),
            inputSchema={
                "type": "object",
                "required": ["cookbook_id"],
                "properties": {
                    "cookbook_id": {"type": "string"},
                },
            },
        ),
        types.Tool(
            name="recipes_share_revoke",
            description=(
                "Soft-delete (deactivate) a share token immediately. "
                "Returns {revoked: true, token_id}. Requires can_write_cookbook."
            ),
            inputSchema={
                "type": "object",
                "required": ["cookbook_id", "token_id"],
                "properties": {
                    "cookbook_id": {"type": "string"},
                    "token_id": {"type": "string"},
                },
            },
        ),
        types.Tool(
            name="recipes_share_rotate",
            description=(
                "Rotate a share token: deactivate the old token and create a new "
                "one with the same name and scope. Returns new_token, new_prefix, "
                "old_token_id, new_token_id, and config_blocks. "
                "Requires can_write_cookbook."
            ),
            inputSchema={
                "type": "object",
                "required": ["cookbook_id", "token_id"],
                "properties": {
                    "cookbook_id": {"type": "string"},
                    "token_id": {"type": "string"},
                },
            },
        ),
        # ── Phase E: fleet tools ─────────────────────────────────────────────
        types.Tool(
            name="recipes_fleet_create",
            description=(
                "Create a named fleet of agents. Returns a one-time fleet API key "
                "(rec_fleet_*) for x-fleet-key authentication. The key is shown ONCE."
            ),
            inputSchema={
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            },
        ),
        types.Tool(
            name="recipes_fleet_subscribe",
            description=(
                "Subscribe a cookbook to a fleet on a given channel " "(stable, canary, frozen). Idempotent."
            ),
            inputSchema={
                "type": "object",
                "required": ["fleet_id", "cookbook_id"],
                "properties": {
                    "fleet_id": {"type": "string"},
                    "cookbook_id": {"type": "string"},
                    "channel": {
                        "type": "string",
                        "enum": ["stable", "canary", "frozen"],
                        "default": "stable",
                    },
                },
            },
        ),
        types.Tool(
            name="recipes_fleet_sync",
            description=(
                "Synchronise all cookbooks subscribed to the fleet. Aggregates "
                "per-cookbook sync results. Pass dry_run=true to preview."
            ),
            inputSchema={
                "type": "object",
                "required": ["fleet_id"],
                "properties": {
                    "fleet_id": {"type": "string"},
                    "dry_run": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, preview changes without applying.",
                    },
                },
            },
        ),
        types.Tool(
            name="recipes_fleet_list",
            description="List all fleets owned by the caller with their cookbook subscriptions.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="recipes_publish_request",
            description=(
                "Submit a skill (SKILL.md + optional scripts/references) for review "
                "and potential public-catalog inclusion. Runs quality gates locally "
                "before opening a labelled GitHub issue. High-severity findings block "
                "submission. Rate limited to 1 request per 24h per (identity, slug)."
            ),
            inputSchema={
                "type": "object",
                "required": ["slug", "content"],
                "properties": {
                    "slug": {"type": "string"},
                    "content": {
                        "type": "string",
                        "description": "SKILL.md content as a string",
                    },
                    "version": {"type": "string", "default": "1.0.0"},
                    "description": {"type": "string"},
                    "tier": {
                        "type": "string",
                        "default": "pro",
                        # Canonical: free|pro|pro_plus. Legacy aliases cook|operator accepted until 2026-06-10.
                        "enum": [
                            "free",
                            "pro",
                            "pro_plus",
                            "cook",
                            "operator",
                        ],  # cook|operator = legacy aliases
                    },
                    "is_public": {"type": "boolean", "default": True},
                    "references": {
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
                    "scripts": {
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
                    "license": {"type": "string", "default": "MIT"},
                    "changelog": {"type": "string"},
                    "force": {"type": "boolean", "default": False},
                    "confirmation": {"type": "string"},
                },
            },
        ),
        # ── integrator_2905 W1: tailor / fork tools ──────────────────────────
        types.Tool(
            name="recipes_fork_list",
            description=(
                "List all forks owned by the authenticated user. "
                "Returns fork_id, name, slug, source_slug for each."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="recipes_tailor",
            description=(
                "Fork a public skill to create an editable private copy. "
                "Returns fork_id and fork_slug. The fork is ready for versioning "
                "via POST /api/forks/{fork_id}/version. Idempotent: if the user "
                "already forked this skill, returns the existing fork."
            ),
            inputSchema={
                "type": "object",
                "required": ["source_slug", "name"],
                "properties": {
                    "source_slug": {
                        "type": "string",
                        "description": "Slug of the public skill to fork.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Human-readable name for the fork.",
                    },
                    "readme": {
                        "type": "string",
                        "description": "Optional README for the fork.",
                    },
                },
            },
        ),
        # ── loopclose_3005 Phase C: close the MCP tailor loop ─────────────────
        types.Tool(
            name="recipes_tailor_version",
            description=(
                "Upload a new version tarball to one of your forks (MCP-native). "
                "The tarball is passed base64-encoded (MCP can't carry multipart "
                "uploads). Mints a fork version and advances the fork's latest "
                "pointer. Step 2 of the tailor loop: tailor -> tailor_version -> "
                "cookbook_attach -> cookbook_install. Pro tier or above."
            ),
            inputSchema={
                "type": "object",
                "required": ["fork_id", "tarball_base64", "semver"],
                "properties": {
                    "fork_id": {
                        "type": "string",
                        "description": "UUID of the fork to version (from recipes_tailor).",
                    },
                    "tarball_base64": {
                        "type": "string",
                        "description": "Base64-encoded .tar.gz of the tailored skill (max 10 MB decoded).",
                    },
                    "semver": {
                        "type": "string",
                        "description": "Semantic version, e.g. '1.0.0'.",
                    },
                    "changelog": {
                        "type": "string",
                        "description": "Optional changelog note for this version.",
                    },
                },
            },
        ),
        types.Tool(
            name="recipes_cookbook_attach",
            description=(
                "Deploy a tailored fork's latest version into one of your "
                "cookbooks. Promotes the fork into a private catalog skill linked "
                "to the cookbook and mints an installable version, so it installs "
                "byte-identically to any catalog skill via recipes_cookbook_install. "
                "Step 3 of the tailor loop. Pro tier or above; you must own the "
                "cookbook."
            ),
            inputSchema={
                "type": "object",
                "required": ["fork_id", "target_cookbook_id"],
                "properties": {
                    "fork_id": {
                        "type": "string",
                        "description": "UUID of the fork to deploy (must have an uploaded version).",
                    },
                    "target_cookbook_id": {
                        "type": "string",
                        "description": "UUID of the cookbook to attach the promoted skill to (you must own it).",
                    },
                    "slug": {
                        "type": "string",
                        "description": (
                            "Optional slug override for the promoted skill "
                            "(defaults to the fork slug). Must match ^[a-z0-9][a-z0-9_-]{0,63}$."
                        ),
                    },
                },
            },
        ),
        # ── loopclose_3005 Phase I: cookbook handoff ──────────────────────────
        types.Tool(
            name="recipes_cookbook_handoff",
            description=(
                "Transfer OR fork a cookbook to a new owner preserving tailored skills. "
                "Only the current owner or master may act. Provide new_owner_user_id OR "
                "new_owner_email. mode='transfer': ownership swaps in-place. "
                "mode='fork': new cookbook with parent lineage + custom-added skills."
            ),
            inputSchema={
                "type": "object",
                "required": ["cookbook_id"],
                "properties": {
                    "cookbook_id": {"type": "string", "description": "UUID of the cookbook to hand off."},
                    "new_owner_user_id": {
                        "type": "string",
                        "description": "UUID of the new owner (or use new_owner_email).",
                    },
                    "new_owner_email": {
                        "type": "string",
                        "description": "Email of the new owner (or use new_owner_user_id).",
                    },
                    "mode": {"type": "string", "enum": ["transfer", "fork"], "default": "transfer"},
                },
            },
        ),
    ]
