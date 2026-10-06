#!/usr/bin/env python3
"""claimgate sync + sweep — runs on wisechef-hq every 10 minutes (crontab).

Source of truth: loopskill-api deploy/claimgate/ (this file + install.sql).
Deploy: copy both to wisechef-hq:~/services/claimgate/; the next run
re-installs install.sql automatically when its hash changes.

Keeps the Postiz-side claims gate (install.sql) fed and honest:

1. SELF-HEAL   trigger missing or disabled (e.g. a Postiz upgrade recreated the
               table) -> re-run install.sql and record it.
2. SYNC        GET /api/marketing/claims (LoopSkill, derived from tiers.yaml)
               -> claimgate.rule (retired + amount rules) in ONE transaction.
               Every pattern is executed by Postgres inside that transaction,
               so a pattern the trigger could not run aborts the sync and the
               previous rules stay in force. Fetch failure = keep old rules.
3. SWEEP       every post in QUEUE (and every DRAFT that was ever queued: its
               publish workflow may still be sleeping) is re-checked with BOTH engines:
               the SQL rules (catches posts queued before a rule existed) and
               POST /api/marketing/claims/check (belt and braces: both engines
               implement the same rules, including tier binding). A violation quarantines the post the only
               way Postiz respects: deletedAt = now(). Restore = clear it, or
               add a claimgate.override row first.

Alerting is NOT done here: Tori's `claimgate-watch` cron reads
claimgate.state_log / claimgate.meta over ssh and posts to #tori. This host
enforces; the supervisor observes. Enforcement never depends on Tori being up.

stdlib only. Exit 0 always (a watchdog that crashes is a watchdog that stops).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTALL_SQL = HERE / "install.sql"
CONTRACT_URLS = (
    "http://127.0.0.1:3370/api/marketing/claims",
    "https://app.loopskill.io/api/marketing/claims",
)
CHECK_URLS = tuple(u + "/check" for u in CONTRACT_URLS)
CONTAINER = "postiz-postgres"
UA = {"User-Agent": "claimgate/1.0 (wisechef-hq)", "Content-Type": "application/json"}


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def _env(name: str) -> str:
    return subprocess.run(
        ["docker", "exec", CONTAINER, "printenv", name], capture_output=True, text=True, check=True
    ).stdout.strip()


_DB: tuple[str, str] | None = None


def psql(sql: str, *, check: bool = True) -> str:
    global _DB
    if _DB is None:
        _DB = (_env("POSTGRES_USER"), _env("POSTGRES_DB"))
    r = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            CONTAINER,
            "psql",
            "-U",
            _DB[0],
            "-d",
            _DB[1],
            "-v",
            "ON_ERROR_STOP=1",
            "-At",
            "-q",
        ],
        input=sql,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if check and r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:500])
    return r.stdout


def lit(s) -> str:
    """Dollar-quoted SQL literal (patterns contain backslashes and quotes)."""
    s = "" if s is None else str(s)
    tag = "cg"
    while f"${tag}$" in s:
        tag += "x"
    return f"${tag}${s}${tag}$"


def http_json(url: str, body: dict | None = None, timeout: int = 20) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=UA, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def first_ok(urls, body=None) -> dict:
    last = None
    for u in urls:
        try:
            return http_json(u, body)
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"all endpoints failed: {last}")


def meta(k: str, v: str) -> None:
    psql(
        f"INSERT INTO claimgate.meta (k, v) VALUES ({lit(k)}, {lit(v)}) "
        "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, at = now();"
    )


def self_heal() -> None:
    state = psql(
        "SELECT tgenabled FROM pg_trigger WHERE tgname = 'claimgate_guard' AND NOT tgisinternal;"
    ).strip()
    sql_hash = __import__("hashlib").sha256(INSTALL_SQL.read_bytes()).hexdigest()[:16]
    installed = psql("SELECT v FROM claimgate.meta WHERE k = 'install_sql_hash';", check=False).strip()
    if state == "O" and installed == sql_hash:
        return
    log(
        f"trigger state={state or 'MISSING'}, install.sql {installed or 'none'} -> {sql_hash}: (re)installing"
    )
    psql(INSTALL_SQL.read_text())
    meta("install_sql_hash", sql_hash)
    meta("reinstalled_at", datetime.now(timezone.utc).isoformat())


def load_contract() -> dict:
    """Live contract, or CLAIMGATE_CONTRACT_FILE for bootstrap/offline runs
    (e.g. before the API that serves contract v2 is deployed)."""
    path = os.environ.get("CLAIMGATE_CONTRACT_FILE")
    if path:
        return json.loads(Path(path).read_text())
    return first_ok(CONTRACT_URLS)


def sync() -> dict:
    c = load_contract()
    retired, amounts = c.get("retired_rules") or [], c.get("amount_rules") or []
    if not retired or not amounts:
        raise RuntimeError("contract looks empty or pre-v2; refusing to replace rules")
    tiers = c.get("public_tiers") or []
    if not tiers:
        raise RuntimeError("contract has no public tiers; refusing to sync")
    stmts = ["BEGIN;", "DELETE FROM claimgate.rule;", "DELETE FROM claimgate.tier;"]
    for t in tiers:
        b = "NULL" if t.get("bundle_limit") is None else int(t["bundle_limit"])
        k = "NULL" if t.get("api_key_cap") is None else int(t["api_key_cap"])
        stmts.append(
            f"INSERT INTO claimgate.tier (name, bundle_cap, key_cap) VALUES ({lit(t['display_name'])}, {b}, {k});"
        )
    for r in retired:
        stmts.append(
            "INSERT INTO claimgate.rule (id, kind, pg_pattern, reason) VALUES "
            f"({lit(r['id'])}, 'retired', {lit(r['pg_pattern'])}, {lit(r.get('reason'))});"
        )
    for r in amounts:
        allowed = "ARRAY[" + ",".join(f"{float(a):.2f}" for a in r["allowed"]) + "]::numeric[]"
        stmts.append(
            "INSERT INTO claimgate.rule (id, kind, pg_pattern, amount_group, allowed, reason) VALUES "
            f"({lit(r['id'])}, 'amount', {lit(r['pg_pattern'])}, {int(r['amount_group'])}, {allowed}, {lit(r.get('reason'))});"
        )
    for r in c.get("exempt_rules") or []:
        stmts.append(
            f"INSERT INTO claimgate.rule (id, kind, pg_pattern, veto, reason) VALUES "
            f"({lit(r['id'])}, 'exempt', {lit(r['pg_pattern'])}, {lit(r['pg_veto'])}, {lit(r.get('reason'))});"
        )
    flagged = [r["id"] for r in amounts if r.get("exemptable")]
    if flagged:
        stmts.append(
            "UPDATE claimgate.rule SET exemptable = true WHERE id IN ("
            + ", ".join(lit(i) for i in flagged)
            + ");"
        )
    # Execute every pattern once inside the transaction: an invalid ARE aborts
    # the whole sync and the previous rules stay live.
    stmts.append("SELECT count(*) FROM claimgate.rule WHERE 'probe' ~* pg_pattern;")
    stmts.append("SELECT claimgate.violations('probe Pro is $1/mo, Pro+');")
    stmts += [
        "INSERT INTO claimgate.meta (k, v) VALUES ('contract_hash', " + lit(c.get("contract_hash")) + ") "
        "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, at = now();",
        "INSERT INTO claimgate.meta (k, v) VALUES ('last_sync', now()::text) "
        "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, at = now();",
        "COMMIT;",
    ]
    psql("\n".join(stmts))
    return c


def sweep() -> int:
    rows = psql(
        "SELECT coalesce(json_agg(json_build_object('id', p.id, 'content', p.content, "
        "'sqlv', claimgate.violations(p.content))), '[]') FROM \"Post\" p "
        "WHERE p.\"deletedAt\" IS NULL AND (p.state = 'QUEUE' OR (p.state = 'DRAFT' AND EXISTS "
        "(SELECT 1 FROM claimgate.state_log l WHERE l.post_id = p.id AND (l.new_state = 'QUEUE' OR l.old_state = 'QUEUE')))) "
        "AND NOT EXISTS (SELECT 1 FROM claimgate.override o WHERE o.post_id = p.id);"
    ).strip()
    quarantined = 0
    for post in json.loads(rows or "[]"):
        reasons = []
        if post.get("sqlv"):
            reasons.append(post["sqlv"])
        try:
            res = first_ok(CHECK_URLS, {"text": (post["content"] or "")[:20000]})
            reasons += [v["rule_id"] + ":" + v["match"] for v in res.get("violations") or []]
        except Exception as e:  # noqa: BLE001 — API down: the SQL verdict still stands
            log(f"check API unavailable for {post['id']}: {e}")
        if not reasons:
            continue
        why = "; ".join(dict.fromkeys(reasons))
        psql(
            "BEGIN;"
            f'UPDATE "Post" SET "deletedAt" = now() WHERE id = {lit(post["id"])} AND "deletedAt" IS NULL;'
            "INSERT INTO claimgate.state_log (post_id, op, new_state, quarantined, violations) "
            f"VALUES ({lit(post['id'])}, 'SWEEP', 'QUEUE', true, {lit(why)});"
            "COMMIT;"
        )
        quarantined += 1
        log(f"QUARANTINED {post['id']}: {why}")
    return quarantined


def main() -> int:
    try:
        self_heal()
    except Exception as e:  # noqa: BLE001
        log(f"self-heal failed: {e}")
    try:
        c = sync()
        log(
            f"synced contract {c.get('contract_hash')}: {len(c['retired_rules'])} retired, "
            f"{len(c['amount_rules'])} amount rules"
        )
    except Exception as e:  # noqa: BLE001 — old rules keep enforcing
        log(f"sync failed (previous rules stay live): {e}")
        try:
            meta("last_sync_error", f"{datetime.now(timezone.utc).isoformat()} {e}"[:500])
        except Exception:  # noqa: BLE001
            pass
    try:
        n = sweep()
        if n:
            log(f"sweep quarantined {n} queued post(s)")
    except Exception as e:  # noqa: BLE001
        log(f"sweep failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
