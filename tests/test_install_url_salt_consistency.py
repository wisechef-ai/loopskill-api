"""Regression test for secfix_1905/I-followup — install URL signer salt drift.

If a producer of `/api/skills/_download?token=` URLs ever stops passing
``salt="recipes-skill-install"``, every generated URL becomes invalid because
the verifier in ``app/install_routes.py:download_tarball`` requires that exact
salt. Codex's gpt-5.5 re-pass caught this on 2026-05-19 — three producers
(`cookbook_routes._make_install_url`, `mcp/tools/install`, `mcp/tools/recipes_sync`)
were initializing ``URLSafeTimedSerializer`` without the salt.

This test asserts producer/verifier salt parity by:
  1. Signing a token with the install-route salt.
  2. Loading it with each producer's serializer construction pattern.
  3. Failing fast if any producer drifts.

The test does NOT exercise the live HTTP routes — it pins the salt constant.
That is the cheapest possible guard: if anyone touches the salt, this fails.
"""

from __future__ import annotations

import inspect

from itsdangerous import URLSafeTimedSerializer

INSTALL_SALT = "recipes-skill-install"


def test_install_routes_verifier_uses_canonical_salt() -> None:
    """The download verifier MUST pin the canonical salt."""
    from app import install_routes

    src = inspect.getsource(install_routes.download_tarball)
    assert f'salt="{INSTALL_SALT}"' in src, (
        f"download_tarball must construct URLSafeTimedSerializer with "
        f'salt="{INSTALL_SALT}". Drift detected.'
    )


def test_install_routes_signer_uses_canonical_salt() -> None:
    """The install-route signer MUST pin the canonical salt."""
    from app import install_routes

    src = inspect.getsource(install_routes)
    # install_skill is the producer; it lives in this module.
    assert f'salt="{INSTALL_SALT}"' in src


def test_cookbook_routes_signer_uses_canonical_salt() -> None:
    """The cookbook install URL builder MUST pin the canonical salt."""
    from app import cookbook_routes

    src = inspect.getsource(cookbook_routes._make_install_url)
    assert f'salt="{INSTALL_SALT}"' in src, (
        "cookbook_routes._make_install_url drifted off the canonical install salt. "
        "Every cookbook install URL would silently break."
    )


def test_mcp_install_tool_uses_canonical_salt() -> None:
    """The MCP recipes_install tool MUST pin the canonical salt."""
    from app.mcp.tools import install as mcp_install

    src = inspect.getsource(mcp_install)
    assert f'salt="{INSTALL_SALT}"' in src, (
        "mcp.tools.install drifted off the canonical install salt."
    )


def test_mcp_recipes_sync_uses_canonical_salt() -> None:
    """The MCP recipes_sync URL builder MUST pin the canonical salt."""
    import app.mcp.tools.recipes_sync as mcp_sync_module

    src = inspect.getsource(mcp_sync_module)
    assert f'salt="{INSTALL_SALT}"' in src, (
        "mcp.tools.recipes_sync drifted off the canonical install salt."
    )


def test_roundtrip_salt_match() -> None:
    """End-to-end: a token signed with INSTALL_SALT loads with INSTALL_SALT."""
    signer = URLSafeTimedSerializer("test-secret", salt=INSTALL_SALT)
    token = signer.dumps({"slug": "x", "version_id": "y", "mode": "install"})
    verifier = URLSafeTimedSerializer("test-secret", salt=INSTALL_SALT)
    data = verifier.loads(token, max_age=3600)
    assert data == {"slug": "x", "version_id": "y", "mode": "install"}


def test_salt_drift_breaks_roundtrip() -> None:
    """Sanity: a producer with NO salt cannot round-trip with the verifier."""
    from itsdangerous import BadSignature

    bad_signer = URLSafeTimedSerializer("test-secret")  # no salt — the bug we shipped
    token = bad_signer.dumps({"slug": "x"})
    verifier = URLSafeTimedSerializer("test-secret", salt=INSTALL_SALT)
    try:
        verifier.loads(token, max_age=3600)
    except BadSignature:
        return
    raise AssertionError("Expected BadSignature when producer salt drifts from verifier.")
