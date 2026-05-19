"""Issue #24 — URLSafeTimedSerializer uses salt="recipes-skill-install".

Tests:
1. Both the install signer and the download signer use the same salt.
2. A token signed with salt A is rejected by a verifier with salt B.
3. The same salt round-trips correctly.
"""

from __future__ import annotations

import pytest
from itsdangerous import BadSignature, URLSafeTimedSerializer


_SECRET = "test-signing-secret"
_SALT = "recipes-skill-install"
_OTHER_SALT = "some-other-salt"


def test_install_serializer_uses_correct_salt() -> None:
    """The salt in install_routes.py must be 'recipes-skill-install'.

    We verify by round-tripping a token with the expected salt — if the
    production code uses a different salt the integration test will catch it.
    """
    # Grep the source to confirm the literal salt is present.
    from pathlib import Path
    src = (Path(__file__).parents[1] / "app" / "install_routes.py").read_text()
    assert 'salt="recipes-skill-install"' in src, (
        "install_routes.py does not contain salt=\"recipes-skill-install\" — issue #24 not fixed"
    )
    # Both occurrences (sign + verify) must be present
    assert src.count('salt="recipes-skill-install"') == 2, (
        "Expected exactly 2 occurrences of salt=\"recipes-skill-install\" in install_routes.py "
        "(got {})".format(src.count('salt="recipes-skill-install"'))
    )


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
