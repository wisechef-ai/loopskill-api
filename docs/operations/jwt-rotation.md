# JWT Key Rotation — Operations Guide

This document describes how to rotate the signing key used for WiseRecipes API
JWT tokens without invalidating any tokens currently in circulation.

---

## Background

JWT tokens are issued at OAuth login and expire after `JWT_EXPIRATION_HOURS`
(default **72 hours**).  A single HMAC key (`WR_JWT_SECRET`) signs every token.
Key rotation is needed when:

- A secret has been exposed or is suspected compromised.
- Periodic scheduled rotation is required by your security policy.

---

## Configuration fields

| Env var | Default | Purpose |
|---|---|---|
| `WR_JWT_SECRET` | `wr-jwt-secret-change-me` | Legacy single-key secret |
| `WR_JWT_KEYS` | `""` | JSON dict mapping `kid` → HMAC secret |
| `WR_JWT_ACTIVE_KID` | `""` | The `kid` used to **sign** new tokens |

When `WR_JWT_KEYS` **and** `WR_JWT_ACTIVE_KID` are both set:

- New tokens include a `kid` JOSE header and are signed with the active key.
- The verifier tries the kid-matched key first, then falls back to
  `WR_JWT_SECRET` — so **old tokens never break during a rotation window**.

When either field is empty, the signer/verifier use only `WR_JWT_SECRET`
(identical to pre-rotation behaviour).

---

## Helper CLI

```
python scripts/rotate_jwt_key.py --help
```

### Subcommands

| Command | Description |
|---|---|
| `status` | Show current key-ring state |
| `add-kid --kid <id> [--secret <s>]` | Add a kid (auto-generates secret if omitted) |
| `activate --kid <id>` | Set active kid for new token signing |
| `retire --kid <id>` | Remove an old kid once its tokens have expired |

---

## Step-by-step rotation procedure

### 1 — Generate a new secret

```bash
python -c "import secrets; print(secrets.token_hex(32))"
# example output: a3f8...0b1c
```

### 2 — Add the new kid (without activating)

```bash
python scripts/rotate_jwt_key.py add-kid --kid v2 --secret a3f8...0b1c
# Prints: WR_JWT_KEYS='{"v1":"<old>","v2":"a3f8...0b1c"}'
```

Update your `.env` or secrets vault with the printed value.
**Do not change `WR_JWT_ACTIVE_KID` yet.**

### 3 — Deploy across all instances

All nodes now accept both keys but still **sign** with v1 (or `WR_JWT_SECRET`).
Zero downtime — existing session tokens remain valid.

### 4 — Activate the new kid

```bash
python scripts/rotate_jwt_key.py activate --kid v2
# Prints: WR_JWT_ACTIVE_KID='v2'
```

Set `WR_JWT_ACTIVE_KID=v2` in your environment and redeploy.
From this point new logins produce v2-signed tokens.

### 5 — Wait for old tokens to expire

The default TTL is **72 hours**.  After that window no valid token signed
with the old key is in circulation.

### 6 — Retire the old kid

```bash
python scripts/rotate_jwt_key.py retire --kid v1
# Prints updated WR_JWT_KEYS without v1
```

Update `.env` / vault and redeploy.  Rotation is complete.

---

## Emergency rotation (secret compromised)

If `WR_JWT_SECRET` or a kid secret has been leaked:

1. Immediately add the replacement key and **activate** it (steps 1–4 above).
2. **Force all sessions to re-authenticate** by temporarily setting
   `WR_JWT_EXPIRATION_HOURS=0` and redeploying (all tokens expire immediately),
   then restore the normal expiry and redeploy again.
3. Retire the compromised key.

---

## Backward compatibility guarantee

- If `WR_JWT_KEYS` is not set, `create_jwt` and `verify_jwt` behave exactly as
  they did before G.3 was shipped.
- Tokens issued before G.3 (no `kid` header) are still verified via the
  `WR_JWT_SECRET` fallback path.
- All pre-existing JWT unit tests pass unchanged without any env changes.
