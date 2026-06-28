"""Phase D/E/J/K inline tools — share tokens, fleet, publish, tailor, fork.

Extracted from registry.py to keep it under the 600-line god-object guard.
"""

from __future__ import annotations

import mcp.types as types


def _share_tools() -> list[types.Tool]:
    """Phase D share-token management tools (loopskill_* primary names)."""
    return [
        types.Tool(
            name="loopskill_share_create",
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
            name="loopskill_share_list",
            description=(
                "List share tokens for a cookbook (metadata only, no plaintext). "
                "Returns {tokens: [{id, prefix, name, scope, is_active, created_at, "
                "last_used_at}]}. Requires can_write_cookbook."
            ),
            inputSchema={
                "type": "object",
                "required": ["cookbook_id"],
                "properties": {"cookbook_id": {"type": "string"}},
            },
        ),
        types.Tool(
            name="loopskill_share_revoke",
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
            name="loopskill_share_rotate",
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
    ]


def _fleet_tools() -> list[types.Tool]:
    """Phase E fleet tools (loopskill_* primary names)."""
    return [
        types.Tool(
            name="loopskill_fleet_create",
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
            name="loopskill_fleet_subscribe",
            description=(
                "Subscribe a cookbook to a fleet on a given channel (stable, canary, frozen). Idempotent."
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
            name="loopskill_fleet_sync",
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
            name="loopskill_fleet_list",
            description="List all fleets owned by the caller with their cookbook subscriptions.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


def _publish_tools() -> list[types.Tool]:
    """Publish-request tool (loopskill_* primary name)."""
    return [
        types.Tool(
            name="loopskill_publish_request",
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
                    "content": {"type": "string", "description": "SKILL.md content as a string"},
                    "version": {"type": "string", "default": "1.0.0"},
                    "description": {"type": "string"},
                    "tier": {
                        "type": "string",
                        "default": "pro",
                        # Canonical: free|pro|pro_plus. Legacy aliases cook|operator accepted until 2026-06-10.
                        "enum": ["free", "pro", "pro_plus", "cook", "operator"],
                    },  # cook|operator = legacy aliases
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
    ]


def _tailor_tools() -> list[types.Tool]:
    """integrator_2905 W1 + loopclose_3005 Phase C/I tailor/fork tools."""
    return [
        types.Tool(
            name="loopskill_fork_list",
            description=(
                "List all forks owned by the authenticated user. "
                "Returns fork_id, name, slug, source_slug for each."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="loopskill_tailor",
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
        types.Tool(
            name="loopskill_tailor_version",
            description=(
                "Upload a new version tarball to one of your forks (MCP-native). "
                "The tarball is passed base64-encoded (MCP can't carry multipart "
                "uploads). Mints a fork version and advances the fork's latest "
                "pointer. Step 2 of the tailor loop: tailor -> tailor_version -> "
                "bundle_attach -> bundle_install. Pro tier or above."
            ),
            inputSchema={
                "type": "object",
                "required": ["fork_id", "tarball_base64", "semver"],
                "properties": {
                    "fork_id": {
                        "type": "string",
                        "description": "UUID of the fork to version (from loopskill_tailor).",
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
            name="loopskill_bundle_attach",
            description=(
                "Deploy a tailored fork's latest version into one of your "
                "cookbooks. Promotes the fork into a private catalog skill linked "
                "to the cookbook and mints an installable version, so it installs "
                "byte-identically to any catalog skill via loopskill_bundle_install. "
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
                            "(defaults to the fork slug). "
                            "Must match ^[a-z0-9][a-z0-9_-]{0,63}$."
                        ),
                    },
                },
            },
        ),
        types.Tool(
            name="loopskill_bundle_handoff",
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
                    "cookbook_id": {
                        "type": "string",
                        "description": "UUID of the cookbook to hand off.",
                    },
                    "new_owner_user_id": {
                        "type": "string",
                        "description": "UUID of the new owner (or use new_owner_email).",
                    },
                    "new_owner_email": {
                        "type": "string",
                        "description": "Email of the new owner (or use new_owner_user_id).",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["transfer", "fork"],
                        "default": "transfer",
                    },
                },
            },
        ),
    ]
