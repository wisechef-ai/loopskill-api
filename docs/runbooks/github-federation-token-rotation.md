# Runbook: GitHub federation token rotation

**Owner surface:** `github-oss` federated source (`app/services/federation_live.py`)
**Token identity:** a GitHub fine-grained personal access token, **public
read-only scope**. No repo write, no org access.
**Where it lives:** Bitwarden item `loopskill-github-federation-token`.
**Expiry:** 1 year from mint date (GitHub fine-grained token max lifetime).
**Env var:** `GITHUB_TOKEN` (or `GH_TOKEN` — either name is accepted).

This document does **not** contain the token value. Never commit a token
value to this repo, in this file or anywhere else.

---

## 1. The silent symptom — read this before you assume the token is fine

When the token expires, is revoked, or is simply absent from the server's
environment, `github-oss` does **not** error. `app/services/federation_live.py`
degrades **gracefully** by design (a GitHub outage or missing token must never
500 the metasearch route):

```python
def github_oss_fetch(query: str) -> list[dict[str, Any]]:
    token = _github_token()
    if not token:
        logger.info("github-oss fetch skipped: no GITHUB_TOKEN configured (graceful empty)")
        return []
```

The observable result on prod:

```
federation_index_cache:
  source = github-oss
  indexed_count = 0
  last_error = NULL      <-- looks healthy
```

**`indexed_count = 0` with `last_error = NULL` is the failure signature.**
There is no red flag anywhere else — the reindex cron reports `status=ok`,
the `/api/skills/external` route returns 200, and every other source keeps
working normally. This is exactly what happened on prod between the
loopskill-api repo rename and 2026-08-10: the token was never wired into the
new `.env`, and github-oss sat at 0 for days with nobody noticing until it
was probed directly.

**Do not conflate this with `well-known`'s legitimate zero.** `well-known` is
discovery-by-URL (no central catalog to walk) and is *always* zero by design
— see `STRUCTURALLY_EMPTY_SOURCES` in `app/services/federation_live.py`.
`github-oss` at zero is never by design; it is always either "no results for
this query" (rare — see §4) or "the token is gone."

### P3.9 fix — the ambiguity is now broken automatically

As of this phase, `app/services/federation_live.py` exposes
`token_gated_source_missing_auth(source_id)`, and
`scripts/federation_reindex.py` calls it on every walk: if `github-oss` comes
back with `indexed=0` **and** no `GITHUB_TOKEN`/`GH_TOKEN` is set in the
process environment, the walker now writes an explicit `last_error` instead
of leaving it `NULL`:

```
github-oss: GITHUB_TOKEN/GH_TOKEN not set — token-gated source silently
returned zero (see docs/runbooks/github-federation-token-rotation.md)
```

So the verification query in §2 below is now sufficient on its own — a
`NULL` `last_error` alongside `indexed_count = 0` means the walk genuinely
found nothing for the sample query that run, not that auth is missing.

---

## 2. Verification SQL

Run this after any deploy, `.env` change, or when investigating a suspiciously
low federation count:

```sql
SELECT source, indexed_count, installable_count, last_error, walked_at
FROM federation_index_cache
WHERE source = 'github-oss';
```

Interpret the result:

| `indexed_count` | `last_error` | Meaning |
|---|---|---|
| `> 0` | `NULL` | Healthy. Token present and working. |
| `0` | mentions `GITHUB_TOKEN`/`GH_TOKEN` | **Token missing from the server env.** Go to §3. |
| `0` | `NULL` | Token present; today's walk genuinely found nothing (rare — code-search is query-dependent). Not an incident. |
| `0` | some other message | A real fetch/API error (rate-limited, GitHub outage, malformed request). Check `wiserecipes-logs/fed_reindex.log` for the exception. |

A quick live-probe alternative (no DB access required):

```bash
curl -sfm10 'https://app.loopskill.io/api/skills/external?limit=1&sources=github-oss&q=terraform' \
  | jq '.per_source["github-oss"]'
```

---

## 3. Rotation steps

### 3a. Verify the CURRENTLY deployed token first

Before touching Bitwarden, confirm whether the deployed token is actually
invalid (an unused vault session is a success, not an oversight):

```bash
# On the host running the API (reads its own env, does NOT log the token value)
ssh <api-host> "cd <repo-path> && set -a; . ./.env; set +a; \
  curl -sf -H \"Authorization: Bearer \$GITHUB_TOKEN\" https://api.github.com/rate_limit | jq '.resources[\"search\"]'"
```

- `200` with a `search` rate-limit block → **token is valid.** The zero is
  something else — re-check §2's table, don't rotate.
- `401` → token is invalid/expired/revoked. Proceed to rotation.

### 3b. Mint a new fine-grained token

1. https://github.com/settings/personal-access-tokens/fine-grained → **Generate new token**.
2. Scope: **public repositories (read-only)** only. No org access, no write
   permissions of any kind.
3. Expiration: 1 year (GitHub's max for a fine-grained token — there is no
   "no expiry" option, which is why this runbook exists).
4. Copy the token value **once** — GitHub will not show it again.

### 3c. Store the new token in Bitwarden

Update the existing item — do not create a duplicate (see the
`bitwarden-key-extraction` skill's duplicate-item pitfall):

```bash
bw get item "loopskill-github-federation-token" > /tmp/item.json
# edit /tmp/item.json's notes/password field with the new token value
bw encode < /tmp/item.json | bw edit item "$(jq -r .id /tmp/item.json)"
rm /tmp/item.json
```

Or via the Bitwarden UI: open `loopskill-github-federation-token`, replace
the token value, save.

### 3d. Deploy the new token to the server

The token is read from the API host's `.env` file (`GITHUB_TOKEN=...`) by
**both** the running API process and the 03:00 reindex cron — see §4 for why
that dual-consumption matters.

```bash
ssh <api-host>
cd <repo-path>
# replace the GITHUB_TOKEN line in .env with the new value (never echo it)
sed -i '/^GITHUB_TOKEN=/d' .env
echo "GITHUB_TOKEN=<new-token>" >> .env
```

Restart the API process so the new env is picked up (the reindex cron always
re-sources `.env` fresh on every run, so it needs no separate restart):

```bash
sudo systemctl restart loopskill-api   # or whatever the service unit is named
```

### 3e. Confirm the rotation worked

```bash
# Run the reindex script manually rather than waiting for 03:00
ssh <api-host> "cd <repo-path> && set -a; . ./.env; set +a; \
  venv/bin/python scripts/federation_reindex.py --source github-oss"
```

Then re-run the §2 verification query and confirm `indexed_count > 0` with
`last_error = NULL`.

---

## 4. Why the cron matters as much as the running process

The GitHub token is consumed by **two independent processes** on the API
host:

1. The **running uvicorn/API process** — for any live query with
   `sources=github-oss` enabled, or an admin `?refresh=1`.
2. The **03:00 daily reindex cron** (`recipes-federation-reindex`) — this is
   what fills `federation_index_cache` so a cold page load never triggers an
   inline walk.

Both read `.env` independently. The crontab entry is:

```
0 3 * * * cd /home/wisechef/loopskill-api && set -a; . ./.env; set +a; \
  venv/bin/python scripts/federation_reindex.py >> /home/wisechef/wiserecipes-logs/fed_reindex.log 2>&1
```

**This `set -a; . ./.env; set +a;` sourcing is not optional** — it was
missing after the `recipes-api` → `loopskill-api` repo rename, which silently
zeroed the token for the cron (verified live 2026-08-10) while the running
API process, launched by systemd with its own `EnvironmentFile=`, kept
working. That asymmetry is exactly how a token can look "fine" (API responds,
live single-query fetches work if a caller passes one manually) while the
persisted `federation_index_cache` — the thing `/api/skills/external` reads
on every cold load — silently stays at zero.

**After any cron edit, verify it still holds** by checking the crontab
literally sources `.env` before invoking `federation_reindex.py`:

```bash
ssh <api-host> "crontab -l | grep federation_reindex"
```

The path must point at the current repo location and venv (`loopskill-api` /
`venv/`, not a stale `wiserecipes-api` / `.venv/` path from before any repo
rename), and the `. ./.env` sourcing must be present. If either drifts, the
token silently stops reaching the cron even though the systemd-run API
process is unaffected — the exact split-brain failure mode this runbook
exists to prevent.

---

## 5. Rate-limit reality — do not "fix" this by requesting more scopes

GitHub's code-search API rate-limits authenticated callers to **10
requests/minute**, not the 5,000/hour the REST API otherwise allows. This is
a hard GitHub-side limit tied to the `search/code` endpoint, not something a
different token scope or org membership changes. `github-oss` is therefore a
**live long-tail lookup resolved per query**, never a bulk index — the daily
reindex cron's `indexed_count` for this source reflects one sample page, not
an owned catalog size. See `app/services/federation.py`'s module docstring
and `docs/SELF_HOST.md` for the same framing stated to self-hosters.
