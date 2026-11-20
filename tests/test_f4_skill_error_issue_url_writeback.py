"""F-4 — the skill-error issue-URL write-back could never land, at three hops.

THE DEFECTS
-----------
`report_skill_error` (MCP) and `POST /api/v1/skill-error` both persist an
**IncidentReport** and fire a `repository_dispatch` with `event_type="skill-error"`,
carrying `report.id`. The Feedback Dispatcher workflow files a GitHub issue and then
PATCHes the URL back to `PATCH /api/internal/feedback/{id}/issue-url`. Every one of
the three hops that had to work was broken:

1. WRONG TABLE. The workflow's `case` had no `skill-error)` arm, so `TABLE` fell
   through to its default `feedback` and the handler looked the id up in
   `feedback_submissions`. No incident id is ever a feedback id → 404 on 100% of
   skill-error dispatches. `table` was also a closed `Literal[...]` with no member
   that could name `incident_reports`, so the correct call was unrepresentable.

2. NO COLUMN. Even with the right table, `incident_reports` had no `issue_url` and
   no `feedback_status` — there was nowhere to put the value. Both are added by
   alembic revision `ser1116_incident_issue_url`.

3. UNREACHABLE POLL. `app/mcp/tools/skill_error.py` tells clients to "poll
   GET /api/feedback/{id} for the resolved URL", but `feedback_status_routes` only
   queried `feedback_submissions` and `recipify_requests` → every skill-error poll
   404'd too.

PLUS: `feedback_ratelimit.update_dedup_url()` is called by all 4 sibling MCP tools
(feedback, recipify-request, publish-request, federation-propose) but never by
`skill_error.py`, which cannot know the URL at call time. So the dedup soft-block
replayed `issue_url=""`: a second reporter with an identical signature was told
"already reported" with no link. The PATCH handler now backfills it.

AND: `transparency_routes._compute_skill_error_rate_7d` filtered
`IncidentReport.created_at`, a column that does not exist (the model's only
timestamp is `occurred_at`). The `AttributeError` was swallowed by a blanket
`except Exception: errors = 0`, so the public "skill error rate 7d" metric read
0.0 permanently. Prod on 2026-11-16: 2 incidents in the 7d window, metric reported 0.

PRODUCTION EVIDENCE
-------------------
`skill_error_reports` has 0 rows ALL TIME, so the card's premise (that this path
writes there) was false — that table is a separate, unused model. The 11 all-time /
2-in-window rows the report counted live in `incident_reports`. GitHub issues #462
(2026-11-13) and #463 (2026-11-14) are the two in-window reports; journalctl shows
both write-back PATCHes returning 404.

These tests pin each hop independently, so a regression in any one of them fails
loudly instead of silently zeroing a public metric.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.models import IncidentReport

INTERNAL_TOKEN = "test-internal-token-f4-skill-error"
WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "feedback-dispatch.yml"


def _wf() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _utc_naive() -> datetime:
    """telemetry_events.created_at is a naive DateTime column; match how PG's
    now()::timestamp lands (UTC, no tzinfo) so the window filter behaves the same
    on SQLite and Postgres."""
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _clean_ratelimit_state():
    """The rate limiter is process-global; never inherit another test's dedup hits."""
    from app import feedback_ratelimit

    feedback_ratelimit.reset_all()
    yield
    feedback_ratelimit.reset_all()


@pytest.fixture()
def app_with_routers(db_session):
    """Minimal app mounting exactly the two routers under test."""
    from app.feedback_status_routes import router as status_router
    from app.internal_routes import router as internal_router

    test_app = FastAPI()
    test_app.include_router(internal_router)
    test_app.include_router(status_router)

    def override_get_db():
        yield db_session

    test_app.dependency_overrides[get_db] = override_get_db
    return test_app


@pytest.fixture()
def internal_client(app_with_routers, monkeypatch):
    monkeypatch.setenv("INTERNAL_PATCH_TOKEN", INTERNAL_TOKEN)
    return TestClient(app_with_routers, raise_server_exceptions=False)


def make_incident(db, *, slug_id=None, occurred_at=None, signature="ab" * 32, agent="codex-test"):
    """Insert a Skill + IncidentReport pair. Returns the IncidentReport."""
    from tests.conftest import make_skill

    skill = make_skill(db, slug=f"f4-{uuid4().hex[:12]}")
    report = IncidentReport(
        id=uuid4(),
        skill_id=skill.id,
        error_signature=signature,
        env_fingerprint={"os": "linux"},
        agent_fp_anon=agent,
        occurred_at=occurred_at or datetime.now(UTC),
    )
    db.add(report)
    db.flush()
    return report


def patch_url(client, row_id, table, issue_url="https://github.com/o/r/issues/42", **extra):
    return client.patch(
        f"/api/internal/feedback/{row_id}/issue-url",
        json={"issue_url": issue_url, "table": table, **extra},
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )


# ── 1. the columns must exist on the model ──────────────────────────────────


class TestIncidentReportHasWriteBackColumns:
    def test_issue_url_and_feedback_status_are_mapped(self):
        """Hop 2: there must be somewhere to put the URL.

        Pinned on the ORM (not the live DB) so a model edit that drops a column
        fails here; the Postgres chain test covers model↔migration drift.
        """
        mapped = {c.key for c in IncidentReport.__table__.columns}
        assert {"issue_url", "feedback_status"} <= mapped, (
            f"incident_reports lost a write-back column; has {sorted(mapped)}. The workflow PATCH cannot "
            "store an issue URL without both."
        )

    def test_incident_reports_still_has_no_created_at(self):
        """The transparency bug depended on `created_at` not existing.

        If a future migration ever ADDS created_at, the transparency test below
        stops being a regression test — this tells us so.
        """
        assert "created_at" not in {c.key for c in IncidentReport.__table__.columns}


# ── 2. the PATCH handler must accept and apply table="incident" ─────────────


class TestPatchAcceptsIncidentTable:
    def test_incident_patch_stores_url_and_marks_filed(self, internal_client, db_session):
        """Hop 1: the workflow's table name must resolve to incident_reports."""
        report = make_incident(db_session)
        url = "https://github.com/wisechef-ai/loopskill-api/issues/499"

        r = patch_url(internal_client, report.id, "incident", issue_url=url)

        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text[:200]}"
        db_session.refresh(report)
        assert report.issue_url == url
        assert report.feedback_status == "filed"

    def test_unknown_incident_id_is_404(self, internal_client):
        """A wrong table 404s and a missing row 404s — they must stay distinguishable
        from a schema gap, so pin the missing-row case too."""
        r = patch_url(internal_client, uuid4(), "incident")
        assert r.status_code == 404

    def test_table_names_are_a_closed_set(self, internal_client):
        """`table` is a Literal. A typo'd table must be rejected at validation,
        not silently routed to the `else:` (recipify) branch."""
        r = patch_url(internal_client, uuid4(), "skill_error")
        assert r.status_code == 422, f"unknown table name should fail validation, got {r.status_code}"

    def test_incident_is_routed_before_the_recipify_default(self):
        """The `else:` branch is recipify. incident must be an explicit elif,
        otherwise incident ids would be looked up in recipify_requests."""
        from app import internal_routes

        src = inspect.getsource(internal_routes.patch_issue_url)
        assert 'body.table == "incident"' in src, "incident lookup must be explicit, not the else default"
        assert '"incident"' in inspect.getsource(internal_routes.IssueUrlPatch), (
            "IssueUrlPatch.table Literal lost its 'incident' member — the workflow's "
            'table="incident" would be rejected with 422'
        )


# ── 3. the status poll must resolve an incident id ───────────────────────────


class TestStatusPollResolvesIncidentId:
    def test_get_status_returns_issue_url_for_incident_row(
        self, internal_client, db_session, app_with_routers
    ):
        """Hop 3: skill_error.py tells clients to poll this endpoint."""
        report = make_incident(db_session)
        url = "https://github.com/wisechef-ai/loopskill-api/issues/500"
        assert patch_url(internal_client, report.id, "incident", issue_url=url).status_code == 200

        poll = TestClient(app_with_routers).get(f"/api/feedback/{report.id}")
        assert poll.status_code == 200, (
            f"GET /api/feedback/{{id}} must resolve incident_reports ids — the MCP tool's "
            f"documented contract is 'clients poll GET /api/feedback/{{id}}'; got {poll.status_code}"
        )
        assert poll.json()["issue_url"] == url

    def test_poll_response_carries_a_timestamp_for_incident_rows(
        self, internal_client, db_session, app_with_routers
    ):
        """FeedbackStatusOut.created_at is REQUIRED, but IncidentReport has no
        created_at column — only occurred_at. The handler used to read
        `row.created_at` unconditionally, so every incident poll raised
        AttributeError and 500'd. This is the pin for that fix."""
        report = make_incident(db_session)
        assert patch_url(internal_client, report.id, "incident").status_code == 200

        poll = TestClient(app_with_routers).get(f"/api/feedback/{report.id}")
        assert poll.status_code == 200, f"incident poll must not 500: {poll.status_code} {poll.text[:200]}"
        body = poll.json()
        assert body["created_at"], "created_at is a required field and must resolve from occurred_at"
        assert body["status"] == "filed"

    def test_status_handler_queries_incident_reports(self):
        from app import feedback_status_routes

        src = inspect.getsource(feedback_status_routes.get_feedback_status)
        assert "IncidentReport" in src, "status endpoint no longer resolves incident ids"


# ── 4. transparency must stop laundering a crash into a healthy zero ─────────


# Defect 3 (transparency created_at -> occurred_at, narrowed guard) landed on main
# independently via t_0111f6c4 / t_61421779; its pins live in
# tests/test_t1116_transparency_provenance.py, so this file no longer duplicates them.


class TestDedupCacheBackfill:
    def test_repeat_reporter_gets_the_real_issue_url(self, internal_client, db_session):
        """A 2nd identical report within 7d used to replay issue_url=""."""
        from app import feedback_ratelimit

        report = make_incident(db_session)
        sig = "cd" * 32
        url = "https://github.com/wisechef-ai/loopskill-api/issues/501"

        first = feedback_ratelimit.check_and_record(identity="agent-f4", tool="skill-error", signature=sig)
        assert first.allowed and first.issue_url == "", "first hit should be allowed with no URL yet"

        r = patch_url(internal_client, report.id, "incident", issue_url=url, signature=sig)
        assert r.status_code == 200

        second = feedback_ratelimit.check_and_record(identity="agent-f4", tool="skill-error", signature=sig)
        assert second.deduped is True, "2nd identical signature inside 7d should soft-block"
        assert second.issue_url == url, (
            f"repeat reporter was told 'already reported' with issue_url={second.issue_url!r} — "
            "the PATCH handler must backfill update_dedup_url()"
        )

    def test_patch_without_signature_is_still_fine(self, internal_client, db_session):
        """signature is optional — older workflow runs must keep working."""
        report = make_incident(db_session)
        assert patch_url(internal_client, report.id, "incident").status_code == 200

    def test_reuses_the_existing_primitive(self):
        """No second implementation of the dedup update may exist.

        update_dedup_url() is the house primitive with 4 existing callers; a
        parallel second copy would let the two drift.
        """
        from app import feedback_ratelimit

        assert not hasattr(feedback_ratelimit, "record_issue_url"), "duplicate of update_dedup_url"
        assert hasattr(feedback_ratelimit, "update_dedup_url")


# ── 6. workflow wiring ───────────────────────────────────────────────────────


class TestWorkflowWiring:
    """The workflow is the other half of the contract; it lives in YAML, so these
    are source pins. Behavioural coverage of the PATCH handler is above."""

    def test_skill_error_targets_the_incident_table(self):
        assert 'skill-error) TABLE="incident"' in _wf(), (
            'skill-error must PATCH table="incident". With no arm here TABLE fell through to '
            '"feedback" and every write-back 404\'d.'
        )

    def test_signature_is_forwarded(self):
        assert "client_payload.signature" in _wf(), "signature must reach the API for the dedup backfill"

    def test_agent_attribution_reads_skill_error_payload_key(self):
        """skill-error sends `agent_fp_anon`; reading only `agent_id` printed
        'agent: unknown' on every skill-error issue (#462/#463)."""
        assert "agent_fp_anon" in _wf(), "skill-error issues were filed as 'agent: unknown'"

    def test_signature_is_sanitised_before_hitting_the_json_body(self):
        """The value is interpolated into a JSON string in a curl -d; it must be
        constrained to hex so it cannot break out of the string."""
        assert "tr -cd '0-9a-fA-F'" in _wf(), "signature must be sanitised before interpolation"

    def test_workflow_file_exists_where_tests_and_docs_point(self):
        assert WORKFLOW.exists(), f"workflow moved: {WORKFLOW}"
