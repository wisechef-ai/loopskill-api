"""MIN_RETENTION_DAYS — a destructive prune must refuse a nonsense window.

Found during adversarial review of the t_28f34a8a retention work (kanban
t_f2b5d11e), after that prune had just been wired to cron and pointed at prod.

The endpoint and the CLI both took an unbounded ``days``. ``retention_cutoff``
day-aligns the window but never validated its sign, so:

  * ``days=0``  -> cutoff = midnight tonight  -> deletes today's ingest too.
  * ``days=-1`` -> cutoff = midnight TOMORROW -> ``created_at < cutoff`` matches
    the whole table.

The rollup coverage guard is not a defence here, and that is the non-obvious
part: ``prune()`` step 1 rolls up every day that has raw rows *before* the guard
runs, so the guard's invariant ("only delete what has an aggregate behind it") is
satisfied for the entire table and it correctly permits the delete. A reviewer
reasoning "the guard will stop it" is wrong. Measured before the bound existed:
``POST /api/admin/sync-report-prune {"days": -1}`` returned 200 and deleted
12/12 seeded raw rows, including rows written minutes earlier.

Seeding is deliberately asymmetric (3 doomed vs 5 in-window) so a mutation that
deletes everything is distinguishable from one that deletes only the doomed set;
symmetric seeds make both read as the same number.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.sync_report import retention_cutoff
from app.services.sync_report_retention import MIN_RETENTION_DAYS, prune_raw
from app.models import LoopRun, LoopRunDailyRollup
from tests.test_sync_report_retention_cron import (
    _counts,
    _master_client,
    _seed,
    cron_cli,  # noqa: F401 — fixture imported for reuse
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RETENTION_SRC = REPO_ROOT / "app" / "services" / "sync_report_retention.py"

DOOMED = 3
IN_WINDOW = 5


def _seed_two_tiers(db):
    """``DOOMED`` rows past 30d and ``IN_WINDOW`` rows inside it.

    Counts differ on purpose: with 3 vs 5, "correct" deletes 3, "bound removed"
    deletes 8, and an inverted comparison deletes 5 — three distinguishable
    outcomes. Equal seeds make the first two indistinguishable.
    """
    now = datetime.now(UTC)
    _seed(db, age_days=60, loop_slug="doomed", n=DOOMED, at=now - timedelta(days=60))
    _seed(db, age_days=2, loop_slug="in-window", n=IN_WINDOW, at=now - timedelta(days=2))
    return now


def _rows(db, slug):
    return db.query(LoopRun).filter(LoopRun.loop_slug == slug).count()


# ── the bound itself ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [0, -1, -30, -10_000])
def test_retention_cutoff_refuses_non_positive_windows(bad):
    """The choke point. Every destructive path derives its cutoff from here."""
    with pytest.raises(ValueError, match=f">= {MIN_RETENTION_DAYS} days"):
        retention_cutoff(older_than_days=bad)


def test_retention_cutoff_accepts_the_minimum_and_default():
    """The gate must not be a no-op in the other direction either: 1 and 30 work."""
    assert retention_cutoff(older_than_days=1) is not None
    assert retention_cutoff(older_than_days=30) is not None
    assert retention_cutoff(older_than_days=MIN_RETENTION_DAYS) is not None


def test_prune_raw_cannot_be_reached_with_a_negative_window(db_session):
    """Direct mechanism call — must not be able to bypass the bound."""
    _seed_two_tiers(db_session)
    before = _counts(db_session)
    with pytest.raises(ValueError):
        prune_raw(db_session, older_than_days=-1)
    assert _counts(db_session) == before, "prune_raw deleted rows despite refusing the window"


# ── HTTP surface ─────────────────────────────────────────────────────────────


def test_http_negative_days_is_rejected_and_nothing_is_deleted(db_session):
    """THE finding, as a regression test."""
    client = _master_client(db_session)
    _seed_two_tiers(db_session)
    before = _counts(db_session)

    r = client.post("/api/admin/sync-report-prune", json={"days": -1})

    assert r.status_code == 422, f"expected 422 refusal, got {r.status_code}: {r.text}"
    assert "invalid_retention_window" in r.text
    assert _counts(db_session) == before, "a refused request still deleted raw telemetry"
    assert _rows(db_session, "in-window") == IN_WINDOW


def test_http_zero_days_is_rejected(db_session):
    client = _master_client(db_session)
    _seed_two_tiers(db_session)
    before = _counts(db_session)

    r = client.post("/api/admin/sync-report-prune", json={"days": 0})

    assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"
    assert _counts(db_session) == before


def test_http_valid_window_still_prunes_and_keeps_the_in_window_rows(db_session):
    """No regression: the legitimate 30d prune still works and still spares the
    in-window set — i.e. the bound did not break the happy path."""
    client = _master_client(db_session)
    _seed_two_tiers(db_session)

    r = client.post("/api/admin/sync-report-prune", json={"days": 30})

    assert r.status_code == 200, r.text
    assert _rows(db_session, "doomed") == 0, "30d prune stopped deleting the doomed set"
    assert _rows(db_session, "in-window") == IN_WINDOW, "30d prune breached the retained window"


# ── CLI surface (what cron invokes) ─────────────────────────────────────────


@pytest.mark.parametrize("bad", ["0", "-1", "-5"])
def test_cli_refuses_out_of_range_days_and_deletes_nothing(cron_cli, bad):  # noqa: F811
    from app.crons.sync_report_pruner import main

    _seed_two_tiers(cron_cli)
    before = _counts(cron_cli)

    rc = main(["--days", bad])

    assert rc == 2, f"--days {bad} must be REFUSED (exit 2), got {rc}"
    assert _counts(cron_cli) == before, f"--days {bad} deleted raw telemetry before refusing"
    # Refusal must precede the rollup too: a refused run writes nothing at all.
    assert cron_cli.query(LoopRunDailyRollup).count() == 0


def test_cli_dry_run_refuses_out_of_range_days(cron_cli):  # noqa: F811
    """--dry-run reads the same argument; it must refuse identically."""
    from app.crons.sync_report_pruner import main

    _seed_two_tiers(cron_cli)
    before = _counts(cron_cli)
    assert main(["--dry-run", "--days", "-1"]) == 2
    assert _counts(cron_cli) == before


def test_cli_default_window_still_prunes(cron_cli):  # noqa: F811
    from app.crons.sync_report_pruner import main

    _seed_two_tiers(cron_cli)
    assert main([]) == 0
    assert cron_cli.query(LoopRun).filter(LoopRun.loop_slug == "doomed").count() == 0
    assert cron_cli.query(LoopRun).filter(LoopRun.loop_slug == "in-window").count() == IN_WINDOW


# ── structural: the bound cannot be quietly relocated or dropped ─────────────


def _function_body_source(path: Path, func_name: str) -> str:
    """Return the decompiled body of ``func_name`` with docstrings removed, so a
    prose mention of the bound cannot satisfy this check while code can."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    body = body[1:]
            return ast.unparse(ast.Module(body=body, type_ignores=[]))
    raise AssertionError(f"{func_name} not found in {path}")


def test_bound_is_enforced_inside_retention_cutoff_not_in_a_caller():
    """Security-shaped assertions need a structural backstop: a route-level test
    can be deleted, this one pins WHERE the check lives. If someone moves it into
    the route "for cleanliness", the CLI and any future caller silently lose it."""
    body = _function_body_source(RETENTION_SRC, "retention_cutoff")
    assert "MIN_RETENTION_DAYS" in body, "bound no longer enforced inside retention_cutoff()"
    assert "older_than_days" in body
    assert "ValueError" in body, "retention_cutoff no longer raises on a bad window"
