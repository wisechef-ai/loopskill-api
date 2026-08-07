#!/usr/bin/env python3
"""mesh_0408 W1 — RED-proof harness for the cross-org isolation P0.

Mutates each fix layer INDEPENDENTLY and asserts the right tests go red, then
restores and asserts green. Trap V4: every mutation asserts it changed bytes,
so a moved anchor raises instead of reporting a silent PASS.

Layer 2 is mutated SEPARATELY from layer 3 on purpose. A predicate-only fix
closes exactly one of the two leak directions — this harness is what proves the
fleet-org resolution is load-bearing rather than decoration.

Usage:  python tools/mesh0408_w1_redproof.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEST = "tests/test_mesh0408_p0_crossorg_isolation.py"

LAYER3 = (
    "app/authz.py",
    '    ctx_org = getattr(ctx, "org_id", None)\n    return ctx_org is None or ctx_org != obj_org\n',
    '    ctx_org = getattr(ctx, "org_id", None)\n    return False  # MUTATED: layer 3 reverted to bare owner-match\n',
)

LAYER2 = (
    "app/middleware/api_key.py",
    "    if api_key_id is not None:\n",
    "    if api_key_id is None:  # MUTATED: layer 2 reverted to oldest-membership\n",
)


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=REPO, capture_output=True, text=True)


def _pytest(*extra: str) -> tuple[int, str]:
    # E4: capture rc from the command itself, never from a pipe tail.
    p = _run(sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", TEST, *extra)
    return p.returncode, p.stdout + p.stderr


def _apply(mutation: tuple[str, str, str]) -> str:
    path, old, new = mutation
    f = REPO / path
    original = f.read_text()
    assert original.count(old) == 1, f"anchor moved or ambiguous in {path}: {original.count(old)} matches"
    mutated = original.replace(old, new)
    assert mutated != original, f"anchor moved — mutation changed no bytes in {path}"
    f.write_text(mutated)
    return original


def _restore(mutation: tuple[str, str, str], original: str) -> None:
    (REPO / mutation[0]).write_text(original)


def _summary(out: str) -> str:
    return next((ln for ln in reversed(out.splitlines()) if " passed" in ln or " failed" in ln), "?")


def main() -> int:
    print("=" * 72)
    print("mesh_0408 W1 RED-PROOF — cross-org bundle isolation")
    print("=" * 72)

    print("\n[0] BASELINE — fix fully in place, expect GREEN")
    rc, out = _pytest()
    print(f"    {_summary(out)}")
    assert rc == 0, f"baseline is not green; refusing to proceed\n{out[-4000:]}"

    print("\n[1] MUTATE LAYER 3 (authz.crosses_tenant -> always False)")
    print("    i.e. revert the tenant check, restoring the bare owner-match")
    original3 = _apply(LAYER3)
    try:
        rc, out = _pytest()
        print(f"    {_summary(out)}")
        assert rc != 0, "LAYER 3 IS NOT LOAD-BEARING — suite stayed green with the tenant check reverted"
        failed = sorted(
            {ln.split("::")[1].split("[")[0] for ln in out.splitlines() if ln.startswith("FAILED ")}
        )
        print("    RED tests:")
        for name in failed:
            print(f"      - {name}")
    finally:
        _restore(LAYER3, original3)

    print("\n[2] RESTORE LAYER 3 — expect GREEN")
    rc, out = _pytest()
    print(f"    {_summary(out)}")
    assert rc == 0, f"restore failed\n{out[-4000:]}"

    print("\n[3] MUTATE LAYER 2 ONLY (_resolve_org_for_key -> oldest membership)")
    print("    layer 3 stays fully in place — this is the 'naive fix' scenario")
    original2 = _apply(LAYER2)
    try:
        rc, out = _pytest()
        print(f"    {_summary(out)}")
        assert rc != 0, (
            "LAYER 2 IS NOT LOAD-BEARING — the suite stayed green with member keys "
            "resolving to the oldest membership, which means half the leak would "
            "survive a predicate-only fix undetected"
        )
        failed = sorted(
            {ln.split("::")[1].split("[")[0] for ln in out.splitlines() if ln.startswith("FAILED ")}
        )
        print("    RED tests (the direction a predicate-only fix would MISS):")
        for name in failed:
            print(f"      - {name}")
    finally:
        _restore(LAYER2, original2)

    print("\n[4] RESTORE LAYER 2 — expect GREEN")
    rc, out = _pytest()
    print(f"    {_summary(out)}")
    assert rc == 0, f"restore failed\n{out[-4000:]}"

    print("\n[5] git diff must be clean w.r.t. the committed tree")
    p = _run("git", "diff", "--stat", "--", LAYER3[0], LAYER2[0])
    print(f"    git diff on mutated files: {p.stdout.strip() or '(empty — clean)'}")
    assert not p.stdout.strip(), "mutated files did not restore byte-identically"

    print("\nRED-PROOF COMPLETE — both layers proven load-bearing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
