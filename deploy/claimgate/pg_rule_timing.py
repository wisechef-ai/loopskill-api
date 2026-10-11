"""Time claimgate.violations() per eval-corpus row and per rule in Postgres.

Installs install.sql + the current contract inside ONE transaction that is
always rolled back (nothing persists), then reports the slowest rows and,
for the slowest row, the cost of each rule pattern on its own. Use it when
a rule change slows the trigger (it runs on every Postiz queue / edit):

    DATABASE_URL=postgresql://... python deploy/claimgate/pg_rule_timing.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from app.services import claims_contract as cc  # noqa: E402

INSTALL_SQL = REPO / "deploy" / "claimgate" / "install.sql"


def _run_install(conn) -> None:
    """install.sql verbatim on the raw DBAPI cursor (no paramstyle on '%')."""
    conn.connection.dbapi_connection.cursor().execute(INSTALL_SQL.read_text())


def main() -> int:
    eng = create_engine(os.environ["DATABASE_URL"])
    rows = [
        json.loads(x)["text"]
        for x in (REPO / "tests/fixtures/claims_eval_corpus.jsonl").read_text().splitlines()
    ]
    with eng.connect() as conn:
        tx = conn.begin()
        try:
            _run_install(conn)
            c = cc.build_contract()
            conn.execute(text("DELETE FROM claimgate.rule"))
            conn.execute(text("DELETE FROM claimgate.tier"))
            for t in c["public_tiers"]:
                conn.execute(
                    text("INSERT INTO claimgate.tier (name, bundle_cap, key_cap) VALUES (:n, :b, :k)"),
                    {"n": t["display_name"], "b": t["bundle_limit"], "k": t["api_key_cap"]},
                )
            for r in c["retired_rules"]:
                conn.execute(
                    text("INSERT INTO claimgate.rule (id, kind, pg_pattern) VALUES (:i, 'retired', :p)"),
                    {"i": r["id"], "p": r["pg_pattern"]},
                )
            for r in c["amount_rules"]:
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
            for r in c["exempt_rules"]:
                conn.execute(
                    text(
                        "INSERT INTO claimgate.rule (id, kind, pg_pattern, veto) VALUES (:i, 'exempt', :p, :v)"
                    ),
                    {"i": r["id"], "p": r["pg_pattern"], "v": r["pg_veto"]},
                )
            conn.execute(
                text("UPDATE claimgate.rule SET exemptable = true WHERE id = ANY(:ids)"),
                {"ids": [r["id"] for r in c["retired_rules"] + c["amount_rules"] if r.get("exemptable")]},
            )
            conn.execute(text("SET LOCAL statement_timeout = '20s'"))
            timings = []
            for body in rows:
                t0 = time.perf_counter()
                try:
                    conn.execute(text("SAVEPOINT s"))
                    conn.execute(text("SELECT claimgate.violations(:b)"), {"b": body})
                    conn.execute(text("RELEASE SAVEPOINT s"))
                except OperationalError as e:  # statement_timeout
                    conn.execute(text("ROLLBACK TO SAVEPOINT s"))
                    print("TIMEOUT", repr(body[:100]), type(e).__name__)
                timings.append((time.perf_counter() - t0, body))
            timings.sort(reverse=True)
            print(f"rows {len(rows)}, total {sum(t for t, _ in timings):.1f}s")
            for t, body in timings[:6]:
                print(f"  {t:7.3f}s {body[:100]!r}")
            worst = timings[0][1]
            per = []
            for kind in ("retired_rules", "amount_rules", "exempt_rules"):
                for r in c[kind]:
                    t0 = time.perf_counter()
                    conn.execute(
                        text("SELECT regexp_instr(:b, :p, 1, 1, 0, 'i')"), {"b": worst, "p": r["pg_pattern"]}
                    )
                    per.append((time.perf_counter() - t0, r["id"]))
            per.sort(reverse=True)
            print("slowest rules on the slowest row:")
            for t, rid in per[:8]:
                print(f"  {t:7.3f}s {rid}")
        finally:
            tx.rollback()
    return 0


if __name__ == "__main__":
    sys.exit(main())
