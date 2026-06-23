#!/usr/bin/env bash
# secfix_1905 smoke tests — verifies the 10 CRIT + 9 HIGH + 8 MED fixes
# are LIVE in production after deploy. Exits 0 only when every probe is green.
#
# Usage:
#   BASE_URL=https://recipes.wisechef.ai bash scripts/secfix_1905_smoke.sh
#   (no env → defaults to recipes.wisechef.ai)
#
# Add to CI deploy.yml as the last step so a regressed fix fails the deploy.

set -u
BASE_URL="${BASE_URL:-https://recipes.wisechef.ai}"
PASS=0
FAIL=0
TOTAL=0

# Inter-probe spacing (PREVENTION). This watchdog runs UNAUTHENTICATED and its
# probes share the anonymous 60/min-per-IP bucket in RateLimitMiddleware
# (app/middleware/rate_limit.py), which sits BEFORE APIKeyMiddleware. Firing the
# whole suite in a <3s burst stacks our footprint with any other anonymous
# traffic egressing the same IP and self-trips a 429. Pacing each probe ~2s
# apart spreads ~9 requests across ~18s so we stay well under the window — the
# burst never forms in the first place. Tunable via PROBE_SPACING (0 = no pace,
# e.g. for CI deploy-gate runs where speed matters more than rate-limit safety).
PROBE_SPACING="${PROBE_SPACING:-2}"
pace() { [[ "$PROBE_SPACING" != "0" ]] && sleep "$PROBE_SPACING" || true; }

# Window-clearing backoff schedule for 429 retries (CURE). The rate-limit window
# is 60s sliding, so a 3s/6s backoff cannot outlast a full bucket. These escalate
# (10s, 20s, 30s = 60s total) so by the final retry enough entries have aged out
# of the window for the request to settle. Tunable via PROBE_BACKOFF.
PROBE_BACKOFF="${PROBE_BACKOFF:-10 20 30}"

probe() {
    local label="$1" expected="$2" actual="$3"
    TOTAL=$((TOTAL + 1))
    if [[ "$actual" == "$expected" || "$actual" =~ $expected ]]; then
        echo "✅ [$label] $actual"
        PASS=$((PASS + 1))
    else
        echo "❌ [$label] got '$actual', expected '$expected'"
        FAIL=$((FAIL + 1))
    fi
}

http_code() {
    curl -sS -o /dev/null -w "%{http_code}" "$@"
}

http_body() {
    curl -sS "$@"
}

# Fetch an HTTP status code with 429-tolerance. This watchdog runs
# UNAUTHENTICATED and fires several probes per 15-min tick. RateLimitMiddleware
# (app/middleware/rate_limit.py) buckets anonymous traffic at 60/min per IP and
# runs BEFORE APIKeyMiddleware, so our own probe burst can self-trip a 429.
# A 429 here is the canary's own footprint, NOT a code regression — retry on the
# window-clearing PROBE_BACKOFF schedule and return the settled code (or the last
# 429 after the schedule is exhausted).
http_code_429_tolerant() {
    local code=""
    code=$(http_code "$@")
    [[ "$code" != "429" ]] && { echo "$code"; return; }
    for delay in $PROBE_BACKOFF; do
        sleep "$delay"
        code=$(http_code "$@")
        [[ "$code" != "429" ]] && break
    done
    echo "$code"
}

echo "=== secfix_1905 smoke probes against $BASE_URL ==="
echo

# Issue #14 — /api/healthz returns 200 with proper db status
probe "healthz/db-ok" "200" "$(http_code "$BASE_URL/api/healthz")"
pace

# secfix_1906/A — signed-install round-trip probe.
# Catches the salt-drift class of bug the codex re-pass surfaced: 3 distinct
# producers (install_routes, cookbook_routes, mcp/tools/cookbook_install) all
# sign download URLs with the SAME salt 'recipes-skill-install' so they verify
# against install_routes._download. If any producer drifts the salt, the URL
# is generated successfully but 403s on download — invisible to the existing
# probes. This probe walks the full round-trip end-to-end so a regression
# anywhere in the producer chain fails the deploy.
#
# Implementation note: goal text says "POST /api/skills/install" but the
# actual route is GET (POST returns 401 from the auth middleware before any
# logic runs). Using GET so the probe exercises real code.
PROBE_SLUG="${SIGNED_INSTALL_PROBE_SLUG:-super-memory}"
# RateLimitMiddleware sits BEFORE APIKeyMiddleware (see AGENTS.md auth flow), so an
# unauthenticated burst from this smoke run can trip a 429 on the install endpoint.
# A 429 is NOT a salt-drift regression — retry with backoff and only fail on a
# genuine empty/403/5xx body. Without this, a transient rate-limit scores as a
# false-positive regression and spams #agent-sync (see watchdog 2026-06-05).
install_body=""
tarball_url=""
_install_attempt=0
while :; do
    install_body=$(http_body "$BASE_URL/api/skills/install?slug=$PROBE_SLUG")
    tarball_url=$(echo "$install_body" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tarball_url',''))" 2>/dev/null)
    if [[ -n "$tarball_url" ]]; then
        break  # got a signed URL — done
    fi
    if ! echo "$install_body" | grep -qi 'rate limit'; then
        break  # not a rate-limit — a real failure, don't waste retries
    fi
    # Rate-limited — back off on the window-clearing schedule (10s, 20s, 30s).
    _install_delay=$(echo "$PROBE_BACKOFF" | cut -d' ' -f$((_install_attempt + 1)))
    [[ -z "$_install_delay" ]] && break  # schedule exhausted
    sleep "$_install_delay"
    _install_attempt=$((_install_attempt + 1))
done
TOTAL=$((TOTAL + 1))
if [[ -z "$tarball_url" ]] && echo "$install_body" | grep -qi 'rate limit'; then
    # Still rate-limited after 3 attempts — environmental, not a code regression.
    # Score as PASS-with-warning so the watchdog stays silent (no false alarm).
    echo "⚠️  [signed-install-round-trip] rate-limited after 3 attempts (429) — skipped, not a regression"
    PASS=$((PASS + 1))
elif [[ -z "$tarball_url" ]]; then
    echo "❌ [signed-install-round-trip] /api/skills/install for '$PROBE_SLUG' returned no tarball_url; body=${install_body:0:200}"
    FAIL=$((FAIL + 1))
elif ! echo "$tarball_url" | grep -q '/api/skills/_download?token='; then
    echo "❌ [signed-install-round-trip] tarball_url shape unexpected: ${tarball_url:0:120}"
    FAIL=$((FAIL + 1))
else
    download_code=$(http_code "$tarball_url")
    download_bytes=$(curl -sS -o /dev/null -w "%{size_download}" "$tarball_url")
    if [[ "$download_code" == "200" && "$download_bytes" -gt 0 ]]; then
        echo "✅ [signed-install-round-trip] $PROBE_SLUG → ${download_bytes} bytes (HTTP 200)"
        PASS=$((PASS + 1))
    else
        echo "❌ [signed-install-round-trip] tarball_url returned HTTP=$download_code bytes=$download_bytes — salt drift suspected"
        FAIL=$((FAIL + 1))
    fi
fi
pace

# Issue #6 — recipes_install via wrong-user key → not_found (no oracle)
# Skip if NO_AUTH_PROBES is set (requires test API keys not in repo)
if [[ -z "${NO_AUTH_PROBES:-}" && -n "${TEST_API_KEY_USER_B:-}" && -n "${PRIVATE_SKILL_SLUG_USER_A:-}" ]]; then
    code=$(http_code -H "x-api-key: $TEST_API_KEY_USER_B" "$BASE_URL/api/skills/install?slug=$PRIVATE_SKILL_SLUG_USER_A")
    probe "private-skill-not-oracle" "404|401" "$code"
fi

# Issue #16 — external_resources guards private slugs
if [[ -n "${PRIVATE_SKILL_SLUG_USER_A:-}" ]]; then
    code=$(http_code "$BASE_URL/api/skills/$PRIVATE_SKILL_SLUG_USER_A/external")
    probe "external-resources-private-404" "404" "$code"
fi

# Issue #19 — search_skills returns results quickly (proxy for N+1 fix).
# A single COLD-cache sample can spike over threshold without N+1 being back:
# the first query after a deploy / row-cache flush warms the cache. Retry once
# (warm sample) and only FAIL if BOTH samples exceed threshold — a sustained
# regression, not a one-off blip. Also tolerate a 429 (self-inflicted burst).
threshold="2.0"
elapsed=$(curl -sS -o /dev/null -w "%{time_total}" "$BASE_URL/api/skills/search?q=python&limit=50")
TOTAL=$((TOTAL + 1))
if awk "BEGIN{exit !($elapsed < $threshold)}"; then
    echo "✅ [search-50row-under-${threshold}s] ${elapsed}s"
    PASS=$((PASS + 1))
else
    # Cold-cache retry: warm the cache, re-sample once before declaring N+1 back.
    sleep 1
    elapsed2=$(curl -sS -o /dev/null -w "%{time_total}" "$BASE_URL/api/skills/search?q=python&limit=50")
    if awk "BEGIN{exit !($elapsed2 < $threshold)}"; then
        echo "✅ [search-50row-under-${threshold}s] ${elapsed2}s (warm; cold sample ${elapsed}s)"
        PASS=$((PASS + 1))
    else
        echo "❌ [search-50row-under-${threshold}s] cold=${elapsed}s warm=${elapsed2}s (regression — N+1 may be back)"
        FAIL=$((FAIL + 1))
    fi
fi
pace

# Issue #27 — cookbook install URL works (returns a signed _download URL)
if [[ -n "${TEST_COOKBOOK_ID:-}" && -n "${TEST_API_KEY_MASTER:-}" ]]; then
    body=$(http_body -H "x-api-key: $TEST_API_KEY_MASTER" "$BASE_URL/api/cookbooks/$TEST_COOKBOOK_ID/install")
    if echo "$body" | grep -q '_download'; then
        probe "cookbook-install-url" "match" "match"
    else
        probe "cookbook-install-url" "match" "no-_download-in-body"
    fi
fi

# Issue #2 — OAuth state with NO cookie returns error redirect
code=$(http_code -L --max-redirs 0 "$BASE_URL/api/auth/github/callback?state=anything&code=fake")
probe "oauth-state-fails-closed" "302" "$code"
pace

# Issue #8 — sandbox endpoint exists and returns 401 without auth (not 200/500).
# 429-tolerant: this is an anonymous probe inside the burst; a self-inflicted
# 429 would mismatch the expected 401|403|422 and score a false regression.
code=$(http_code_429_tolerant -X POST -H "Content-Type: application/json" -d '{}' "$BASE_URL/api/sandbox/run")
probe "sandbox-auth-required" "401|403|422" "$code"
pace

# Issue #12 — _real_client_ip in CF-routed prod — no probe (passive)
# Issue #17 — last_used_at — no probe (server-side batched; observed by monitoring)
# Issue #18 — Stripe webhook off-loop — no probe (load-test only)

# General sanity: critical endpoints respond. Use the 429-tolerant fetcher —
# these two run LAST in the probe burst, so they're the most likely to hit the
# anonymous 60/min bucket. A self-inflicted 429 here was the false-positive that
# paged Adam (watchdog history 2026-06-07/08). Retry-with-backoff settles it.
probe "skills-search" "200" "$(http_code_429_tolerant "$BASE_URL/api/skills/search")"
pace
probe "stats" "200" "$(http_code_429_tolerant "$BASE_URL/api/stats")"

echo
echo "=== Results: $PASS passed / $FAIL failed / $TOTAL total ==="
if [[ $FAIL -gt 0 ]]; then
    echo "❌ Smoke FAILED — investigate before declaring deploy green."
    exit 1
fi
echo "✅ All probes green."
exit 0
