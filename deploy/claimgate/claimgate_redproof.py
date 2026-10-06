#!/usr/bin/env python3
"""RED-proof for the claimgate trigger. Everything runs inside ONE transaction
that is ROLLED BACK: no row in the Postiz database is changed.

Cases (each must hold, or exit 1):
  A  a DRAFT with retired claims -> set QUEUE -> trigger quarantines it
     (deletedAt set, state_log row quarantined=true with rule ids)
  B  a clean DRAFT -> set QUEUE -> NOT quarantined, transition logged
  C  a clean DRAFT -> content edited to an off-ladder price while QUEUE ->
     quarantined (content edits re-check, not just state flips)
  D  override row present -> retired content allowed through
  E  fail-closed: a broken rule makes entering QUEUE quarantine, not pass
Usage: claimgate_redproof.py <contract.json>   (curl -s .../api/marketing/claims > c.json)
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, __import__("os").path.dirname(__file__))
from claimgate_sync import lit, psql  # noqa: E402

contract = json.load(open(sys.argv[1]))

load = ["DELETE FROM claimgate.rule;"]
for r in contract["retired_rules"]:
    load.append(
        f"INSERT INTO claimgate.rule (id, kind, pg_pattern) VALUES ({lit(r['id'])}, 'retired', {lit(r['pg_pattern'])});"
    )
for r in contract["amount_rules"]:
    allowed = "ARRAY[" + ",".join(f"{float(a):.2f}" for a in r["allowed"]) + "]::numeric[]"
    load.append(
        "INSERT INTO claimgate.rule (id, kind, pg_pattern, amount_group, allowed) VALUES "
        f"({lit(r['id'])}, 'amount', {lit(r['pg_pattern'])}, {int(r['amount_group'])}, {allowed});"
    )

for r in contract.get("exempt_rules") or []:
    load.append(
        "INSERT INTO claimgate.rule (id, kind, pg_pattern, veto) VALUES "
        f"({lit(r['id'])}, 'exempt', {lit(r['pg_pattern'])}, {lit(r['pg_veto'])});"
    )
_flagged = [r["id"] for r in contract["amount_rules"] if r.get("exemptable")]
if _flagged:
    load.append(
        "UPDATE claimgate.rule SET exemptable = true WHERE id IN ("
        + ", ".join(lit(i) for i in _flagged)
        + ");"
    )

# Pick test rows: a DRAFT that violates, and DRAFTs that are clean.
pick = psql(
    "SELECT json_build_object("
    "'bad', (SELECT id FROM \"Post\" WHERE state='DRAFT' AND content ILIKE '%pro+%' ORDER BY \"createdAt\" DESC LIMIT 1),"
    "'clean', (SELECT json_agg(id) FROM (SELECT id FROM \"Post\" WHERE state='DRAFT' "
    'AND id NOT IN (SELECT id FROM "Post" WHERE content ILIKE \'%pro+%\') ORDER BY "createdAt" DESC LIMIT 3) s));'
).strip()
ids = json.loads(pick)
bad, (c1, c2, c3) = ids["bad"], ids["clean"][:3]
print("test rows:", bad, c1, c2, c3)
ALL = ",".join(lit(x) for x in (bad, c1, c2, c3))
snap_sql = f"SELECT md5(string_agg(id || state::text || coalesce(\"deletedAt\"::text,'') || md5(content), ',' ORDER BY id)) FROM \"Post\" WHERE id IN ({ALL});"
before = psql(snap_sql).strip()

q = lambda pid: (  # noqa: E731
    f'SELECT \'{pid}\', (SELECT "deletedAt" IS NOT NULL FROM "Post" WHERE id={lit(pid)}), '
    f"(SELECT string_agg(coalesce(violations,'-') || '|' || quarantined, ',') FROM claimgate.state_log WHERE post_id={lit(pid)} AND at > now() - interval '1 minute');"
)

CLEAN = "<p>Install the loopskill skill from app.loopskill.io/skill. Pro $9.95/month.</p>"
sql = (
    ["BEGIN;"]
    + load
    + [
        f'UPDATE "Post" SET "deletedAt"=NULL WHERE id IN ({ALL});',
        f'UPDATE "Post" SET content={lit(CLEAN)} WHERE id IN ({lit(c1)},{lit(c2)},{lit(c3)});',
        # A
        f"UPDATE \"Post\" SET state='QUEUE' WHERE id={lit(bad)};",
        "SELECT 'A';",
        q(bad),
        # B
        f"UPDATE \"Post\" SET state='QUEUE' WHERE id={lit(c1)};",
        "SELECT 'B';",
        q(c1),
        # C
        f"UPDATE \"Post\" SET state='QUEUE' WHERE id={lit(c2)};",
        f"UPDATE \"Post\" SET content = content || ' Pro is $20/mo.' WHERE id={lit(c2)};",
        "SELECT 'C';",
        q(c2),
        # D
        f"INSERT INTO claimgate.override VALUES ({lit(c3)}, 'redproof', 'test');",
        f"UPDATE \"Post\" SET content = content || ' Pro+ gets you 20 cookbooks.', state='QUEUE' WHERE id={lit(c3)};",
        "SELECT 'D';",
        q(c3),
        # E: break a rule, then a clean row entering QUEUE must be quarantined (fail closed)
        "UPDATE claimgate.rule SET pg_pattern = '(' WHERE kind='retired' AND id = (SELECT min(id) FROM claimgate.rule WHERE kind='retired');",
        f'UPDATE "Post" SET state=\'DRAFT\', "deletedAt"=NULL WHERE id={lit(c1)};',
        f"UPDATE \"Post\" SET state='QUEUE' WHERE id={lit(c1)};",
        "SELECT 'E';",
        q(c1),
        "ROLLBACK;",
    ]
)
out = psql("\n".join(sql))
print(out)
lines = [l for l in out.splitlines() if l.strip()]
res = {}
cur = None
for l in lines:
    if l in "ABCDE" and len(l) == 1:
        cur = l
    elif cur:
        res[cur] = l
ok = (
    res.get("A", "").split("|")[1] == "t"
    and "tier-not-public" in res.get("A", "")
    and res.get("B", "").split("|")[1] == "f"
    and res.get("C", "").split("|")[1] == "t"
    and "price-recurring" in res.get("C", "")
    and res.get("D", "").split("|")[1] == "f"
    and res.get("E", "").split("|")[1] == "t"
    and "claimgate-error" in res.get("E", "")
)
# rows unchanged after rollback
after = "4" if psql(snap_sql).strip() == before else "0"
print("rows intact after rollback:", after, "/ 4")
print("REDPROOF", "PASS" if ok and after == "4" else "FAIL")
sys.exit(0 if ok and after == "4" else 1)
