"""Tests for JWT auth module."""

import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.auth import create_jwt, verify_jwt


class TestJWT:
    """Test JWT creation and verification."""

    def test_create_and_verify_jwt(self):
        """Create a JWT and verify it round-trips correctly."""
        user_id = uuid4()
        token = create_jwt.__wrapped__(user_id) if hasattr(create_jwt, '__wrapped__') else None

        # Direct test using the function
        from app.models import User
        user = User(
            id=user_id,
            display_name="Test Creator",
            email="test@example.com",
        )
        token = create_jwt(user)
        assert isinstance(token, str)
        assert len(token) > 20

        payload = verify_jwt(token)
        assert payload is not None
        assert payload["sub"] == str(user_id)
        assert payload["email"] == "test@example.com"
        assert payload["iss"] == "wiserecipes"
        assert "exp" in payload

    def test_expired_jwt_rejected(self):
        """Expired tokens should be rejected."""
        import jwt as pyjwt
        from app.config import settings

        payload = {
            "sub": str(uuid4()),
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
            "iat": datetime.now(timezone.utc) - timedelta(hours=73),
            "iss": "wiserecipes",
        }
        token = pyjwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
        result = verify_jwt(token)
        assert result is None

    def test_invalid_signature_rejected(self):
        """Tokens signed with wrong secret should be rejected."""
        import jwt as pyjwt

        payload = {
            "sub": str(uuid4()),
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "iat": datetime.now(timezone.utc),
            "iss": "wiserecipes",
        }
        token = pyjwt.encode(payload, "wrong-secret", algorithm="HS256")
        result = verify_jwt(token)
        assert result is None

    def test_wrong_issuer_rejected(self):
        """Tokens with wrong issuer should be rejected."""
        import jwt as pyjwt
        from app.config import settings

        payload = {
            "sub": str(uuid4()),
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "iat": datetime.now(timezone.utc),
            "iss": "wrong-app",
        }
        token = pyjwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
        result = verify_jwt(token)
        assert result is None

    def test_malformed_token_rejected(self):
        """Garbage tokens should be rejected."""
        assert verify_jwt("not.a.token") is None
        assert verify_jwt("") is None
        assert verify_jwt("eyJhbGciOiJIUzI1NiJ9.garbage") is None
