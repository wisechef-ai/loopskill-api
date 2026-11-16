"""t_a7ebcc96 — the fleet sync-report skill-error rail stays retired.

Why a test for a deletion
-------------------------
``skill_error_reports`` held **0 rows across its entire existence**
(2026-07-02 → 2026-11-16) while ``POST /api/sync-report`` ingested 22,834
accepted sync cycles (~167/day across the 3 live fleet members). The ingest was
never broken — ``cron_health_snapshots`` was still receiving rows minutes before
this audit — so this is not a story about an outage, it is a story about a field
no client produces. It was not empty because agent loops never failed; it was
empty because no client has ever produced the payload key its writer read
(scripts/loopskill-collect-reports.py::build_payload() has no ``skill_errors``
field — the only code in the repo that ever set it was a load tester).

Three surfaces nonetheless advertised that the signal existed:

  * ``voice_counts.skill_errors`` on ``GET /api/fleets/{id}/dashboard``
  * ``type: "skill_error"`` items from MCP ``loopskill_voice_inbox_read``
  * the FB-phase voice inbox in ``app/voice_routes.py``

Each could only ever answer 0. A permanently-zero metric is not a metric, it is
a cover story, and this file is what stops the cover story being rebuilt:

  1. the model and the table are gone,
  2. nothing under app/ may reference them again,
  3. the wire field is accepted but honest (counted, named, ignored),
  4. the drop migration refuses data and its downgrade walks back.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION = REPO_ROOT / "alembic" / "versions" / "ser1116b_retire_skill_error_reports.py"


# ── 1. model + table are gone ────────────────────────────────────────────────


def test_model_and_table_removed():
    import app.models as models

    assert not hasattr(models, "SkillErrorReport"), (
        "SkillErrorReport is back. It was retired in t_a7ebcc96 after 0 rows in "
        "its whole life; re-adding the model without a client that populates it "
        "recreates the dead rail."
    )
    from app.models import Base

    assert "skill_error_reports" not in Base.metadata.tables


# ── 2. no producer or reader may reappear under app/ ─────────────────────────


def _code_lines(path: Path):
    """Yield (lineno, line) for executable lines only.

    Comments AND docstrings are excluded, because this PR deliberately leaves
    prose behind explaining what was retired and why ("skill_error_reports"
    appears in three module docstrings). Matching on raw text would flag the
    very documentation that prevents someone rebuilding the dead rail.

    Docstring ranges come from the AST: every ``Expr`` whose value is a string
    constant is a docstring (module, class, or function level) and is skipped.
    """
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    excluded: set[int] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:  # Rationale: a file pytest can import cannot fail here; guard keeps the test honest rather than red.
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(
                node.value.value, str
            ):
                for ln in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                    excluded.add(ln)
    for i, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or i in excluded:
            continue
        yield i, raw


@pytest.mark.parametrize(
    "pattern",
    [
        r"\bSkillErrorReport\b",
        r"\bskill_error_reports\b",
        r"\bMAX_SKILL_ERRORS\b",
    ],
)
def test_app_has_no_code_reference(pattern: str):
    """No executable line under app/ may name the retired rail.

    Prose in comments/docstrings is allowed (it records the decision); code is
    not. A re-appearing import here would be the first step back to a surface
    that can never fire.
    """
    rx = re.compile(pattern)
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        for lineno, line in _code_lines(path):
            if rx.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "retired skill-error rail referenced in code:\n" + "\n".join(offenders)


def test_dashboard_voice_counts_has_no_skill_errors_key():
    """The pending badge must not come back as a hard-coded 0 either."""
    src = (REPO_ROOT / "app" / "dashboard_routes.py").read_text()
    assert 'voice_counts = {"recipify_requests": 0, "feedback": 0}' in src
    assert '"skill_errors"' not in re.sub(r"#.*", "", src)


# ── 3. the wire field is accepted, counted, and honest about being ignored ───


def test_sync_report_ingest_counts_skill_errors_as_ignored(db_session):
    """A client that sends skill_errors sees its own count dropped, not stored.

    The pre-change ack returned ``"skill_errors": N`` for a table that never
    held a row — a number that could not be falsified. ``skill_errors_ignored``
    is falsifiable: it reports what arrived and states that it went nowhere.
    """
    from app.models import APIKey, Fleet, FleetMember, User
    from app.services.sync_report import ingest_sync_report

    owner = User(email="se@t.com", display_name="SE", subscription_tier="pro")
    db_session.add(owner)
    db_session.flush()
    key = APIKey(user_id=owner.id, key_prefix="rec_live_se", key_hash="h-se", is_active=True)
    db_session.add(key)
    db_session.flush()
    fleet = Fleet(owner_user_id=owner.id, name="se-fleet", fleet_api_key_hash="hash-se")
    db_session.add(fleet)
    db_session.flush()
    member = FleetMember(
        fleet_id=fleet.id, host="h", profile="d", skills_dir="~", api_key_id=key.id, is_active=True
    )
    db_session.add(member)
    db_session.commit()

    payload = {
        "cycle_ts": "2026-11-16T12:00:00+00:00",
        "skill_errors": [
            {"slug": "s1", "semver": "1.0.0", "signature": "a", "summary": "boom"},
            {"slug": "s2", "signature": "b", "summary": "bang"},
        ],
    }
    recorded, truncated = ingest_sync_report(db_session, member, payload)

    assert recorded["skill_errors_ignored"] == 2
    assert "skill_errors" not in recorded
    assert "skill_errors" not in truncated
    # Nothing was written for the retired rail: the member row still commits,
    # which is what proves the ingest path completed rather than erroring out.
    db_session.refresh(member)
    assert member.updated_at is not None


def test_non_list_skill_errors_degrades_to_zero(db_session):
    """A malformed skill_errors value must not 500 the whole sync cycle.

    The telemetry drain failing its cron is worse than no telemetry, so a
    client that sends a dict/string instead of a list is counted as zero, not
    an exception.
    """
    from app.models import APIKey, Fleet, FleetMember, User
    from app.services.sync_report import ingest_sync_report

    owner = User(email="se2@t.com", display_name="SE2", subscription_tier="pro")
    db_session.add(owner)
    db_session.flush()
    key = APIKey(user_id=owner.id, key_prefix="rec_live_se2", key_hash="h-se2", is_active=True)
    db_session.add(key)
    db_session.flush()
    fleet = Fleet(owner_user_id=owner.id, name="se2-fleet", fleet_api_key_hash="hash-se2")
    db_session.add(fleet)
    db_session.flush()
    member = FleetMember(
        fleet_id=fleet.id, host="h", profile="d", skills_dir="~", api_key_id=key.id, is_active=True
    )
    db_session.add(member)
    db_session.commit()

    recorded, _ = ingest_sync_report(db_session, member, {"skill_errors": {"not": "a list"}})
    assert recorded["skill_errors_ignored"] == 0


# ── 4. the migration refuses data and walks back ─────────────────────────────


def _load_migration():
    spec = importlib.util.spec_from_file_location("ser1116b_migration", MIGRATION)
    assert spec and spec.loader, f"missing migration file: {MIGRATION}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_exists_with_walkable_downgrade():
    mod = _load_migration()
    assert callable(getattr(mod, "upgrade", None))
    assert callable(getattr(mod, "downgrade", None)), "drop migration must be walkable"
    assert mod.revision == "ser1116b_retire_skill_err"
    # Single-head chain: this must sit on the head that main had at authoring.
    assert mod.down_revision == "paywall0925_hits"
    # Downgrade must rebuild exactly what act0701_pt_sync_report created.
    src = MIGRATION.read_text()
    for col in ('"slug"', '"semver"', '"signature"', '"summary"', '"feedback_status"', '"created_at"'):
        assert col in src, f"downgrade does not restore column {col}"
    for idx in (
        "ix_skill_error_reports_member_id",
        "ix_skill_error_reports_fleet_id",
        "ix_skill_error_reports_slug",
        "ix_skill_error_reports_created_at",
    ):
        assert idx in src, f"downgrade does not restore index {idx}"


def _sqlite_with_rows(rows: int):
    """A real SQLite DB holding skill_error_reports with `rows` rows.

    Real database, not a mocked bind: the guard's whole job is to interrogate
    whatever dialect it was handed, so a fake `.execute()` would pin the test to
    one call shape and pass even if the interrogation were wrong (which is exactly
    how the Postgres-only information_schema version slipped through).
    """
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE skill_error_reports (id TEXT PRIMARY KEY, signature TEXT)")
        )
        for i in range(rows):
            conn.execute(
                text("INSERT INTO skill_error_reports (id, signature) VALUES (:i, 's')"),
                {"i": str(i)},
            )
    return engine


def _bind_for(monkeypatch, engine):
    import alembic.op as _op

    monkeypatch.setattr(_op, "get_bind", lambda: engine.connect())


def test_migration_refuses_to_drop_a_non_empty_table(monkeypatch):
    """The safety hatch: data present => RuntimeError, not an irreversible DELETE."""
    mod = _load_migration()
    _bind_for(monkeypatch, _sqlite_with_rows(7))
    with pytest.raises(RuntimeError, match="REFUSING to drop skill_error_reports"):
        mod._assert_table_empty()


def test_migration_allows_drop_when_table_is_empty(monkeypatch):
    """The production case: table exists, 0 rows, drop proceeds."""
    mod = _load_migration()
    _bind_for(monkeypatch, _sqlite_with_rows(0))
    mod._assert_table_empty()  # must not raise


def test_migration_is_idempotent_when_table_absent(monkeypatch):
    """A DB that never ran Phase T (or a re-apply) must pass, not crash.

    Regression pin: the first version of this guard asked information_schema,
    which does not exist on SQLite and took four unrelated migration tests down
    with it by breaking `alembic upgrade head` on the SQLite harness.
    """
    mod = _load_migration()
    from sqlalchemy import create_engine

    _bind_for(monkeypatch, create_engine("sqlite:///:memory:"))
    mod._assert_table_empty()  # must not raise


def test_client_collector_still_has_no_skill_errors_producer():
    """The root cause, pinned.

    If someone ever adds a ``skill_errors`` key to the fleet client's payload,
    THIS test must fail loudly so the change is a deliberate re-design (with a
    table, readers and a triage path) rather than a payload field that silently
    goes nowhere.
    """
    src = (REPO_ROOT / "scripts" / "loopskill-collect-reports.py").read_text()
    assert '"skill_errors"' not in src, (
        "The fleet client now sends skill_errors but the server ignores it. "
        "Either re-design the rail end to end or delete the field from the client."
    )
