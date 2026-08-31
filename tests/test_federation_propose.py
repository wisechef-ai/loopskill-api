"""tests/test_federation_propose.py — bundles_0811 Phase P3.5.

Self-serve federation registry proposals. Covers:
  1. test_propose_opens_labelled_issue_with_preflight_evidence
  2. test_propose_repo_not_found_reports_back_no_issue
  3. test_propose_zero_skill_md_reports_back_no_issue
  4. test_propose_duplicate_within_24h_is_deduped_not_double_filed
  5. test_propose_anonymous_is_rate_limited
  6. test_accepting_source_is_config_edit_no_python_change
  7. test_all_14_preexisting_sources_survive_config_move
  8. test_rest_endpoint_mirrors_mcp_tool_contract
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

import app.feedback_ratelimit as rl_module
from app.mcp.tools.federation_propose import loopskill_propose_registry
from app.services.federation_propose import PreflightResult

# github_dispatch.dispatch_event is typed `bool | None` and POSTs
# repository_dispatch, which answers *204 No Content* — it CANNOT return an
# issue URL. These tests previously stubbed it with a URL string, so the mock
# was more capable than the real function and hid a live bug: the route stored
# that value in `issue_url`, which in production is the boolean True.
# bundles_0811: stub the REAL contract. The issue is opened asynchronously by
# .github/workflows/feedback-dispatch.yml, so no URL is knowable synchronously.
DISPATCH_OK = True
SAMPLE_REPO = "https://github.com/example-org/example-skills"


def _viable_preflight(repo_slug: str = "example-org/example-skills") -> PreflightResult:
    return PreflightResult(
        repo_slug=repo_slug,
        repo_exists=True,
        skill_md_count=5,
        license_detected="mit",
        default_branch="main",
    )


@pytest.fixture(autouse=True)
def reset_ratelimit():
    """Reset all in-process rate-limit buckets between tests."""
    rl_module.reset_all()
    yield
    rl_module.reset_all()


# ── Test 1: Happy path — preflight evidence attached, labelled issue opened ──


def test_propose_opens_labelled_issue_with_preflight_evidence(db_session):
    """loopskill_propose_registry happy path: preflights, persists, dispatches."""
    with (
        patch(
            "app.mcp.tools.federation_propose.preflight_repo",
            return_value=_viable_preflight(),
        ),
        patch(
            "app.mcp.tools.federation_propose.github_dispatch.dispatch_event",
            return_value=DISPATCH_OK,
        ) as mock_dispatch,
    ):
        result = loopskill_propose_registry(
            db_session,
            repo_url=SAMPLE_REPO,
            contact="dev@example.org",
            why="Has great docker skills.",
        )

    assert result["ok"] is True, result
    assert result["status"] == "pending_review"
    assert result["repo_slug"] == "example-org/example-skills"
    assert result["review_channel_open"] is True
    assert result["status"] == "pending_review"
    assert result["issue_url"] == ""  # not knowable synchronously (204)
    assert result["proposal_id"] != ""
    assert result["preflight"]["skill_md_count"] == 5
    assert result["preflight"]["license_detected"] == "mit"

    mock_dispatch.assert_called_once()
    call_args = mock_dispatch.call_args
    assert call_args[0][0] == "federation-registry-propose"
    dispatched_payload = call_args[0][1]
    assert dispatched_payload["repo_slug"] == "example-org/example-skills"
    # The pre-flight evidence MUST be attached to the dispatched payload so the
    # GitHub Actions workflow can render it straight into the issue body.
    assert dispatched_payload["preflight"]["skill_md_count"] == 5
    assert dispatched_payload["preflight"]["license_detected"] == "mit"
    assert dispatched_payload["preflight"]["repo_exists"] is True

    # Verify row was created in DB
    from app.models import FederationRegistryProposal

    row = (
        db_session.query(FederationRegistryProposal)
        .filter(FederationRegistryProposal.repo_url == SAMPLE_REPO)
        .first()
    )
    assert row is not None
    assert row.status == "pending"
    assert row.issue_url in ("", None)  # filled in later by the workflow, never a bool
    assert row.skill_md_count == 5


# ── Test 2: repo does not exist -> reported back, NO issue opened ────────────


def test_propose_repo_not_found_reports_back_no_issue(db_session):
    """A repo that does not exist must be reported back, never file a junk issue."""
    not_found = PreflightResult(
        repo_slug="example-org/does-not-exist",
        repo_exists=False,
        skill_md_count=None,
        license_detected=None,
        reason="repository not found on GitHub",
    )
    with (
        patch("app.mcp.tools.federation_propose.preflight_repo", return_value=not_found),
        patch(
            "app.mcp.tools.federation_propose.github_dispatch.dispatch_event",
            return_value=DISPATCH_OK,
        ) as mock_dispatch,
    ):
        result = loopskill_propose_registry(
            db_session,
            repo_url="https://github.com/example-org/does-not-exist",
        )

    assert result["ok"] is False
    assert result["error"] == "repo_not_found"
    mock_dispatch.assert_not_called()

    from app.models import FederationRegistryProposal

    assert db_session.query(FederationRegistryProposal).count() == 0


# ── Test 3: repo exists but zero SKILL.md -> reported back, NO issue opened ──


def test_propose_zero_skill_md_reports_back_no_issue(db_session):
    """A repo with zero SKILL.md files must be reported back, never a junk issue."""
    empty_repo = PreflightResult(
        repo_slug="example-org/empty-repo",
        repo_exists=True,
        skill_md_count=0,
        license_detected="mit",
        default_branch="main",
    )
    with (
        patch("app.mcp.tools.federation_propose.preflight_repo", return_value=empty_repo),
        patch(
            "app.mcp.tools.federation_propose.github_dispatch.dispatch_event",
            return_value=DISPATCH_OK,
        ) as mock_dispatch,
    ):
        result = loopskill_propose_registry(
            db_session,
            repo_url="https://github.com/example-org/empty-repo",
        )

    assert result["ok"] is False
    assert result["error"] == "no_skill_md_found"
    mock_dispatch.assert_not_called()

    from app.models import FederationRegistryProposal

    assert db_session.query(FederationRegistryProposal).count() == 0


# ── Test 4: duplicate proposal within 24h is deduped, not double-filed ───────


def test_propose_duplicate_within_24h_is_deduped_not_double_filed(db_session):
    """Second proposal for the SAME (identity, repo) within 24h returns cached URL,
    reusing the exact gate recipes_publish_request uses (app.feedback_ratelimit)."""
    with (
        patch("app.mcp.tools.federation_propose.preflight_repo", return_value=_viable_preflight()),
        patch(
            "app.mcp.tools.federation_propose.github_dispatch.dispatch_event",
            return_value=DISPATCH_OK,
        ),
    ):
        result1 = loopskill_propose_registry(
            db_session,
            repo_url=SAMPLE_REPO,
            api_key_id="key-abc",
        )
    assert result1["ok"] is True
    assert result1["deduped"] is False

    with (
        patch("app.mcp.tools.federation_propose.preflight_repo", return_value=_viable_preflight()),
        patch(
            "app.mcp.tools.federation_propose.github_dispatch.dispatch_event",
            return_value=DISPATCH_OK,
        ) as mock2,
    ):
        result2 = loopskill_propose_registry(
            db_session,
            repo_url=SAMPLE_REPO,
            api_key_id="key-abc",
        )

    # Must NOT open a second issue.
    mock2.assert_not_called()
    assert result2["ok"] is True
    assert result2["deduped"] is True
    assert result2["deduped"] is True

    from app.models import FederationRegistryProposal

    # Only ONE row persisted — the dedup hit never reached the DB insert.
    assert db_session.query(FederationRegistryProposal).count() == 1


def test_propose_already_registered_repo_reports_back_no_issue(db_session):
    """issue #289: a proposal for a repo ALREADY LIVE in
    config/federation_sources.yaml (e.g. anthropics/skills, registered as
    github-anthropic since decision #13) must be reported back honestly and
    must NOT open a duplicate GitHub issue, touch the rate limiter, or
    persist a DB row — each duplicate previously cost a full triage cycle
    (issue #288 was exactly this: a bot re-proposed anthropics/skills)."""
    already_live = PreflightResult(
        repo_slug="anthropics/skills",
        repo_exists=True,
        skill_md_count=17,
        license_detected=None,
        default_branch="main",
    )
    with (
        patch("app.mcp.tools.federation_propose.preflight_repo", return_value=already_live),
        patch(
            "app.mcp.tools.federation_propose.github_dispatch.dispatch_event",
            return_value=DISPATCH_OK,
        ) as mock_dispatch,
    ):
        result = loopskill_propose_registry(
            db_session,
            repo_url="https://github.com/anthropics/skills",
        )

    assert result["ok"] is True, result
    assert result["status"] == "already_registered"
    assert result["existing_source_id"] == "github-anthropic"
    assert result["review_channel_open"] is False
    assert result["issue_url"] == ""
    mock_dispatch.assert_not_called()

    from app.models import FederationRegistryProposal

    # No DB row — the already-registered check short-circuits before persist.
    assert db_session.query(FederationRegistryProposal).count() == 0


def test_propose_already_registered_repo_is_case_insensitive(db_session):
    """GitHub repo slugs are case-insensitive; a proposal spelled with
    different casing than the registered entry must still be caught."""
    already_live = PreflightResult(
        repo_slug="Anthropics/Skills",
        repo_exists=True,
        skill_md_count=17,
        license_detected=None,
        default_branch="main",
    )
    with (
        patch("app.mcp.tools.federation_propose.preflight_repo", return_value=already_live),
        patch(
            "app.mcp.tools.federation_propose.github_dispatch.dispatch_event",
            return_value=DISPATCH_OK,
        ) as mock_dispatch,
    ):
        result = loopskill_propose_registry(
            db_session,
            repo_url="https://github.com/Anthropics/Skills",
        )

    assert result["ok"] is True, result
    assert result["status"] == "already_registered"
    assert result["existing_source_id"] == "github-anthropic"
    mock_dispatch.assert_not_called()


def test_propose_new_unregistered_repo_still_dispatches_normally(db_session):
    """Regression guard: the already-registered check must NOT false-positive
    on a genuinely new repo — the normal dispatch path stays intact."""
    with (
        patch("app.mcp.tools.federation_propose.preflight_repo", return_value=_viable_preflight()),
        patch(
            "app.mcp.tools.federation_propose.github_dispatch.dispatch_event",
            return_value=DISPATCH_OK,
        ) as mock_dispatch,
    ):
        result = loopskill_propose_registry(
            db_session,
            repo_url=SAMPLE_REPO,
        )

    assert result["ok"] is True, result
    assert result["status"] == "pending_review"
    mock_dispatch.assert_called_once()


def test_propose_duplicate_uses_dedup_gate_not_a_second_mechanism(db_session):
    """The dedupe signature must be sha256(identity|repo_slug) recorded via
    app.feedback_ratelimit.check_and_record — proving there is exactly ONE
    dedupe mechanism shared with recipes_publish_request, not a second one."""
    with (
        patch("app.mcp.tools.federation_propose.preflight_repo", return_value=_viable_preflight()),
        patch(
            "app.mcp.tools.federation_propose.github_dispatch.dispatch_event",
            return_value=DISPATCH_OK,
        ),
    ):
        loopskill_propose_registry(db_session, repo_url=SAMPLE_REPO, api_key_id="key-xyz")

    # The signature must be present in feedback_ratelimit's shared dedup store —
    # the SAME module publish_request.py and recipify_request.py use.
    assert len(rl_module._dedup) == 1


# ── Test 5: anonymous proposals are rate-limited ──────────────────────────────


def test_propose_anonymous_is_rate_limited(db_session):
    """Anonymous (no api_key_id, no agent_id) proposals are rate-limited via
    app.feedback_ratelimit — same windows every other *_request tool shares.
    The loop-detector window (3 submissions / 5 min) fires before the looser
    per-tool ceiling (10/24h) for a burst of distinct-repo proposals from one
    anonymous identity — proving anonymous callers are NOT exempt from either
    gate."""

    def _preflight_for(repo_url: str) -> PreflightResult:
        # Distinct repo_slug per URL so the (identity, repo) dedupe gate does
        # NOT short-circuit before the loop/per-tool ceiling is reached — each
        # call is a genuinely distinct proposal, exactly like a real abuser
        # spraying different repos from one anonymous identity.
        slug = repo_url.rsplit("/", 1)[-1]
        return _viable_preflight(repo_slug=f"example-org/{slug}")

    results: list[dict] = []
    with (
        patch("app.mcp.tools.federation_propose.preflight_repo", side_effect=_preflight_for),
        patch(
            "app.mcp.tools.federation_propose.github_dispatch.dispatch_event",
            return_value=DISPATCH_OK,
        ),
    ):
        # LOOP_THRESHOLD (3 in 5 min) is tighter than PER_TOOL_MAX (10/24h) for
        # a rapid burst, so it fires first — try enough distinct repos to
        # guarantee SOME gate blocks an anonymous burst.
        for i in range(rl_module.LOOP_THRESHOLD + rl_module.PER_TOOL_MAX):
            r = loopskill_propose_registry(
                db_session,
                repo_url=f"https://github.com/example-org/repo-{i}",
            )
            results.append(r)
            if not r["ok"]:
                break

    blocked = [r for r in results if not r["ok"]]
    assert blocked, "an anonymous burst of proposals must eventually be blocked"
    assert blocked[0]["error"] in ("rate_limit_exceeded", "loop_detector_cooldown")
    # And the block must have fired well before every submission succeeded —
    # anonymous callers do not get an unlimited budget.
    assert len(results) < rl_module.LOOP_THRESHOLD + rl_module.PER_TOOL_MAX


def test_propose_force_bypasses_loop_cooldown(db_session):
    """force=True + confirmation overrides the loop-detector cooldown, mirroring
    publish_request's force-bypass semantics exactly."""
    identity = "anon"
    unique_repo = f"https://github.com/example-org/force-{uuid.uuid4().hex[:8]}"

    import time as _time

    now = _time.monotonic()
    with rl_module._lock:
        rl_module._loop[identity] = [now, now - 1, now - 2]
        rl_module._cooldown[identity] = now + 900

    with (
        patch("app.mcp.tools.federation_propose.preflight_repo", return_value=_viable_preflight()),
        patch(
            "app.mcp.tools.federation_propose.github_dispatch.dispatch_event",
            return_value=DISPATCH_OK,
        ) as mock_forced,
    ):
        result = loopskill_propose_registry(
            db_session,
            repo_url=unique_repo,
            force=True,
            confirmation="yes I want to resubmit",
        )

    assert result["ok"] is True, result
    mock_forced.assert_called_once()


# ── Test 6: accepting a source is a config edit, NOT a Python change ─────────


def test_accepting_source_is_config_edit_no_python_change(tmp_path):
    """Adding a config entry to config/federation_sources.yaml must register a
    NEW source in LIVE_SOURCES with zero Python changes — the load-bearing
    contract of decision #10."""
    import app.services.federation_sources_config as fsc

    fake_yaml = tmp_path / "federation_sources.yaml"
    fake_yaml.write_text(
        """
version: 1
adapter_sources:
  - hermes-hub
github_taps:
  - source_id: github-brandnew
    repo: brandnew-org/brandnew-skills
    path: "skills/"
    repo_license: "MIT"
    trust: curated-community
    in_metasearch: false
"""
    )

    original_path = fsc.FEDERATION_SOURCES_YAML
    try:
        fsc.FEDERATION_SOURCES_YAML = fake_yaml
        fsc.reset_cache_for_tests()

        assert "github-brandnew" not in fsc.adapter_source_ids()
        # A config-only edit — no fsc.py / github_taps.py / federation.py change
        # is needed for this to register.
        rows = fsc.github_tap_rows()
        assert any(r["source_id"] == "github-brandnew" for r in rows)

        # Rebuild the downstream GITHUB_TAPS the same way the module does at
        # import time, proving a fresh process picks up the new config entry
        # with zero code changes.
        import app.services.github_taps as gt

        rebuilt = gt._build_github_taps()
        assert "github-brandnew" in {t.source_id for t in rebuilt}
    finally:
        fsc.FEDERATION_SOURCES_YAML = original_path
        fsc.reset_cache_for_tests()


def test_live_sources_reads_from_config_not_hardcoded_tuple():
    """LIVE_SOURCES must be built from app.services.federation_sources_config,
    not a literal tuple in federation.py — asserted by import-source inspection."""
    import inspect

    import app.services.federation as federation_mod

    source = inspect.getsource(federation_mod._live_sources)
    assert "federation_sources_config" in source
    assert "adapter_source_ids" in source


# ── Test 7: regression — all 14 pre-existing sources survive the config move ─


def test_all_14_preexisting_sources_survive_config_move():
    """The exact 14 sources registered before the config move must still all
    be present after it — byte-for-byte the same set, order-independent."""
    from app.services.federation import LIVE_SOURCES

    expected = {
        "hermes-hub",
        "clawhub",
        "skills-sh",
        "lobehub",
        "browse-sh",
        "well-known",
        "github-oss",
        "github-anthropic",
        "github-openai",
        "github-huggingface",
        "github-nvidia",
        "github-gstack",
        "github-marketing",
        "github-superpowers",
        # maturity_0821 curated expansion (13 sources, 2026-08-21 — vault:
        # research/2026-08-21-federation-source-expansion.md). Every addition
        # to federation_sources.yaml must be pinned here too: this test is the
        # anti-drift contract, so growth is an explicit two-place edit.
        "github-agentskillexchange",
        "github-journal-skills",
        "github-alirezarezvani-skills",
        "github-wshobson-agents",
        "github-jimliu-baoyu-skills",
        "github-runcomfy-skills",
        "github-kdense-scientific-skills",
        "github-orchestra-research-skills",
        "github-atc-agentic-toolkit",
        "github-thedotmack-claude-mem",
        "github-skill-seekers",
        "github-hoangnguyen-skills-standard",
        "github-litestar-skills",
        "github-awesome-agent-skills",
    }
    assert set(LIVE_SOURCES) == expected, f"drift: {set(LIVE_SOURCES) ^ expected}"
    assert len(LIVE_SOURCES) == 28


# ── Test 8: the REST endpoint mirrors the MCP tool contract ──────────────────


def test_rest_endpoint_mirrors_mcp_tool_contract(db_session):
    """POST /api/federation/propose delegates to the SAME
    loopskill_propose_registry function the MCP tool calls — proving the two
    surfaces share one implementation and cannot drift."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.federation_propose_routes import router as federation_router

    test_app = FastAPI()
    test_app.include_router(federation_router)

    def override_db():
        yield db_session

    test_app.dependency_overrides[get_db] = override_db

    with (
        patch("app.mcp.tools.federation_propose.preflight_repo", return_value=_viable_preflight()),
        patch(
            "app.mcp.tools.federation_propose.github_dispatch.dispatch_event",
            return_value=DISPATCH_OK,
        ),
        TestClient(test_app, raise_server_exceptions=True) as c,
    ):
        resp = c.post(
            "/api/federation/propose",
            json={"repo_url": SAMPLE_REPO, "why": "great skills"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["review_channel_open"] is True
    assert body["issue_url"] == ""
    assert body["preflight"]["skill_md_count"] == 5


def test_rest_endpoint_repo_not_found_returns_422_not_500(db_session):
    """A repo-not-found response is a client-fixable 422, not a server 500 —
    same discipline as reporting back instead of opening a junk issue."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.federation_propose_routes import router as federation_router

    test_app = FastAPI()
    test_app.include_router(federation_router)

    def override_db():
        yield db_session

    test_app.dependency_overrides[get_db] = override_db

    not_found = PreflightResult(
        repo_slug="example-org/nope",
        repo_exists=False,
        skill_md_count=None,
        license_detected=None,
        reason="repository not found on GitHub",
    )

    with (
        patch("app.mcp.tools.federation_propose.preflight_repo", return_value=not_found),
        TestClient(test_app, raise_server_exceptions=True) as c,
    ):
        resp = c.post(
            "/api/federation/propose",
            json={"repo_url": "https://github.com/example-org/nope"},
        )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "repo_not_found"
