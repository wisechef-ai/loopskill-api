#!/usr/bin/env python3
"""Generate the route manifest FROM the running FastAPI app (P7, bundles_0811).

Source of truth: ``app.main.app.routes`` (the registered FastAPI surface) AND
``app.middleware.api_key.APIKeyMiddleware`` (the auth gate that actually
decides who can reach each route). The manifest classifies every route as
``public`` / ``authenticated`` / ``admin`` / ``internal`` by replaying the
middleware's dispatch() decision tree against the route's (method, path),
with two additions:

* admin / internal routes are detected from path prefix + module, because
  those gates live inside the handler, not in the middleware (admin routes
  require the master key and check ``request.state.api_key_user_id is None``
  in the handler body; internal routes use ``X-Internal-Token``).

Re-run this script any time routes change; the committed
``docs/route-manifest.json`` + ``docs/route-manifest.md`` must match its
output byte-for-byte — enforced by
``tests/test_route_manifest_regenerates.py``.

Usage:
    python scripts/generate_route_manifest.py            # print summary
    python scripts/generate_route_manifest.py --write     # (re)write the docs/ files
"""

from __future__ import annotations

import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Must be set before any app.* import so Settings() doesn't raise the
# production-secrets RuntimeError. Mirrors repo-root conftest.py exactly —
# this script runs in dev/CI, never against a live prod DB.
os.environ.setdefault("WR_DATABASE_URL", "sqlite:///./test_dev.db")
os.environ.setdefault("WR_COOKIES_SECURE", "false")
os.environ.setdefault("WR_STRIPE_PRICE_PRO", "price_test_pro")
os.environ.setdefault("WR_STRIPE_PRICE_PRO_PLUS", "price_test_pro_plus")

MANIFEST_JSON = REPO_ROOT / "docs" / "route-manifest.json"
MANIFEST_MD = REPO_ROOT / "docs" / "route-manifest.md"


def _route_source(route: Any) -> str:
    """Best-effort source text for a route's endpoint function.

    Rationale: some endpoints are wrapped (functools.wraps) or dynamically
    generated; inspect.getsource can raise OSError for those. Fall back to
    an empty string rather than crashing the whole manifest over one route.
    """
    try:
        return inspect.getsource(route.endpoint)
    except (OSError, TypeError):
        # Rationale: dynamically-built or C-level endpoints have no
        # retrievable source; classify from path/tags signals only.
        return ""


def _classify(method: str, path: str, module: str, tags: list[str], source: str) -> str:
    """Replay APIKeyMiddleware.dispatch()'s decision tree for a route.

    Returns one of: ``public`` | ``authenticated`` | ``admin`` | ``internal``.

    Order mirrors dispatch() exactly. Two additions cover gates that live
    inside the handler rather than the middleware:

    * admin — path prefix ``/api/admin`` OR module ``app.admin_routes``
    * internal — path prefix ``/api/internal`` OR X-Internal-Token source
      signal
    """
    # 1. Handler-level admin gate (master key) — runs AFTER the middleware
    #    lets the request through, so we detect these from module/path, not
    #    from the middleware's prefixes.
    if path.startswith("/api/admin") or module == "app.admin_routes" or "admin" in tags:
        return "admin"
    # 2. Handler-level internal gate (X-Internal-Token).
    if (
        path.startswith("/api/internal")
        or module == "app.internal_routes"
        or "internal" in tags
        or "X-Internal-Token" in source
        or "INTERNAL_PATCH_TOKEN" in source
    ):
        return "internal"

    # ── Mirror APIKeyMiddleware.dispatch() (see app/middleware/api_key.py) ──
    # Exempt (exact match) → public.
    from app.middleware._public_paths import EXEMPT_PATHS, PUBLIC_PREFIXES
    from app.middleware._public_paths import is_public_plugin_manifest_path
    from app.middleware.api_key import APIKeyMiddleware

    if path in EXEMPT_PATHS:
        return "public"
    if path.startswith("/docs/") or path == "/docs":
        return "public"
    # Stripe webhook: signature-verified, not api-key-gated. Not user-facing.
    if path in APIKeyMiddleware.WEBHOOK_PATHS:
        return "internal"
    # JWT cookie prefix: these routes bypass the x-api-key gate and require
    # a valid wr_jwt cookie instead (enforced by the routes themselves). They
    # are authenticated surfaces — just via a different credential.
    jwt_prefixes = tuple(APIKeyMiddleware.JWT_AUTH_PREFIXES)
    if path.startswith(jwt_prefixes):
        return "authenticated"
    # Public prefixes → public (opportunistic auth still applies, but no
    # credential is required).
    if path.startswith(tuple(PUBLIC_PREFIXES)):
        return "public"
    if is_public_plugin_manifest_path(path, method):
        return "public"
    # Method-aware public POST-only paths.
    if method == "POST" and path in APIKeyMiddleware.PUBLIC_POST_ONLY_PATHS:
        return "public"
    # mesh_0408 T3-A — GET /api/orgs/{org_id}/a2a-directory uses a bearer
    # token, not x-api-key. Authenticated.
    if method == "GET" and path.startswith("/api/orgs/") and path.endswith("/a2a-directory"):
        return "authenticated"
    # Public skill-detail GETs — /api/skills/{slug} and {slug}/{related,graph,
    # files,file} but NOT the auth verbs install/_download/_publish/_audit.
    if method == "GET" and path.startswith("/api/skills/"):
        tail = path[len("/api/skills/") :]
        auth_verbs = APIKeyMiddleware.PUBLIC_SKILL_DETAIL_AUTH_VERBS
        # Free-tier install without a key is explicitly public.
        if tail == "install":
            return "public"
        if tail == "graph":
            return "public"
        if "/" not in tail and not tail.startswith("_") and tail not in auth_verbs:
            return "public"
        if "/" in tail:
            slug, _, suffix = tail.partition("/")
            if (
                slug
                and not slug.startswith("_")
                and slug not in auth_verbs
                and suffix in {"files", "file", "related", "graph"}
            ):
                return "public"
    # Everything else hits the ``if not key: return 401`` branch → requires
    # an x-api-key. Still authenticated.
    return "authenticated"


def build_manifest() -> dict[str, Any]:
    from fastapi.routing import APIRoute  # noqa: PLC0415

    from app.main import app  # noqa: PLC0415

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        module = getattr(route.endpoint, "__module__", "")
        source = _route_source(route)
        for method in sorted(route.methods or []):
            if method == "HEAD":
                continue  # implicit companion of GET, not a distinct surface
            key = (method, route.path)
            if key in seen:
                continue
            seen.add(key)
            classification = _classify(
                method,
                route.path,
                module,
                [str(t) for t in (route.tags or [])],
                source,
            )
            doc = (route.endpoint.__doc__ or "").strip().splitlines()
            summary = doc[0].strip() if doc else ""
            rows.append(
                {
                    "method": method,
                    "path": route.path,
                    "name": route.name,
                    "module": module,
                    "tags": sorted(str(t) for t in (route.tags or [])),
                    "classification": classification,
                    "summary": summary,
                }
            )

    rows.sort(key=lambda r: (r["path"], r["method"]))
    by_class: dict[str, int] = {}
    for r in rows:
        by_class[r["classification"]] = by_class.get(r["classification"], 0) + 1

    return {
        "source": (
            "app.main.app.routes + app.middleware.api_key.APIKeyMiddleware "
            "(dispatch tree replayed), derived at import time"
        ),
        "generator": "scripts/generate_route_manifest.py",
        "route_count": len(rows),
        "counts_by_classification": dict(sorted(by_class.items())),
        "routes": rows,
    }


def render_markdown(manifest: dict[str, Any]) -> str:
    lines = [
        "<!-- AUTO-GENERATED by scripts/generate_route_manifest.py. Do not hand-edit. -->",
        "<!-- Regenerate: python scripts/generate_route_manifest.py --write -->",
        "",
        "# Route Manifest (bundles_0811 P7)",
        "",
        f"Derived from `app.main.app.routes` cross-referenced with the actual "
        f"auth gate (`app.middleware.api_key.APIKeyMiddleware`) — **"
        f"{manifest['route_count']}** distinct (method, path) surfaces.",
        "",
        "## How classification works",
        "",
        "Each (method, path) pair is classified by replaying "
        "`APIKeyMiddleware.dispatch()`'s decision tree, with two additions for "
        "gates that live inside the handler body rather than the middleware:",
        "",
        "1. **admin** — path prefix `/api/admin` or module `app.admin_routes`. "
        "Requires the master key; handler checks `api_key_user_id is None` "
        "and raises 403 otherwise.",
        "2. **internal** — path prefix `/api/internal` or source contains "
        "`X-Internal-Token` / `INTERNAL_PATCH_TOKEN`. Gated by a shared "
        "secret between the API and the GitHub Actions workflow.",
        "3. **public** — `dispatch()` lets the request through with no "
        "credential: exact `EXEMPT_PATHS` (healthz, `/`, `.well-known`, "
        "`/skill`), `PUBLIC_PREFIXES` (`/api/skills/search`, `/api/loops`, "
        "`/api/marketing/`, `/x/`…), method-aware `PUBLIC_POST_ONLY_PATHS`, "
        "the public plugin-manifest path, and the special skill-detail GET "
        "shape `/api/skills/{slug}` (excluding install/_download/_publish/_audit).",
        "4. **authenticated** — everything else: the `if not key: return 401` "
        "branch. Requires a valid `x-api-key`, `wr_jwt` cookie, or `cbt_` share "
        "token. The JWT-auth prefixes (`/api/auth/`, `/api/checkout/`, …) are "
        "also authenticated — just via cookie/JWT instead of x-api-key.",
        "5. **internal** — the Stripe webhook (`/api/stripe/webhook`) uses "
        "signature verification, not a user credential.",
        "",
        "Because the middleware is the source of truth, this manifest "
        "reflects the **actually enforced** posture — not the docstring's "
        "intent.",
        "",
        "## Counts by classification",
        "",
        "| Classification | Routes |",
        "|---|---|",
    ]
    for cls, count in sorted(manifest["counts_by_classification"].items()):
        lines.append(f"| {cls} | {count} |")
    lines += [
        "",
        "## Full manifest",
        "",
        "| Method | Path | Classification | Module | Tags |",
        "|---|---|---|---|---|",
    ]
    for r in manifest["routes"]:
        tags = ", ".join(r["tags"]) or "-"
        lines.append(
            f"| {r['method']} | `{r['path']}` | {r['classification']} " f"| `{r['module']}` | {tags} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    manifest = build_manifest()
    write = "--write" in sys.argv

    print(f"route_count: {manifest['route_count']}")
    for cls, count in sorted(manifest["counts_by_classification"].items()):
        print(f"  {cls}: {count}")

    if write:
        MANIFEST_JSON.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n")
        MANIFEST_MD.write_text(render_markdown(manifest))
        print(f"wrote {MANIFEST_JSON}")
        print(f"wrote {MANIFEST_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
