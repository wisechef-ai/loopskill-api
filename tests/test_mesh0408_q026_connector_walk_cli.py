"""mesh_0408 Q-026 — CLI entrypoint for the connector daily walk.

Covers scripts/connector_walk.py, the thin driver around
``app.services.connector_taps.run_daily_walk`` that closes the "no CLI, no
scheduler" gap identified in Q-026 (see module docstring on the script for
full context).

Tests exercise ``connector_walk.run_walk`` directly (same pattern as
``tests/test_purge_fake_creator_payout.py`` — the inner function takes the
db session as an explicit argument, so tests never need to go through
``main()``'s own ``SessionLocal()`` construction). All network access is
stubbed via the ``_get`` injectable that ``run_daily_walk`` / the walker
functions accept — no test in this file touches the network (and the
repo-root ``conftest.py`` autouse network guard would fail the test if one
tried).

Covers:
  * --dry-run performs ZERO writes to ExternalConnector (row count unchanged).
  * exit code 0 on a successful staged walk (>=1 staged).
  * exit code 1 when the walk stages zero.
  * summary output (both human and --json) contains discovered/staged/blocked.
  * regression guard: the CLI never creates a real Connector row, staged or not.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.models import Connector, ExternalConnector
from scripts.connector_walk import run_walk


class _FakeResp:
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _one_dir_docker_get(url: str, timeout: float | None = None):
    """Fake ``_get`` that returns exactly one candidate from the
    docker-mcp-registry endpoint and an empty/dead response from the other
    two endpoints — enough for a walk that discovers+stages >=1 row."""
    if "docker/mcp-registry" in url:
        return _FakeResp(200, [{"name": "sqlite", "type": "dir", "html_url": "https://github.com/x"}])
    if "modelcontextprotocol/servers" in url:
        return _FakeResp(200, [])
    if "registry.modelcontextprotocol.io" in url:
        return _FakeResp(200, {"servers": []})
    return _FakeResp(404, {})


def _all_empty_get(url: str, timeout: float | None = None):
    """Fake ``_get`` that returns nothing from every source — the walk
    discovers and stages 0 rows (the 'upstream catalogs all dead' case)."""
    if "docker/mcp-registry" in url or "modelcontextprotocol/servers" in url:
        return _FakeResp(200, [])
    if "registry.modelcontextprotocol.io" in url:
        return _FakeResp(200, {"servers": []})
    return _FakeResp(404, {})


class TestDryRunZeroWrites:
    def test_dry_run_row_count_unchanged(self, db_session: Session, capsys):
        before = db_session.query(ExternalConnector).count()

        code = run_walk(db_session, dry_run=True, _get=_one_dir_docker_get)

        after = db_session.query(ExternalConnector).count()
        assert before == after == 0
        assert code == 0

    def test_dry_run_with_existing_rows_still_unchanged(self, db_session: Session):
        from app.services import connector_taps

        # Seed one real staged row first via the (non-dry-run) staging path.
        connector_taps.stage_candidates(
            db_session,
            [
                connector_taps.Candidate(
                    source="docker-mcp-registry",
                    external_id="servers/Preexisting",
                    slug="docker-mcp-registry--preexisting",
                    title="Preexisting",
                    trust_tier=connector_taps.TRUST_TRUSTED_SOURCE,
                )
            ],
        )
        before = db_session.query(ExternalConnector).count()
        assert before == 1

        run_walk(db_session, dry_run=True, _get=_one_dir_docker_get)

        after = db_session.query(ExternalConnector).count()
        assert after == before == 1, "dry-run must not add, modify, or remove any ExternalConnector row"


class TestExitCodes:
    def test_exit_code_zero_on_successful_staged_walk(self, db_session: Session):
        code = run_walk(db_session, dry_run=False, _get=_one_dir_docker_get)
        assert code == 0
        assert db_session.query(ExternalConnector).count() >= 1

    def test_exit_code_one_when_zero_staged(self, db_session: Session):
        code = run_walk(db_session, dry_run=False, _get=_all_empty_get)
        assert code == 1
        assert db_session.query(ExternalConnector).count() == 0

    def test_dry_run_always_exits_zero_even_with_zero_discovered(self, db_session: Session):
        """--dry-run never applies the staged>=1 failure signal — it hasn't
        attempted to stage anything, so 'discovered 0' isn't a walk failure
        by itself (that's what a non-dry-run invocation is for)."""
        code = run_walk(db_session, dry_run=True, _get=_all_empty_get)
        assert code == 0


class TestSummaryOutput:
    def test_human_summary_contains_counts(self, db_session: Session, capsys):
        run_walk(db_session, dry_run=False, as_json=False, _get=_one_dir_docker_get)
        out = capsys.readouterr().out
        assert "discovered=" in out
        assert "staged=" in out
        assert "blocked=" in out

    def test_json_summary_contains_counts(self, db_session: Session, capsys):
        import json

        run_walk(db_session, dry_run=False, as_json=True, _get=_one_dir_docker_get)
        out = capsys.readouterr().out.strip()
        body = json.loads(out)
        assert "discovered" in body
        assert "staged" in body
        assert "blocked" in body
        assert body["staged"] >= 1

    def test_dry_run_summary_reports_dry_run_no_writes(self, db_session: Session, capsys):
        run_walk(db_session, dry_run=True, as_json=False, _get=_one_dir_docker_get)
        out = capsys.readouterr().out
        assert "DRY RUN" in out


class TestNeverCreatesRealConnector:
    """Regression guard: the CLI is a thin driver over stage_candidates,
    which writes ONLY to ExternalConnector. No code path in the CLI (or in
    connector_taps.py that it calls) may create a real Connector row."""

    def test_successful_walk_creates_zero_real_connectors(self, db_session: Session):
        assert db_session.query(Connector).count() == 0

        code = run_walk(db_session, dry_run=False, _get=_one_dir_docker_get)

        assert code == 0
        assert db_session.query(ExternalConnector).count() >= 1, "sanity: staging did happen"
        assert db_session.query(Connector).count() == 0, (
            "connector_walk must NEVER materialize a real Connector row — "
            "staging is not publishing"
        )

    def test_dry_run_creates_zero_real_connectors(self, db_session: Session):
        run_walk(db_session, dry_run=True, _get=_one_dir_docker_get)
        assert db_session.query(Connector).count() == 0

    def test_zero_staged_walk_creates_zero_real_connectors(self, db_session: Session):
        run_walk(db_session, dry_run=False, _get=_all_empty_get)
        assert db_session.query(Connector).count() == 0


class TestStagedRowsReviewRequired:
    """The review_required=True invariant must survive the CLI wrapper —
    it's hardcoded in stage_candidates and this test proves the CLI doesn't
    bypass or override it."""

    def test_staged_rows_land_review_required_true(self, db_session: Session):
        run_walk(db_session, dry_run=False, _get=_one_dir_docker_get)
        rows = db_session.query(ExternalConnector).all()
        assert rows, "sanity: at least one row must have been staged"
        assert all(row.review_required is True for row in rows)
