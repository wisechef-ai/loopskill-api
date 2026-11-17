"""Tests for tools/mutation_proof.py — the harness that falsifies the anti-skip gate.

Why this file exists
--------------------
``tools/mutation_proof.py`` is the thing that proves
``tools/assert_postgres_gates_ran.py`` is load-bearing rather than decorative. A
harness that quietly stops mutating — because an anchor it greps for was renamed,
or because a workflow step's title drifted — reports *success* while testing
nothing. That is the same silent-green failure the gate itself exists to kill, so
the harness gets its own tests.

The highest-value case here is :func:`TestAnchors.test_anchors_appear_exactly_once`:
every layer-2 mutation is an anchored literal replacement, and a renamed anchor
turns a mutation into a NOOP. These tests turn that into a red build instead of a
green report.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import assert_postgres_gates_ran as gate  # noqa: E402
from tools import mutation_proof as mp  # noqa: E402

GATE_TEST_SOURCE = (REPO_ROOT / "tests" / "test_assert_postgres_gates_ran.py").read_text(encoding="utf-8")
# Markers that come from pytest's collection machinery rather than a test name;
# they cannot be checked against the test file's source.
NON_TEST_MARKERS = {"cannot import name 'assert_postgres_gates_ran'"}


def _file(tmp_path: Path, text: str = "alpha beta gamma\n", name: str = "target.txt") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _case(tmp_path: Path, **overrides) -> mp.Case:
    """A synthetic Case that only ever touches files under tmp_path.

    The default target is created only when the caller did not supply one:
    creating it first and letting ``update()`` override the path would clobber
    the caller's file with the default contents (both live under the same name).
    """
    defaults: dict = {
        "name": "c",
        "layer": "2-classifier",
        "old": "alpha",
        "new": "MUTATED",
        "argv": [sys.executable, "-c", "raise SystemExit(1)"],
        "expect_exit": 1,
        "expect": "",
    }
    if "target" not in overrides:
        defaults["target"] = _file(tmp_path)
    defaults.update(overrides)
    return mp.Case(**defaults)  # type: ignore[arg-type]


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """A tiny committed repo so fingerprint() has something real to hash."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


# --------------------------------------------------------------------------- #
# the anchors: if one drifts, its mutation becomes a silent NOOP
# --------------------------------------------------------------------------- #
class TestAnchors:
    @pytest.mark.parametrize(
        "anchor",
        [mp.SKIPPED_ANCHOR, mp.ALWAYS_PASSED_ANCHOR, mp.MISSING_ANCHOR, mp.GREEN_ANCHOR],
        ids=["skipped", "always-passed", "missing", "green"],
    )
    def test_anchors_appear_exactly_once(self, anchor):
        text = mp.CHECKER.read_text(encoding="utf-8")
        assert text.count(anchor) == 1, (
            "mutation anchor no longer matches the checker exactly once — the matching "
            f"mutation would report success while changing nothing: {anchor!r}"
        )

    def test_anchors_are_valid_python_when_substituted(self):
        """A mutated checker must still import — a SyntaxError 'failure' proves nothing."""
        import ast

        checker = mp.CHECKER.read_text(encoding="utf-8")
        for old, new in (
            (mp.SKIPPED_ANCHOR, '        elif tag == "skipped":\n            pass  # MUTATION\n'),
            (
                mp.ALWAYS_PASSED_ANCHOR,
                "def _classify_testcase(case: ET.Element) -> str:\n    return PASSED\n",
            ),
            (mp.MISSING_ANCHOR, "        return self.nodeids.get(_norm_nodeid(nodeid), PASSED)\n"),
            (
                mp.GREEN_ANCHOR,
                "    violations: list[str] = []\n    return Verdict(ok=True, exit_code=EXIT_OK, lines=[])\n",
            ),
        ):
            ast.parse(checker.replace(old, new, 1))


# --------------------------------------------------------------------------- #
# the case list itself
# --------------------------------------------------------------------------- #
class TestCaseList:
    def _cases(self):
        return mp.build_cases("j.xml", "b.xml", "m.log")

    def test_case_count_matches_the_expected_total(self):
        assert len(self._cases()) == mp.EXPECTED_CASES, (
            "a case was added or removed without updating EXPECTED_CASES — report() "
            "fails on a mismatch, so this number is the harness's own anti-vacuity floor"
        )

    def test_names_are_unique(self):
        names = [case.name for case in self._cases()]
        assert len(names) == len(set(names))

    def test_every_target_file_exists(self):
        for case in self._cases():
            assert case.target.is_file(), f"{case.name}: target {case.target} is gone"

    def test_layers_are_known(self):
        known = {"0-baseline", "1-required-list", "2-classifier", "3-wiring"}
        assert {case.layer for case in self._cases()} <= known

    def test_baselines_do_not_mutate_and_mutations_do(self):
        for case in self._cases():
            if case.layer == "0-baseline":
                assert case.mutates is False and isinstance(case, mp.Baseline)
            else:
                assert case.mutates is True

    def test_layer_2_and_3_expectations_name_real_tests(self):
        """Expectations cannot rot when a test is renamed: the marker must exist."""
        for case in self._cases():
            if case.layer in ("2-classifier", "3-wiring") and case.expect not in NON_TEST_MARKERS:
                assert case.expect in GATE_TEST_SOURCE, (
                    f"{case.name} expects marker {case.expect!r}, which is not in the gate's "
                    "test file any more — the mutation would report 'marker ABSENT' forever"
                )

    def test_layer_2_and_3_run_the_gate_test_file(self):
        for case in self._cases():
            if case.layer in ("2-classifier", "3-wiring"):
                assert mp.GATE_TESTS in case.argv, f"{case.name} must be guarded by the unit tests"

    def test_layer_1_uses_the_broken_evidence(self):
        """Vacuity must be tested against evidence that is actually adversarial."""
        for case in self._cases():
            if case.layer == "1-required-list":
                assert "b.xml" in case.argv


# --------------------------------------------------------------------------- #
# mutation mechanics
# --------------------------------------------------------------------------- #
class TestMutationMechanics:
    def test_literal_mutation_changes_and_restores(self, tmp_path):
        case = _case(tmp_path)
        before = case.target.read_text(encoding="utf-8")
        assert case.apply() is True
        assert "MUTATED" in case.target.read_text(encoding="utf-8")
        case.restore(before)
        assert case.target.read_text(encoding="utf-8") == before

    def test_missing_anchor_is_a_noop_not_a_pass(self, tmp_path):
        case = _case(tmp_path, old="this-text-does-not-exist")
        assert case.apply() is False

    def test_ambiguous_anchor_is_a_noop(self, tmp_path):
        case = _case(tmp_path, target=_file(tmp_path, "alpha alpha\n"))
        assert case.apply() is False, "a 2-match anchor would mutate one of two sites silently"

    def test_whole_file_rewrite_noop_is_detected(self, tmp_path):
        case = mp.WholeFileCase(
            name="c",
            layer="1-required-list",
            target=_file(tmp_path),
            rewrite=lambda text: text,
            argv=[sys.executable, "-c", "raise SystemExit(2)"],
            expect_exit=2,
            expect="",
        )
        assert case.apply() is False

    def test_comment_out_entries_blanks_every_entry(self, tmp_path):
        text = mp.REQUIRED.read_text(encoding="utf-8")
        blanked = mp._comment_out_entries(text)
        assert blanked != text
        with pytest.raises(gate.GateUsageError, match="empty"):
            gate.parse_required(_file(tmp_path, blanked, name="blank.txt"))
        # comments and blank lines survive — the file's documentation is not erased
        assert "SINGLE SOURCE OF TRUTH" in blanked

    def test_scope_untag_leaves_migrations_intact(self, tmp_path):
        text = mp.REQUIRED.read_text(encoding="utf-8")
        untag = next(
            case for case in mp.build_cases("a", "b", "c") if case.name == "random_order_scope_untagged"
        )
        result = untag.rewrite(text)  # type: ignore[attr-defined]
        assert "\nall " not in result
        entries = gate.parse_required(_file(tmp_path, result, name="untagged.txt"))
        assert gate.select(entries, "migrations")
        with pytest.raises(gate.GateUsageError, match="ZERO"):
            gate.select(entries, "random-order")


class TestStepDeletion:
    def test_removes_only_the_named_step(self, tmp_path):
        target = _file(tmp_path, mp.RANDOM_ORDER_YML.read_text(encoding="utf-8"), name="random-order.yml")
        case = mp.StepDeletion(
            name="c",
            layer="3-wiring",
            target=target,
            step_marker="Summarize seed and result",
            argv=[sys.executable, "-c", "raise SystemExit(1)"],
            expect_exit=1,
            expect="",
        )
        before = target.read_text(encoding="utf-8")
        assert case.apply() is True
        after = target.read_text(encoding="utf-8")
        assert "Summarize seed and result" not in after
        assert "Upload junit XML and log" in after, "deletion ran past the end of the step"
        assert "Assert the Postgres-only proofs actually RAN" in after, "deletion ate a sibling step"
        case.restore(before)
        assert target.read_text(encoding="utf-8") == before

    def test_missing_marker_is_a_noop(self, tmp_path):
        target = _file(tmp_path, "      - name: something else\n        run: x\n", name="wf.yml")
        case = mp.StepDeletion(
            name="c",
            layer="3-wiring",
            target=target,
            step_marker="not there",
            argv=[sys.executable, "-c", "raise SystemExit(1)"],
            expect_exit=1,
            expect="",
        )
        assert case.apply() is False

    def test_file_removal_moves_and_restores(self, tmp_path):
        target = _file(tmp_path, "content\n", name="checker.py")
        case = mp.FileRemoval(
            name="c",
            layer="3-wiring",
            target=target,
            argv=[sys.executable, "-c", "raise SystemExit(1)"],
            expect_exit=1,
            expect="",
        )
        assert case.apply() is True
        assert not target.is_file()
        case.restore("content\n")
        assert target.read_text(encoding="utf-8") == "content\n"


# --------------------------------------------------------------------------- #
# fingerprinting and the restore guarantee
# --------------------------------------------------------------------------- #
class TestFingerprint:
    def test_stable_without_changes(self, git_repo):
        assert mp.fingerprint(git_repo) == mp.fingerprint(git_repo)

    def test_tracked_edit_changes_it(self, git_repo):
        before = mp.fingerprint(git_repo)
        (git_repo / "tracked.txt").write_text("two\n", encoding="utf-8")
        assert mp.fingerprint(git_repo) != before

    def test_untracked_file_changes_it(self, git_repo):
        before = mp.fingerprint(git_repo)
        (git_repo / "new.txt").write_text("x\n", encoding="utf-8")
        assert mp.fingerprint(git_repo) != before


class TestRunOne:
    def test_is_ok_when_the_case_behaves(self, git_repo):
        case = mp.Case(
            name="c",
            layer="3-wiring",
            target=git_repo / "tracked.txt",
            old="one",
            new="two",
            argv=[sys.executable, "-c", "print('marker-boom'); raise SystemExit(1)"],
            expect_exit=1,
            expect="marker-boom",
        )
        result = mp.run_one(case, git_repo)
        assert result.ok is True, result.detail
        assert result.skipped_reason is None

    def test_noop_is_reported_as_failure_not_pass(self, git_repo):
        case = mp.Case(
            name="c",
            layer="3-wiring",
            target=git_repo / "tracked.txt",
            old="absent-anchor",
            new="two",
            argv=[sys.executable, "-c", "raise SystemExit(1)"],
            expect_exit=1,
            expect="",
        )
        result = mp.run_one(case, git_repo)
        assert result.ok is False
        assert result.skipped_reason == "mutation-noop"

    def test_restore_mismatch_is_caught(self, git_repo):
        class StickyMutation(mp.Case):
            def restore(self, before: str) -> None:  # deliberately broken restore
                pass

        case = StickyMutation(
            name="c",
            layer="3-wiring",
            target=git_repo / "tracked.txt",
            old="one",
            new="left-behind",
            argv=[sys.executable, "-c", "print('boom'); raise SystemExit(1)"],
            expect_exit=1,
            expect="boom",
        )
        result = mp.run_one(case, git_repo)
        assert result.ok is False
        assert result.skipped_reason == "restore-mismatch", (
            "a mutation left behind in the tree must never report success — that mutated "
            "file would then be committed and read green forever"
        )

    def test_expected_but_passed_is_flagged_when_a_mutation_survives(self, git_repo):
        case = mp.Case(
            name="c",
            layer="2-classifier",
            target=git_repo / "tracked.txt",
            old="one",
            new="two",
            argv=[sys.executable, "-c", "print('gate says all good')"],
            expect_exit=1,
            expect="anything",
        )
        result = mp.run_one(case, git_repo)
        assert result.ok is False
        assert result.expected_but_passed is True, "a mutation the gate survived must be reported"

    def test_baselines_are_never_flagged_as_surviving_mutations(self, git_repo):
        case = mp.Baseline(
            name="c",
            layer="0-baseline",
            target=git_repo / "tracked.txt",
            argv=[sys.executable, "-c", "print('green')"],
            expect_exit=0,
            expect="green",
        )
        result = mp.run_one(case, git_repo)
        assert result.ok is True and result.expected_but_passed is False


class TestReport:
    def _result(self, name: str, ok: bool, **kw) -> mp.Result:
        return mp.Result(name=name, layer="2-classifier", ok=ok, detail="d", **kw)

    def test_zero_only_when_all_ok_and_count_matches(self):
        results = [self._result(f"c{i}", True) for i in range(mp.EXPECTED_CASES)]
        assert mp.report(results, []) == 0

    def test_one_bad_case_fails_the_report(self, capsys):
        results = [self._result(f"c{i}", True) for i in range(mp.EXPECTED_CASES - 1)]
        results.append(self._result("broken", False))
        assert mp.report(results, []) == 1
        assert "cases_failed=1" in capsys.readouterr().out

    def test_short_case_list_fails_the_report(self):
        assert mp.report([self._result("c", True)], []) == 1, (
            "a harness that runs fewer cases than expected must not report success"
        )

    def test_noop_and_restore_flags_are_printed(self, capsys):
        results = [self._result("c", False, skipped_reason="mutation-noop")]
        mp.report(results, ["expected_but_passed: x"])
        out = capsys.readouterr().out
        assert "mutation-noop: c" in out
        assert "expected_but_passed: x" in out
