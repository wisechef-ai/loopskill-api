#!/usr/bin/env python3
"""Certify that the named Postgres gates ACTUALLY RAN — not merely "did not fail".

Why this exists
---------------
A pytest job in which every Postgres-gated test silently SKIPS is GREEN. It
proves nothing and looks exactly like a job that ran the gates. That single
shape has burned this repo repeatedly:

* F-4 (2026-11-16): ci.yml's ``test`` job started a postgres:17 service but never
  exported WITH_POSTGRES/POSTGRES_DSN, so every Postgres-dialect test skipped and
  the check stayed green for months.
* t_3e8a5037 (2026-11-17): the ``random-order pytest (postgres, non-blocking)``
  leg runs pytest WITHOUT ``-v``/``-rA``, so a *passing* test never prints its
  node id. Its only in-log signal is the ABSENCE of a ``FAILED`` line — and
  absence-of-red is not evidence of execution. If that leg's postgres service or
  DSN ever broke, the ~634 ``postgres_only`` tests would all skip, the leg would
  read ``success``, and nothing in the repo would notice.

So the rule enforced here is positive, not negative: a named test must be
PRESENT in the evidence and report a clean PASS. Everything else — skipped,
failed, errored, or simply absent — is a hard failure.

Two evidence shapes, one required list
--------------------------------------
``--evidence`` accepts either shape, auto-detected by extension:

* a pytest **junit XML** (``--junitxml``, written in-job by the very run being
  certified — no artifact download, no network, no cross-job trust), or
* a pytest ``-rA`` **short summary log** (``PASSED <node-id>`` lines).

The names live in ONE file — ``tools/postgres_gate_required.txt`` — tagged per
scope, so ci.yml's ``migrations against Postgres`` job and random-order.yml's
postgres leg certify against a single source of truth instead of each keeping a
private copy that can rot.

Anti-vacuity
------------
Three properties make this gate load-bearing rather than decorative; each is
covered by tests/test_assert_postgres_gates_ran.py:

1. A requested scope that selects ZERO required tests is a hard error — an
   emptied list can never silently certify anything.
2. Unusable evidence (missing file, unparseable XML, zero testcases, summary log
   with no summary lines) exits 2, deliberately distinct from 1, so "the gate
   could not run" is never read as "the gate passed".
3. Totals floors (``--max-skipped`` / ``--min-tests``, junit mode) catch the
   "everything silently skipped" and "nothing was collected" shapes even for
   tests nobody thought to name.

Usage
-----
    # ci.yml, `migrations against Postgres` job (evidence: -rA log)
    python tools/assert_postgres_gates_ran.py \
        --evidence postgres-migrations.log --scope migrations

    # random-order.yml, postgres leg (evidence: in-job junit XML)
    python tools/assert_postgres_gates_ran.py \
        --evidence junit-random-order-postgres.xml --scope random-order \
        --max-skipped 400 --min-tests 8500

Exit codes
----------
* 0 — every named gate reported a clean PASS and the totals floors hold.
* 1 — anti-skip violation: a named test is missing/skipped/not-passed, or a
  totals floor was breached.
* 2 — the evidence or the required list itself is unusable (file absent,
  malformed XML, empty selection, floor flag used against a log).
"""

from __future__ import annotations

import argparse
import os
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REQUIRED = REPO_ROOT / "tools" / "postgres_gate_required.txt"

KNOWN_SCOPES = ("migrations", "random-order")
ALL_SCOPES = frozenset(KNOWN_SCOPES)

PASSED = "passed"
SKIPPED = "skipped"
FAILED = "failed"
ERRORED = "error"
MISSING = "missing"

_WHY_NOT_PASSED = {
    MISSING: "never collected (absent from the evidence)",
    SKIPPED: "SKIPPED, not executed",
    FAILED: "FAILED",
    ERRORED: "ERRORED",
}

# Worst wins when the same node id appears more than once (pytest emits one
# <testcase> for the body and another when a fixture errors during teardown).
# Taking the worst status means a passing body with an erroring teardown cannot
# read PASS.
_SEVERITY = {PASSED: 0, SKIPPED: 1, FAILED: 2, ERRORED: 3}

# junit child tags that carry no verdict. Anything NOT listed is treated as an
# error: a schema this tool does not understand must never read as "passed".
_NEUTRAL_JUNIT_CHILDREN = frozenset({"system-out", "system-err", "properties", "property", "rerun"})

# <testsuite> declares counts under junit's own names; cross-check them against
# what this tool measures from the <testcase> elements (which stay authoritative).
_JUNIT_ATTR_TO_MEASURED = {
    "tests": "tests",
    "errors": "errors",
    "failures": "failed",
    "skipped": "skipped",
}

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_UNUSABLE = 2


class GateUsageError(Exception):
    """The evidence or the required list is unusable — exit 2, not exit 1."""


@dataclass(frozen=True)
class Required:
    """One line of tools/postgres_gate_required.txt."""

    nodeid: str
    scopes: frozenset[str]
    source: str  # "filename:lineno" for error messages

    def belongs(self, scope: str) -> bool:
        return scope in self.scopes


@dataclass
class Totals:
    """Totals of an evidence document, measured from its <testcase> elements."""

    tests: int = 0
    passed: int = 0
    skipped: int = 0
    failed: int = 0
    errors: int = 0
    declared: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class Evidence:
    """Every test status the run reported.

    junit evidence is indexed twice: by the ``(classname, name)`` pair the XML
    actually carries, and by a reconstructed node id (fallback + reporting).
    """

    kind: str  # "junit" | "log"
    pairs: dict[tuple[str, str], str] = field(default_factory=dict)
    nodeids: dict[str, str] = field(default_factory=dict)
    totals: Totals = field(default_factory=Totals)

    def status_of(self, nodeid: str) -> str:
        """Status for ``nodeid``: exact junit pair first, reconstructed id second."""
        if self.kind == "junit":
            status = self.pairs.get(junit_pair(nodeid))
            if status is not None:
                return status
        return self.nodeids.get(_norm_nodeid(nodeid), MISSING)


def _worse(left: str, right: str) -> str:
    return left if _SEVERITY[left] >= _SEVERITY[right] else right


def _record(mapping: dict, key, status: str) -> None:
    mapping[key] = _worse(mapping[key], status) if key in mapping else status


# --------------------------------------------------------------------------- #
# node-id normalisation
# --------------------------------------------------------------------------- #
def _norm_nodeid(nodeid: str) -> str:
    """Canonicalise a pytest node id so log and junit spellings collide."""
    text = nodeid.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _module_dotted(file_part: str) -> str:
    """``tests/migrations/test_x.py`` -> ``tests.migrations.test_x``."""
    stem = _norm_nodeid(file_part)
    if stem.endswith(".py"):
        stem = stem[:-3]
    return stem.replace("/", ".")


def junit_pair(nodeid: str) -> tuple[str, str]:
    """Expected ``(classname, name)`` junit attributes for a pytest node id.

    Matching on the pair derived from the REQUIRED list — instead of guessing
    where junit's dotted ``classname`` stops being a module path and starts
    being a class — keeps the lookup exact for parametrised ids such as
    ``test_x[Skill]`` and for nested classes, and immune to a module that
    happens to be named ``TestSomething``.
    """
    parts = _norm_nodeid(nodeid).split("::")
    if len(parts) < 2:
        raise GateUsageError(f"node id has no '::' separator: {nodeid!r}")
    module = _module_dotted(parts[0])
    tail = parts[1:]
    if len(tail) == 1:
        return module, tail[0]
    return ".".join([module, *tail[:-1]]), tail[-1]


def junit_to_nodeid(classname: str, name: str) -> str:
    """Best-effort inverse of :func:`junit_pair`, for the fallback index.

    Heuristic: the first dot-segment beginning with an uppercase letter starts
    the class chain. Fallback only — :func:`junit_pair` is the primary key.
    """
    segments = classname.split(".") if classname else []
    cut = len(segments)
    for index, segment in enumerate(segments):
        if segment[:1].isupper():
            cut = index
            break
    path = "/".join(segments[:cut])
    if path:
        path = f"{path}.py"
    tail = ".".join(segments[cut:])
    return "::".join(part for part in (path, tail, name) if part)


# --------------------------------------------------------------------------- #
# required list
# --------------------------------------------------------------------------- #
def parse_required(path: Path) -> list[Required]:
    """Parse the shared required-test list. Blank and ``#`` lines are ignored."""
    if not path.is_file():
        raise GateUsageError(f"required list not found: {path}")
    entries: list[Required] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        source = f"{path.name}:{lineno}"
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise GateUsageError(f"{source}: expected '<scopes> <node-id>', got {raw.strip()!r}")
        scopes_raw, nodeid = parts
        scopes = _parse_scopes(scopes_raw, source)
        if "::" not in nodeid:
            raise GateUsageError(f"{source}: {nodeid!r} is not a pytest node id (missing '::')")
        # Validate the junit pairing at load time: a malformed id must fail the
        # GATE, not silently never match anything.
        junit_pair(nodeid)
        entries.append(Required(nodeid=_norm_nodeid(nodeid), scopes=scopes, source=source))
    if not entries:
        raise GateUsageError(f"required list is empty: {path}")
    return entries


def _parse_scopes(raw: str, source: str) -> frozenset[str]:
    scopes = {part.strip() for part in raw.split(",") if part.strip()}
    if not scopes:
        raise GateUsageError(f"{source}: empty scope field")
    if "all" in scopes:
        return frozenset(ALL_SCOPES)
    unknown = sorted(scopes - set(ALL_SCOPES))
    if unknown:
        raise GateUsageError(
            f"{source}: unknown scope(s) {', '.join(unknown)}; known: {', '.join(KNOWN_SCOPES)}, all"
        )
    return frozenset(scopes)


def select(entries: Iterable[Required], scope: str) -> list[Required]:
    """Entries tagged for ``scope``. An empty selection is a hard error.

    The anti-vacuity tripwire: delete the list, rename the scope, or mis-tag
    every line and the gate FAILS LOUD instead of certifying nothing.
    """
    chosen = [entry for entry in entries if entry.belongs(scope)]
    if not chosen:
        raise GateUsageError(
            f"scope {scope!r} selects ZERO required tests — refusing to certify nothing. "
            f"Known scopes: {', '.join(KNOWN_SCOPES)}, all."
        )
    return chosen


# --------------------------------------------------------------------------- #
# evidence readers
# --------------------------------------------------------------------------- #
def _classify_testcase(case: ET.Element) -> str:
    status = PASSED
    for child in case:
        tag = child.tag
        if tag == "failure":
            status = _worse(status, FAILED)
        elif tag == "error":
            status = _worse(status, ERRORED)
        elif tag == "skipped":
            status = _worse(status, SKIPPED)
        elif tag not in _NEUTRAL_JUNIT_CHILDREN:
            # Unknown child: assume the worst.
            status = _worse(status, ERRORED)
    return status


def _read_junit(path: Path) -> Evidence:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:  # malformed evidence is not a passing run
        raise GateUsageError(f"junit XML is not parseable ({path}): {exc}") from exc

    pairs: dict[tuple[str, str], str] = {}
    nodeids: dict[str, str] = {}
    cases = 0
    for case in root.iter("testcase"):
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        if not name:
            continue
        cases += 1
        status = _classify_testcase(case)
        _record(pairs, (classname, name), status)
        _record(nodeids, junit_to_nodeid(classname, name), status)

    if cases == 0:
        raise GateUsageError(f"junit XML contains zero <testcase> elements ({path})")

    declared: dict[str, str] = {}
    notes: list[str] = []
    for suite in root.iter("testsuite"):
        for key in ("tests", "errors", "failures", "skipped"):
            value = suite.get(key)
            if value is not None:
                declared[key] = value

    measured = {
        "tests": cases,
        "passed": sum(1 for value in pairs.values() if value == PASSED),
        "skipped": sum(1 for value in pairs.values() if value == SKIPPED),
        "failed": sum(1 for value in pairs.values() if value == FAILED),
        "errors": sum(1 for value in pairs.values() if value == ERRORED),
    }
    for attr, measured_key in _JUNIT_ATTR_TO_MEASURED.items():
        value = declared.get(attr)
        if value is None:
            continue
        try:
            declared_int = int(value)
        except ValueError:
            notes.append(f"testsuite@{attr}={value!r} is not an integer")
            continue
        if declared_int != measured[measured_key]:
            notes.append(f"testsuite@{attr}={declared_int} but {measured[measured_key]} <testcase> counted")

    totals = Totals(
        tests=measured["tests"],
        passed=measured["passed"],
        skipped=measured["skipped"],
        failed=measured["failed"],
        errors=measured["errors"],
        declared=declared,
        notes=notes,
    )
    return Evidence(kind="junit", pairs=pairs, nodeids=nodeids, totals=totals)


# The four pytest -rA summary verbs; mapped to this tool's statuses.
_RALOG_VERBS = {"PASSED": PASSED, "SKIPPED": SKIPPED, "FAILED": FAILED, "ERROR": ERRORED}


def _read_ralog(path: Path) -> Evidence:
    """Parse a pytest ``-rA`` short-summary log.

    Summary lines look like ``PASSED tests/x.py::test_y`` or ``FAILED
    tests/x.py::test_y - AssertionError: ...``. Everything else is ignored, and
    a log with no summary lines at all is *unusable evidence*, not a pass.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    nodeids: dict[str, str] = {}
    for line in text.splitlines():
        verb, _, rest = line.partition(" ")
        status = _RALOG_VERBS.get(verb.strip())
        if status is None or not rest.strip():
            continue
        nodeid = _norm_nodeid(rest.split(" ", 1)[0].split(" - ")[0])
        if "::" not in nodeid:
            continue
        _record(nodeids, nodeid, status)
    if not nodeids:
        raise GateUsageError(
            f"no 'PASSED|SKIPPED|FAILED|ERROR <node-id>' summary lines in {path} — "
            "was pytest run with -rA (log mode) or --junitxml (junit mode)?"
        )
    totals = Totals(
        tests=len(nodeids),
        passed=sum(1 for value in nodeids.values() if value == PASSED),
        skipped=sum(1 for value in nodeids.values() if value == SKIPPED),
        failed=sum(1 for value in nodeids.values() if value == FAILED),
        errors=sum(1 for value in nodeids.values() if value == ERRORED),
    )
    return Evidence(kind="log", nodeids=nodeids, totals=totals)


def read_evidence(path: Path, kind: str = "auto") -> Evidence:
    """Read evidence from disk; unusable evidence raises :class:`GateUsageError`."""
    if not path.is_file():
        raise GateUsageError(f"evidence file not found: {path}")
    if kind == "auto":
        kind = "junit" if path.suffix.lower() in (".xml", ".junit") else "log"
    return _read_junit(path) if kind == "junit" else _read_ralog(path)


# --------------------------------------------------------------------------- #
# the assertion
# --------------------------------------------------------------------------- #
@dataclass
class Verdict:
    """Outcome of one certification."""

    ok: bool
    exit_code: int
    lines: list[str]


def evaluate(
    evidence: Evidence,
    required: list[Required],
    *,
    label: str,
    max_skipped: int | None = None,
    min_tests: int | None = None,
) -> Verdict:
    """Compare the required list against what the run actually reported."""
    totals = evidence.totals
    lines = [
        f"evidence: {evidence.kind} — tests={totals.tests} passed={totals.passed} "
        f"skipped={totals.skipped} failed={totals.failed} errors={totals.errors}"
    ]
    lines.extend(f"  note: {note} (counted <testcase> elements are authoritative)" for note in totals.notes)

    violations: list[str] = []
    for entry in required:
        status = evidence.status_of(entry.nodeid)
        if status == PASSED:
            continue
        violations.append(f"{entry.nodeid} — {_WHY_NOT_PASSED[status]}")

    if max_skipped is not None and totals.skipped > max_skipped:
        violations.append(
            f"skipped={totals.skipped} exceeds the documented ceiling {max_skipped} — "
            "the Postgres-only suite is being silently skipped, not executed"
        )
    if min_tests is not None and totals.tests < min_tests:
        violations.append(
            f"tests={totals.tests} is below the collection floor {min_tests} — "
            "the suite did not collect (import error, bad --ignore, crashed runner?)"
        )

    if violations:
        lines.append(
            f"::error::ANTI-SKIP VIOLATION ({label}): {len(violations)} required gate(s) not proven PASSED"
        )
        lines.extend(f"  - {item}" for item in violations)
        lines.append(
            "A job where every Postgres gate skips is GREEN and proves nothing. Check the service "
            "container, the DSN env vars, and that these tests are still collected."
        )
        return Verdict(ok=False, exit_code=EXIT_VIOLATION, lines=lines)

    lines.append(
        f"All {len(required)} named Postgres gates ran and passed. "
        f"(scope={_scope_of(required)}, evidence={evidence.kind}, tests={totals.tests}, "
        f"passed={totals.passed}, skipped={totals.skipped})"
    )
    return Verdict(ok=True, exit_code=EXIT_OK, lines=lines)


def _scope_of(required: list[Required]) -> str:
    scopes: set[str] = set()
    for entry in required:
        scopes |= set(entry.scopes)
    return "+".join(sorted(scopes))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """Command-line surface (see module docstring for the two call sites)."""
    parser = argparse.ArgumentParser(
        prog="assert_postgres_gates_ran.py",
        description="Fail the job if a named Postgres gate did not report PASSED.",
    )
    parser.add_argument("--evidence", required=True, help="junit XML or pytest -rA summary log")
    parser.add_argument(
        "--scope", required=True, choices=[*KNOWN_SCOPES, "all"], help="which tags to certify"
    )
    parser.add_argument("--required", default=str(DEFAULT_REQUIRED), help="shared required-test list")
    parser.add_argument("--format", default="auto", choices=("auto", "junit", "log"), help="evidence kind")
    parser.add_argument("--label", default="", help="job label used in failure messages")
    parser.add_argument("--max-skipped", type=int, default=None, help="junit only: ceiling on skipped tests")
    parser.add_argument("--min-tests", type=int, default=None, help="junit only: floor on collected tests")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code; prints the verdict."""
    args = build_parser().parse_args(argv)
    label = args.label or args.scope
    annotate = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"
    try:
        required = select(parse_required(Path(args.required)), args.scope)
        evidence = read_evidence(Path(args.evidence), args.format)
        if evidence.kind != "junit" and (args.max_skipped is not None or args.min_tests is not None):
            raise GateUsageError(
                "--max-skipped/--min-tests need junit evidence (a -rA log has no totals); "
                "drop the flags or point --evidence at the junit XML"
            )
    except GateUsageError as exc:
        message = f"ANTI-SKIP GATE UNUSABLE ({label}): {exc}"
        print(f"::error::{message}" if annotate else message, file=sys.stderr)
        return EXIT_UNUSABLE

    verdict = evaluate(
        evidence,
        required,
        label=label,
        max_skipped=args.max_skipped,
        min_tests=args.min_tests,
    )
    for line in verdict.lines:
        if not annotate and line.startswith("::error::"):
            line = "error: " + line[len("::error::") :]
        print(line)
    return verdict.exit_code


if __name__ == "__main__":
    sys.exit(main())
