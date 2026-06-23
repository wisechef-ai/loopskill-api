"""Issue #24 / Phase 3+4 — install URL signer salt discipline.

Phase 3+4 renamed the canonical salt from "recipes-skill-install" to
"loopskill-install". The verifier (_verify_signed_token) accepts BOTH salts
so in-flight tokens survive the rename. Tests:
1. The signer uses the new "loopskill-install" salt.
2. The old "recipes-skill-install" salt is still accepted as a fallback.
3. A token signed with salt A is rejected by a verifier with salt B.
4. The same salt round-trips correctly.
"""

from __future__ import annotations

import pytest
from itsdangerous import BadSignature, URLSafeTimedSerializer


_SECRET = "test-signing-secret"
_SALT = "loopskill-install"  # Phase 3+4 canonical salt
_OLD_SALT = "recipes-skill-install"  # compat fallback — still accepted
_OTHER_SALT = "some-other-salt"


def test_install_serializer_uses_correct_salt() -> None:
    """The signer must use the new 'loopskill-install' salt (Phase 3+4)."""
    from pathlib import Path

    src = (Path(__file__).parents[1] / "app" / "install_routes.py").read_text()
    assert (
        'salt="loopskill-install"' in src
    ), 'install_routes.py does not contain salt="loopskill-install" — Phase 3+4 not applied'
    # The old salt must still be present in the fallback verifier
    assert _OLD_SALT in src, f"Old salt {_OLD_SALT!r} not in install_routes.py — compat fallback missing"


def test_cross_salt_token_rejected() -> None:
    """A token signed with salt A must not verify under salt B."""
    signer_a = URLSafeTimedSerializer(_SECRET, salt=_SALT)
    signer_b = URLSafeTimedSerializer(_SECRET, salt=_OTHER_SALT)

    token = signer_a.dumps({"slug": "test-skill", "version_id": "abc", "mode": "files"})
    with pytest.raises(BadSignature):
        signer_b.loads(token, max_age=3600)


def test_same_salt_roundtrips() -> None:
    """A token signed with the correct salt verifies successfully."""
    signer = URLSafeTimedSerializer(_SECRET, salt=_SALT)
    payload = {"slug": "test-skill", "version_id": "abc123", "mode": "files"}
    token = signer.dumps(payload)
    result = signer.loads(token, max_age=3600)
    assert result == payload
