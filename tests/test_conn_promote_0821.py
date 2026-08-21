"""conn_promote_0821 — quality-gated staged->listed connector promotion.

Covers ``app.services.connector_promote`` (every gate RED+GREEN, idempotency,
dup-slug, trust-label discipline) and ``scripts/connector_promote.py`` (the
CLI driver, dry-run zero-writes, exit codes).

All network access is stubbed via the ``_head`` injectable ``evaluate_candidate``
/ ``run_promotion_pass`` accept (same pattern as ``connector_taps``'s ``_get``
injectable) — no test in this file touches the network, and the repo-root
``conftest.py`` autouse network guard would fail any test that tried.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import Connector, ExternalConnector
from app.services import connector_promote as cp
from app.services.connector_taps import TRUST_TRUSTED_SOURCE
from scripts.connector_promote import run as cli_run

_CLEAN_CONFIG = {"command": "npx", "args": ["-y", "@scope/some-mcp-server"], "env": {"K": "${K}"}}
_HTTP_CONFIG = {"url": "https://example.com/mcp"}


def _stage(
    db: Session,
    *,
    source: str = "docker-mcp-registry",
    external_id: str | None = None,
    slug: str | None = None,
    title: str = "Some Connector",
    description: str = "A useful MCP server.",
    connector_type: str = "stdio",
    config_template: dict | None = None,
    origin_url: str | None = "https://github.com/x/y",
    license_: str | None = "mit",
    trust_tier: str = TRUST_TRUSTED_SOURCE,
) -> ExternalConnector:
    uid = uuid4().hex[:8]
    row = ExternalConnector(
        id=uuid4(),
        source=source,
        external_id=external_id or f"servers/{uid}",
        slug=slug or f"{source}--{uid}",
        title=title,
        description=description,
        connector_type=connector_type,
        config_template=_CLEAN_CONFIG if config_template is None else config_template,
        origin_url=origin_url,
        license=license_,
        trust_tier=trust_tier,
        review_required=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _always_reachable(*a, **kw) -> int:
    return 200


def _always_404(*a, **kw) -> int:
    return 404


def _always_429(*a, **kw) -> int:
    return 429


def _always_none(*a, **kw) -> int | None:
    return None


# ─────────────────────────── G1 license_allowlist ──────────────────────────


class TestGateLicenseAllowlist:
    def test_red_missing_license_rejected(self, db_session: Session) -> None:
        row = _stage(db_session, license_=None)
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert not result.passed
        assert any("G1" in r for r in result.reasons)

    def test_red_unknown_license_rejected(self, db_session: Session) -> None:
        row = _stage(db_session, license_="proprietary-closed-source")
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert not result.passed
        assert any("G1" in r for r in result.reasons)

    def test_green_mit_license_passes_gate(self, db_session: Session) -> None:
        row = _stage(db_session, license_="mit")
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert not any("G1" in r for r in result.reasons)

    def test_green_apache_license_passes_gate(self, db_session: Session) -> None:
        row = _stage(db_session, license_="Apache-2.0")
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert not any("G1" in r for r in result.reasons)


# ─────────────────────────── G2 structural_sanity ───────────────────────────


class TestGateStructuralSanity:
    def test_red_missing_connector_type_rejected(self, db_session: Session) -> None:
        row = _stage(db_session, connector_type=None)
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert not result.passed
        assert any("G2" in r for r in result.reasons)

    def test_red_stdio_missing_command_rejected(self, db_session: Session) -> None:
        row = _stage(db_session, connector_type="stdio", config_template={"args": ["-y"]})
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert not result.passed
        assert any("G2" in r for r in result.reasons)

    def test_red_literal_secret_rejected(self, db_session: Session) -> None:
        # Build the secret-shaped string at runtime so no literal appears in
        # source (GitHub push protection rejects secret-shaped literals).
        leaked = "sk_live_" + ("a" * 24)
        row = _stage(
            db_session,
            connector_type="http",
            config_template={"url": "https://example.com/mcp", "headers": {"Authorization": leaked}},
        )
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert not result.passed
        assert any("G2" in r for r in result.reasons)

    def test_green_clean_stdio_config_passes_gate(self, db_session: Session) -> None:
        row = _stage(db_session, connector_type="stdio", config_template=_CLEAN_CONFIG)
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert not any("G2" in r for r in result.reasons)

    def test_green_clean_http_config_passes_gate(self, db_session: Session) -> None:
        row = _stage(db_session, connector_type="http", config_template=_HTTP_CONFIG)
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert not any("G2" in r for r in result.reasons)


# ─────────────────────────── G3 ssrf_guard_recheck ──────────────────────────


class TestGateSsrfRecheck:
    def test_red_ssrf_url_rejected(self, db_session: Session) -> None:
        row = _stage(
            db_session,
            connector_type="http",
            config_template={"url": "http://169.254.169.254/latest/meta-data/"},
        )
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert not result.passed
        assert any("G3" in r for r in result.reasons)

    def test_red_dangerous_command_rejected(self, db_session: Session) -> None:
        row = _stage(db_session, connector_type="stdio", config_template={"command": "rm -rf /"})
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert not result.passed
        assert any("G3" in r for r in result.reasons)

    def test_green_clean_config_passes_gate(self, db_session: Session) -> None:
        row = _stage(db_session, connector_type="stdio", config_template=_CLEAN_CONFIG)
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert not any("G3" in r for r in result.reasons)


# ───────────────────────── G4 name_description_sanity ──────────────────────


class TestGateNameDescriptionSanity:
    def test_red_empty_title_rejected(self, db_session: Session) -> None:
        row = _stage(db_session, title="   ")
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert not result.passed
        assert any("G4" in r for r in result.reasons)

    def test_red_empty_description_rejected(self, db_session: Session) -> None:
        row = _stage(db_session, description="")
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert not result.passed
        assert any("G4" in r for r in result.reasons)

    def test_red_none_description_rejected(self, db_session: Session) -> None:
        row = _stage(db_session, description=None)
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert not result.passed
        assert any("G4" in r for r in result.reasons)

    def test_green_populated_fields_pass_gate(self, db_session: Session) -> None:
        row = _stage(db_session, title="Real Title", description="Real description.")
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert not any("G4" in r for r in result.reasons)


# ─────────────────────────────── G5 dup_slug ────────────────────────────────


class TestGateDupSlug:
    def test_red_slug_collides_with_real_connector_rejected(self, db_session: Session) -> None:
        real = Connector(id=uuid4(), slug="taken-slug", title="Real", connector_type="stdio", is_public=True)
        db_session.add(real)
        db_session.commit()

        row = _stage(db_session, slug="taken-slug")
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert not result.passed
        assert any("G5" in r for r in result.reasons)

    def test_green_unique_slug_passes_gate(self, db_session: Session) -> None:
        row = _stage(db_session, slug=f"free-slug-{uuid4().hex[:6]}")
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert not any("G5" in r for r in result.reasons)

    def test_apply_rejects_race_where_slug_taken_between_evaluate_and_apply(
        self, db_session: Session
    ) -> None:
        """Belt-and-braces re-check inside apply_promotion_results: even if
        evaluate_candidate saw a free slug, a concurrent promotion landing
        first must not be silently overwritten."""
        row = _stage(db_session, slug=f"race-slug-{uuid4().hex[:6]}")
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert result.passed

        # Simulate a concurrent promotion winning the race.
        real = Connector(id=uuid4(), slug=row.slug, title="Raced In", connector_type="stdio", is_public=True)
        db_session.add(real)
        db_session.commit()

        outcome = cp.apply_promotion_results(db_session, [result])
        assert outcome.rejected == 1
        assert outcome.promoted == 0
        db_session.refresh(row)
        assert row.promotion_status == cp.REJECTED
        assert "concurrent" in row.promotion_reason


# ───────────────────────────── G6 reachable_probe ───────────────────────────


class TestGateReachableProbe:
    def test_red_definitive_404_rejected(self, db_session: Session) -> None:
        row = _stage(db_session)
        result = cp.evaluate_candidate(db_session, row, _head=_always_404)
        assert not result.passed
        assert not result.transient
        assert any("G6" in r for r in result.reasons)

    def test_green_200_passes_gate(self, db_session: Session) -> None:
        row = _stage(db_session)
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert result.passed

    def test_transient_429_deferred_not_rejected(self, db_session: Session) -> None:
        row = _stage(db_session)
        result = cp.evaluate_candidate(db_session, row, _head=_always_429)
        assert not result.passed
        assert result.transient
        assert any("G6" in r for r in result.reasons)

    def test_transient_probe_error_deferred_not_rejected(self, db_session: Session) -> None:
        row = _stage(db_session)
        result = cp.evaluate_candidate(db_session, row, _head=_always_none)
        assert not result.passed
        assert result.transient

    def test_deferred_row_untouched_by_apply(self, db_session: Session) -> None:
        row = _stage(db_session)
        result = cp.evaluate_candidate(db_session, row, _head=_always_429)
        outcome = cp.apply_promotion_results(db_session, [result])
        assert outcome.deferred == 1
        assert outcome.promoted == 0
        assert outcome.rejected == 0
        db_session.refresh(row)
        assert row.promotion_status is None
        assert row.review_required is True

    def test_no_probeable_url_rejected_not_transient(self, db_session: Session) -> None:
        row = _stage(db_session, config_template={"command": "npx", "args": ["x"]}, origin_url=None)
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert not result.passed
        assert not result.transient
        assert any("G6" in r for r in result.reasons)


# ───────────────────────────── Trust label discipline ───────────────────────


class TestTrustLabelDiscipline:
    def test_promoted_connector_gets_community_indexed_never_curated(self, db_session: Session) -> None:
        row = _stage(db_session, slug=f"trust-{uuid4().hex[:6]}")
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert result.passed
        outcome = cp.apply_promotion_results(db_session, [result])
        assert outcome.promoted == 1

        conn = db_session.query(Connector).filter(Connector.slug == row.slug).first()
        assert conn is not None
        assert conn.trust_label == "community-indexed"
        assert conn.trust_label != "curated"

    def test_promoted_connector_in_metasearch_defaults_false(self, db_session: Session) -> None:
        row = _stage(db_session, slug=f"meta-{uuid4().hex[:6]}")
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        outcome = cp.apply_promotion_results(db_session, [result])
        assert outcome.promoted == 1

        conn = db_session.query(Connector).filter(Connector.slug == row.slug).first()
        assert conn.in_metasearch is False

    def test_module_source_never_contains_literal_curated_assignment(self) -> None:
        """Static guard: no code path in this module ever assigns the string
        'curated' to trust_label — only 'community-indexed' is ever written."""
        import re

        import inspect

        src = inspect.getsource(cp)
        assignments = re.findall(r"trust_label\s*=\s*(\S+)", src)
        assert assignments, "expected at least one trust_label assignment in the module"
        for value in assignments:
            assert "curated" not in value or "community-indexed" in value


# ───────────────────────────── Idempotency ──────────────────────────────────


class TestIdempotency:
    def test_promoted_row_not_reevaluated_by_candidate_query(self, db_session: Session) -> None:
        row = _stage(db_session, slug=f"idem-{uuid4().hex[:6]}")
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        cp.apply_promotion_results(db_session, [result])

        candidates = cp.candidate_query(db_session)
        assert row.id not in {r.id for r in candidates}

    def test_rerunning_apply_on_already_promoted_row_is_a_noop(self, db_session: Session) -> None:
        row = _stage(db_session, slug=f"idem2-{uuid4().hex[:6]}")
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        outcome1 = cp.apply_promotion_results(db_session, [result])
        assert outcome1.promoted == 1

        # Re-apply the SAME (now-stale) GateResult again.
        outcome2 = cp.apply_promotion_results(db_session, [result])
        assert outcome2.already_promoted == 1
        assert outcome2.promoted == 0

        # Only ONE Connector row exists for the slug.
        count = db_session.query(Connector).filter(Connector.slug == row.slug).count()
        assert count == 1

    def test_rejected_row_reevaluated_on_next_pass(self, db_session: Session) -> None:
        """A rejected row is NOT permanently excluded — candidate_query still
        surfaces it (review_required stays True) so a later source-data fix
        (e.g. upstream adds a LICENSE) can be picked up on a re-run."""
        row = _stage(db_session, license_=None, slug=f"retry-{uuid4().hex[:6]}")
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert not result.passed
        cp.apply_promotion_results(db_session, [result])
        db_session.refresh(row)
        assert row.review_required is True
        assert row.promotion_status == cp.REJECTED

        candidates = cp.candidate_query(db_session)
        assert row.id in {r.id for r in candidates}


# ─────────────────────── multi-reason reporting (no short-circuit) ─────────


class TestMultiReasonReporting:
    def test_row_failing_multiple_gates_reports_all_reasons(self, db_session: Session) -> None:
        row = _stage(db_session, license_=None, title="", description="")
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        assert not result.passed
        codes = {r.split(":")[0].split()[0] for r in result.reasons}
        assert "G1" in codes
        assert "G4" in codes


# ─────────────────────────── evaluate is read-only ──────────────────────────


class TestEvaluateNeverWrites:
    def test_evaluate_candidates_zero_db_writes(self, db_session: Session) -> None:
        row = _stage(db_session)
        before = db_session.query(ExternalConnector).count()
        before_conn = db_session.query(Connector).count()
        cp.evaluate_candidates(db_session, [row], _head=_always_reachable)
        after = db_session.query(ExternalConnector).count()
        after_conn = db_session.query(Connector).count()
        assert before == after
        assert before_conn == after_conn
        db_session.refresh(row)
        assert row.promotion_status is None
        assert row.review_required is True


# ───────────────────────── run_promotion_pass dry-run ───────────────────────


class TestRunPromotionPassDryRun:
    def test_dry_run_zero_writes(self, db_session: Session) -> None:
        _stage(db_session)
        before = db_session.query(Connector).count()
        results, outcome = cp.run_promotion_pass(db_session, apply=False, _head=_always_reachable)
        after = db_session.query(Connector).count()
        assert before == after
        assert outcome is None
        assert len(results) >= 1

    def test_apply_true_writes_promotions(self, db_session: Session) -> None:
        _stage(db_session, slug=f"apply-{uuid4().hex[:6]}")
        results, outcome = cp.run_promotion_pass(db_session, apply=True, _head=_always_reachable)
        assert outcome is not None
        assert outcome.promoted >= 1

    def test_limit_caps_evaluated_rows(self, db_session: Session) -> None:
        for _ in range(5):
            _stage(db_session)
        results, _ = cp.run_promotion_pass(db_session, apply=False, limit=2, _head=_always_reachable)
        assert len(results) == 2


# ───────────────────────────────── CLI ──────────────────────────────────────


class TestCliDriver:
    def test_cli_dry_run_exit_0_when_would_pass(self, db_session: Session, capsys) -> None:
        _stage(db_session)
        code = cli_run(db_session, apply=False, limit=None, as_json=False, _head=_always_reachable)
        assert code == 0

    def test_cli_dry_run_exit_1_when_nothing_would_pass(self, db_session: Session, capsys) -> None:
        _stage(db_session, license_=None)
        code = cli_run(db_session, apply=False, limit=None, as_json=False, _head=_always_reachable)
        assert code == 1

    def test_cli_apply_promotes_and_exits_0(self, db_session: Session, capsys) -> None:
        _stage(db_session, slug=f"cli-{uuid4().hex[:6]}")
        code = cli_run(db_session, apply=True, limit=None, as_json=False, _head=_always_reachable)
        assert code == 0
        out = capsys.readouterr().out
        assert "promoted=" in out

    def test_cli_apply_zero_writes_when_nothing_passes(self, db_session: Session, capsys) -> None:
        _stage(db_session, license_=None)
        before = db_session.query(Connector).count()
        code = cli_run(db_session, apply=True, limit=None, as_json=False, _head=_always_reachable)
        after = db_session.query(Connector).count()
        assert code == 1
        assert before == after

    def test_cli_json_output_is_valid_json(self, db_session: Session, capsys) -> None:
        import json

        _stage(db_session)
        cli_run(db_session, apply=False, limit=None, as_json=True, _head=_always_reachable)
        out = capsys.readouterr().out.strip()
        parsed = json.loads(out)
        assert "evaluated" in parsed
        assert "would_pass" in parsed

    def test_cli_never_writes_in_dry_run_even_with_deferred_rows(self, db_session: Session, capsys) -> None:
        _stage(db_session)
        before = db_session.query(Connector).count()
        cli_run(db_session, apply=False, limit=None, as_json=False, _head=_always_429)
        after = db_session.query(Connector).count()
        assert before == after


# ───────────────────────── promoted_connector_id linkage ────────────────────


class TestPromotedConnectorLinkage:
    def test_staged_row_links_to_the_minted_connector(self, db_session: Session) -> None:
        row = _stage(db_session, slug=f"link-{uuid4().hex[:6]}")
        result = cp.evaluate_candidate(db_session, row, _head=_always_reachable)
        cp.apply_promotion_results(db_session, [result])
        db_session.refresh(row)
        assert row.promoted_connector_id is not None
        conn = db_session.query(Connector).filter(Connector.id == row.promoted_connector_id).first()
        assert conn is not None
        assert conn.slug == row.slug
        assert row.promoted_at is not None
        assert row.promotion_status == cp.PROMOTED
