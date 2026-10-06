"""claimgate_1006 — Python check == Postgres trigger, on the same corpus.

The Postiz publish trigger (deploy/claimgate/install.sql) runs the contract's
retired and amount rules (``pg_pattern``) on ``claimgate.normalize(content)``.
If the two engines ever disagree, a claim the producers' API check rejects
could still publish (or vice versa). This test installs the real install.sql
into the CI Postgres, loads the live contract the way claimgate_sync.py does,
and asserts identical rule-id verdicts for every fixture in
tests/test_claims_contract.py plus encoding edge cases.

Postgres only (the SQLite leg skips): it is the engine the trigger runs on.
Every check, including the nearest-tier binding, runs in both engines;
there is no Python-only rule left for "post now" to slip past. Agreement
alone is not enough (both engines could share a wrong verdict), so the
fixtures in tests/test_claims_contract.py also pin the EXPECTED verdict, and
test_expected_verdicts_in_postgres below re-asserts them on the trigger side.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from app.services import claims_contract as cc
from tests.test_claims_contract import (
    CLEAN_CORPUS,
    MALFORMED_RUNS,
    REAL_INCIDENTS,
    REVIEW_BYPASSES,
    REVIEW_MUST_PASS,
)

INSTALL_SQL = Path(__file__).resolve().parent.parent / "deploy" / "claimgate" / "install.sql"


def _run_install(conn) -> None:
    """Execute install.sql verbatim on the raw DBAPI cursor (no paramstyle
    interpolation of the "%" its LIKE / RAISE statements contain), inside the
    test's own transaction."""
    conn.connection.dbapi_connection.cursor().execute(INSTALL_SQL.read_text())


# Every invisible / space-like character the normalisation maps, in context.
CHAR_CASES = [f"Pro{c}+ and Pro{c}$199/month" for c in cc.ZERO_WIDTH + cc.SPACE_LIKE] + [
    f"{c}Pro{c}" for c in cc.ZERO_WIDTH + cc.SPACE_LIKE
]

EDGE_CASES = [
    # round-2 review inputs
    "Pro costs 199 USD/month",
    "Pro&#43 plan",
    "Pro&Tab;$199/month",
    "Pro&#38;plus; x",
    "P<b>ro</b> includes 2 private bundles",
    "Pro&amp;plus;",
    "Pro &#x1F600; &#0; &#xD800; &#1114112; &unknown; &copy;",
    "Free gives you 2. Pro includes 2 private bundles. Free has 1 API key.",
    "Pro at $9.95/month includes 20 private bundles",
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
        + CHAR_CASES
    )


@pytest.fixture
def pg(db_session):
    if db_session.bind.dialect.name != "postgresql":
        pytest.skip("trigger parity runs on the postgres CI leg only")
    conn = db_session.connection()
    # install.sql writes to fixed names (schema claimgate, public."Post"), not
    # the per-worker xdist schema. Everything below lives in db_session's outer
    # transaction, which always rolls back, and this transaction-scoped lock
    # serialises the claimgate tests across workers under ANY --dist mode.
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('claimgate-tests'))"))
    _run_install(conn)
    contract = cc.build_contract()
    conn.execute(text("DELETE FROM claimgate.rule"))
    conn.execute(text("DELETE FROM claimgate.tier"))
    for t in contract["public_tiers"]:
        conn.execute(
            text("INSERT INTO claimgate.tier (name, bundle_cap, key_cap) VALUES (:n, :b, :k)"),
            {"n": t["display_name"], "b": t["bundle_limit"], "k": t["api_key_cap"]},
        )
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
    for r in contract["exempt_rules"]:
        conn.execute(
            text("INSERT INTO claimgate.rule (id, kind, pg_pattern, veto) VALUES (:i, 'exempt', :p, :v)"),
            {"i": r["id"], "p": r["pg_pattern"], "v": r["pg_veto"]},
        )
    conn.execute(
        text("UPDATE claimgate.rule SET exemptable = true WHERE id = ANY(:ids)"),
        {"ids": [r["id"] for r in contract["amount_rules"] if r.get("exemptable")]},
    )
    yield conn
    conn.exec_driver_sql("DROP SCHEMA IF EXISTS claimgate CASCADE")


def _sql_ids(conn, body: str) -> set[str]:
    v = conn.execute(text("SELECT claimgate.violations(:b)"), {"b": body}).scalar()
    return {h.split(":")[0] for h in (v or "").split("; ") if h}


def _py_ids(body: str) -> set[str]:
    return {v["rule_id"] for v in cc.check_text(body)}


@pytest.mark.parametrize("body", _corpus())
def test_python_and_postgres_agree(pg, body: str) -> None:
    assert _sql_ids(pg, body) == _py_ids(body)


def test_normalize_agrees(pg) -> None:
    for body in _corpus():
        assert pg.execute(text("SELECT claimgate.normalize(:b)"), {"b": body}).scalar() == cc.normalize(
            body
        ), body


@pytest.mark.parametrize(("body", "expected"), REAL_INCIDENTS + REVIEW_BYPASSES)
def test_expected_verdicts_in_postgres(pg, body: str, expected: set[str]) -> None:
    missing = expected - _sql_ids(pg, body)
    assert not missing, f"trigger missed {missing}"


@pytest.mark.parametrize("body", list(CLEAN_CORPUS) + list(REVIEW_MUST_PASS))
def test_clean_copy_passes_in_postgres(pg, body: str) -> None:
    assert _sql_ids(pg, body) == set()


def test_named_entity_table_matches_python(pg) -> None:
    for name, char in cc.NAMED_ENTITIES.items():
        got = pg.execute(text("SELECT claimgate.decode_entities(:b)"), {"b": f"x&{name};y"}).scalar()
        assert got == f"x{char}y", name


# ── the trigger itself, on a minimal Post table ──────────────────────────────


@pytest.fixture
def post_table(pg):
    pg.exec_driver_sql(
        'CREATE TABLE public."Post" (id text PRIMARY KEY, state text NOT NULL, "deletedAt" timestamptz, '
        'content text, "publishDate" timestamptz, "integrationId" text)'
    )
    _run_install(pg)  # now installs the trigger too
    yield pg
    pg.exec_driver_sql('DROP TABLE IF EXISTS public."Post" CASCADE')


def _queue(conn, pid: str, content: str) -> tuple[bool, str | None]:
    conn.execute(
        text("INSERT INTO public.\"Post\" (id, state, content) VALUES (:i, 'QUEUE', :c)"),
        {"i": pid, "c": content},
    )
    deleted = conn.execute(
        text('SELECT "deletedAt" IS NOT NULL FROM public."Post" WHERE id = :i'), {"i": pid}
    ).scalar()
    log = conn.execute(
        text("SELECT violations FROM claimgate.state_log WHERE post_id = :i AND quarantined"), {"i": pid}
    ).scalar()
    return deleted, log


def test_trigger_quarantines_retired_claim(post_table) -> None:
    deleted, log = _queue(post_table, "p1", REAL_INCIDENTS[0][0])
    assert deleted and "tier-not-public-pro_plus" in log


def test_trigger_passes_clean_copy(post_table) -> None:
    assert _queue(post_table, "p2", CLEAN_CORPUS[0]) == (False, None)


def test_trigger_enforces_tier_binding(post_table) -> None:
    deleted, log = _queue(post_table, "p3", "Pro includes 2 private bundles")
    assert deleted and "tier-bundle-cap" in log


def test_trigger_rechecks_content_edit_and_requeue(post_table) -> None:
    _queue(post_table, "p4", CLEAN_CORPUS[0])
    post_table.execute(text("UPDATE public.\"Post\" SET content = 'Pro is $20/mo' WHERE id = 'p4'"))
    assert post_table.execute(
        text('SELECT "deletedAt" IS NOT NULL FROM public."Post" WHERE id = \'p4\'')
    ).scalar()


def test_trigger_honours_override(post_table) -> None:
    post_table.execute(
        text("INSERT INTO claimgate.override VALUES ('p5', 'test', 'confirmed false positive')")
    )
    assert _queue(post_table, "p5", "Pro+ for agencies") == (False, None)


def test_trigger_fails_closed_on_broken_rule(post_table) -> None:
    post_table.execute(text("UPDATE claimgate.rule SET pg_pattern = '(' WHERE id = 'brand-recipes-domain'"))
    deleted, log = _queue(post_table, "p6", CLEAN_CORPUS[0])
    assert deleted and log.startswith("claimgate-error")


def test_trigger_fails_closed_on_missing_capture(post_table) -> None:
    post_table.execute(text("UPDATE claimgate.rule SET amount_group = 999 WHERE id = 'price-tier-pro'"))
    deleted, log = _queue(post_table, "p7", "Pro $199/month")
    assert deleted and log.startswith("claimgate-error")


def test_trigger_fails_closed_when_contract_not_loaded(post_table) -> None:
    post_table.execute(text("DELETE FROM claimgate.rule"))
    deleted, log = _queue(post_table, "p8", CLEAN_CORPUS[0])
    assert deleted and "contract not loaded" in log


def test_trigger_fails_closed_when_tier_table_empty(post_table) -> None:
    post_table.execute(text("DELETE FROM claimgate.tier"))
    deleted, log = _queue(post_table, "p10", "Pro includes 2 private bundles")
    assert deleted and "tier table empty" in log


def test_trigger_gates_queue_to_draft_content_change(post_table) -> None:
    """Round 5: queued clean, then flipped to DRAFT with new copy. The sleeping
    publish workflow would still post it, so the trigger must quarantine."""
    _queue(post_table, "p11", CLEAN_CORPUS[0])
    post_table.execute(
        text("UPDATE public.\"Post\" SET state = 'DRAFT', content = 'Pro+ for agencies' WHERE id = 'p11'")
    )
    assert post_table.execute(
        text('SELECT "deletedAt" IS NOT NULL FROM public."Post" WHERE id = \'p11\'')
    ).scalar()


def test_trigger_gates_draft_edit_of_ever_queued_post(post_table) -> None:
    _queue(post_table, "p12", CLEAN_CORPUS[0])
    post_table.execute(text("UPDATE public.\"Post\" SET state = 'DRAFT' WHERE id = 'p12'"))
    post_table.execute(text("UPDATE public.\"Post\" SET content = 'Pro is $20/mo' WHERE id = 'p12'"))
    assert post_table.execute(
        text('SELECT "deletedAt" IS NOT NULL FROM public."Post" WHERE id = \'p12\'')
    ).scalar()


def test_trigger_blocks_undelete_of_quarantined_post_as_draft(post_table) -> None:
    deleted, _ = _queue(post_table, "p13", "Pro+ for agencies")
    assert deleted
    post_table.execute(
        text("UPDATE public.\"Post\" SET state = 'DRAFT', \"deletedAt\" = NULL WHERE id = 'p13'")
    )
    assert post_table.execute(
        text('SELECT "deletedAt" IS NOT NULL FROM public."Post" WHERE id = \'p13\'')
    ).scalar()


_POST_DDL = (
    'CREATE TABLE public."Post" (id text PRIMARY KEY, state text NOT NULL, "deletedAt" timestamptz, '
    'content text, "publishDate" timestamptz, "integrationId" text)'
)


def test_pre_install_queued_post_is_gated(pg) -> None:
    """Round 6: a row queued BEFORE the trigger existed, then DRAFTed, then
    edited, must still be gated (install snapshot + old_state history)."""
    pg.exec_driver_sql(_POST_DDL)
    pg.execute(
        text("INSERT INTO public.\"Post\" (id, state, content) VALUES ('p20', 'QUEUE', :c)"),
        {"c": CLEAN_CORPUS[0]},
    )
    _run_install(pg)  # installs the trigger + snapshots p20
    pg.execute(text("UPDATE public.\"Post\" SET state = 'DRAFT' WHERE id = 'p20'"))
    pg.execute(text("UPDATE public.\"Post\" SET content = 'Pro+ for agencies' WHERE id = 'p20'"))
    assert pg.execute(text('SELECT "deletedAt" IS NOT NULL FROM public."Post" WHERE id = \'p20\'')).scalar()


def test_old_state_history_alone_gates(pg) -> None:
    """Without the snapshot row, the QUEUE->DRAFT transition logged as
    old_state = QUEUE is enough history."""
    pg.exec_driver_sql(_POST_DDL)
    pg.execute(
        text("INSERT INTO public.\"Post\" (id, state, content) VALUES ('p21', 'QUEUE', :c)"),
        {"c": CLEAN_CORPUS[0]},
    )
    _run_install(pg)
    pg.execute(text("DELETE FROM claimgate.state_log WHERE post_id = 'p21' AND op = 'SNAPSHOT'"))
    pg.execute(text("UPDATE public.\"Post\" SET state = 'DRAFT' WHERE id = 'p21'"))
    pg.execute(text("UPDATE public.\"Post\" SET content = 'Pro is $20/mo' WHERE id = 'p21'"))
    assert pg.execute(text('SELECT "deletedAt" IS NOT NULL FROM public."Post" WHERE id = \'p21\'')).scalar()


def test_exempt_rule_without_veto_fails_closed(pg) -> None:
    pg.execute(text("UPDATE claimgate.rule SET veto = NULL WHERE kind = 'exempt'"))
    with pytest.raises(Exception), pg.begin_nested():
        pg.execute(text("SELECT claimgate.violations('WiseChef runs it from $199/month')")).scalar()


def test_trigger_quarantines_misattributed_other_product_price(post_table) -> None:
    deleted, log = _queue(post_table, "p15", "LoopSkill costs, unlike WiseChef, $199/month.")
    assert deleted and "price-recurring" in log
    assert _queue(
        post_table, "p16", "LoopSkill is free to self-host. WiseChef runs it for you from $199/month."
    ) == (
        False,
        None,
    )


@pytest.mark.parametrize(("body", "expected"), MALFORMED_RUNS)
def test_trigger_quarantines_malformed_runs(post_table, body: str, expected: set[str]) -> None:
    pid = "m" + str(abs(hash(body)) % 10**9)
    deleted, log = _queue(post_table, pid, body)
    assert deleted, body
    assert expected <= {h.split(":")[0] for h in log.split("; ")}, log


def test_never_queued_draft_edits_are_not_gated(post_table) -> None:
    post_table.execute(
        text("INSERT INTO public.\"Post\" (id, state, content) VALUES ('p14', 'DRAFT', 'clean')")
    )
    post_table.execute(
        text("UPDATE public.\"Post\" SET content = 'Pro+ idea, fix before queueing' WHERE id = 'p14'")
    )
    assert not post_table.execute(
        text('SELECT "deletedAt" IS NOT NULL FROM public."Post" WHERE id = \'p14\'')
    ).scalar()


def test_drafts_are_not_gated_and_transitions_are_logged(post_table) -> None:
    post_table.execute(
        text("INSERT INTO public.\"Post\" (id, state, content) VALUES ('p9', 'DRAFT', 'Pro+ draft')")
    )
    assert not post_table.execute(
        text('SELECT "deletedAt" IS NOT NULL FROM public."Post" WHERE id = \'p9\'')
    ).scalar()
    post_table.execute(text("UPDATE public.\"Post\" SET state = 'QUEUE' WHERE id = 'p9'"))
    rows = post_table.execute(
        text(
            "SELECT op, old_state, new_state, quarantined FROM claimgate.state_log WHERE post_id = 'p9' ORDER BY id"
        )
    ).all()
    assert rows[0][:3] == ("INSERT", None, "DRAFT")
    assert rows[-1] == ("UPDATE", "DRAFT", "QUEUE", True)
