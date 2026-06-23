# Cookbook Share Tokens

> "Create a personal cookbook, fill it with skills, drop a token on another agent, watch them install everything." That's the offering — this is how the auth actually works.

## TL;DR

```bash
# 1. Owner: create a cookbook + add skills + mint a share token
python3 tools/recipes_cli.py share <COOKBOOK_ID> --name "to-varys"

# 2. Recipient: paste the token into MCP config, call recipes_cookbook_install
recipes_cookbook_install                       # bulk: install ALL skills in the cookbook
recipes_cookbook_install slug=atomic-habits    # single-skill: install just this one
```

## Scope vocabulary

Three values, from least to most authority:

| Scope     | GET routes | POST `/install` | POST other (skills, rotate, …) | Use case |
|-----------|------------|-----------------|-------------------------------|----------|
| `read`    | ✅         | ❌              | ❌                            | Audit / read-only review |
| `install` | ✅         | ✅              | ❌                            | **Default.** Share-with-another-agent for installation. |
| `edit`    | ✅         | ✅              | ✅                            | Co-author collaborator with write access. |

Default since 2026-05-21 is **`install`**. Rationale: the offering literally says "give them a token, they install" — that's the user expectation. `read` is still one CLI flag away (`--read-only`).

`edit` is still available and is unchanged from the pre-2026-05-21 behaviour — owner-equivalent operations on the cookbook. It does NOT grant the ability to mint further child tokens (no privilege loop).

## How a recipient installs

Once the owner sends the token (`cbt_<8hex>_<32hex>`), the recipient drops it into MCP config under `x-api-key` and gets two install entry points:

### MCP tool

```jsonc
// Bulk: install every active skill in the cookbook
{ "tool": "recipes_cookbook_install", "args": {} }

// Single skill (slug from the cookbook)
{ "tool": "recipes_cookbook_install", "args": { "slug": "atomic-habits-self-improvement-engine" } }
```

Returns the same payload shape as `recipes_install` (single-skill) and `POST /api/cookbooks/{id}/install` (bulk): `{slug, version, tarball_url, checksum_sha256, source}` per skill. The recipient agent fetches each tarball via the signed URL and unpacks.

### REST equivalents (if not using MCP)

| Operation     | Endpoint                                                |
|---------------|---------------------------------------------------------|
| Manifest      | `GET  /api/cookbooks/{cookbook_id}/manifest`            |
| Bulk install  | `POST /api/cookbooks/{cookbook_id}/install`             |
| Single skill  | `GET  /api/cookbooks/{cookbook_id}/skills/{slug}/install` |
| Sync          | `GET  /api/cookbooks/{cookbook_id}/sync?since=…`        |

All four routes accept the share token via the `x-api-key` header. The token is scoped to ONE specific cookbook (encoded in its prefix); attempts to read a different cookbook return `403 Token scope mismatch (wrong cookbook)`.

## What the recipient CANNOT do (with any scope)

Hard-coded in middleware, independent of scope:

- Access any non-cookbook route (`/api/skills/*`, `/api/users/*`, …) → `403`
- Publish a skill (`/_publish` anywhere in path) → `403`
- Mint a child share-token from this cookbook → `403` (privilege-loop guard)
- Read skills NOT in the scoped cookbook (private skills outside) → `404` (no oracle)

## Migration notes (2026-05-21)

Existing tokens are unaffected: their stored `scope` (`read` or `edit`) stays exactly as-is. The migration widens the allowed-set from `{read, edit}` to `{read, edit, install}` and flips the server-side `DEFAULT` to `install` for **new** rows only.

Auto-upgrade was deliberately NOT done. A recipient holding a `read` token agreed to read-only — auto-promoting to `install` is a silent privilege expansion. Cookbook owners can rotate any token via the existing rotate flow (`recipes_share_rotate` MCP tool, or `POST /api/cookbooks/{id}/share-tokens/{token_id}/rotate`) if they want to upgrade the recipient.

## Internals

- Token format: `cbt_<8-hex-cookbook-prefix>_<32-hex-random>`. Only the SHA-256 hash is stored; plaintext returned exactly once at creation/rotate.
- Middleware: `app/middleware.py:382-454` (cbt_ token path). Stamps `AuthContext(scope="cbt_token", cookbook_scope=<uuid>)` on `request.state.auth_ctx`.
- Authz predicate: `app/authz.py:can_read_skill` clause 4 — `ctx.scope == "cbt_token" and ctx.cookbook_scope == cookbook_id_of(skill)` via `CookbookSkill` join.
- Routes: `app/cookbook_routes.py` (manifest, install, single-skill install, sync, …).
- MCP tool: `app/mcp/tools/cookbook_install.py:recipes_cookbook_install`.
- Migration: `alembic/versions/d8c8a3f721ec_cookbook_share_install_scope.py`.

## Revoke

```bash
python3 tools/recipes_cli.py revoke <COOKBOOK_ID> <TOKEN_ID>
# or DELETE /api/cookbooks/{cookbook_id}/share-tokens/{token_id}
```

Soft-delete (`is_active=False`). Instant — the next request from the recipient gets `401 Invalid or revoked share token`.
