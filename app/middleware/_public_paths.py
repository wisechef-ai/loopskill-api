"""Public (no-auth) path allowlists for APIKeyMiddleware.

Extracted from ``api_key.py`` to keep that module under the NEVER-waived
600-line god-object cap (test_w0_2_pyfile_size_discipline). Pure data — no
imports, no logic — so it's a config surface, not middleware behavior.

DUAL-SURFACE NOTE: the bundle surface is primary; ``/api/cookbooks/*`` paths
are retained as backward-compat aliases. Public discovery/leaderboard paths
appear for BOTH surfaces so neither 401s where the other is open.
"""

# Prefixes whose requests skip API-key auth entirely (public read surfaces).
PUBLIC_PREFIXES: tuple[str, ...] = (
    "/api/carousel/",
    "/api/bootcamp",  # bootcamp_0607 — curated install curricula, public discovery (list + detail)
    "/api/skills/search",
    "/api/skills/trending",
    "/api/skills/access",
    "/api/skills/_download",
    "/api/skills/external",  # evergreen_0206 F2 — external-only funnel: public discovery + fetch-origin install
    "/api/stats",
    "/api/forks/_download",
    "/api/graph",  # B.5: graph extension — public read; master-only write enforced inline
    # Phase D — anonymous heartbeat write endpoint (mathematically anonymous
    # schema). The READ endpoint /api/v1/fleet/weekly is gated separately.
    "/api/v1/heartbeat",
    # Phase A v2 — MCP healthz/discovery is unauthenticated so MCP clients can
    # probe availability before sending credentials. SSE transport + message
    # endpoint remain auth-required and re-validate per request.
    "/api/mcp/healthz",
    # top1pct_1105 Phase A — marketing counts is the SSOT for every public
    # surface (hero, /skills, /pricing, /docs); reachable without auth, no PII.
    "/api/marketing/",
    # spotify_0608 Ph B — public discovery (CRUD stays auth-gated). Dual-surface:
    # bundle primary + cookbook compat alias both public.
    "/api/bundles/discover",
    "/api/bundles/public/",
    "/api/cookbooks/discover",  # compat-alias
    "/api/cookbooks/public/",  # compat-alias
    # spotify_0608 Ph G — public reputation leaderboards (verify stays auth-gated).
    "/api/bundles/leaderboard",
    "/api/cookbooks/leaderboard",  # compat-alias
    # marketing_1205 — UTM redirectors for social platforms. Public, set cookie + 302.
    "/x/",
    "/li/",
    "/ig/",
    "/yt/",
    "/fb/",
)
