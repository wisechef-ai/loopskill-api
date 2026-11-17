#!/usr/bin/env python3
"""Prove the Postgres anti-skip gate is load-bearing, not decorative.

A gate that has never failed is unproven. ``tools/assert_postgres_gates_ran.py``
turns green into a claim ("these Postgres tests really ran"), so the claim has to
be falsified before it is trusted — the same discipline that caught F-4, where a
Postgres job had been green for months while every Postgres test inside it
skipped.

This harness breaks the gate ON PURPOSE, one way at a time, and demands that the
check which exists to catch that breakage goes red with the right words in its
output. Plus three BASELINE cases that prove the evidence is not toothless: the
gate must be GREEN on a real healthy postgres junit, RED on the real sqlite junit
(that is what "the postgres leg lost its service" looks like), and GREEN on the
real ``-rA`` migrations log.

Thirteen cases, grouped by the layer that must catch them:

  Baselines — no mutation. If these are not green/red exactly as stated, every
          mutation result below is meaningless, because the inputs prove nothing.
  Layer 1 — the required list. Empty it, blank it, or re-tag every line away from
          a scope, and the gate must exit 2 ("could not certify"), never 0. An
          anti-skip list that can be emptied in silence is not a gate.
  Layer 2 — the classifier. Make a skipped test read as passed, make everything
          read as passed, make an ABSENT test read as passed, or hardcode the
          verdict green, and the gate's own unit tests must go red. The unit
          tests are the guard here: no real artifact can show a weakened
          classifier, because healthy evidence has nothing to catch.
  Layer 3 — the wiring. Delete a workflow step, or the checker file itself, and
          tests/test_assert_postgres_gates_ran.py must fail. That file is the
          tripwire for the "shipped a gate wired to nothing" rot class; if it
          never reddens, it is not actually reading the workflows.

Two disciplines are load-bearing, both imported from
tools/mesh0408_w1_redproof.py:

* **Every mutation must change bytes.** ``mutation-noop`` is a failure, never a
  pass: an anchor that silently stopped matching would report a satisfied check
  while mutating nothing.
* **Every mutation must name the evidence it expects.** "Something failed" lets a
  precondition error certify red while the assertion itself stays broken, so
  ``expect_exit`` and ``expect`` are both asserted and both are reported.

Restores are verified, not assumed: the worktree fingerprint (``git diff`` plus
the bytes of every untracked file) is captured before each mutation and re-checked
after its restore. A restore that quietly failed would otherwise ship a mutated
gate — and a mutated gate reads GREEN forever, which is precisely the failure this
file exists to prevent.

Usage
-----
    # real artifacts (see the card: download them from a main run, and reproduce
    # the migrations job locally — skill loopskill-api-local-ci-parity)
    python tools/mutation_proof.py \
        --junit        /path/junit-random-order-postgres.xml \
        --junit-broken /path/junit-random-order-sqlite.xml \
        --log          /path/postgres-migrations.log

    # this harness's own self-tests
    python tools/mutation_proof.py --self-test

Exit codes: 0 = every case behaved as required; 1 = at least one did not (read the
``expected_but_passed`` lines first — those are mutations the gate survived when it
should not have); 2 = evidence files not found.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHECKER = REPO / "tools" / "assert_postgres_gates_ran.py"
REQUIRED = REPO / "tools" / "postgres_gate_required.txt"
RANDOM_ORDER_YML = REPO / ".github" / "workflows" / "random-order.yml"
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
GATE_TESTS = "tests/test_assert_postgres_gates_ran.py"
HARNESS_TESTS = "tests/test_mutation_harness.py"

# Anchored on real source lines so a rename makes a mutation NOOP loudly instead
# of silently doing nothing. tests/test_mutation_harness.py asserts each of these
# still appears exactly once in the checker, which is what keeps them honest.
SKIPPED_ANCHOR = '        elif tag == "skipped":\n            status = _worse(status, SKIPPED)\n'
ALWAYS_PASSED_ANCHOR = "def _classify_testcase(case: ET.Element) -> str:\n    status = PASSED\n"
MISSING_ANCHOR = "        return self.nodeids.get(_norm_nodeid(nodeid), MISSING)\n"
GREEN_ANCHOR = "    violations: list[str] = []\n"

EXPECTED_CASES = 13
FLOORS = ("--max-skipped", "400", "--min-tests", "8500")

# Block-end needles for deleting one workflow step: the next step, or the next job.
_STEP_END_NEEDLES = ("\n      - name: ", "\n  secret-scan:", "\n  random-order:", "\nconcurrency:")


@dataclass
class Result:
    """One case's outcome. ``expected_but_passed`` is the line that matters most."""

    name: str
    layer: str
    ok: bool
    detail: str
    expected_but_passed: bool = False
    skipped_reason: str | None = None


@dataclass
class Case:
    """A reversible break (possibly none) plus the evidence it must produce."""

    name: str
    layer: str
    target: Path
    argv: list[str]
    expect_exit: int
    expect: str
    old: str = ""
    new: str = ""
    # A baseline has nothing to mutate; anything that does must report NOOP when
    # its anchor stops matching.
    mutates: bool = True

    def apply(self) -> bool:
        """Literal anchor replacement. False = the anchor did not match (NOOP)."""
        text = self.target.read_text(encoding="utf-8")
        if self.old not in text or text.count(self.old) != 1:
            return False
        updated = text.replace(self.old, self.new, 1)
        if updated == text:
            return False
        self.target.write_text(updated, encoding="utf-8")
        return True

    def restore(self, before: str) -> None:
        """Write back the exact bytes captured before the mutation."""
        self.target.write_text(before, encoding="utf-8")


@dataclass
class Baseline(Case):
    """No mutation — proves the evidence itself carries the claimed signal."""

    mutates: bool = False

    def apply(self) -> bool:
        return True


@dataclass
class WholeFileCase(Case):
    """A mutation whose replacement text is computed from the whole file."""

    rewrite: Callable[[str], str] = str

    def apply(self) -> bool:
        text = self.target.read_text(encoding="utf-8")
        updated = self.rewrite(text)
        if updated == text:
            return False
        self.target.write_text(updated, encoding="utf-8")
        return True


@dataclass
class StepDeletion(Case):
    """Delete one `- name:` block from a workflow, restoring it byte-for-byte."""

    step_marker: str = ""

    def apply(self) -> bool:
        text = self.target.read_text(encoding="utf-8")
        marker = f"      - name: {self.step_marker}\n"
        start = text.find(marker)
        if start < 0:
            return False
        rest = text[start + len(marker) :]
        ends = [
            start + len(marker) + rest.find(needle) for needle in _STEP_END_NEEDLES if rest.find(needle) >= 0
        ]
        end = min(ends) if ends else len(text)
        updated = text[:start] + text[end:]
        if updated == text:
            return False
        self.target.write_text(updated, encoding="utf-8")
        return True


@dataclass
class FileRemoval(Case):
    """Move a file out of the tree (restored byte-for-byte)."""

    held_at: Path | None = None

    def apply(self) -> bool:
        if not self.target.is_file():
            return False
        self.held_at = self.target.with_suffix(self.target.suffix + ".mutation-held")
        shutil.move(str(self.target), str(self.held_at))
        return True

    def restore(self, before: str) -> None:
        if self.held_at is not None and self.held_at.is_file():
            shutil.move(str(self.held_at), str(self.target))
            self.held_at = None


# --------------------------------------------------------------------------- #
# the cases
# --------------------------------------------------------------------------- #
def _gate_cmd(evidence: str, scope: str, *extra: str) -> list[str]:
    return [
        sys.executable,
        "tools/assert_postgres_gates_ran.py",
        "--evidence",
        evidence,
        "--scope",
        scope,
        *extra,
    ]


def _gate_tests_cmd() -> list[str]:
    return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", GATE_TESTS]


def _comment_out_entries(text: str) -> str:
    return (
        "\n".join(
            f"# {line}" if ("::" in line and not line.lstrip().startswith("#")) else line
            for line in text.splitlines()
        )
        + "\n"
    )


def build_cases(junit: str, junit_broken: str, log: str) -> list[Case]:
    """The three baselines and the eleven deliberate breakages."""
    return [
        # -- Baselines: the evidence must carry the signal --------------------
        Baseline(
            name="healthy_postgres_junit_is_green",
            layer="0-baseline",
            target=CHECKER,
            argv=_gate_cmd(junit, "random-order", *FLOORS),
            expect_exit=0,
            expect="named Postgres gates ran and passed",
        ),
        Baseline(
            name="service_dead_fingerprint_is_red",
            layer="0-baseline",
            target=CHECKER,
            argv=_gate_cmd(junit_broken, "random-order", *FLOORS),
            expect_exit=1,
            expect="SKIPPED, not executed",
        ),
        Baseline(
            name="migrations_log_is_green",
            layer="0-baseline",
            target=CHECKER,
            argv=_gate_cmd(log, "migrations"),
            expect_exit=0,
            expect="named Postgres gates ran and passed",
        ),
        # -- Layer 1: the required list must never certify "nothing" ----------
        WholeFileCase(
            name="required_list_emptied",
            layer="1-required-list",
            target=REQUIRED,
            rewrite=_comment_out_entries,
            argv=_gate_cmd(junit_broken, "random-order", *FLOORS),
            expect_exit=2,
            expect="required list is empty",
        ),
        FileRemoval(
            name="required_list_missing",
            layer="1-required-list",
            target=REQUIRED,
            argv=_gate_cmd(junit_broken, "random-order"),
            expect_exit=2,
            expect="required list not found",
        ),
        WholeFileCase(
            name="random_order_scope_untagged",
            layer="1-required-list",
            target=REQUIRED,
            rewrite=lambda text: text.replace("\nall ", "\nmigrations "),
            argv=_gate_cmd(junit_broken, "random-order"),
            expect_exit=2,
            expect="ZERO required tests",
        ),
        # -- Layer 2: the classifier must never read absence/skip as a pass ---
        Case(
            name="skipped_reads_as_passed",
            layer="2-classifier",
            target=CHECKER,
            old=SKIPPED_ANCHOR,
            new='        elif tag == "skipped":\n            status = _worse(status, PASSED)  # MUTATION\n',
            argv=_gate_tests_cmd(),
            expect_exit=1,
            expect="test_required_test_skipped_fails_and_names_it",
        ),
        Case(
            name="classifier_always_reports_passed",
            layer="2-classifier",
            target=CHECKER,
            old=ALWAYS_PASSED_ANCHOR,
            new="def _classify_testcase(case: ET.Element) -> str:\n    return PASSED  # MUTATION\n",
            argv=_gate_tests_cmd(),
            expect_exit=1,
            expect="test_required_test_failed_fails",
        ),
        Case(
            name="absent_test_reads_as_passed",
            layer="2-classifier",
            target=CHECKER,
            old=MISSING_ANCHOR,
            new="        return self.nodeids.get(_norm_nodeid(nodeid), PASSED)  # MUTATION\n",
            argv=_gate_tests_cmd(),
            expect_exit=1,
            expect="test_required_test_missing_fails",
        ),
        Case(
            name="evaluate_hardcoded_green",
            layer="2-classifier",
            target=CHECKER,
            old=GREEN_ANCHOR,
            new=(
                "    violations: list[str] = []  # MUTATION below\n"
                "    return Verdict(ok=True, exit_code=EXIT_OK, lines=['MUTANT: always green'])\n"
            ),
            argv=_gate_tests_cmd(),
            expect_exit=1,
            expect="test_total_collapse_is_caught_by_the_named_list_and_the_ceiling",
        ),
        # -- Layer 3: the wiring tripwire must actually read the workflows ----
        StepDeletion(
            name="random_order_step_removed",
            layer="3-wiring",
            target=RANDOM_ORDER_YML,
            step_marker="Assert the Postgres-only proofs actually RAN (anti-skip)",
            argv=_gate_tests_cmd(),
            expect_exit=1,
            expect="test_random_order_calls_the_checker_on_the_postgres_leg_only",
        ),
        StepDeletion(
            name="migrations_step_removed",
            layer="3-wiring",
            target=CI_YML,
            step_marker="Assert the Postgres gates actually RAN (anti-skip)",
            argv=_gate_tests_cmd(),
            expect_exit=1,
            expect="test_migrations_job_still_calls_the_same_checker",
        ),
        FileRemoval(
            name="checker_file_removed",
            layer="3-wiring",
            target=CHECKER,
            argv=_gate_tests_cmd(),
            expect_exit=2,
            expect="cannot import name 'assert_postgres_gates_ran'",
        ),
    ]


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
def _run(repo: Path, argv: list[str]) -> tuple[int, str]:
    proc = subprocess.run(argv, cwd=repo, capture_output=True, text=True, timeout=900, check=False)
    return proc.returncode, proc.stdout + proc.stderr


def fingerprint(repo: Path) -> str:
    """Content hash of everything a mutation could have touched.

    ``git diff`` for tracked files plus the bytes of every untracked file, so an
    added or deleted file counts too. Before/after must be equal or the case fails
    with ``restore-mismatch`` — a mutation left behind in a tree that then gets
    committed is worse than no mutation proof at all.
    """
    digest = hashlib.sha256()
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"], cwd=repo, capture_output=True, text=True, check=False
    )
    digest.update(diff.stdout.encode("utf-8", "replace"))
    others = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    for name in sorted(others.stdout.split()):
        digest.update(name.encode())
        path = repo / name
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
    return digest.hexdigest()


def _show(output: str) -> None:
    lines = [line for line in output.splitlines() if line.strip()]
    shown = lines[:4] + (["    ..."] if len(lines) > 12 else []) + lines[-8:]
    for line in shown:
        print(f"    | {line[:170]}")


def run_one(case: Case, repo: Path = REPO) -> Result:
    """Apply, demand the specified RED, restore, verify the bytes are identical."""
    if not case.target.is_file():
        return Result(
            case.name, case.layer, False, f"missing target {case.target}", skipped_reason="target-missing"
        )
    before = case.target.read_text(encoding="utf-8")
    print(f"\n=== {case.layer}: {case.name} ===")
    pre = fingerprint(repo)
    if not case.apply():
        return Result(
            case.name,
            case.layer,
            False,
            "anchor did not match — mutation was a NOOP",
            skipped_reason="mutation-noop",
        )

    try:
        code, output = _run(repo, case.argv)
    except Exception as exc:  # noqa: BLE001 — a harness reports, it does not crash
        case.restore(before)
        return Result(
            case.name, case.layer, False, f"command raised {exc!r}", skipped_reason="command-exception"
        )

    print(f"    exit={code} (expected {case.expect_exit})")
    _show(output)

    marker_found = case.expect in output
    ok = code == case.expect_exit and marker_found
    detail = f"exit={code} want={case.expect_exit}; marker {'present' if marker_found else 'ABSENT'}"

    case.restore(before)
    post = fingerprint(repo)
    if post != pre:
        return Result(
            case.name,
            case.layer,
            False,
            f"{detail} — AND RESTORE FAILED ({pre[:12]} -> {post[:12]})",
            expected_but_passed=code == 0 and case.mutates,
            skipped_reason="restore-mismatch",
        )
    print("    PASS  behaved as required, restored byte-clean" if ok else f"    FAIL  {detail}")
    return Result(case.name, case.layer, ok, detail, expected_but_passed=code == 0 and case.mutates)


def run(junit: str, junit_broken: str, log: str, repo: Path = REPO) -> tuple[list[Result], list[str]]:
    """Run every case. Returns (results, expected_but_passed lines)."""
    results: list[Result] = []
    for case in build_cases(junit, junit_broken, log):
        try:
            results.append(run_one(case, repo))
        except Exception as exc:  # noqa: BLE001 — one bad case must not lose the report
            results.append(
                Result(
                    case.name,
                    case.layer,
                    False,
                    f"harness error: {exc!r}",
                    skipped_reason="harness-exception",
                )
            )
    expected_but_passed = [
        f"expected_but_passed: {r.name} — the gate stayed green through it"
        for r in results
        if r.expected_but_passed
    ]
    return results, expected_but_passed


def report(results: list[Result], expected_but_passed: list[str]) -> int:
    """Print the report; return the process exit code."""
    total = len(results)
    passed = sum(1 for result in results if result.ok)
    print("\n" + "=" * 76)
    print(f"mutation-proof report: {passed}/{total} cases behaved as required (expected {EXPECTED_CASES})")
    for result in results:
        flag = "OK " if result.ok else "BAD"
        print(f"  [{flag}] {result.layer:17s} {result.name:34s} {result.detail}")
    for line in expected_but_passed:
        print(f"  !! {line}")
    for result in results:
        if result.skipped_reason in (
            "mutation-noop",
            "restore-mismatch",
            "target-missing",
            "command-exception",
            "harness-exception",
        ):
            print(f"  !! {result.skipped_reason}: {result.name}")
    if passed != total or total != EXPECTED_CASES:
        print(f"  !! cases_failed={total - passed} cases_total={total} expected={EXPECTED_CASES}")
    print("=" * 76)
    return 0 if passed == total == EXPECTED_CASES else 1


def self_test() -> int:
    """Run this harness's own pytest file (anchors, fixtures, restore bookkeeping)."""
    code, output = _run(REPO, [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", HARNESS_TESTS])
    print(output.rstrip())
    return code


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Falsify the Postgres anti-skip gate on purpose.")
    parser.add_argument("--junit", help="real healthy junit-random-order-postgres.xml")
    parser.add_argument(
        "--junit-broken",
        dest="junit_broken",
        help="junit where the Postgres gates did NOT run (the sqlite leg is exactly that)",
    )
    parser.add_argument("--log", help="real postgres-migrations.log (-rA summary)")
    parser.add_argument("--self-test", action="store_true", help=f"run {HARNESS_TESTS} and exit")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()
    missing = [
        name
        for name, value in (
            ("--junit", args.junit),
            ("--junit-broken", args.junit_broken),
            ("--log", args.log),
        )
        if not value
    ]
    if missing:
        parser.error(f"missing required argument(s): {', '.join(missing)} (see the module docstring)")
    for path in (args.junit, args.junit_broken, args.log):
        if not Path(path).is_file():
            print(f"evidence not found: {path}", file=sys.stderr)
            return 2
    results, expected_but_passed = run(args.junit, args.junit_broken, args.log)
    return report(results, expected_but_passed)


if __name__ == "__main__":
    sys.exit(main())
