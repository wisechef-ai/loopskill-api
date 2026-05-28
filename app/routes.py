"""API routes — backward-compat shell module (Phase L — topshelf_2605).

After Phase L, logic lives in dedicated modules:
  - app/core_routes.py      → POST /api/telemetry, GET /api/stats
  - app/marketing_routes.py → GET /api/marketing/counts|snapshot
                               + wisechef_router: GET/POST /api/wisechef/demo-*
  - app/health_routes.py    → GET /healthz
  - app/skill_routes.py     → GET /api/skills/* (search, trending, detail, etc.)
  - app/install_routes.py   → GET /api/skills/install + GET /api/skills/_download
  - app/access_routes.py    → GET /api/skills/access
  - app/recipe_routes.py    → GET /api/recipes/{slug} + GET /api-library/{slug}
  - app/utm_redirects.py    → /x/, /li/, /ig/, /yt/, /fb/ short-link redirectors
  - app/_skill_helpers.py   → pure helper functions + get_retired_set()

This file re-exports everything that outside callers imported from app.routes so
that ``from app.routes import X`` continues to work for one release window.
Tracked for removal in topshelf_2606.
"""

# ── Core router (telemetry + stats) — main.py imports router from here ──────
from app.core_routes import router  # noqa: F401
from app.core_routes import VERSION  # noqa: F401

# ── UTM router — main.py imports utm_router from here ───────────────────────
from app.utm_redirects import utm_router  # noqa: F401

# ── Backward-compat re-exports from _skill_helpers ──────────────────────────
from app._skill_helpers import (  # noqa: F401
    _UTM_COOKIE_MAX_AGE,
    _UTM_COOKIE_NAME,
    _UTM_REF_ALLOWLIST,
    GRAPH_RAIL_CAP,
    RELATED_SKILLS_CAP,
    _build_manifest,
    _count_today_installs,
    _hydrate_skill_outs,
    _install_counts_for,
    _resolve_caller_tier_for_install,
    _resolve_related,
    _set_utm_ref_cookie,
    _skill_to_out,
    get_retired_set,
)

# ── Backward-compat re-exports from split route modules ─────────────────────
from app.access_routes import TIER_INSTALL_LIMITS, TIER_RANK  # noqa: F401
from app.install_routes import download_tarball  # noqa: F401
from app.skill_routes import (  # noqa: F401
    get_full_skill_graph,
    get_skill_detail,
    get_skill_external,
    get_skill_graph,
    get_skill_related,
    search_skills,
    trending_skills,
)
