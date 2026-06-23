"""Regression for secfix_1905/I-followup + Phase 3+4 — install URL signer salt discipline.

Phase 3+4 renamed the canonical salt from "recipes-skill-install" to
"loopskill-install". All producers now use the new salt. The verifier
(_verify_signed_token) accepts BOTH salts so in-flight URLs survive the rename.

Three producers that drifted in the original incident are pinned here:
  - cookbook_routes._make_install_url
  - mcp/tools/install
  - mcp/tools/recipes_sync._build_install_urls
"""

from __future__ import annotations

import inspect

from itsdangerous import URLSafeTimedSerializer

INSTALL_SALT = "loopskill-install"  # Phase 3+4 canonical salt
OLD_INSTALL_SALT = "recipes-skill-install"  # compat fallback — still accepted by verifier


def test_install_routes_verifier_uses_canonical_salt() -> None:
    """The download verifier must use the new canonical salt (Phase 3+4)."""
    from app import install_routes

    src = inspect.getsource(install_routes)
    assert (
        f'salt="{INSTALL_SALT}"' in src
    ), f'install_routes must contain salt="{INSTALL_SALT}". Drift detected.'
    # Old salt must still be present for the fallback verifier path
    assert OLD_INSTALL_SALT in src, f"Old salt {OLD_INSTALL_SALT!r} not found — compat fallback missing"


def test_install_routes_signer_uses_canonical_salt() -> None:
    """The install-route signer MUST use the new canonical salt."""
    from app import install_routes

    src = inspect.getsource(install_routes)
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
    """The MCP install tool MUST pin the canonical salt."""
    from app.mcp.tools import install as mcp_install

    src = inspect.getsource(mcp_install)
    assert f'salt="{INSTALL_SALT}"' in src, "mcp.tools.install drifted off the canonical install salt."


def test_mcp_recipes_sync_uses_canonical_salt() -> None:
    """The MCP recipes_sync URL builder MUST pin the canonical salt.

    NOTE: ``import app.mcp.tools.recipes_sync as m`` does NOT give the module
    here — ``app/mcp/tools/__init__.py`` rebinds the ``recipes_sync`` package
    attribute to the *function* of the same name, so the ``import ... as``
    form resolves to the function and ``inspect.getsource`` would return only
    that function's body (which never contains the salt — the salt lives in
    the ``_build_install_urls`` producer). Inspect the producer function
    directly, mirroring ``test_cookbook_routes_signer_uses_canonical_salt``.
    """
    from app.mcp.tools.recipes_sync import _build_install_urls

    src = inspect.getsource(_build_install_urls)
    assert f'salt="{INSTALL_SALT}"' in src, (
        "mcp.tools.recipes_sync._build_install_urls drifted off the canonical "
        "install salt. Every recipes_sync install URL would silently break."
    )


def test_roundtrip_salt_match() -> None:
    """End-to-end: a token signed with INSTALL_SALT loads with INSTALL_SALT."""
    signer = URLSafeTimedSerializer("test-secret", salt=INSTALL_SALT)
    token = signer.dumps({"slug": "x", "version_id": "y", "mode": "install"})
    verifier = URLSafeTimedSerializer("test-secret", salt=INSTALL_SALT)
    data = verifier.loads(token, max_age=3600)
    assert data == {"slug": "x", "version_id": "y", "mode": "install"}


def test_old_salt_compat_roundtrip() -> None:
    """End-to-end: a token signed with the OLD salt still loads via _verify_signed_token."""
    from app.install_routes import _verify_signed_token

    old_signer = URLSafeTimedSerializer("test-secret", salt=OLD_INSTALL_SALT)
    token = old_signer.dumps({"slug": "x", "version_id": "y", "mode": "install"})
    data = _verify_signed_token(token, secret="test-secret", max_age=3600)
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
