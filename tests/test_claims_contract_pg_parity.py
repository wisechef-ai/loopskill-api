"""claimgate_1006 — Python check == Postgres trigger, on the same corpus.

The Postiz publish trigger (deploy/claimgate/install.sql) runs the contract's
retired and amount rules (``pg_pattern``) on ``claimgate.normalize(content)``.
If the two engines ever disagree, a claim the producers' API check rejects
could still publish (or vice versa). This test installs the real install.sql
into the CI Postgres, loads the live contract the way claimgate_sync.py does,
and asserts identical rule-id verdicts for every fixture in
tests/test_claims_contract.py plus encoding edge cases.

Postgres only (the SQLite leg skips): it is the engine the trigger runs on.
The Python-only tier-binding check (kind == "tier-number") is excluded by
design; the trigger enforces its coarse form via the count-* amount rules.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from app.services import claims_contract as cc
from tests.test_claims_contract import CLEAN_CORPUS, REAL_INCIDENTS, REVIEW_BYPASSES, REVIEW_MUST_PASS

INSTALL_SQL = Path(__file__).resolve().parent.parent / "deploy" / "claimgate" / "install.sql"

EDGE_CASES = [
    "Pro\u00a0$20/mo",
    "Pro&nbsp;$20/mo",
    "Pro&#43; plan",
    "Pro&#x2B; plan",
    "&amp;#43; is not a plus",
    "<p>Pro</p><p>+</p>",
    "Pro   +",
    "price: €9,95 per month",
    "éPro+ accented prefix",
    "$２０/mo fullwidth digits",
    "Pro\tis\t$20/mo",
    "",
]


def _corpus() -> list[str]:
    return (
        [t for t, _ in REAL_INCIDENTS]
        + [t for t, _ in REVIEW_BYPASSES]
        + list(CLEAN_CORPUS)
        + list(REVIEW_MUST_PASS)
        + EDGE_CASES
    )


@pytest.fixture
def pg(db_session):
    if db_session.bind.dialect.name != "postgresql":
        pytest.skip("trigger parity runs on the postgres CI leg only")
    conn = db_session.connection()
    conn.exec_driver_sql(INSTALL_SQL.read_text())
    contract = cc.build_contract()
    conn.execute(text("DELETE FROM claimgate.rule"))
    for r in contract["retired_rules"]:
        conn.execute(
            text("INSERT INTO claimgate.rule (id, kind, pg_pattern) VALUES (:i, 'retired', :p)"),
            {"i": r["id"], "p": r["pg_pattern"]},
        )
    for r in contract["amount_rules"]:
        conn.execute(
            text(
                "INSERT INTO claimgate.rule (id, kind, pg_pattern, amount_group, allowed) "
                "VALUES (:i, 'amount', :p, :g, CAST(:a AS numeric[]))"
            ),
            {
                "i": r["id"],
                "p": r["pg_pattern"],
                "g": r["amount_group"],
                "a": "{" + ",".join(str(x) for x in r["allowed"]) + "}",
            },
        )
    yield conn
    conn.exec_driver_sql("DROP SCHEMA IF EXISTS claimgate CASCADE")


def _sql_ids(conn, body: str) -> set[str]:
    v = conn.execute(text("SELECT claimgate.violations(:b)"), {"b": body}).scalar()
    return {h.split(":")[0] for h in (v or "").split("; ") if h}


def _py_ids(body: str) -> set[str]:
    return {v["rule_id"] for v in cc.check_text(body) if v["kind"] != "tier-number"}


@pytest.mark.parametrize("body", _corpus())
def test_python_and_postgres_agree(pg, body: str) -> None:
    assert _sql_ids(pg, body) == _py_ids(body)


def test_normalize_agrees(pg) -> None:
    for body in _corpus():
        assert pg.execute(text("SELECT claimgate.normalize(:b)"), {"b": body}).scalar() == cc.normalize(
            body
        ), body


def test_broken_rule_fails_closed(pg) -> None:
    """A rule Postgres cannot execute must raise inside violations(); the
    trigger turns that into a quarantine (install.sql: fail CLOSED)."""
    pg.execute(text("UPDATE claimgate.rule SET pg_pattern = '(' WHERE kind = 'retired'"))
    with pytest.raises(Exception):
        with pg.begin_nested():
            pg.execute(text("SELECT claimgate.violations('anything')")).scalar()
