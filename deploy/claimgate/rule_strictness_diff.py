"""Rule-set strictness diff: the contract a sync would install vs what Postgres enforces now.

Run BEFORE letting claimgate_sync.py replace claimgate.rule / claimgate.tier
(it deletes and re-inserts both in one transaction). Exit 0 only when the new
set is at least as strict: every enforced rule id is present with the same
kind, pattern, amount group and veto, no allowed amount was added, no rule
became exemptable, and the tier caps are unchanged or lower.

    # on wisechef-hq: export what is enforced now
    docker exec -i postiz-postgres psql -U "$U" -d "$D" -At -c "SELECT json_build_object(
      'rules', (SELECT json_agg(r) FROM (SELECT id, kind, pg_pattern, amount_group, allowed,
                 exemptable, veto, reason FROM claimgate.rule) r),
      'tiers', (SELECT json_agg(t) FROM claimgate.tier t))" > enforced.json
    curl -s https://app.loopskill.io/api/marketing/claims > new.json
    python3 rule_strictness_diff.py enforced.json new.json

Vetoes compare exactly: any change is reported as weaker (review it by hand).
Exit codes: 0 = at least as strict, 1 = weaker (do NOT sync), 2 = bad input.
stdlib only.
"""

from __future__ import annotations

import json
import sys


def _new_rules(c: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    exemptable = {
        r["id"] for r in (c.get("retired_rules") or []) + (c.get("amount_rules") or []) if r.get("exemptable")
    }
    for kind, key in (("retired", "retired_rules"), ("amount", "amount_rules"), ("exempt", "exempt_rules")):
        for r in c.get(key) or []:
            out[r["id"]] = {
                "kind": kind,
                "pg_pattern": r["pg_pattern"],
                "amount_group": r.get("amount_group") if kind == "amount" else None,
                "allowed": sorted(round(float(a), 2) for a in r.get("allowed") or [])
                if kind == "amount"
                else None,
                "exemptable": r["id"] in exemptable,
                "veto": r.get("pg_veto") if kind == "exempt" else None,
            }
    return out


def _old_rules(e: dict) -> dict[str, dict]:
    return {
        r["id"]: {
            "kind": r["kind"],
            "pg_pattern": r["pg_pattern"],
            "amount_group": r.get("amount_group"),
            "allowed": sorted(round(float(a), 2) for a in r.get("allowed") or [])
            if r["kind"] == "amount"
            else None,
            "exemptable": bool(r.get("exemptable")),
            "veto": r.get("veto"),
        }
        for r in e.get("rules") or []
    }


def diff(enforced: dict, new: dict) -> tuple[list[str], list[str]]:
    old_r, new_r = _old_rules(enforced), _new_rules(new)
    weaker, notes = [], []
    for rid, o in sorted(old_r.items()):
        n = new_r.get(rid)
        if n is None:
            weaker.append(f"REMOVED {o['kind']} rule {rid}")
            continue
        for k in ("kind", "pg_pattern", "amount_group"):
            if o[k] != n[k]:
                weaker.append(f"CHANGED {rid}.{k}: {o[k]!r} -> {n[k]!r}")
        if o["kind"] == "amount":
            added = sorted(set(n["allowed"]) - set(o["allowed"]))
            if added:
                weaker.append(f"ALLOWED MORE {rid}: +{added}")
            if set(o["allowed"]) - set(n["allowed"]):
                notes.append(f"stricter {rid}: allowed {o['allowed']} -> {n['allowed']}")
        if n["exemptable"] and not o["exemptable"]:
            weaker.append(f"NOW EXEMPTABLE {rid}")
        if o["veto"] != n["veto"]:
            # Exact compare: a regex veto has no safe "equivalent" short of a
            # proof, and a looser veto exempts more text (review of #395).
            weaker.append(f"CHANGED {rid}.veto: {o['veto']!r} -> {n['veto']!r}")
    for rid in sorted(new_r.keys() - old_r.keys()):
        (weaker if new_r[rid]["kind"] == "exempt" else notes).append(f"NEW {new_r[rid]['kind']} rule {rid}")
    old_t = {t["name"]: t for t in enforced.get("tiers") or []}
    new_t = {t["display_name"]: t for t in new.get("public_tiers") or []}
    for name in sorted(old_t.keys() | new_t.keys()):
        o, n = old_t.get(name), new_t.get(name)
        if o is None or n is None:
            weaker.append(f"TIER SET CHANGED: {name} {'added' if o is None else 'removed'}")
            continue
        for ok, nk in (("bundle_cap", "bundle_limit"), ("key_cap", "api_key_cap")):
            if o[ok] != n.get(nk):
                weaker.append(f"TIER {name}.{ok}: {o[ok]} -> {n.get(nk)}")
    return weaker, notes


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    enforced, new = (json.load(open(p)) for p in argv)
    weaker, notes = diff(enforced, new)
    counts = lambda d: {
        k: sum(1 for v in d.values() if v["kind"] == k) for k in ("retired", "amount", "exempt")
    }  # noqa: E731
    print(f"enforced: {counts(_old_rules(enforced))} tiers={len(enforced.get('tiers') or [])}")
    print(f"new:      {counts(_new_rules(new))} tiers={len(new.get('public_tiers') or [])}")
    for line in notes:
        print("  note:", line)
    for line in weaker:
        print("  WEAKER:", line)
    print("VERDICT:", "WEAKER - do not sync" if weaker else "at least as strict")
    return 1 if weaker else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
