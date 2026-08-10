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
    "/api/bootcamp",  # bootcamp_0607 — curated install curricula, public discovery (list + detail)
    "/api/skills/search",
    "/api/skills/trending",
    "/api/skills/access",
    "/api/skills/_download",
    "/api/skills/external",  # evergreen_0206 F2 — external-only funnel: public discovery + fetch-origin install
    "/api/skills/metasearch",  # metasearch_0710 P0 — unified query-time router (one ranked list, public discovery)
    # loopskill_portal_0627 — the runnable loop REGISTRY is the #1 stars-conversion
    # surface; its browse (GET /api/loops) + detail (GET /api/loops/{slug}) MUST be
    # publicly readable so the portal hero can render without baking a key into
    # client JS. WRITES stay protected: run_loop / rate_loop / publish_loop each
    # self-enforce auth via request.state.auth_ctx scope (401 anonymous), so this
    # method-agnostic prefix does NOT expose them.
    "/api/loops",
    # loopskill_activate_0701 Phase A1 — canonical verifier surface. Dual-mount:
    # /api/verifiers serves the SAME handlers as /api/loops with byte-identical
    # payloads (no redirect). Public-read mirrors /api/loops; writes self-enforce
    # auth (401 anonymous) so the method-agnostic prefix does NOT expose them.  # compat-alias
    "/api/verifiers",
    # activate_0701 Phase B — connector registry: same shape as loops. GET
    # browse/detail are public; publish/versions/bundle-declare self-enforce
    # auth (401 anon), so the method-agnostic prefix does NOT expose writes.
    "/api/connectors",
    # loopskill_portal_0627 — personalities registry: same shape as loops. GET
    # browse/detail are public so the portal /personalities page renders without
    # a key; publish_personality self-enforces auth (401 anon), so the
    # method-agnostic prefix does NOT expose writes.
    "/api/personalities",
    # loopskill_activate_0701 Phase A2 — composite loop registry: NEW surface
    # (council §6). GET browse/detail are public so the portal renders without a
    # key; publish + version-publish self-enforce auth (401 anon) so the
    # method-agnostic prefix does NOT expose writes.
    "/api/composite-loops",
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
    # feat/unified-search — GET /api/search: anonymous cross-type search (skills,
    # loops, bundles, personalities). Each group already filters to the SAME
    # public-visibility rows as its dedicated public route (see
    # app/services/unified_search.py for the copied filter provenance), so this
    # method-agnostic prefix does not expose anything the per-type public
    # routes don't already expose. There is no write verb on this path.
    "/api/search",
)


# fdeloop_0808 Phase D — public plugin.json manifest surface. Matches
# GET /api/bundles/{slug}/plugin.json (and the /api/cookbooks/* compat
# alias) WITHOUT exposing the rest of the bundle-detail CRUD surface, which
# stays auth-gated. Kept as a function (not a plain prefix string) because a
# bare "/plugin.json" suffix match is needed, not a prefix match — a prefix
# entry would also have to special-case every other bundle sub-route.
def is_public_plugin_manifest_path(path: str, method: str) -> bool:
    """True for a public-bundle Agent Plugins manifest GET.

    Visibility is enforced by the route handler itself (404 for a private or
    unknown slug — see app/bundle_routes.py:_build_plugin_manifest and its
    caller); this only decides whether the request needs an x-api-key at all.
    """
    if method != "GET":
        return False
    return path.startswith(("/api/bundles/", "/api/cookbooks/")) and path.endswith("/plugin.json")


# Exact paths that skip API-key auth entirely.
# Moved here from ``api_key.py`` (mesh_0408 T0-D) for the same reason
# PUBLIC_PREFIXES was: adding the two mesh discovery endpoints pushed that
# module to 603 lines, over the NEVER-waived 600-line cap. Pure data, so it
# belongs on this config surface rather than in middleware behavior.
EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/docs",
        "/openapi.json",
        "/redoc",
        "/healthz",
        "/",
        "/api/healthz",
        "/api/health",  # spotify_1507 Ph0: public liveness status (no auth, no DB)
        "/api/health/transparency",  # Stream 0: public transparency scorecard
        # loopclose_3005 Phase B — canonical /skill serve (the install front-door
        # printed on the hero + every integrations card). MUST be public so an
        # agent can curl the meta-skill before it has a key. Serves the clean
        # in-repo SKILL.md as text/plain; no PII. (app/skill_serve_routes.py)
        "/skill",
        "/skill/",
        "/SKILL.md",
        # fleetos_1607 T — fleet-control-plane SKILL.md (public GET-only doc).
        "/fleet/skill",
        "/fleet/skill/",
        "/fleet/SKILL.md",
        # mesh_0408 T0-D — mesh credential discovery (spec §9, §9.1).
        # Unauthenticated BY DESIGN: verification must need no credential.
        "/.well-known/jwks.json",
        "/.well-known/oauth-authorization-server",
    }
)
