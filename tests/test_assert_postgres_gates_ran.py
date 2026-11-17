"""Tests for tools/assert_postgres_gates_ran.py — the Postgres anti-skip gate.

Why this file exists
--------------------
t_3e8a5037: the ``random-order pytest (postgres, non-blocking)`` leg runs pytest
WITHOUT ``-v``/``-rA``, so a passing test never prints its node id and the leg's
only in-log signal is the absence of a ``FAILED`` line. Absence-of-red is not
evidence of execution: if that leg's postgres service or DSN broke, the ~634
``postgres_only`` tests would all skip and the leg would still read ``success``.

The tool under test turns that into a positive assertion. These tests are what
prove it is load-bearing rather than decorative, and they deliberately cover the
"gate itself is broken" shapes (empty list, unusable evidence) with a distinct
exit code — a green anti-skip gate that certified nothing is the exact bug class
this exists to kill (F-4, t_3e8a5037).

Two layers:
  * behavioural — synthetic junit/-rA documents, every non-PASS shape.
  * structural  — the required list still names real tests, both scopes still
    select something, the documented ceilings still sit between the measured
    baseline and the measured total-collapse fingerprint, and BOTH workflows
    actually invoke the tool (the "shipped a gate wired to nothing" rot class).
"""

from __future__ import annotations

import ast
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import assert_postgres_gates_ran as gate  # noqa: E402

REQUIRED_FILE = REPO_ROOT / "tools" / "postgres_gate_required.txt"
CHECKER = REPO_ROOT / "tools" / "assert_postgres_gates_ran.py"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
RANDOM_ORDER_YML = REPO_ROOT / ".github" / "workflows" / "random-order.yml"

# The two proofs this gate exists to protect (issue282 RED-proof). Named here on
# purpose: test_named_proofs_are_required_in_both_scopes below fails if they ever
# vanish from the shared list, so the constants cannot silently go stale.
REDPROOF_SEQ_SCAN = (
    "tests/migrations/test_issue282_fed_hub_trgm.py::"
    "TestRedProofLegacyThreeIndexDesignWasBroken::test_three_column_indexes_with_or_still_seq_scans"
)
REDPROOF_IMMUNE = (
    "tests/migrations/test_issue282_fed_hub_trgm.py::"
    "TestRedProofLegacyThreeIndexDesignWasBroken::test_red_proof_is_immune_to_shared_table_state"
)
PARAMETISED = (
    "tests/migrations/test_issue282_fed_hub_trgm.py::"
    "TestQueryResultEquivalence::test_new_expression_matches_original_or_clauses[Skill]"
)
CHAIN_BASELINE = "tests/migrations/test_chain_postgres.py::test_alembic_upgrade_head_from_baseline_postgres"

# Measured on run 43985349634 (main @ e55afea, 2026-11-17) from the uploaded
# junit artifacts — the numbers the random-order ceilings are derived from:
#   postgres leg: tests=9328 passed=9179 skipped=148 failures=1 errors=0
#   sqlite   leg: tests=9328 passed=8546 skipped=782 failures=0 errors=0
# If the postgres service/DSN dies, the postgres leg collapses onto the sqlite
# fingerprint: skipped -> 782. A ceiling must sit strictly between 148 and 782.
MEASURED_PG_SKIPPED = 148
MEASURED_COLLAPSE_SKIPPED = 782
MEASURED_TESTS = 9328


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _junit_xml(
    cases: list[dict], *, tests: int | None = None, skipped: int = 0, failures: int = 0, errors: int = 0
) -> str:
    """Render a junit document in pytest's own shape.

    ``cases`` entries: ``{classname, name, status}``. ``status`` is one of
    ``passed``/``skipped``/``failed``/``error`` — note junit's own tag for a
    failure is ``<failure>``, which is exactly what the classifier must read;
    anything else it does not recognise is deliberately treated as not-passed.
    An ``xml.etree.ElementTree.Element`` status is emitted verbatim, used by the
    unknown-child test.
    """
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<testsuites name="pytest tests">',
        f'<testsuite name="pytest" tests="{tests if tests is not None else len(cases)}" '
        f'errors="{errors}" failures="{failures}" skipped="{skipped}" time="1.0">',
    ]
    for case in cases:
        head = f'  <testcase classname="{case["classname"]}" name="{case["name"]}" time="0.1"'
        status = case.get("status", "passed")
        if status == "passed":
            lines.append(head + " />")
        elif isinstance(status, ET.Element):  # pre-built exotic child
            lines.append(head + f'><{status.tag} message="x" /></testcase>')
        elif status == "failed":
            lines.append(head + '><failure message="assert False" /></testcase>')
        elif status == "skipped":
            lines.append(head + '><skipped type="pytest.skip" message="no reachable Postgres" /></testcase>')
        elif status == "error":
            lines.append(head + '><error message="teardown failed" /></testcase>')
        else:
            raise AssertionError(f"unknown status in fixture: {status!r}")
    lines.append("</testsuite>")
    lines.append("</testsuites>")
    return "\n".join(lines)


def _case_for(nodeid: str, status: str = "passed") -> dict:
    classname, name = gate.junit_pair(nodeid)
    return {"classname": classname, "name": name, "status": status}


def _write(tmp_path: Path, text: str, name: str = "junit-random-order-postgres.xml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _evidence(tmp_path: Path, cases: list[dict], **attrs) -> gate.Evidence:
    return gate.read_evidence(_write(tmp_path, _junit_xml(cases, **attrs)))


def _run(tmp_path: Path, cases: list[dict], *, scope="random-order", **flags) -> tuple[int, str]:
    """Invoke the CLI the way the workflows do; return (exit_code, output+err)."""
    import contextlib
    import io

    path = _write(tmp_path, _junit_xml(cases, **{"tests": len(cases)}))
    argv = ["--evidence", str(path), "--scope", scope, "--required", str(REQUIRED_FILE)]
    for key, value in flags.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = gate.main(argv)
    return code, out.getvalue() + err.getvalue()


def _required() -> list[gate.Required]:
    return gate.parse_required(REQUIRED_FILE)


# --------------------------------------------------------------------------- #
# behaviour: every non-PASS shape must redden the gate
# --------------------------------------------------------------------------- #
class TestJunitStatuses:
    def test_all_required_passed_is_green(self, tmp_path):
        cases = [_case_for(entry.nodeid, "passed") for entry in gate.select(_required(), "random-order")]
        code, text = _run(tmp_path, cases, max_skipped=400, min_tests=len(cases))
        assert code == gate.EXIT_OK, text
        assert "All 9 named Postgres gates ran and passed." in text

    def test_required_test_skipped_fails_and_names_it(self, tmp_path):
        cases = [_case_for(entry.nodeid, "passed") for entry in gate.select(_required(), "random-order")]
        cases[0]["status"] = "skipped"
        code, text = _run(tmp_path, cases, max_skipped=400, min_tests=len(cases))
        assert code == gate.EXIT_VIOLATION
        assert "SKIPPED, not executed" in text
        assert REDPROOF_SEQ_SCAN in text

    def test_required_test_missing_fails(self, tmp_path):
        cases = [_case_for(entry.nodeid, "passed") for entry in gate.select(_required(), "random-order")]
        dropped = cases.pop(1)
        code, text = _run(tmp_path, cases, max_skipped=400, min_tests=len(cases))
        assert code == gate.EXIT_VIOLATION
        assert "never collected" in text
        assert dropped["name"] in text

    def test_required_test_failed_fails(self, tmp_path):
        cases = [_case_for(entry.nodeid, "passed") for entry in gate.select(_required(), "random-order")]
        cases[2]["status"] = "failed"
        code, text = _run(tmp_path, cases, max_skipped=400, min_tests=len(cases))
        assert code == gate.EXIT_VIOLATION
        assert "FAILED" in text
        assert "test_explain_shows_index_scan_not_seq_scan" in text

    def test_required_test_errored_fails(self, tmp_path):
        cases = [_case_for(entry.nodeid, "passed") for entry in gate.select(_required(), "random-order")]
        cases[2]["status"] = "error"
        code, text = _run(tmp_path, cases, max_skipped=400, min_tests=len(cases))
        assert code == gate.EXIT_VIOLATION
        assert "ERRORED" in text

    def test_teardown_error_beats_a_passing_body(self, tmp_path):
        """A duplicate <testcase> with <error> must not be out-voted by the pass."""
        passing = _case_for(REDPROOF_SEQ_SCAN, "passed")
        teardown_error = dict(passing, status="error")
        cases = [_case_for(entry.nodeid, "passed") for entry in gate.select(_required(), "random-order")]
        cases.append(teardown_error)
        code, text = _run(tmp_path, cases, max_skipped=400, min_tests=len(cases))
        assert code == gate.EXIT_VIOLATION, text
        assert "ERRORED" in text

    def test_unknown_child_is_never_read_as_passed(self, tmp_path):
        cases = [_case_for(entry.nodeid, "passed") for entry in gate.select(_required(), "random-order")]
        cases[0]["status"] = ET.Element("flaky")
        code, text = _run(tmp_path, cases, max_skipped=400, min_tests=len(cases))
        assert code == gate.EXIT_VIOLATION, text

    def test_system_out_child_does_not_redden(self, tmp_path):
        cases = [_case_for(entry.nodeid, "passed") for entry in gate.select(_required(), "random-order")]
        cases[0]["status"] = ET.Element("system-out")
        code, text = _run(tmp_path, cases, max_skipped=400, min_tests=len(cases))
        assert code == gate.EXIT_OK, text

    def test_parametrised_node_id_matches_junit_attrs(self, tmp_path):
        """`test_x[Skill]` must be found even though junit splits it across attrs."""
        evidence = _evidence(tmp_path, [_case_for(PARAMETISED, "passed")])
        assert evidence.status_of(PARAMETISED) == gate.PASSED
        assert evidence.status_of(PARAMETISED.replace("[Skill]", "[nope]")) == gate.MISSING

    def test_node_id_without_py_suffix_still_matches(self, tmp_path):
        """ci.yml's log spells ids with .py; junit splits them — both must resolve."""
        evidence = _evidence(tmp_path, [_case_for(REDPROOF_IMMUNE, "passed")])
        assert evidence.status_of("./" + REDPROOF_IMMUNE) == gate.PASSED


def _pad(count: int, status: str = "skipped") -> list[dict]:
    """``count`` synthetic testcases with unique names, to reproduce measured totals.

    The status is part of the class name so a skipped pad and a passed pad can
    never collide on ``(classname, name)`` — a collision would let the
    worst-status-wins merge silently turn the passed filler into skips.
    """
    return [
        {"classname": f"tests.test_pad_{status}_{index // 50}", "name": f"test_pad_{index}", "status": status}
        for index in range(count)
    ]


def _named(scope: str, status: str) -> list[dict]:
    return [_case_for(entry.nodeid, status) for entry in gate.select(_required(), scope)]


def _scope_only(scope: str) -> list[gate.Required]:
    """Entries tagged for exactly this scope — excludes the ``all`` ones.

    Needed because the 9 issue282 proofs carry scope `all`, i.e. they belong to
    BOTH scopes; re-listing them under `migrations` would emit a second
    <testcase> for the same node id and let worst-status-wins overwrite the
    status the fixture is trying to assert.
    """
    return [entry for entry in gate.select(_required(), scope) if entry.scopes == frozenset({scope})]


def _leg_document(
    named_status: str = "skipped", total_skipped: int = MEASURED_COLLAPSE_SKIPPED
) -> list[dict]:
    """A junit case list reproducing the measured leg totals (9328 collected).

    Measured on run 43985349634 (main @ e55afea): the healthy postgres leg is
    9328 collected / 148 skipped. A dead service or DSN collapses the leg onto
    the sqlite fingerprint — 9328 / 782 skipped — because the ~634
    ``postgres_only`` tests skip as well. Built from those measured numbers so
    the ceilings are tested against the real shapes rather than guesses.

    ``named_status`` is what the REQUIRED random-order gates report; the
    WITH_POSTGRES-gated ones always skip on this leg (that env is never set
    here), which is why they are rendered as skips.
    """
    cases = _named("random-order", named_status) + [
        _case_for(e.nodeid, "skipped") for e in _scope_only("migrations")
    ]
    unique: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for case in cases:
        key = (case["classname"], case["name"], case["status"])
        if key not in seen:
            seen.add(key)
            unique.append(case)
    skipped = sum(1 for case in unique if case["status"] == "skipped")
    passed = len(unique) - skipped
    assert total_skipped >= skipped, "fixture asks for fewer skips than the named tests produce"
    unique += _pad(total_skipped - skipped, "skipped")
    unique += _pad(MEASURED_TESTS - total_skipped - passed, "passed")
    return unique


# --------------------------------------------------------------------------- #
# behaviour: the "everything silently skipped" / "nothing collected" shapes
# --------------------------------------------------------------------------- #
class TestTotalsFloors:
    def test_the_collapse_fixture_is_the_measured_shape(self, tmp_path):
        """Ground the fixture: it must reproduce the measured sqlite fingerprint."""
        evidence = _evidence(tmp_path, _leg_document())
        assert evidence.totals.tests == MEASURED_TESTS
        assert evidence.totals.skipped == MEASURED_COLLAPSE_SKIPPED
        assert evidence.totals.passed == MEASURED_TESTS - MEASURED_COLLAPSE_SKIPPED

    def test_total_collapse_is_caught_by_the_named_list_and_the_ceiling(self, tmp_path):
        code, text = _run(
            tmp_path, _leg_document("skipped", MEASURED_COLLAPSE_SKIPPED), max_skipped=400, min_tests=8500
        )
        assert code == gate.EXIT_VIOLATION
        assert "SKIPPED, not executed" in text
        assert "exceeds the documented ceiling 400" in text

    def test_ceiling_alone_catches_it_even_with_a_hollow_list(self, tmp_path):
        """The belt: named gates all look fine, only the totals give it away."""
        code, text = _run(
            tmp_path, _leg_document("passed", MEASURED_COLLAPSE_SKIPPED), max_skipped=400, min_tests=8500
        )
        assert code == gate.EXIT_VIOLATION, text
        assert "exceeds the documented ceiling 400" in text
        assert "SKIPPED, not executed" not in text

    def test_measured_baseline_is_under_the_shipped_ceiling(self, tmp_path):
        """The measured healthy postgres leg (148 skipped) must pass the ceiling."""
        code, text = _run(
            tmp_path, _leg_document("passed", MEASURED_PG_SKIPPED), max_skipped=400, min_tests=8500
        )
        assert code == gate.EXIT_OK, text

    def test_min_tests_floor_catches_a_stalled_collection(self, tmp_path):
        code, text = _run(
            tmp_path, _named("random-order", "passed"), max_skipped=400, min_tests=MEASURED_TESTS
        )
        assert code == gate.EXIT_VIOLATION
        assert "below the collection floor" in text

    def test_declared_totals_mismatch_is_reported_not_fatal(self, tmp_path):
        cases = _named("random-order", "passed")
        evidence = gate.read_evidence(_write(tmp_path, _junit_xml(cases, tests=99999, skipped=7)))
        assert any("testsuite@tests=99999" in note for note in evidence.totals.notes)
        assert evidence.totals.tests == len(cases)
        verdict = gate.evaluate(evidence, gate.select(_required(), "random-order"), label="random-order")
        assert verdict.ok, verdict.lines


# --------------------------------------------------------------------------- #
# behaviour: unusable evidence exits 2, never 0
# --------------------------------------------------------------------------- #
class TestUnusableEvidenceIsNotGreen:
    def test_missing_evidence_file_exits_2(self, tmp_path):
        argv = [
            "--evidence",
            str(tmp_path / "nope.xml"),
            "--scope",
            "random-order",
            "--required",
            str(REQUIRED_FILE),
        ]
        assert gate.main(argv) == gate.EXIT_UNUSABLE

    def test_malformed_xml_exits_2(self, tmp_path):
        path = _write(tmp_path, "<testsuites><testsuite")
        with pytest.raises(gate.GateUsageError):
            gate.read_evidence(path)

    def test_zero_testcase_exits_2(self, tmp_path):
        path = _write(tmp_path, _junit_xml([]))
        with pytest.raises(gate.GateUsageError, match="zero <testcase>"):
            gate.read_evidence(path)

    def test_log_without_summary_lines_exits_2(self, tmp_path):
        path = _write(tmp_path, "platform linux -- Python 3.12.3\nno summary here\n", name="run.log")
        with pytest.raises(gate.GateUsageError, match="summary lines"):
            gate.read_evidence(path)

    def test_totals_flags_against_a_log_exits_2(self, tmp_path):
        path = _write(tmp_path, "PASSED tests/x.py::test_y\n", name="run.log")
        argv = [
            "--evidence",
            str(path),
            "--scope",
            "random-order",
            "--required",
            str(REQUIRED_FILE),
            "--max-skipped",
            "400",
        ]
        assert gate.main(argv) == gate.EXIT_UNUSABLE

    def test_missing_required_list_exits_2(self, tmp_path):
        cases = [_case_for(entry.nodeid, "passed") for entry in gate.select(_required(), "random-order")]
        path = _write(tmp_path, _junit_xml(cases))
        argv = [
            "--evidence",
            str(path),
            "--scope",
            "random-order",
            "--required",
            str(tmp_path / "absent.txt"),
        ]
        assert gate.main(argv) == gate.EXIT_UNUSABLE

    def test_malformed_required_line_exits_2(self, tmp_path):
        path = tmp_path / "req.txt"
        path.write_text("random-order this-has-no-separators\n", encoding="utf-8")
        with pytest.raises(gate.GateUsageError, match="not a pytest node id"):
            gate.parse_required(path)

    def test_unknown_scope_token_exits_2(self, tmp_path):
        path = tmp_path / "req.txt"
        path.write_text("migrrations tests/x.py::test_y\n", encoding="utf-8")
        with pytest.raises(gate.GateUsageError, match="unknown scope"):
            gate.parse_required(path)


# --------------------------------------------------------------------------- #
# anti-vacuity: an emptied / mis-tagged list can never certify "nothing"
# --------------------------------------------------------------------------- #
class TestAntiVacuity:
    def test_scope_selecting_zero_tests_is_fatal(self, tmp_path):
        path = tmp_path / "req.txt"
        path.write_text("migrations tests/x.py::test_y\n", encoding="utf-8")
        with pytest.raises(gate.GateUsageError, match="ZERO required tests"):
            gate.select(gate.parse_required(path), "random-order")

    def test_empty_list_is_fatal(self, tmp_path):
        path = tmp_path / "req.txt"
        path.write_text("# everything deleted\n", encoding="utf-8")
        with pytest.raises(gate.GateUsageError, match="empty"):
            gate.parse_required(path)

    def test_shipped_list_selects_for_both_scopes(self):
        entries = _required()
        assert gate.select(entries, "random-order"), "random-order scope must never be empty"
        assert gate.select(entries, "migrations"), "migrations scope must never be empty"

    def test_named_proofs_are_required_in_both_scopes(self):
        entries = {entry.nodeid: entry.scopes for entry in _required()}
        for nodeid in (REDPROOF_SEQ_SCAN, REDPROOF_IMMUNE, PARAMETISED):
            assert nodeid in entries, f"{nodeid} vanished from the shared required list"
            assert {"migrations", "random-order"} <= entries[nodeid]

    def test_wth_postgres_gated_tests_are_not_asserted_in_random_order(self):
        """test_chain_postgres/*_psycopg2 skip on the random-order leg (no
        WITH_POSTGRES there), so asserting them there would be a permanent
        false-red. This pins that decision against a well-meaning `all` retag."""
        entries = {entry.nodeid: entry.scopes for entry in _required()}
        assert CHAIN_BASELINE in entries
        assert "random-order" not in entries[CHAIN_BASELINE]
        assert "migrations" in entries[CHAIN_BASELINE]

    def test_no_duplicate_entries(self):
        nodeids = [entry.nodeid for entry in _required()]
        assert len(nodeids) == len(set(nodeids)), "duplicate node ids in the shared list"


# --------------------------------------------------------------------------- #
# structural: the list names tests that still exist
# --------------------------------------------------------------------------- #
def _ast_symbols(path: Path) -> tuple[set[str], dict[str, set[str]]]:
    """Return (top-level function names, {class name: {method names}})."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions: set[str] = set()
    classes: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            functions.add(node.name)
        elif isinstance(node, ast.ClassDef):
            classes[node.name] = {
                n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    return functions, classes


class TestRequiredListPointsAtRealTests:
    """A stale node id is a silent-green machine: it can only ever be MISSING,
    which reddens the gate but tells nobody the test was renamed. Fail here,
    locally, instead."""

    @pytest.mark.parametrize("entry", _required(), ids=lambda e: e.nodeid.split("::")[-1][:60])
    def test_node_id_exists_in_the_repo(self, entry):
        file_part, *tail = entry.nodeid.split("::")
        target = REPO_ROOT / file_part
        assert target.is_file(), f"{entry.source}: {file_part} does not exist"
        functions, classes = _ast_symbols(target)
        if len(tail) == 1:
            assert tail[0].split("[")[0] in functions, f"{entry.source}: no such function {tail[0]}"
            return
        klass, method = tail[0], tail[1]
        assert klass in classes, f"{entry.source}: no such class {klass} in {file_part}"
        assert method.split("[")[0] in classes[klass], f"{entry.source}: no such method {method}"


# --------------------------------------------------------------------------- #
# structural: the ceilings stay where the measurements put them
# --------------------------------------------------------------------------- #
def _workflow_flags(path: Path, needle: str) -> dict[str, str]:
    """Pull `--flag value` pairs out of the workflow step that runs `needle`."""
    import re

    text = path.read_text(encoding="utf-8")
    blocks = [block for block in text.split("- name:") if needle in block]
    assert len(blocks) == 1, f"expected exactly one step calling {needle} in {path.name}, found {len(blocks)}"
    block = blocks[0]
    return dict(re.findall(r"--([a-z-]+)\s+([^\s\\]+)", block))


class TestShippedCeilingsAreDefensible:
    def test_random_order_calls_the_checker_on_the_postgres_leg_only(self):
        text = RANDOM_ORDER_YML.read_text(encoding="utf-8")
        assert "assert_postgres_gates_ran.py" in text
        assert "--scope random-order" in text
        # The assertion must be confined to the postgres matrix leg; on sqlite
        # these tests legitimately skip and a gate there would be a permanent red.
        assert "matrix.engine == 'postgres'" in text

    def test_random_order_ceilings_sit_between_baseline_and_collapse(self):
        flags = _workflow_flags(RANDOM_ORDER_YML, "assert_postgres_gates_ran.py")
        max_skipped = int(flags["max-skipped"])
        min_tests = int(flags["min-tests"])
        assert MEASURED_PG_SKIPPED < max_skipped < MEASURED_COLLAPSE_SKIPPED, (
            f"--max-skipped={max_skipped} must sit strictly between the measured postgres "
            f"baseline ({MEASURED_PG_SKIPPED}) and the total-collapse fingerprint "
            f"({MEASURED_COLLAPSE_SKIPPED})"
        )
        # Room for real test growth, but far below the measured collection.
        assert 0.8 * MEASURED_TESTS <= min_tests <= MEASURED_TESTS

    def test_migrations_job_still_calls_the_same_checker(self):
        text = CI_YML.read_text(encoding="utf-8")
        assert "assert_postgres_gates_ran.py" in text
        assert "--scope migrations" in text
        assert "--evidence postgres-migrations.log" in text
        # The single-source-of-truth list must be referenced by both, and no
        # workflow may keep a private REQUIRED = [...] list again.
        for path in (CI_YML, RANDOM_ORDER_YML):
            assert "REQUIRED = [" not in path.read_text(encoding="utf-8"), (
                f"{path.name} keeps an inline required list — that is the duplication "
                "that let the random-order leg go uncertified (t_3e8a5037)"
            )

    def test_checker_exists_and_is_executable_source(self):
        assert CHECKER.is_file()
        ast.parse(CHECKER.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# behaviour: -rA log mode (ci.yml's migrations job)
# --------------------------------------------------------------------------- #
class TestLogMode:
    def _log(self, tmp_path, lines: list[str]) -> Path:
        return _write(tmp_path, "\n".join(lines) + "\n", name="postgres-migrations.log")

    def test_all_passed_lines_are_green(self, tmp_path):
        entries = gate.select(_required(), "migrations")
        path = self._log(tmp_path, [f"PASSED {entry.nodeid}" for entry in entries])
        assert gate.read_evidence(path).status_of(entries[0].nodeid) == gate.PASSED

    def test_missing_line_is_a_violation(self, tmp_path):
        entries = gate.select(_required(), "migrations")
        kept = [f"PASSED {entry.nodeid}" for entry in entries if entry.nodeid != CHAIN_BASELINE]
        verdict = gate.evaluate(
            gate.read_evidence(self._log(tmp_path, kept)),
            entries,
            label="migrations-postgres",
        )
        assert not verdict.ok
        assert any(CHAIN_BASELINE in line and "never collected" in line for line in verdict.lines)

    def test_skipped_line_is_a_violation(self, tmp_path):
        entries = gate.select(_required(), "migrations")
        lines = [f"PASSED {entry.nodeid}" for entry in entries]
        lines[0] = f"SKIPPED {entries[0].nodeid}"
        verdict = gate.evaluate(gate.read_evidence(self._log(tmp_path, lines)), entries, label="migrations")
        assert not verdict.ok
        assert any("SKIPPED, not executed" in line for line in verdict.lines)

    def test_failed_line_with_message_is_a_violation(self, tmp_path):
        entries = gate.select(_required(), "migrations")
        lines = [f"PASSED {entry.nodeid}" for entry in entries]
        lines[3] = f"FAILED {entries[3].nodeid} - AssertionError: boom"
        verdict = gate.evaluate(gate.read_evidence(self._log(tmp_path, lines)), entries, label="migrations")
        assert not verdict.ok
        assert any("FAILED" in line for line in verdict.lines)

    def test_verbose_body_lines_do_not_count_as_passed(self, tmp_path):
        """`-v` body lines read `tests/x.py::test_y PASSED [ 12%]` — not a summary."""
        entries = gate.select(_required(), "migrations")
        noise = [f"{entry.nodeid} PASSED [ 12%]" for entry in entries]
        with pytest.raises(gate.GateUsageError, match="summary lines"):
            gate.read_evidence(self._log(tmp_path, noise))
