"""Dual-surface symmetry: every /api/cookbooks/* route MUST have an
/api/bundles/* twin, and vice-versa. The bundle surface is the primary
standalone-brand surface; /api/cookbooks is retained only as a backward-compat
alias. This pins that they stay in lockstep — a route added to one surface but
not the other is a regression (asymmetric API + half-applied rename).

Context: the cookbook→bundle rename kept /api/cookbooks as a compat alias, but
5 route modules (reconcile, sse, well-known, share-tokens, promotion-report)
were originally cookbook-PRIMARY with no bundle twin — surfaced by a cold-boot
OpenAPI audit. This test guards the fix (all dual-mounted) against regression.
"""

from __future__ import annotations


def _surface_paths(prefix: str) -> set[str]:
    from app.main import create_app

    app = create_app()
    out = set()
    for r in app.routes:
        p = getattr(r, "path", None)
        if p and p.startswith(prefix):
            # normalize the surface base away so the two sets are comparable
            out.add(p[len(prefix) :])
    return out


def test_every_cookbook_route_has_a_bundle_twin():
    bundles = _surface_paths("/api/bundles")
    cookbooks = _surface_paths("/api/cookbooks")
    missing_bundle_twin = cookbooks - bundles
    assert not missing_bundle_twin, (
        "These /api/cookbooks routes have NO /api/bundles twin — the bundle "
        "surface is incomplete: " + ", ".join(sorted(missing_bundle_twin))
    )


def test_every_bundle_route_has_a_cookbook_compat_alias():
    bundles = _surface_paths("/api/bundles")
    cookbooks = _surface_paths("/api/cookbooks")
    missing_compat = bundles - cookbooks
    assert not missing_compat, (
        "These /api/bundles routes lost their /api/cookbooks compat alias: "
        + ", ".join(sorted(missing_compat))
    )


def test_surfaces_are_nonempty_and_symmetric():
    bundles = _surface_paths("/api/bundles")
    cookbooks = _surface_paths("/api/cookbooks")
    assert bundles, "no /api/bundles routes mounted at all"
    assert bundles == cookbooks, (
        f"surface asymmetry: {len(bundles)} bundle vs {len(cookbooks)} cookbook routes"
    )


def test_public_discovery_paths_consistent_across_surfaces():
    """The public (no-auth) allowlist must cover BOTH surfaces for the public
    discovery/leaderboard routes, or one surface 401s where the other is open.
    Asserts against the live PUBLIC_PREFIXES tuple (wherever it's defined)."""
    from app.middleware.api_key import APIKeyMiddleware

    prefixes = set(APIKeyMiddleware.PUBLIC_PREFIXES)
    for leaf in ("discover", "public/", "leaderboard"):
        has_bundle = f"/api/bundles/{leaf}" in prefixes
        has_cookbook = f"/api/cookbooks/{leaf}" in prefixes
        assert has_bundle and has_cookbook, (
            f"public allowlist asymmetric for '{leaf}': bundle={has_bundle} cookbook={has_cookbook}"
        )
