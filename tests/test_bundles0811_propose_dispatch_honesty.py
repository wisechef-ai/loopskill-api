"""bundles_0811 — a registry proposal must never claim a review it did not open.

FOUND BY THE §4.5 TERMINAL-GATE COLD-RUN PROBE (2026-08-11)
-----------------------------------------------------------
Probing step 8 anonymously against prod:

    POST /api/federation/propose {"repo_url": ".../anthropics/skills"}
      -> 200 {"ok": true, "status": "pending_review", "issue_url": ""}
    gh issue list --search "federation registry proposal"  ->  none

The pre-flight was real (repo verified, 18 SKILL.md counted) but NO issue
existed. `pending_review` is a promise that a human will see it, and that
promise was false.

TWO distinct bugs, both covered here:

1. **Silent dispatch failure.** `github_dispatch.dispatch_event` returns None
   when `GITHUB_DISPATCH_PAT` is unset (deliberate: the API write must stay
   durable when GitHub is down). The route ignored that and answered
   `pending_review` regardless — the same silent-failure class as the
   federation token that indexed 0 rows with `last_error = NULL`.

2. **`issue_url` held a boolean.** `dispatch_event` is typed `bool | None`
   because `repository_dispatch` answers *204 No Content* — the issue number is
   not knowable synchronously. The code named the result `gh_url` and assigned
   it to `row.issue_url`, storing the literal `True` in a string column.

The durability behaviour is deliberately PRESERVED: a failed dispatch still
records the proposal. What changes is that the caller is told the truth.

issue #289 update (2026-08-27): the original prod incident used
anthropics/skills as the probe repo. That repo is now a genuinely registered
github_taps entry (github-anthropic, decision #13), so it now correctly
short-circuits through the NEW already-registered dedup check before ever
reaching the dispatch-honesty branches this file exists to test. Switched
the fixture repo to an unregistered synthetic slug so this file keeps
testing dispatch mechanics in isolation from the registry-dedup concern
(covered separately by tests/test_federation_propose.py).
"""

from __future__ import annotations

from unittest.mock import patch

from app.mcp.tools import federation_propose as fp
from app.services.federation_propose import PreflightResult

# Deliberately NOT a repo present in config/federation_sources.yaml's
# github_taps — see the issue #289 update note above.
_UNREGISTERED_REPO = "dispatch-honesty-fixture-org/probe-skills"


def _preflight() -> PreflightResult:
    """A real PreflightResult, not a hand-rolled stub.

    Using the actual dataclass means this test breaks if its shape changes,
    instead of silently drifting from what the route consumes.
    """
    return PreflightResult(
        repo_slug=_UNREGISTERED_REPO,
        repo_exists=True,
        skill_md_count=18,
        license_detected=None,
    )


def _call(monkeypatch, db_session, *, dispatch_returns, repo=_UNREGISTERED_REPO):
    """Run a proposal with dispatch_event stubbed to a given outcome.

    Only the network edges are stubbed: the repo pre-flight (a live GitHub call)
    and dispatch_event. The real rate-limit gate runs — it is reset per-test so
    each proposal is a first submission rather than a dedupe hit.
    """
    monkeypatch.setattr(fp, "preflight_repo", lambda *a, **k: _preflight())
    fp.feedback_ratelimit.reset_all()

    with patch.object(fp.github_dispatch, "dispatch_event", return_value=dispatch_returns):
        return fp.loopskill_propose_registry(
            db_session,
            repo_url=f"https://github.com/{repo}",
            source_id="dispatch-honesty-fixture",
            contact="probe@example.com",
            why="terminal gate step 8",
        )


class TestDispatchFailureIsNotSilent:
    def test_unset_pat_does_not_claim_pending_review(self, db_session, monkeypatch):
        """dispatch_event -> None (no PAT) must NOT report pending_review."""
        out = _call(monkeypatch, db_session, dispatch_returns=None)

        assert out["status"] == "recorded_not_dispatched", (
            "a proposal that opened no issue must not claim 'pending_review' — "
            "that promises a human will see it"
        )
        assert out["review_channel_open"] is False
        assert out["issue_url"] == ""

    def test_the_proposal_is_still_recorded_when_dispatch_fails(self, db_session, monkeypatch):
        """Durability is deliberate: GitHub being down must not lose the proposal."""
        from uuid import UUID

        out = _call(monkeypatch, db_session, dispatch_returns=None)

        assert out["ok"] is True
        assert out["proposal_id"], "the row must still be written"
        row = (
            db_session.query(fp.FederationRegistryProposal)
            .filter(fp.FederationRegistryProposal.id == UUID(out["proposal_id"]))
            .first()
        )
        assert row is not None, "a failed dispatch must not roll back the record"

    def test_preflight_evidence_survives_a_failed_dispatch(self, db_session, monkeypatch):
        out = _call(monkeypatch, db_session, dispatch_returns=None)
        assert out["preflight"]["skill_md_count"] == 18


class TestSuccessfulDispatch:
    def test_dispatch_true_reports_pending_review(self, db_session, monkeypatch):
        out = _call(monkeypatch, db_session, dispatch_returns=True)

        assert out["status"] == "pending_review"
        assert out["review_channel_open"] is True

    def test_issue_url_is_never_a_boolean(self, db_session, monkeypatch):
        """repository_dispatch answers 204, so no URL is knowable synchronously.

        The old code assigned dispatch_event's bool straight into issue_url.
        """
        out = _call(monkeypatch, db_session, dispatch_returns=True)

        assert not isinstance(out["issue_url"], bool), (
            "issue_url must be a string, never dispatch_event's boolean"
        )
        assert isinstance(out["issue_url"], str)


class TestDedupePathIsAlsoHonest:
    """The dedupe early-return is a SECOND path with the same promise to keep.

    Found by a live prod probe AFTER the main-path fix shipped: proposing a repo
    twice returned `status: "pending_review"` together with
    `review_channel_open: false` — self-contradictory, and the exact dishonesty
    the main path had just been fixed for. The first suite never exercised this
    branch, so it passed while prod still lied. Fixing one branch of a two-branch
    promise is not fixing the promise.
    """

    def test_dedupe_after_a_failed_dispatch_does_not_claim_pending_review(
        self, db_session, monkeypatch
    ):
        # 1st submission: dispatch fails (no PAT) -> nothing queued for review.
        first = _call(monkeypatch, db_session, dispatch_returns=None)
        assert first["status"] == "recorded_not_dispatched"

        # 2nd identical submission hits the dedupe branch. There is still no
        # issue, so it must NOT suddenly claim a review is pending.
        monkeypatch.setattr(fp, "preflight_repo", lambda *a, **k: _preflight())
        with patch.object(fp.github_dispatch, "dispatch_event", return_value=None):
            second = fp.loopskill_propose_registry(
                db_session,
                repo_url=f"https://github.com/{_UNREGISTERED_REPO}",
                source_id="dispatch-honesty-fixture",
                contact="probe@example.com",
                why="terminal gate step 8",
            )

        if not second.get("deduped"):
            import pytest

            pytest.skip("rate-limit window did not dedupe; branch not exercised")

        assert second["review_channel_open"] is False
        assert second["status"] == "recorded_not_dispatched", (
            "a dedupe hit whose original never dispatched must not claim "
            "'pending_review' — that is the bug this file exists for"
        )

    def test_status_and_channel_flag_never_contradict(self, db_session, monkeypatch):
        """pending_review AND review_channel_open=False is incoherent."""
        for dispatch in (None, True):
            fp.feedback_ratelimit.reset_all()
            out = _call(monkeypatch, db_session, dispatch_returns=dispatch)
            if out["status"] == "pending_review":
                assert out["review_channel_open"] is True, (
                    f"claimed pending_review with no open channel: {out}"
                )
            else:
                assert out["review_channel_open"] is False, out
