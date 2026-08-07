#!/usr/bin/env python3
"""mesh_0408 W1 / W1b — RED-proof harness for the cross-org isolation P0.

Mutates each guard INDEPENDENTLY and asserts the RIGHT tests go red, then
restores and asserts green + a byte-clean tree.

Two disciplines are load-bearing here and neither is optional:

* Trap V4 — every mutation asserts it changed bytes, so a moved anchor RAISES
  instead of reporting a silent PASS.
* Codex review of PR #202, finding 5 — every mutation names the tests that MUST
  redden. Asserting only "something failed" lets a harness certify RED from an
  unrelated test (a precondition, a fixture assertion) while the endpoint test
  the guard exists for stays green. ``required`` is the fix: the named tests
  must be IN the failure set, not merely accompanied by one.

Each layer is mutated SEPARATELY on purpose. A predicate-only fix closes exactly
one of the two leak directions, and a REST-only fix closes none of the MCP
surface — this harness is what proves each piece is load-bearing rather than
decoration.

Usage:  python tools/mesh0408_w1_redproof.py
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

P0 = "tests/test_mesh0408_p0_crossorg_isolation.py"
W1B = "tests/test_mesh0408_w1b_mcp_and_oracles.py"
LOCK = "tests/test_mesh0408_w1_bundle_lock_tenant_scope.py"
FEEDBACK = "tests/test_loopclose_3005_j_feedback_routing.py"

ALL_TESTS = (P0, W1B, LOCK, FEEDBACK)


@dataclass
class Mutation:
    """One reverted guard, and the tests that must notice."""

    label: str
    why: str
    path: str
    old: str
    new: str
    tests: tuple[str, ...]
    required: tuple[str, ...] = field(default=())


MUTATIONS: list[Mutation] = [
    # ── Layer 3 — the shared tenant predicate ────────────────────────────
    Mutation(
        label="layer 3 — authz.crosses_tenant always False",
        why="reverts the tenant check, restoring the bare owner-match",
        path="app/authz.py",
        old='    ctx_org = getattr(ctx, "org_id", None)\n    return ctx_org is None or ctx_org != obj_org\n',
        new='    ctx_org = getattr(ctx, "org_id", None)\n'
        "    return False  # MUTATED: layer 3 reverted to bare owner-match\n",
        tests=(P0, LOCK, W1B),
        required=(
            "test_site_bundle_detail",
            "test_site_reconcile",
            "test_site_connector_declare",
            "test_site_promotion_report",
            "test_truth_table",
            "test_cross_org_bundle_lock_access_is_denied",
            "test_mint_lock",
        ),
    ),
    # Layer 3 has TWO implementations that must agree: the row predicate above
    # and the SQL clause the list/query sites filter on. Mutating only the first
    # leaves every LIST site green, which would misreport them as proven.
    Mutation(
        label="layer 3 — the SQL twin (owner_match_within_tenant_clause)",
        why="proves the LIST sites (bundle list, library, MCP status block) are gated too",
        path="app/authz.py",
        old="    if ctx_org is None:\n"
        "        return and_(owner_col == user_id, org_col.is_(None))\n"
        "    return and_(owner_col == user_id, or_(org_col.is_(None), org_col == ctx_org))\n",
        new="    return owner_col == user_id  # MUTATED: SQL clause reverted to bare owner-match\n",
        tests=(P0, W1B),
        required=(
            "test_site_bundle_list_is_tenant_scoped",
            "test_sql_clause_agrees_with_row_predicate",
            "test_site_library_owned_bundles_are_tenant_scoped",
            "test_status_block_never_names_the_other_tenant",
            "test_status_fails_closed_without_a_resolved_tenant",
        ),
    ),
    # ── Layer 2 — the fleet-member tenant resolution ─────────────────────
    Mutation(
        label="layer 2 — resolve_org_for_key falls back to oldest membership",
        why="the 'naive fix' scenario: layer 3 stays fully in place, half the leak survives",
        path="app/middleware/_org_scope.py",
        old="    if api_key_id is not None:\n",
        new="    if api_key_id is None:  # MUTATED: layer 2 reverted to oldest-membership\n",
        tests=(P0, W1B),
        required=(
            "test_v2_precondition_same_identity_different_tenant",
            "test_layer2_is_load_bearing_oldest_membership_does_not_win",
            "test_mcp_member_key_resolves_its_fleets_org_not_the_oldest",
            "test_mcp_v2_precondition",
        ),
    ),
    Mutation(
        label="layer 2 — the deactivated-member fail-closed (W1b finding 4)",
        why="a stood-down member must get personal scope, never the account's oldest org",
        path="app/middleware/_org_scope.py",
        old="            if not member_is_active:\n                return (None, False)\n",
        new="            pass  # MUTATED: deactivated members no longer fail closed\n",
        tests=(W1B,),
        required=(
            "test_layer2_fails_closed_for_a_deactivated_member",
            "test_deactivated_member_key_cannot_read_any_tenants_bundle",
        ),
    ),
    # ── W1b finding 1 — bundle-lock routes ───────────────────────────────
    Mutation(
        label="bundle-lock — the tenant predicate (W1b finding 1)",
        why="the routes that MINT and REPLAY a client's immutable lock",
        path="app/bundle_lock_routes.py",
        old='    if ctx.scope == "user" and authz.owner_match_within_tenant(ctx, bundle):\n',
        new='    if ctx.scope == "user" and ctx.user_id == bundle.bundle_owner:  # MUTATED\n',
        tests=(LOCK, W1B),
        required=(
            "test_cross_org_bundle_lock_access_is_denied",
            "test_mint_lock",
            "test_lock_history_is_tenant_scoped",
            "test_drift_is_tenant_scoped",
        ),
    ),
    Mutation(
        label="bundle-lock — the 403-vs-404 existence oracle (W1b finding 3)",
        why="403 not_bundle_owner confirms to an unauthorized caller that the id is real",
        path="app/bundle_lock_routes.py",
        old='    raise HTTPException(status_code=404, detail="bundle_not_found")\n\n\nclass DriftRequestSkill',
        new='    raise HTTPException(status_code=403, detail="not_bundle_owner")  # MUTATED'
        "\n\n\nclass DriftRequestSkill",
        tests=(W1B,),
        required=("test_no_existence_oracle",),
    ),
    # ── W1b finding 2 — MCP, the universal path (lock #31) ───────────────
    Mutation(
        label="MCP auth — validate_key re-derives org from membership",
        why="discards the layer-2 fix on every MCP call; the P0 stays exploitable over MCP",
        path="app/mcp/auth.py",
        old="        from app.middleware.api_key import _resolve_org_for_key\n\n"
        "        org_id, is_org_owner = _resolve_org_for_key(db, api_key_obj.id, api_key_obj.user_id)\n",
        new="        from app.middleware.api_key import _resolve_org_membership  # MUTATED\n\n"
        "        org_id, is_org_owner = _resolve_org_membership(db, api_key_obj.user_id)\n",
        tests=(W1B,),
        required=(
            "test_mcp_member_key_resolves_its_fleets_org_not_the_oldest",
            "test_mcp_org_id_agrees_with_the_rest_path",
            "test_mcp_v2_precondition",
            "test_status_block_never_names_the_other_tenant",
            "test_list_bundle_explicit_id_is_denied_cross_tenant",
            "test_bundle_install_is_denied_cross_tenant",
        ),
    ),
    Mutation(
        label="MCP decoration — bundle_status selects on a bare owner-match",
        why="the block injected into EVERY tool response leaked the other tenant's bundles",
        path="app/mcp/bundle_status.py",
        old="        .filter(owner_match_within_tenant_clause("
        "SimpleNamespace(user_id=user_id, org_id=org_id), Bundle))\n",
        new="        .filter(Bundle.bundle_owner == user_id)  # MUTATED\n",
        tests=(W1B,),
        required=(
            "test_status_block_never_names_the_other_tenant",
            "test_status_fails_closed_without_a_resolved_tenant",
        ),
    ),
    Mutation(
        label="MCP loopskill_bundle_install — the bare owner-match",
        why="the tool that pulls a bundle's private skill payloads onto an agent",
        path="app/mcp/tools/bundle_install.py",
        old='    if ctx.scope == "user" and authz.owner_match_within_tenant(ctx, cb):\n',
        new='    if ctx.scope == "user" and cb.bundle_owner == ctx.user_id:  # MUTATED\n',
        tests=(W1B,),
        required=("test_bundle_install_is_denied_cross_tenant",),
    ),
    Mutation(
        label="MCP loopskill_list_bundle — the implicit target",
        why="the no-argument call had no authz site at all, it just picked a bundle",
        path="app/mcp/tools/list_cookbook.py",
        old="                .filter(authz.owner_match_within_tenant_clause(scope_ctx, Bundle))\n",
        new="                .filter(Bundle.bundle_owner == owner)  # MUTATED\n",
        tests=(W1B,),
        required=("test_list_bundle_implicit_target_stays_in_tenant",),
    ),
    Mutation(
        label="MCP loopskill_skillify — the implicit authoring target",
        why="a draft authored from client B landed in client A's bundle",
        path="app/mcp/tools/recipify.py",
        old="                .filter(authz.owner_match_within_tenant_clause(scope_ctx, Bundle))\n",
        new="                .filter(Bundle.bundle_owner == owner_id)  # MUTATED\n",
        tests=(W1B,),
        required=("test_skillify_implicit_target_stays_in_tenant",),
    ),
    Mutation(
        label="MCP configure_feedback — the ownership gate + its oracle",
        why="cross-tenant feedback repointing, and 'you do not own this' vs 'not found'",
        path="app/mcp/tools/configure_feedback.py",
        old="    if cb is None or not authz.can_write_cookbook(ctx, cb):\n"
        '        return {"ok": False, "error": "Bundle not found"}\n',
        new="    if cb is None:  # MUTATED\n"
        '        return {"ok": False, "error": "Bundle not found"}\n'
        '    if ctx.scope != "master" and cb.bundle_owner != _coerce_uuid(ctx.user_id):\n'
        '        return {"ok": False, "error": "You do not own this cookbook"}\n',
        tests=(W1B, FEEDBACK),
        required=(
            "test_configure_feedback_explicit_id_is_denied_cross_tenant",
            "test_unowned_cookbook_rejected",
        ),
    ),
    Mutation(
        label="MCP configure_feedback — the implicit target",
        why="a bare configure_feedback(repo=...) from client B repointed client A",
        path="app/mcp/tools/configure_feedback.py",
        old="            authz.owner_match_within_tenant_clause(ctx, Bundle),\n",
        new="            Bundle.bundle_owner == ctx.user_id,  # MUTATED\n",
        tests=(W1B,),
        required=("test_configure_feedback_implicit_target_stays_in_tenant",),
    ),
    # ── W1b finding 3 — the remaining existence oracles / writes ─────────
    Mutation(
        label="engagement — the unauthorized like/favourite write (W1b finding 3/5)",
        why="a cross-tenant caller committed a SkillLike row and read back a like_count",
        path="app/engagement_routes.py",
        old="    if skill_id is None:\n        return  # federated track — no local row, nothing to authorize\n",
        new="    return  # MUTATED: every track is treated as readable\n",
        tests=(P0,),
        required=("test_site_authz_can_read_skill_bundle_clause",),
    ),
    # The same oracle pair, in the five MCP tools codex did NOT name. Hard rule
    # #7: fix the CLASS, and prove each member of it independently.
    Mutation(
        label="MCP loopskill_sync — the forbidden-vs-not_found oracle",
        why="a distinct cookbook_forbidden confirmed a guessed bundle id is real",
        path="app/mcp/tools/loopskill_sync.py",
        old='        return {"error": "not_found", "cookbook_id": cookbook_id}\n\n'
        "    outdated = _find_outdated_skills(db, cb_uuid)\n",
        new='        return {"error": "cookbook_forbidden", "cookbook_id": cookbook_id}  # MUTATED\n\n'
        "    outdated = _find_outdated_skills(db, cb_uuid)\n",
        tests=(W1B,),
        required=("test_loopskill_sync",),
    ),
    Mutation(
        label="MCP share_revoke — the forbidden-vs-not_found oracle",
        why="four share verbs carried the same pair",
        path="app/mcp/tools/share.py",
        old="    if not authz.can_write_cookbook(ctx, cb):\n"
        "        # mesh_0408 W1b (codex PR #202, finding 3): same answer as the absent\n"
        "        # case above — a distinct `cookbook_forbidden` was an existence oracle.\n"
        '        return {"error": "cookbook_not_found", "cookbook_id": cookbook_id}\n\n'
        "    try:\n        _revoke_service(db, cookbook=cb, token_id=token_id)\n",
        new="    if not authz.can_write_cookbook(ctx, cb):\n"
        '        return {"error": "cookbook_forbidden", "cookbook_id": cookbook_id}  # MUTATED\n\n'
        "    try:\n        _revoke_service(db, cookbook=cb, token_id=token_id)\n",
        tests=(W1B,),
        required=("test_share_revoke",),
    ),
    Mutation(
        label="MCP harvest — the 403-vs-404 oracle",
        why="the fleet harvest rail answered 403 for an existing bundle",
        path="app/mcp/tools/harvest.py",
        old='        return {"error": "bundle_not_found", "code": 404}\n',
        new='        return {"error": "forbidden", "code": 403}  # MUTATED\n',
        tests=(W1B,),
        required=("test_harvest",),
    ),
    Mutation(
        label="MCP bundle_handoff — the forbidden-vs-not_found oracle",
        why="'only the owner may hand off this cookbook' named a real bundle",
        path="app/mcp/tools/bundle_handoff.py",
        old='        return {"error": "cookbook_not_found", "message": "Cookbook not found."}\n\n'
        "    # ── resolve new owner",
        new='        return {"error": "forbidden", "message": "Only the owner."}  # MUTATED\n\n'
        "    # ── resolve new owner",
        tests=(W1B,),
        required=("test_bundle_handoff",),
    ),
    Mutation(
        label="reconcile engine — the forbidden-vs-not_found oracle",
        why="the HTTP twin already 404s; the MCP engine kept the oracle",
        path="app/services/reconcile.py",
        old='        return {"error": "not_found", "cookbook_id": cookbook_id}\n\n    local_states = [\n',
        new='        return {"error": "cookbook_forbidden", "cookbook_id": cookbook_id}  # MUTATED\n\n'
        "    local_states = [\n",
        tests=(W1B,),
        required=("test_reconcile_engine",),
    ),
    Mutation(
        label="follow — the 400-vs-404 existence oracle (W1b finding 3)",
        why="cannot_follow_own_bundle vs bundle_not_found told client B that A's id is real",
        path="app/follow_routes.py",
        old='    if bundle.visibility != "public" and authz.crosses_tenant(ctx, bundle):\n'
        '        raise HTTPException(status_code=404, detail="bundle_not_found")\n'
        "    if bundle.bundle_owner == user_id:\n",
        new="    if bundle.bundle_owner == user_id:  # MUTATED: tenant guard removed\n",
        tests=(W1B,),
        required=("test_follow_has_no_cross_tenant_existence_oracle",),
    ),
]


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=REPO, capture_output=True, text=True)


def _pytest(tests: tuple[str, ...]) -> tuple[int, str]:
    # E4: capture rc from the command itself, never from a pipe tail.
    p = _run(sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests)
    return p.returncode, p.stdout + p.stderr


def _failed_names(out: str) -> set[str]:
    """Test FUNCTION names in the failure set, parametrisation stripped."""
    names = set()
    for line in out.splitlines():
        if not line.startswith("FAILED "):
            continue
        parts = line.split("::")
        if len(parts) < 2:
            continue
        names.add(parts[-1].split("[")[0].split(" ")[0])
    return names


def _apply(m: Mutation) -> str:
    f = REPO / m.path
    original = f.read_text()
    count = original.count(m.old)
    assert count == 1, f"anchor moved or ambiguous in {m.path}: {count} matches"
    mutated = original.replace(m.old, m.new)
    assert mutated != original, f"anchor moved — mutation changed no bytes in {m.path}"
    f.write_text(mutated)
    return original


def _summary(out: str) -> str:
    return next((ln for ln in reversed(out.splitlines()) if " passed" in ln or " failed" in ln), "?")


def main() -> int:
    print("=" * 78)
    print("mesh_0408 W1 / W1b RED-PROOF — cross-org isolation")
    print("=" * 78)

    print("\n[baseline] every guard in place, expect GREEN")
    rc, out = _pytest(ALL_TESTS)
    print(f"    {_summary(out)}")
    assert rc == 0, f"baseline is not green; refusing to proceed\n{out[-6000:]}"

    for i, m in enumerate(MUTATIONS, 1):
        print(f"\n[{i}/{len(MUTATIONS)}] MUTATE {m.label}")
        print(f"        {m.why}")
        original = _apply(m)
        try:
            rc, out = _pytest(m.tests)
            print(f"        {_summary(out)}")
            assert rc != 0, f"NOT LOAD-BEARING — the suite stayed green with '{m.label}' reverted"
            failed = _failed_names(out)
            missing = sorted(set(m.required) - failed)
            for name in sorted(failed):
                print(f"          RED  {name}{'  <- required' if name in m.required else ''}")
            assert not missing, (
                f"WRONG TESTS WENT RED for '{m.label}'.\n"
                f"  required but still green: {missing}\n"
                f"  actually red: {sorted(failed)}\n"
                "The guard is unproven: the harness would have certified RED off an "
                "unrelated test while the endpoint this guard exists for stayed green."
            )
        finally:
            (REPO / m.path).write_text(original)

    print("\n[restore] every mutation reverted, expect GREEN")
    rc, out = _pytest(ALL_TESTS)
    print(f"    {_summary(out)}")
    assert rc == 0, f"restore failed\n{out[-6000:]}"

    print("\n[tree] git diff must be clean w.r.t. the committed tree")
    paths = sorted({m.path for m in MUTATIONS})
    p = _run("git", "diff", "--stat", "--", *paths)
    print(f"    git diff on mutated files: {p.stdout.strip() or '(empty — clean)'}")
    assert not p.stdout.strip(), "mutated files did not restore byte-identically"

    print(f"\nRED-PROOF COMPLETE — {len(MUTATIONS)} guards proven load-bearing by NAMED tests.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
