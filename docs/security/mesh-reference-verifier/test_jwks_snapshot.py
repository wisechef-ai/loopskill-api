"""JWKS snapshot state machine — spec §3. Tests the PyJWKClient-replacement
component itself: fresh/aging/stale/cold states, refresh scheduling,
rate-limited unknown-kid refresh, and the "never a network call during
key_for()" invariant.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from jwks_snapshot import (  # noqa: E402
    HARD_EXPIRY_SECONDS,
    REFRESH_AT_SECONDS,
    UNKNOWN_KID_REFRESH_COOLDOWN_SECONDS,
    JWKSStateMachine,
    JWKSUnavailableError,
    UnknownKidError,
)


def _fake_fetch(keys, call_log):
    def _fetch():
        call_log.append(1)
        return keys
    return _fetch


class TestColdStart:
    def test_no_snapshot_rejects_everything(self):
        snap = JWKSStateMachine(fetch_jwks_fn=lambda: {})
        try:
            snap.key_for("any-kid")
            assert False, "expected JWKSUnavailableError"
        except JWKSUnavailableError:
            pass


class TestFreshSnapshot:
    def test_resolves_known_kid(self):
        calls = []
        snap = JWKSStateMachine(fetch_jwks_fn=_fake_fetch({"k1": "PUBKEY"}, calls))
        snap.bootstrap()
        assert snap.key_for("k1") == "PUBKEY"

    def test_unknown_kid_rejects(self):
        calls = []
        snap = JWKSStateMachine(fetch_jwks_fn=_fake_fetch({"k1": "PUBKEY"}, calls))
        snap.bootstrap()
        try:
            snap.key_for("k-nope")
            assert False, "expected UnknownKidError"
        except UnknownKidError:
            pass

    def test_missing_kid_rejects(self):
        calls = []
        snap = JWKSStateMachine(fetch_jwks_fn=_fake_fetch({"k1": "PUBKEY"}, calls))
        snap.bootstrap()
        try:
            snap.key_for(None)
            assert False, "expected UnknownKidError"
        except UnknownKidError:
            pass


class TestAgingSnapshotSchedulesRefresh:
    def test_refresh_not_due_before_2880s(self):
        calls = []
        snap = JWKSStateMachine(fetch_jwks_fn=_fake_fetch({"k1": "PUBKEY"}, calls))
        snap.bootstrap(now=0)
        assert len(calls) == 1
        snap.maybe_refresh(now=REFRESH_AT_SECONDS - 10)
        assert len(calls) == 1, "must not refresh before the 80% threshold"

    def test_refresh_due_at_2880s(self):
        calls = []
        snap = JWKSStateMachine(fetch_jwks_fn=_fake_fetch({"k1": "PUBKEY"}, calls))
        snap.bootstrap(now=0)
        snap.maybe_refresh(now=REFRESH_AT_SECONDS + 1)
        assert len(calls) == 2, "must refresh once the 80% threshold is crossed"


class TestStaleServesPastFreshTtlUpToHardExpiry:
    """Spec §3 — 'Snapshot stale, LoopSkill unreachable -> keep verifying
    against the stale snapshot. Hard stop 86400s past last success.'"""

    def test_still_resolves_kid_after_fresh_ttl_but_before_hard_expiry(self):
        calls = []
        snap = JWKSStateMachine(fetch_jwks_fn=_fake_fetch({"k1": "PUBKEY"}, calls))
        snap.bootstrap(now=0)
        # Simulate LoopSkill being unreachable: key_for at 3700s (past the
        # 3600s fresh TTL) must still resolve — no refresh call happens
        # inside key_for.
        assert snap.key_for("k1", now=3700) == "PUBKEY"

    def test_rejects_past_hard_expiry(self):
        calls = []
        snap = JWKSStateMachine(fetch_jwks_fn=_fake_fetch({"k1": "PUBKEY"}, calls))
        snap.bootstrap(now=0)
        try:
            snap.key_for("k1", now=HARD_EXPIRY_SECONDS + 1)
            assert False, "expected JWKSUnavailableError past hard expiry"
        except JWKSUnavailableError:
            pass

    def test_accepts_at_boundary_just_under_hard_expiry(self):
        calls = []
        snap = JWKSStateMachine(fetch_jwks_fn=_fake_fetch({"k1": "PUBKEY"}, calls))
        snap.bootstrap(now=0)
        assert snap.key_for("k1", now=HARD_EXPIRY_SECONDS - 1) == "PUBKEY"


class TestFetchFailureRetainsPreviousSnapshot:
    def test_failed_refresh_keeps_old_keys(self):
        calls = []
        state = {"fail": False}

        def _fetch():
            calls.append(1)
            if state["fail"]:
                raise ConnectionError("loopskill unreachable")
            return {"k1": "PUBKEY"}

        snap = JWKSStateMachine(fetch_jwks_fn=_fetch)
        snap.bootstrap(now=0)
        state["fail"] = True
        snap.maybe_refresh(now=REFRESH_AT_SECONDS + 1)
        # Refresh attempted (calls incremented) but failed — old key still resolves.
        assert len(calls) == 2
        assert snap.key_for("k1", now=REFRESH_AT_SECONDS + 2) == "PUBKEY"


class TestUnknownKidRefreshIsRateLimited:
    """Spec §3.1 — at most one refresh per 60s per process triggered by an
    unknown-kid spray. This is the DoS mitigation the council caught."""

    def test_repeated_unknown_kid_within_cooldown_only_refreshes_once(self):
        calls = []
        snap = JWKSStateMachine(fetch_jwks_fn=_fake_fetch({"k1": "PUBKEY"}, calls))
        snap.bootstrap(now=0)
        assert len(calls) == 1

        for i in range(20):
            try:
                snap.key_for("spray-kid", now=10 + i)
            except UnknownKidError:
                pass
        # Bootstrap (1) + at most one rate-limited refresh triggered by the
        # unknown-kid spray, despite 20 attempts.
        assert len(calls) == 2, f"expected at most 2 fetch calls (bootstrap + 1 rate-limited refresh), got {len(calls)}"

    def test_unknown_kid_refresh_allowed_again_after_cooldown(self):
        calls = []
        snap = JWKSStateMachine(fetch_jwks_fn=_fake_fetch({"k1": "PUBKEY"}, calls))
        snap.bootstrap(now=0)

        try:
            snap.key_for("spray-1", now=1)
        except UnknownKidError:
            pass
        assert len(calls) == 2  # bootstrap + first spray refresh

        try:
            snap.key_for("spray-2", now=1 + UNKNOWN_KID_REFRESH_COOLDOWN_SECONDS + 1)
        except UnknownKidError:
            pass
        assert len(calls) == 3  # cooldown elapsed — refresh allowed again


class TestAtomicSwap:
    def test_reader_never_sees_a_partial_snapshot(self):
        """The swap replaces the whole _Snapshot object at once — verified
        indirectly: after a refresh, key_for reflects EITHER the old or the
        new full key set, never a mix."""
        calls = []
        state = {"keys": {"k1": "OLD"}}

        def _fetch():
            calls.append(1)
            return dict(state["keys"])

        snap = JWKSStateMachine(fetch_jwks_fn=_fetch)
        snap.bootstrap(now=0)
        assert snap.key_for("k1", now=1) == "OLD"

        state["keys"] = {"k2": "NEW"}
        snap.maybe_refresh(now=REFRESH_AT_SECONDS + 1)

        # Old kid is now gone (retired), new kid resolves — the swap was
        # whole-object, not merged.
        try:
            snap.key_for("k1", now=REFRESH_AT_SECONDS + 2)
            assert False, "old kid should no longer resolve after a full swap"
        except UnknownKidError:
            pass
        assert snap.key_for("k2", now=REFRESH_AT_SECONDS + 2) == "NEW"
