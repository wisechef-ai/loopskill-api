"""tests/test_loopclose_3005_j_feedback_routing.py

Phase J (loopclose_3005) — THE MOAT: user-routable feedback.

Test inventory:
  T1.  Vault: encrypt + decrypt round-trip.
  T2.  Vault: decrypt with wrong key raises ValueError.
  T3.  Vault: encrypt rejects empty PAT.
  T4.  Vault: _safe_token masks correctly.
  T5.  feedback_github._validate_repo: rejects bad formats.
  T6.  feedback_github._validate_repo: accepts good formats.
  T7.  feedback_github.verify_repo_access: 200 + push=True → True.
  T8.  feedback_github.verify_repo_access: 200 + push=False, labels probe OK → True.
  T9.  feedback_github.verify_repo_access: 404 → False.
  T10. feedback_github.verify_repo_access: 403 → False.
  T11. feedback_github.create_issue: 201 → issue_url.
  T12. feedback_github.create_issue: HTTP error → None.
  T13. configure_feedback: free-tier caller rejected.
  T14. configure_feedback: Pro caller, bad repo format → error.
  T15. configure_feedback: Pro caller, missing PAT → error.
  T16. configure_feedback: Pro caller, PAT fails verify → error.
  T17. configure_feedback: Pro caller, PAT OK → stored encrypted.
  T18. configure_feedback: clear path (repo=None) → clears fields.
  T19. configure_feedback: github_app mode → not-yet-live error.
  T20. configure_feedback: unowned cookbook → rejected.
  T21. recipes_feedback: default path (no custom routing).
  T22. recipes_feedback: user-routed path dispatches to user's repo.
  T23. recipes_feedback: user-routed path fails → fallback to default.
  T24. recipes_feedback: no custom routing configured → default.
  T25. Regression: dispatch_event signature unchanged.
  T26. SECRET HYGIENE: PAT never appears in log output.
  T27. Migration: new columns present in model schema.
  T28. configure_feedback: MCP _dispatch wiring works end-to-end.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ── Rate-limiter reset (prevent test-to-test bleed) ──────────────────────────

import app.feedback_ratelimit as rl_module


def _mk_provenance_for_cookbook(db_session, cb):
    """spotify_0608 Ph E helper: mint a provenance_id mapped to an install in
    ``cb`` so feedback routing (now provenance-deterministic) can resolve the
    cookbook's configured repo. Returns the provenance_id string."""
    from app.models import Skill
    from app.services.provenance import record_install_with_provenance

    s = Skill(
        id=uuid.uuid4(), slug=f"prov-{uuid.uuid4().hex[:8]}", title="prov", is_public=True, install_count=0
    )
    db_session.add(s)
    db_session.flush()
    _ev, pid = record_install_with_provenance(
        db_session, skill=s, version_semver="1.0.0", source="cookbook", cookbook_id=cb.id, commit=True
    )
    return pid


@pytest.fixture(autouse=True)
def reset_ratelimit():
    """Reset rate-limit state between tests to prevent bleed."""
    rl_module.reset_all()
    yield
    rl_module.reset_all()


# ── Vault tests ──────────────────────────────────────────────────────────────


class TestFeedbackCredVault:
    """T1-T4: Fernet PAT vault encrypt/decrypt/mask."""

    def _set_key(self, monkeypatch):
        """Set a valid Fernet key in the environment."""
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        monkeypatch.setenv("WR_FEEDBACK_CRED_KEY", key)
        return key

    def test_round_trip(self, monkeypatch):
        """T1: encrypt + decrypt round-trips correctly."""
        self._set_key(monkeypatch)
        from app.feedback_cred_vault import decrypt_pat, encrypt_pat

        pat = "github_pat_ABCDEF1234567890"
        enc = encrypt_pat(pat)
        assert enc != pat
        assert decrypt_pat(enc) == pat

    def test_wrong_key_raises(self, monkeypatch):
        """T2: decrypt with a different key raises ValueError."""
        from cryptography.fernet import Fernet

        key1 = Fernet.generate_key().decode()
        key2 = Fernet.generate_key().decode()
        monkeypatch.setenv("WR_FEEDBACK_CRED_KEY", key1)

        from app.feedback_cred_vault import encrypt_pat

        enc = encrypt_pat("test-token")

        monkeypatch.setenv("WR_FEEDBACK_CRED_KEY", key2)

        from importlib import reload

        import app.feedback_cred_vault as _vault

        reload(_vault)
        with pytest.raises(ValueError, match="decryption failed"):
            _vault.decrypt_pat(enc)

        # Reset
        monkeypatch.setenv("WR_FEEDBACK_CRED_KEY", key1)
        reload(_vault)

    def test_empty_pat_rejected(self, monkeypatch):
        """T3: empty PAT is rejected by encrypt_pat."""
        self._set_key(monkeypatch)
        from app.feedback_cred_vault import encrypt_pat

        with pytest.raises(ValueError, match="empty"):
            encrypt_pat("")

    def test_safe_token_masks(self):
        """T4: _safe_token returns first 4 chars + ***."""
        from app.feedback_cred_vault import _safe_token

        assert _safe_token("ghp_ABCDEFGHIJKLMNOP") == "ghp_***"
        assert _safe_token("ab") == "***"
        assert _safe_token("abcd") == "abcd***"


# ── feedback_github tests ────────────────────────────────────────────────────


class TestFeedbackGithub:
    """T5-T12: repo validation and GitHub API calls."""

    def test_validate_repo_rejects_bad(self):
        """T5: _validate_repo rejects bad formats."""
        from app.feedback_github import _validate_repo

        bad = [
            "no-slash",
            "owner/name/extra",
            "../traversal/path",
            "owner name/repo",
            "",
            "x" * 40 + "/repo",
        ]
        for r in bad:
            with pytest.raises(ValueError):
                _validate_repo(r)

    def test_validate_repo_accepts_good(self):
        """T6: _validate_repo accepts valid formats."""
        from app.feedback_github import _validate_repo

        good = [
            "adamkrawczyk/recipes-feedback-e2e",
            "owner123/my.repo-name_1",
            "a/b",
        ]
        for r in good:
            _validate_repo(r)  # should not raise

    def test_verify_repo_access_push_true(self):
        """T7: 200 + push=True → True."""
        import json

        from app.feedback_github import verify_repo_access

        resp_body = json.dumps({"permissions": {"push": True, "admin": False}}).encode()
        with patch("app.feedback_github._gh_request", return_value=(200, resp_body)):
            assert verify_repo_access("owner/repo", "ghp_test") is True

    def test_verify_repo_access_labels_fallback(self):
        """T8: 200 + push=False, labels probe OK → True."""
        import json

        from app.feedback_github import verify_repo_access

        # Main repo call: push=False; labels probe: 200
        resp_body = json.dumps({"permissions": {"push": False, "admin": False}}).encode()

        call_count = [0]

        def mock_gh(url, token, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return (200, resp_body)
            return (200, b"[]")  # labels probe

        with patch("app.feedback_github._gh_request", side_effect=mock_gh):
            assert verify_repo_access("owner/repo", "ghp_test") is True
        assert call_count[0] == 2

    def test_verify_repo_404(self):
        """T9: 404 → False."""
        from app.feedback_github import verify_repo_access

        with patch("app.feedback_github._gh_request", return_value=(404, b"Not Found")):
            assert verify_repo_access("owner/repo", "ghp_test") is False

    def test_verify_repo_403(self):
        """T10: 403 → False."""
        from app.feedback_github import verify_repo_access

        with patch("app.feedback_github._gh_request", return_value=(403, b"Forbidden")):
            assert verify_repo_access("owner/repo", "ghp_test") is False

    def test_create_issue_success(self):
        """T11: 201 → issue_url."""
        import json

        from app.feedback_github import create_issue

        resp = json.dumps({"html_url": "https://github.com/owner/repo/issues/1"}).encode()
        with patch("app.feedback_github._gh_request", return_value=(201, resp)):
            url = create_issue(
                "owner/repo",
                "ghp_test",
                title="Test issue",
                body="body",
                labels=["feedback"],
            )
        assert url == "https://github.com/owner/repo/issues/1"

    def test_create_issue_http_error(self):
        """T12: HTTP error → None."""
        from app.feedback_github import create_issue

        with patch("app.feedback_github._gh_request", return_value=(403, b"Forbidden")):
            result = create_issue("owner/repo", "ghp_test", title="T", body="B")
        assert result is None


# ── configure_feedback tool tests ────────────────────────────────────────────


def _make_cookbook(db, owner_id, is_base=False):
    """Helper: create a cookbook row in the test DB."""
    from app.models import Cookbook

    cb = Cookbook(
        id=uuid.uuid4(),
        name="Test Cookbook",
        is_base=is_base,
        cookbook_owner=owner_id,
    )
    db.add(cb)
    db.flush()
    return cb


class TestConfigureFeedback:
    """T13-T20: configure_feedback tool logic."""

    def test_free_tier_rejected(self, db_session):
        """T13: free-tier caller is rejected."""
        from app.auth_ctx import AuthContext
        from app.mcp.tools.configure_feedback import recipes_configure_feedback

        ctx = AuthContext(scope="user", user_id=uuid.uuid4(), tier="free")
        result = recipes_configure_feedback(db_session, repo="owner/repo", mode="pat", pat="ghp_x", ctx=ctx)
        assert result["ok"] is False
        assert "Pro" in result["error"]

    def test_bad_repo_format(self, db_session):
        """T14: bad repo format → error."""
        from app.auth_ctx import AuthContext
        from app.mcp.tools.configure_feedback import recipes_configure_feedback

        user_id = uuid.uuid4()
        _make_cookbook(db_session, user_id)
        ctx = AuthContext(scope="user", user_id=user_id, tier="pro")
        result = recipes_configure_feedback(db_session, repo="notavalid", mode="pat", pat="ghp_x", ctx=ctx)
        assert result["ok"] is False
        assert "Invalid repo" in result["error"]

    def test_missing_pat(self, db_session):
        """T15: mode=pat but no PAT → error."""
        from app.auth_ctx import AuthContext
        from app.mcp.tools.configure_feedback import recipes_configure_feedback

        user_id = uuid.uuid4()
        _make_cookbook(db_session, user_id)
        ctx = AuthContext(scope="user", user_id=user_id, tier="pro")
        result = recipes_configure_feedback(db_session, repo="owner/repo", mode="pat", pat=None, ctx=ctx)
        assert result["ok"] is False
        assert "pat is required" in result["error"]

    def test_pat_verify_fails(self, db_session):
        """T16: PAT fails verification → error, nothing stored."""
        from app.auth_ctx import AuthContext
        from app.mcp.tools.configure_feedback import recipes_configure_feedback

        user_id = uuid.uuid4()
        cb = _make_cookbook(db_session, user_id)
        ctx = AuthContext(scope="user", user_id=user_id, tier="pro")

        with patch("app.mcp.tools.configure_feedback.verify_repo_access", return_value=False):
            result = recipes_configure_feedback(
                db_session, repo="owner/repo", mode="pat", pat="ghp_badtoken", ctx=ctx
            )

        assert result["ok"] is False
        assert "PAT verification failed" in result["error"]
        # Ensure nothing was stored
        db_session.refresh(cb)
        assert cb.feedback_repo is None
        assert cb.feedback_pat_enc is None

    def test_configure_pat_success(self, db_session, monkeypatch):
        """T17: Pro caller, PAT OK → fields stored encrypted."""
        from cryptography.fernet import Fernet

        monkeypatch.setenv("WR_FEEDBACK_CRED_KEY", Fernet.generate_key().decode())

        from app.auth_ctx import AuthContext
        from app.feedback_cred_vault import decrypt_pat
        from app.mcp.tools.configure_feedback import recipes_configure_feedback

        user_id = uuid.uuid4()
        cb = _make_cookbook(db_session, user_id)
        ctx = AuthContext(scope="user", user_id=user_id, tier="pro")

        with patch("app.mcp.tools.configure_feedback.verify_repo_access", return_value=True):
            result = recipes_configure_feedback(
                db_session,
                repo="myuser/my-feedback-repo",
                mode="pat",
                pat="ghp_REALTESTTOKEN",
                ctx=ctx,
            )

        assert result["ok"] is True
        assert result["repo"] == "myuser/my-feedback-repo"
        assert result["mode"] == "pat"

        db_session.refresh(cb)
        assert cb.feedback_repo == "myuser/my-feedback-repo"
        assert cb.feedback_mode == "pat"
        assert cb.feedback_pat_enc is not None
        # Confirm the stored value is encrypted (not plaintext)
        assert "ghp_REALTESTTOKEN" not in cb.feedback_pat_enc
        # Confirm round-trip
        assert decrypt_pat(cb.feedback_pat_enc) == "ghp_REALTESTTOKEN"

    def test_clear_routing(self, db_session, monkeypatch):
        """T18: clear path (repo=None) → clears all feedback fields."""
        from cryptography.fernet import Fernet

        monkeypatch.setenv("WR_FEEDBACK_CRED_KEY", Fernet.generate_key().decode())

        from app.auth_ctx import AuthContext
        from app.mcp.tools.configure_feedback import recipes_configure_feedback

        user_id = uuid.uuid4()
        cb = _make_cookbook(db_session, user_id)
        ctx = AuthContext(scope="user", user_id=user_id, tier="pro")

        # First configure
        with patch("app.mcp.tools.configure_feedback.verify_repo_access", return_value=True):
            recipes_configure_feedback(db_session, repo="owner/repo", mode="pat", pat="ghp_x", ctx=ctx)
        db_session.refresh(cb)
        assert cb.feedback_repo == "owner/repo"

        # Then clear
        result = recipes_configure_feedback(db_session, repo=None, ctx=ctx)
        assert result["ok"] is True
        assert result.get("cleared") is True
        db_session.refresh(cb)
        assert cb.feedback_repo is None
        assert cb.feedback_mode is None
        assert cb.feedback_pat_enc is None

    def test_github_app_not_live(self, db_session):
        """T19: github_app mode → not-yet-live error."""
        from app.auth_ctx import AuthContext
        from app.mcp.tools.configure_feedback import recipes_configure_feedback

        user_id = uuid.uuid4()
        _make_cookbook(db_session, user_id)
        ctx = AuthContext(scope="user", user_id=user_id, tier="pro_plus")
        result = recipes_configure_feedback(db_session, repo="owner/repo", mode="github_app", ctx=ctx)
        assert result["ok"] is False
        assert "not yet live" in result["error"]

    def test_unowned_cookbook_rejected(self, db_session):
        """T20: user doesn't own cookbook → rejected."""
        from app.auth_ctx import AuthContext
        from app.mcp.tools.configure_feedback import recipes_configure_feedback

        owner_id = uuid.uuid4()
        attacker_id = uuid.uuid4()
        cb = _make_cookbook(db_session, owner_id)
        ctx = AuthContext(scope="user", user_id=attacker_id, tier="pro")

        result = recipes_configure_feedback(
            db_session,
            repo="owner/repo",
            mode="pat",
            pat="ghp_x",
            cookbook_id=str(cb.id),
            ctx=ctx,
        )
        assert result["ok"] is False
        assert "do not own" in result["error"]


# ── recipes_feedback routing tests ───────────────────────────────────────────


class TestFeedbackRouting:
    """T21-T24: feedback tool routing logic."""

    def _make_feedback_db(self, db_session, user_id, repo=None, mode=None, pat_enc=None):
        """Setup: create a cookbook with optional feedback routing."""
        from app.models import Cookbook

        cb = Cookbook(
            id=uuid.uuid4(),
            name="Test",
            is_base=False,
            cookbook_owner=user_id,
            feedback_repo=repo,
            feedback_mode=mode,
            feedback_pat_enc=pat_enc,
        )
        db_session.add(cb)
        db_session.flush()
        return cb

    def test_default_path_no_routing(self, db_session):
        """T21: user with no custom routing → default dispatch_event."""
        from app.auth_ctx import AuthContext
        from app.mcp.tools.feedback import recipes_feedback

        user_id = uuid.uuid4()
        ctx = AuthContext(scope="user", user_id=user_id, tier="free")

        with patch("app.mcp.tools.feedback.github_dispatch") as mock_gd:
            mock_gd.dispatch_event.return_value = True
            result = recipes_feedback(
                db_session,
                category="ux",
                message="test feedback default path",
                ctx=ctx,
            )

        assert result["ok"] is True
        mock_gd.dispatch_event.assert_called_once()
        # dispatch_issue should NOT have been called
        mock_gd.dispatch_issue.assert_not_called()

    def test_user_routed_dispatch(self, db_session, monkeypatch):
        """T22: user with custom repo → dispatch_issue called with that repo."""
        from cryptography.fernet import Fernet

        fernet_key = Fernet.generate_key().decode()
        monkeypatch.setenv("WR_FEEDBACK_CRED_KEY", fernet_key)

        from app.auth_ctx import AuthContext
        from app.feedback_cred_vault import encrypt_pat
        from app.mcp.tools.feedback import recipes_feedback

        user_id = uuid.uuid4()
        enc = encrypt_pat("ghp_USERTOKEN")
        cb = self._make_feedback_db(
            db_session,
            user_id,
            repo="testuser/feedback-repo",
            mode="pat",
            pat_enc=enc,
        )
        # spotify_0608 Ph E: routing is now DETERMINISTIC via provenance, not the
        # old "first cookbook the user owns" guess. Mint a provenance_id from an
        # install in THIS cookbook and pass it so the report routes to the repo.
        prov_id = _mk_provenance_for_cookbook(db_session, cb)
        ctx = AuthContext(scope="user", user_id=user_id, tier="pro")

        with patch("app.mcp.tools.feedback.github_dispatch") as mock_gd:
            mock_gd.dispatch_issue.return_value = "https://github.com/testuser/feedback-repo/issues/7"
            result = recipes_feedback(
                db_session,
                category="billing",
                message="test user-routed feedback",
                ctx=ctx,
                provenance_id=prov_id,
            )

        assert result["ok"] is True
        assert result["issue_url"] == "https://github.com/testuser/feedback-repo/issues/7"
        mock_gd.dispatch_issue.assert_called_once()
        call_kwargs = mock_gd.dispatch_issue.call_args
        # Repo is positional arg 1
        assert call_kwargs[0][0] == "testuser/feedback-repo"
        # Token is positional arg 2 — verify it is decrypted PAT (not encrypted blob)
        assert call_kwargs[0][1] == "ghp_USERTOKEN"
        # dispatch_event should NOT have been called (no fallback needed)
        mock_gd.dispatch_event.assert_not_called()

    def test_user_routed_dispatch_fails_fallback(self, db_session, monkeypatch):
        """T23: user-routed dispatch fails → fall back to default dispatch_event."""
        from cryptography.fernet import Fernet

        monkeypatch.setenv("WR_FEEDBACK_CRED_KEY", Fernet.generate_key().decode())

        from app.auth_ctx import AuthContext
        from app.feedback_cred_vault import encrypt_pat
        from app.mcp.tools.feedback import recipes_feedback

        user_id = uuid.uuid4()
        enc = encrypt_pat("ghp_FAILING")
        cb = self._make_feedback_db(
            db_session,
            user_id,
            repo="testuser/broken-repo",
            mode="pat",
            pat_enc=enc,
        )
        prov_id = _mk_provenance_for_cookbook(db_session, cb)
        ctx = AuthContext(scope="user", user_id=user_id, tier="pro")

        with patch("app.mcp.tools.feedback.github_dispatch") as mock_gd:
            # dispatch_issue fails (returns None)
            mock_gd.dispatch_issue.return_value = None
            # Fallback dispatch_event succeeds
            mock_gd.dispatch_event.return_value = True
            result = recipes_feedback(
                db_session,
                category="ux",
                message="test fallback path",
                ctx=ctx,
                provenance_id=prov_id,
            )

        assert result["ok"] is True
        mock_gd.dispatch_issue.assert_called_once()
        mock_gd.dispatch_event.assert_called_once()  # fallback triggered

    def test_no_user_ctx_uses_default(self, db_session):
        """T24: no ctx (anonymous) → default dispatch_event."""
        from app.mcp.tools.feedback import recipes_feedback

        with patch("app.mcp.tools.feedback.github_dispatch") as mock_gd:
            mock_gd.dispatch_event.return_value = True
            result = recipes_feedback(
                db_session,
                category="ux",
                message="anonymous feedback",
                ctx=None,
            )

        assert result["ok"] is True
        mock_gd.dispatch_event.assert_called_once()
        mock_gd.dispatch_issue.assert_not_called()


# ── Regression tests ─────────────────────────────────────────────────────────


class TestRegressions:
    """T25-T28: regression + hygiene checks."""

    def test_dispatch_event_signature_unchanged(self):
        """T25: dispatch_event still works with (event_type, payload) signature."""
        import inspect

        from app.github_dispatch import dispatch_event

        sig = inspect.signature(dispatch_event)
        params = list(sig.parameters.keys())
        assert params[0] == "event_type"
        assert params[1] == "payload"

    def test_pat_not_in_logs(self, db_session, monkeypatch, caplog):
        """T26: PAT token never appears in log output during dispatch."""
        from cryptography.fernet import Fernet

        monkeypatch.setenv("WR_FEEDBACK_CRED_KEY", Fernet.generate_key().decode())

        from app.auth_ctx import AuthContext
        from app.feedback_cred_vault import encrypt_pat
        from app.mcp.tools.feedback import recipes_feedback

        secret_token = "ghp_SUPERSECRETTOKEN_MUST_NOT_APPEAR"
        user_id = uuid.uuid4()
        enc = encrypt_pat(secret_token)

        from app.models import Cookbook

        cb = Cookbook(
            id=uuid.uuid4(),
            name="LogTest",
            is_base=False,
            cookbook_owner=user_id,
            feedback_repo="testuser/logrepo",
            feedback_mode="pat",
            feedback_pat_enc=enc,
        )
        db_session.add(cb)
        db_session.flush()

        ctx = AuthContext(scope="user", user_id=user_id, tier="pro")

        with caplog.at_level(logging.DEBUG, logger="app"):
            with patch("app.mcp.tools.feedback.github_dispatch") as mock_gd:
                mock_gd.dispatch_issue.return_value = "https://github.com/t/r/issues/1"
                recipes_feedback(
                    db_session,
                    category="ux",
                    message="secret test",
                    ctx=ctx,
                )

        # The plaintext token must NEVER appear in any log record
        for record in caplog.records:
            assert (
                secret_token not in record.getMessage()
            ), f"SECRET TOKEN APPEARED IN LOG: {record.getMessage()}"

    def test_new_columns_in_model(self):
        """T27: Cookbook model has the new Phase J columns."""
        from app.models import Cookbook

        assert hasattr(Cookbook, "feedback_repo")
        assert hasattr(Cookbook, "feedback_mode")
        assert hasattr(Cookbook, "feedback_pat_enc")

    def test_configure_feedback_dispatch_wiring(self, db_session, monkeypatch):
        """T28: MCP _dispatch routes 'recipes_configure_feedback' correctly."""
        from app.mcp.server import _dispatch

        user_id = uuid.uuid4()
        cb_id = uuid.uuid4()
        from app.models import Cookbook

        cb = Cookbook(
            id=cb_id,
            name="Dispatch Wiring Test",
            is_base=False,
            cookbook_owner=user_id,
        )
        db_session.add(cb)
        db_session.flush()

        caller = {
            "scope": "user",
            "user_id": str(user_id),
            "auth_ctx": __import__("app.auth_ctx", fromlist=["AuthContext"]).AuthContext(
                scope="user", user_id=user_id, tier="pro"
            ),
        }

        # Patch verify_repo_access to avoid real HTTP
        with patch("app.mcp.tools.configure_feedback.verify_repo_access", return_value=False):
            result = _dispatch(
                "recipes_configure_feedback",
                db_session,
                {
                    "repo": "test/repo",
                    "mode": "pat",
                    "pat": "ghp_test",
                    "cookbook_id": str(cb_id),
                },
                caller,
            )

        # PAT verification failed — but the tool was called (wiring works)
        assert isinstance(result, dict)
        assert result["ok"] is False
        assert "PAT verification" in result["error"]
