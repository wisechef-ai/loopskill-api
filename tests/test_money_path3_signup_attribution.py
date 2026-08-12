"""Tests for money-path-3: first-touch UTM/ref attribution capture at signup.

2026-08-12 money-path audit, Fix #3. Covers the pure resolver
(``app.services.signup_attribution.resolve_signup_attribution``) at unit
level, AND the real end-to-end signup path through
``GET /api/auth/github/callback`` (house rule: route input through the real
request path, no raw-SQL/direct-attribute seeding proving nothing).

2026-08-12 codex REQUEST_CHANGES follow-up (PR #250) adds coverage for the
7 review findings (P1-a/b/c/d, P2-e/f/g) — see TestWriteOnceRace,
TestCommitFailureRecovery, TestCreatorRefValidation,
TestUtmCtxCookieProducer, TestGoogleCallbackVariant, and the malformed-
cookie-through-real-path cases below.

Test list:
  (a) signup with ref cookie only            -> attribution persisted, ref set
  (b) signup with utm query params only      -> attribution persisted
  (c) signup with both cookie ctx AND query  -> cookie wins (first-touch)
  (d) second login (existing user)           -> does NOT overwrite
  (e) oversized / control-char utm values    -> safely bounded, never stored raw
  (f) signup with neither cookie nor query   -> null attribution, no error
  (g) cookies blocked (client sends none)    -> signup still succeeds (never a blocker)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Creator, User
from app.services.signup_attribution import (
    _UTM_CTX_COOKIE_MAX_LEN,
    _UTM_CTX_COOKIE_NAME,
    _UTM_FIELD_MAX_LEN,
    resolve_signup_attribution,
)


# ── Unit-level tests: resolve_signup_attribution() against a fake Request ──


class _FakeRequest:
    """Minimal stand-in for fastapi.Request — only .cookies / .query_params used."""

    def __init__(self, cookies: dict | None = None, query_params: dict | None = None):
        self.cookies = cookies or {}
        self.query_params = query_params or {}


class TestResolveSignupAttributionUnit:
    def test_cookie_ref_only(self):
        """(a) ref cookie present, no UTM ctx cookie, no query -> ref captured."""
        req = _FakeRequest(cookies={"recipes_utm_ref": "li"})
        result = resolve_signup_attribution(req)
        assert result is not None
        assert result["ref"] == "li"
        assert result["utm_source"] is None
        assert "captured_at" in result

    def test_query_utm_only(self):
        """(b) no cookies, UTM query params present -> attribution persisted."""
        req = _FakeRequest(
            query_params={
                "utm_source": "twitter",
                "utm_medium": "social",
                "utm_campaign": "launch_week",
                "utm_content": "banner_a",
            }
        )
        result = resolve_signup_attribution(req)
        assert result is not None
        assert result["utm_source"] == "twitter"
        assert result["utm_medium"] == "social"
        assert result["utm_campaign"] == "launch_week"
        assert result["utm_content"] == "banner_a"
        assert result["ref"] is None

    def test_cookie_ctx_wins_over_query(self):
        """(c) both UTM ctx cookie AND query params present -> cookie wins."""
        ctx = json.dumps({"utm_source": "cookie_source", "utm_medium": "cookie_medium"})
        req = _FakeRequest(
            cookies={_UTM_CTX_COOKIE_NAME: ctx},
            query_params={"utm_source": "query_source", "utm_medium": "query_medium"},
        )
        result = resolve_signup_attribution(req)
        assert result["utm_source"] == "cookie_source"
        assert result["utm_medium"] == "cookie_medium"
        # never falls back to the query values when the cookie already won
        assert result["utm_source"] != "query_source"

    def test_oversized_and_control_char_values_bounded(self):
        """(e) oversized value truncated; control-char value dropped, not stored raw."""
        oversized = "x" * 500
        control_char = "evil\x00payload"
        req = _FakeRequest(
            query_params={
                "utm_source": oversized,
                "utm_medium": control_char,
                "utm_campaign": "clean_value",
            }
        )
        result = resolve_signup_attribution(req)
        assert result is not None
        assert len(result["utm_source"]) == _UTM_FIELD_MAX_LEN
        assert result["utm_source"] == oversized[:_UTM_FIELD_MAX_LEN]
        assert result["utm_medium"] is None  # control-char value rejected outright
        assert result["utm_campaign"] == "clean_value"

    def test_ref_cookie_unknown_value_rejected(self):
        """(e) a ref cookie value outside the allowlist/creator: shape is dropped."""
        req = _FakeRequest(cookies={"recipes_utm_ref": "totally-made-up-platform"})
        result = resolve_signup_attribution(req)
        assert result is None  # nothing else to attribute either

    def test_ref_cookie_oversized_rejected(self):
        """(e) an absurdly long ref cookie is rejected before any further processing."""
        req = _FakeRequest(cookies={"recipes_utm_ref": "x" * 1000})
        result = resolve_signup_attribution(req)
        assert result is None

    def test_creator_namespaced_ref_without_db_is_dropped(self):
        """P1-d: without a ``db`` handle, a creator:<handle> ref cannot be
        verified against the Creator table and is dropped (fail closed),
        NOT trusted blind. Supersedes the pre-fix behaviour where a stale
        comment claimed the cookie was "already validated" at set-time and
        trusted it unconditionally — the cookie is client-writable and
        forgeable, so that trust was unverifiable at read-time."""
        req = _FakeRequest(cookies={"recipes_utm_ref": "creator:somecreator"})
        result = resolve_signup_attribution(req)  # no db passed
        assert result is None

    def test_creator_namespaced_ref_forged_with_db_is_dropped(self):
        """P1-d: even WITH a db handle, a creator: ref for a handle that
        does not exist in the Creator table (the forged-cookie attack) is
        dropped, not stored."""
        req = _FakeRequest(cookies={"recipes_utm_ref": "creator:nonexistent-forged-handle"})
        result = resolve_signup_attribution(req, db=object())  # dummy, query will fail -> False
        assert result is None

    def test_creator_namespaced_ref_real_handle_accepted(self, db_session: Session):
        """P1-d: a creator: ref for a handle that DOES exist in the Creator
        table is accepted — the fix re-validates, it doesn't just reject
        everything."""
        from uuid import uuid4

        creator = Creator(id=uuid4(), name="Real Creator", slug="real-creator-slug", handle="realhandle")
        db_session.add(creator)
        db_session.commit()

        req = _FakeRequest(cookies={"recipes_utm_ref": "creator:realhandle"})
        result = resolve_signup_attribution(req, db=db_session)
        assert result is not None
        assert result["ref"] == "creator:realhandle"

    def test_no_signal_returns_none(self):
        """(f) nothing set anywhere -> None, no exception."""
        req = _FakeRequest()
        result = resolve_signup_attribution(req)
        assert result is None

    def test_malformed_ctx_cookie_json_falls_through(self):
        """Garbage JSON in the ctx cookie must not raise; falls through to query."""
        req = _FakeRequest(
            cookies={_UTM_CTX_COOKIE_NAME: "{not valid json"},
            query_params={"utm_source": "fallback_source"},
        )
        result = resolve_signup_attribution(req)
        assert result is not None
        assert result["utm_source"] == "fallback_source"


# ── Integration tests: real OAuth callback route, real DB write path ───────


@pytest.fixture()
def auth_client(db_session: Session):
    """TestClient wired to the real /api/auth/github/callback route.

    House rule: route input through the real request path — no raw-SQL /
    direct-attribute seeding of signup_attribution proving nothing. This
    fixture only mocks the external GitHub HTTP exchange (exchange_github_code);
    everything downstream (find_or_create_user_by_github, the attribution
    capture, the DB write) runs for real against the sqlite test session.
    """
    from app.config import settings as real_settings

    test_app = FastAPI()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    from app.auth_routes import router as auth_router

    test_app.include_router(auth_router)
    test_app.dependency_overrides[get_db] = override_get_db

    with (
        patch.object(real_settings, "GITHUB_CLIENT_ID", "test_gh_id"),
        patch.object(real_settings, "GITHUB_CLIENT_SECRET", "test_gh_secret"),
    ):
        with TestClient(test_app, raise_server_exceptions=True) as c:
            yield c


def _do_github_callback(
    auth_client,
    *,
    github_id: int,
    cookies: dict | None = None,
    query: str = "",
):
    """Drive the real GitHub OAuth callback: state-cookie handshake + mocked exchange."""
    state = "test-state-abc"
    all_cookies = {"oauth_state": state}
    all_cookies.update(cookies or {})
    for k, v in all_cookies.items():
        auth_client.cookies.set(k, v)

    fake_github_data = {
        "provider": "github",
        "github_id": github_id,
        "username": f"user{github_id}",
        "display_name": f"Test User {github_id}",
        "email": f"user{github_id}@example.com",
        "avatar_url": None,
    }

    with patch("app.auth_routes.exchange_github_code", new=AsyncMock(return_value=fake_github_data)):
        url = f"/api/auth/github/callback?code=abc&state={state}"
        if query:
            url += f"&{query}"
        resp = auth_client.get(url, follow_redirects=False)
    return resp


class TestSignupAttributionEndToEnd:
    def test_ref_cookie_persisted_on_signup(self, auth_client, db_session):
        """(a) signup with ref cookie -> attribution persisted."""
        resp = _do_github_callback(auth_client, github_id=90001, cookies={"recipes_utm_ref": "li"})
        assert resp.status_code == 302

        user = db_session.query(User).filter(User.github_id == 90001).first()
        assert user is not None
        assert user.signup_attribution is not None
        assert user.signup_attribution["ref"] == "li"

    def test_utm_query_only_persisted_on_signup(self, auth_client, db_session):
        """(b) signup with utm query params only -> attribution persisted."""
        resp = _do_github_callback(
            auth_client,
            github_id=90002,
            query="utm_source=x&utm_medium=social&utm_campaign=launch",
        )
        assert resp.status_code == 302

        user = db_session.query(User).filter(User.github_id == 90002).first()
        assert user is not None
        assert user.signup_attribution is not None
        assert user.signup_attribution["utm_source"] == "x"
        assert user.signup_attribution["utm_campaign"] == "launch"
        assert user.signup_attribution["ref"] is None

    def test_cookie_wins_over_query_on_signup(self, auth_client, db_session):
        """(c) both cookie ctx and query present -> cookie wins (first-touch)."""
        ctx = json.dumps({"utm_source": "cookie_wins_source"})
        resp = _do_github_callback(
            auth_client,
            github_id=90003,
            cookies={_UTM_CTX_COOKIE_NAME: ctx},
            query="utm_source=query_loses_source",
        )
        assert resp.status_code == 302

        user = db_session.query(User).filter(User.github_id == 90003).first()
        assert user.signup_attribution["utm_source"] == "cookie_wins_source"

    def test_second_login_does_not_overwrite(self, auth_client, db_session):
        """(d) a returning user's second login must NOT clobber first-touch attribution."""
        # First login: captures ref=li.
        resp1 = _do_github_callback(auth_client, github_id=90004, cookies={"recipes_utm_ref": "li"})
        assert resp1.status_code == 302
        user = db_session.query(User).filter(User.github_id == 90004).first()
        original_attribution = dict(user.signup_attribution)

        # Second login: different ref cookie this time (e.g. clicked a
        # different platform link while already having an account) — must
        # NOT overwrite the original first-touch record.
        resp2 = _do_github_callback(auth_client, github_id=90004, cookies={"recipes_utm_ref": "x"})
        assert resp2.status_code == 302

        db_session.refresh(user)
        assert user.signup_attribution == original_attribution
        assert user.signup_attribution["ref"] == "li"  # unchanged, not "x"

    def test_oversized_garbage_utm_safely_bounded_on_signup(self, auth_client, db_session):
        """(e) oversized/garbage utm on the real signup path -> bounded, not raw-stored."""
        oversized = "y" * 900
        resp = _do_github_callback(
            auth_client,
            github_id=90005,
            query=f"utm_source={oversized}&utm_medium=%00control",
        )
        assert resp.status_code == 302

        user = db_session.query(User).filter(User.github_id == 90005).first()
        assert user.signup_attribution is not None
        assert len(user.signup_attribution["utm_source"]) == _UTM_FIELD_MAX_LEN
        assert user.signup_attribution["utm_medium"] is None

    def test_no_attribution_signal_null_no_error(self, auth_client, db_session):
        """(f) signup with neither cookie nor query -> null attribution, 302 succeeds."""
        resp = _do_github_callback(auth_client, github_id=90006)
        assert resp.status_code == 302

        user = db_session.query(User).filter(User.github_id == 90006).first()
        assert user is not None
        assert user.signup_attribution is None

    def test_signup_succeeds_when_attribution_cookies_blocked(self, auth_client, db_session):
        """(g) adversarial self-review: if the client blocks ALL cookies except the
        mandatory oauth_state (CSRF) cookie the OAuth handshake itself requires,
        signup must still succeed — attribution is best-effort, never a blocker.
        This is the literal 'cookies blocked' scenario: no recipes_utm_ref, no
        recipes_utm_ctx, no referral cookie at all.
        """
        resp = _do_github_callback(auth_client, github_id=90007)  # no attribution cookies
        assert resp.status_code == 302
        assert "auth=success" in resp.headers.get("location", "") or resp.headers.get(
            "location", ""
        ).startswith("/library")

        user = db_session.query(User).filter(User.github_id == 90007).first()
        assert user is not None  # signup completed
        assert user.signup_attribution is None  # nothing to attribute, no crash

    def test_signup_succeeds_when_resolver_raises(self, auth_client, db_session):
        """(g) even if resolve_signup_attribution itself raises unexpectedly, the
        auth_routes.py try/except around the call site must still let signup
        complete — belt-and-suspenders on top of the resolver's own internal
        never-raise design.
        """
        with patch(
            "app.services.signup_attribution.resolve_signup_attribution",
            side_effect=RuntimeError("boom"),
        ):
            resp = _do_github_callback(auth_client, github_id=90008)
        assert resp.status_code == 302

        user = db_session.query(User).filter(User.github_id == 90008).first()
        assert user is not None  # signup still completed despite the raise
        assert user.signup_attribution is None

    def test_malformed_ctx_cookie_through_real_path_does_not_crash_signup(self, auth_client, db_session):
        """P2-g: a malformed (non-JSON) recipes_utm_ctx cookie sent on the REAL
        request path must not crash signup, and must fall through cleanly
        (no query params here either, so no attribution at all)."""
        resp = _do_github_callback(
            auth_client,
            github_id=90009,
            cookies={_UTM_CTX_COOKIE_NAME: "{this is not json at all"},
        )
        assert resp.status_code == 302

        user = db_session.query(User).filter(User.github_id == 90009).first()
        assert user is not None
        assert user.signup_attribution is None

    def test_oversized_ctx_cookie_through_real_path_discarded(self, auth_client, db_session):
        """P2-f, real request path: a recipes_utm_ctx cookie bigger than the
        2048-byte bound is discarded before json.loads ever runs — signup
        succeeds, no attribution captured from that cookie."""
        huge_ctx = json.dumps({"utm_source": "x" * (_UTM_CTX_COOKIE_MAX_LEN + 500)})
        assert len(huge_ctx) > _UTM_CTX_COOKIE_MAX_LEN
        resp = _do_github_callback(
            auth_client,
            github_id=90010,
            cookies={_UTM_CTX_COOKIE_NAME: huge_ctx},
        )
        assert resp.status_code == 302

        user = db_session.query(User).filter(User.github_id == 90010).first()
        assert user is not None
        assert user.signup_attribution is None


# ── P1-a: write-once race — second write must be a genuine no-op ───────────


class TestWriteOnceRace:
    """Reproduces the read-then-write race the review flagged: two writers
    for the SAME brand-new user, both starting from signup_attribution=NULL.
    Fixed via a conditional ``UPDATE ... WHERE signup_attribution IS NULL``
    checked by rowcount — this class asserts the loser's write is a no-op,
    not just that the end state happens to look right."""

    def test_second_write_is_a_true_noop_sequential(self, db_session: Session):
        """Two sequential calls into _capture_signup_attribution for the SAME
        user, simulating two callbacks racing on a brand-new row: the first
        writer's UPDATE flips NULL->{...}; the second writer's UPDATE (still
        believing the row might be NULL) must match zero rows and change
        nothing, even though it targets the same primary key."""
        from uuid import uuid4

        from app.services.signup_attribution import capture_signup_attribution as _capture_signup_attribution

        uid = uuid4()
        user = User(id=uid, display_name="race-user", github_id=80001)
        db_session.add(user)
        db_session.commit()

        req_first = _FakeRequest(cookies={"recipes_utm_ref": "li"})
        req_second = _FakeRequest(cookies={"recipes_utm_ref": "x"})

        _capture_signup_attribution(req_first, user, db_session)
        first_result = dict(user.signup_attribution)
        assert first_result["ref"] == "li"

        _capture_signup_attribution(req_second, user, db_session)

        db_session.refresh(user)
        assert user.signup_attribution == first_result
        assert user.signup_attribution["ref"] == "li"  # NOT overwritten to "x"

    def test_concurrent_writers_on_fresh_row_second_is_noop(self, engine_fixture):
        """A closer approximation of true concurrency: two INDEPENDENT
        sessions both load the same brand-new user (both see
        signup_attribution=NULL), then both attempt the write. The atomic
        conditional UPDATE means whichever commits first wins and the other
        matches zero rows — proving the fix is a DB-layer guarantee, not
        just "don't call it twice from one session."""
        from uuid import uuid4

        from sqlalchemy.orm import sessionmaker

        from app.services.signup_attribution import capture_signup_attribution as _capture_signup_attribution

        SessionLocal = sessionmaker(bind=engine_fixture, autocommit=False, autoflush=False)
        uid = uuid4()

        seed = SessionLocal()
        seed.add(User(id=uid, display_name="concurrent-race", github_id=80002))
        seed.commit()
        seed.close()

        session_a = SessionLocal()
        session_b = SessionLocal()
        try:
            user_a = session_a.query(User).filter(User.id == uid).first()
            user_b = session_b.query(User).filter(User.id == uid).first()
            assert user_a.signup_attribution is None
            assert user_b.signup_attribution is None  # both see NULL, "concurrently"

            req_a = _FakeRequest(cookies={"recipes_utm_ref": "li"})
            req_b = _FakeRequest(cookies={"recipes_utm_ref": "x"})

            # A commits first (wins the race).
            _capture_signup_attribution(req_a, user_a, session_a)
            # B's conditional UPDATE now matches zero rows.
            _capture_signup_attribution(req_b, user_b, session_b)

            verify = SessionLocal()
            final = verify.query(User).filter(User.id == uid).first()
            assert final.signup_attribution["ref"] == "li"
            verify.close()
        finally:
            session_a.close()
            session_b.close()


# ── P1-b: a commit failure inside attribution capture must not poison the
#    session — signup must still complete (real IntegrityError, real path) ──


class TestCommitFailureRecovery:
    def test_signup_completes_when_attribution_commit_raises_integrity_error(self, auth_client, db_session):
        """Force a REAL SQLAlchemy IntegrityError at the exact db.commit()
        inside _capture_signup_attribution (via a UNIQUE constraint
        collision planted right before that commit runs), through the real
        OAuth callback request path. Before the fix, the session was left
        poisoned (PendingRollbackError) and create_jwt(user) — called right
        after — raised, 500ing the whole signup. After the fix, the
        function catches the failure, rolls back, and signup still
        succeeds with a 302."""
        from uuid import uuid4

        from app.models import User as UserModel

        # Pre-seed a colliding github_id so the poisoning insert below fails
        # its UNIQUE constraint for real (not mocked).
        db_session.add(UserModel(id=uuid4(), display_name="collision-seed", github_id=444))
        db_session.commit()

        import app.services.signup_attribution as sa_mod

        real_resolve = sa_mod.resolve_signup_attribution

        def poisoning_resolve(request, db=None):
            result = real_resolve(request, db=db)
            # Plant a genuine uncommitted UNIQUE-constraint violation into
            # the SAME session right before _capture_signup_attribution's
            # own db.commit() runs.
            db_session.add(UserModel(id=uuid4(), display_name="poison", github_id=444))
            return result

        with patch(
            "app.services.signup_attribution.resolve_signup_attribution", side_effect=poisoning_resolve
        ):
            resp = _do_github_callback(
                auth_client,
                github_id=90011,
                cookies={"recipes_utm_ref": "li"},
            )

        assert resp.status_code == 302  # signup succeeded despite the commit failure
        assert "auth=success" in resp.headers.get("location", "") or resp.headers.get(
            "location", ""
        ).startswith("/library")

    def test_session_usable_after_attribution_commit_failure(self, db_session: Session):
        """Unit-level: after _capture_signup_attribution swallows a commit
        failure, the session must be immediately usable again (proves the
        rollback actually ran, not just that no exception escaped)."""
        from uuid import uuid4

        from app.models import User as UserModel
        from app.services.signup_attribution import capture_signup_attribution as _capture_signup_attribution

        db_session.add(UserModel(id=uuid4(), display_name="collision-seed-2", github_id=555))
        db_session.commit()

        target_uid = uuid4()
        target_user = UserModel(id=target_uid, display_name="target", github_id=556)
        db_session.add(target_user)
        db_session.commit()

        import app.services.signup_attribution as sa_mod

        real_resolve = sa_mod.resolve_signup_attribution

        def poisoning_resolve(request, db=None):
            result = real_resolve(request, db=db)
            db_session.add(UserModel(id=uuid4(), display_name="poison-2", github_id=555))
            return result

        with patch(
            "app.services.signup_attribution.resolve_signup_attribution", side_effect=poisoning_resolve
        ):
            req = _FakeRequest(cookies={"recipes_utm_ref": "li"})
            _capture_signup_attribution(req, target_user, db_session)

        # The session must be usable again — no PendingRollbackError.
        refetched = db_session.query(UserModel).filter(UserModel.id == target_uid).first()
        assert refetched is not None


# ── P1-c: the /login initiation route is the in-repo UTM-ctx cookie producer ─


def _decode_cookie_json(raw: str) -> dict:
    """Undo RFC-6265 cookie-value quoting/escaping (stdlib http.cookies wraps
    any value containing '{', '"', ',' etc. in quotes and octal-escapes
    special chars) before handing the value to json.loads. The TestClient's
    cookie jar hands back the RAW wire value, not an unquoted one."""
    import http.cookies

    return json.loads(http.cookies._unquote(raw))


class TestUtmCtxCookieProducer:
    """The review flagged that nothing in this repo ever MINTS the
    recipes_utm_ctx cookie the callback reads — a dead consumer path. Fix:
    /api/auth/{provider}/login now accepts utm_* query params and stamps
    them into that exact cookie, mirroring how ?ref= already survives the
    OAuth round-trip. These tests drive the full login -> callback round
    trip through the real routes."""

    def test_login_stamps_utm_ctx_cookie(self, auth_client):
        resp = auth_client.get(
            "/api/auth/github/login?utm_source=twitter&utm_medium=social&utm_campaign=launch",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        cookie_val = resp.cookies.get(_UTM_CTX_COOKIE_NAME)
        assert cookie_val is not None
        payload = _decode_cookie_json(cookie_val)
        assert payload["utm_source"] == "twitter"
        assert payload["utm_medium"] == "social"
        assert payload["utm_campaign"] == "launch"

    def test_login_without_utm_params_stamps_no_cookie(self, auth_client):
        resp = auth_client.get("/api/auth/github/login", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.cookies.get(_UTM_CTX_COOKIE_NAME) is None

    def test_full_login_to_callback_round_trip_persists_attribution(self, auth_client, db_session):
        """The complete producer->consumer journey: /login stamps the
        cookie from query params, the browser (TestClient) carries it
        through to /callback, and the callback resolves + persists it on
        the new user — closing the exact gap P1-c identified."""
        login_resp = auth_client.get(
            "/api/auth/github/login?utm_source=newsletter&utm_medium=email&utm_campaign=aug_promo",
            follow_redirects=False,
        )
        assert login_resp.status_code == 302
        # TestClient's cookie jar now carries recipes_utm_ctx + oauth_state
        # forward automatically, exactly like a real browser would.

        resp = _do_github_callback(auth_client, github_id=90012)
        assert resp.status_code == 302

        user = db_session.query(User).filter(User.github_id == 90012).first()
        assert user is not None
        assert user.signup_attribution is not None
        assert user.signup_attribution["utm_source"] == "newsletter"
        assert user.signup_attribution["utm_medium"] == "email"
        assert user.signup_attribution["utm_campaign"] == "aug_promo"


# ── P2-g: a second OAuth provider variant (Google) gets its own coverage ───


def _do_google_callback(
    auth_client,
    *,
    google_id: str,
    cookies: dict | None = None,
    query: str = "",
):
    """Drive the real Google OAuth callback: state-cookie handshake + mocked exchange."""
    state = "test-state-google-abc"
    all_cookies = {"oauth_state": state}
    all_cookies.update(cookies or {})
    for k, v in all_cookies.items():
        auth_client.cookies.set(k, v)

    fake_google_data = {
        "provider": "google",
        "google_id": google_id,
        "display_name": f"Google User {google_id}",
        "email": f"{google_id}@example.com",
        "avatar_url": None,
    }

    with patch("app.auth_routes.exchange_google_code", new=AsyncMock(return_value=fake_google_data)):
        url = f"/api/auth/google/callback?code=abc&state={state}"
        if query:
            url += f"&{query}"
        resp = auth_client.get(url, follow_redirects=False)
    return resp


class TestGoogleCallbackVariant:
    def test_ref_cookie_persisted_on_google_signup(self, auth_client, db_session):
        """money-path-3 must not be a GitHub-only fix — same write-once
        attribution capture is wired into the Google callback."""
        from app.config import settings as real_settings

        with (
            patch.object(real_settings, "GOOGLE_CLIENT_ID", "test_google_id"),
            patch.object(real_settings, "GOOGLE_CLIENT_SECRET", "test_google_secret"),
        ):
            resp = _do_google_callback(auth_client, google_id="g90001", cookies={"recipes_utm_ref": "yt"})
        assert resp.status_code == 302

        user = db_session.query(User).filter(User.google_id == "g90001").first()
        assert user is not None
        assert user.signup_attribution is not None
        assert user.signup_attribution["ref"] == "yt"

    def test_google_second_login_does_not_overwrite(self, auth_client, db_session):
        from app.config import settings as real_settings

        with (
            patch.object(real_settings, "GOOGLE_CLIENT_ID", "test_google_id"),
            patch.object(real_settings, "GOOGLE_CLIENT_SECRET", "test_google_secret"),
        ):
            resp1 = _do_google_callback(auth_client, google_id="g90002", cookies={"recipes_utm_ref": "yt"})
            assert resp1.status_code == 302
            user = db_session.query(User).filter(User.google_id == "g90002").first()
            original = dict(user.signup_attribution)

            resp2 = _do_google_callback(auth_client, google_id="g90002", cookies={"recipes_utm_ref": "ig"})
            assert resp2.status_code == 302

        db_session.refresh(user)
        assert user.signup_attribution == original
        assert user.signup_attribution["ref"] == "yt"

    def test_login_stamps_utm_ctx_cookie_google(self, auth_client):
        """P1-c producer wired symmetrically into the Google /login route."""
        from app.config import settings as real_settings

        with (
            patch.object(real_settings, "GOOGLE_CLIENT_ID", "test_google_id"),
            patch.object(real_settings, "GOOGLE_CLIENT_SECRET", "test_google_secret"),
        ):
            resp = auth_client.get(
                "/api/auth/google/login?utm_source=twitter&utm_campaign=g_launch",
                follow_redirects=False,
            )
        assert resp.status_code == 302
        cookie_val = resp.cookies.get(_UTM_CTX_COOKIE_NAME)
        assert cookie_val is not None
        payload = _decode_cookie_json(cookie_val)
        assert payload["utm_source"] == "twitter"
        assert payload["utm_campaign"] == "g_launch"
