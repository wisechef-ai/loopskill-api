#!/usr/bin/env python3
"""Build ci-proof.json — a machine-readable receipt of what CI actually verified.

moneypath G8 (build half): a green checkmark proves nothing by itself. This
script collects the FACTS of a CI run on a specific commit — test counts
actually collected and passed, lint/typecheck status, commit SHA, run URL —
into one artifact so a reviewer can later ask "what did CI actually prove on
this SHA?" and get a factual answer.

stdlib-only: runs on any CI Python with no pip install.

Usage:
  python scripts/ci_proof.py build \
      --junit-glob "test-results/*.xml" \
      --status lint:pass --status typecheck:fail \
      --output ci-proof.json

  python scripts/ci_proof.py verify ci-proof.json   # exits 2 if malformed
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

SCHEMA_VERSION = 1


def _parse_junit(path: str) -> dict:
    totals = dict(tests=0, passed=0, failures=0, errors=0, skipped=0)
    tree = ET.parse(path)
    root = tree.getroot()
    # JUnit XML: counters live on <testsuite> (pytest) or on the child
    # <testsuite> nodes of a <testsuites> root (some runners). A testsuites
    # root may ALSO carry aggregates — counting both double-counts, so pick
    # exactly one level: children if they carry counts, else the root.
    if root.tag == "testsuites":
        children_with_counts = [c for c in root if c.get("tests") is not None]
        nodes = children_with_counts if children_with_counts else [root]
    else:
        nodes = [root]
    for node in nodes:
        for key in totals:
            raw = node.get(key)
            if raw is None:
                continue
            try:
                totals[key] += int(raw)
            except ValueError:
                pass
    # pytest reports "failures" as an ERROR-attribute on the root when the
    # run itself crashed; be conservative and keep the sum.
    return totals


def _collect_junit(patterns: list[str]) -> dict:
    suites: list[dict] = []
    agg = dict(tests=0, passed=0, failures=0, errors=0, skipped=0)
    files: list[str] = []
    for pattern in patterns:
        files += sorted(glob.glob(pattern))
    for path in files:
        try:
            t = _parse_junit(path)
        except (ET.ParseError, OSError) as exc:
            suites.append({"file": path, "error": str(exc)})
            continue
        suites.append({"file": path, **t})
        for key in agg:
            agg[key] += t[key]
    # If the runner does not emit per-test "passed", derive it.
    if agg["passed"] == 0 and agg["tests"] > 0:
        agg["passed"] = max(
            0, agg["tests"] - agg["failures"] - agg["errors"] - agg["skipped"]
        )
    return {"junit_files": suites, "totals": agg}


def _parse_status(items: list[str]) -> dict:
    out: dict = {}
    for item in items:
        name, _, value = item.partition(":")
        value = (value or "").strip().lower()
        if value not in ("pass", "fail", "skipped", "not-run"):
            raise SystemExit(
                f"ci_proof: bad --status '{item}' (want name:pass|fail|skipped|not-run)"
            )
        out[name.strip()] = value
    return out


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def build(args: argparse.Namespace) -> int:
    gha = _env("GITHUB_ACTIONS") == "true"
    junit = _collect_junit(args.junit_glob or [])
    statuses = _parse_status(args.status or [])

    # A proof that claims passing tests with zero collected tests is exactly
    # the fake green checkmark this artifact exists to kill — refuse it.
    t = junit["totals"]
    if t["tests"] == 0 and any(v == "pass" for k, v in statuses.items() if k.startswith("tests")):
        print(
            "ci_proof: REFUSING to certify 'tests:pass' — 0 tests collected "
            "from JUnit XML. Point --junit-glob at real reports.",
            file=sys.stderr,
        )
        return 2

    proof = {
        "schema": "ci.proof/1",
        "schema_version": SCHEMA_VERSION,
        "repo": _env("GITHUB_REPOSITORY"),
        "commit_sha": _env("GITHUB_SHA"),
        "commit_short": _env("GITHUB_SHA", "")[:12],
        "branch": _env("GITHUB_REF_NAME"),
        "run_id": _env("GITHUB_RUN_ID"),
        "run_url": (
            f"{_env('GITHUB_SERVER_URL')}/{_env('GITHUB_REPOSITORY')}"
            f"/actions/runs/{_env('GITHUB_RUN_ID')}"
            if gha and _env("GITHUB_RUN_ID")
            else None
        ),
        "workflow": _env("GITHUB_WORKFLOW"),
        "job": _env("GITHUB_JOB"),
        "runner": _env("RUNNER_NAME"),
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "tests": {
            "collected": t["tests"],
            "passed": t["passed"],
            "failed": t["failures"] + t["errors"],
            "skipped": t["skipped"],
            "suites": junit["junit_files"],
        },
        "checks": statuses,
        "assertions": {
            "all_tests_passed": t["tests"] > 0
            and (t["failures"] + t["errors"]) == 0,
            "lint_passed": statuses.get("lint") == "pass",
            "typecheck_passed": statuses.get("typecheck") == "pass",
        },
    }
    payload = json.dumps(proof, indent=2) + "\n"
    if args.output:
        with open(args.output, "w") as fh:
            fh.write(payload)
        print(f"ci_proof: wrote {args.output}")
    else:
        sys.stdout.write(payload)
    return 0


def verify(args: argparse.Namespace) -> int:
    with open(args.path) as fh:
        try:
            proof = json.load(fh)
        except json.JSONDecodeError as exc:
            print(f"ci_proof verify: INVALID JSON: {exc}", file=sys.stderr)
            return 2
    problems: list[str] = []
    if proof.get("schema") != "ci.proof/1":
        problems.append(f"unexpected schema {proof.get('schema')!r}")
    for key in ("repo", "commit_sha", "run_url", "generated_at"):
        if not proof.get(key):
            problems.append(f"missing required field {key!r}")
    tests = proof.get("tests", {})
    for key in ("collected", "passed", "failed", "skipped"):
        if not isinstance(tests.get(key), int):
            problems.append(f"tests.{key} missing or not an int")
    checks = proof.get("checks", {})
    if not isinstance(checks, dict):
        problems.append("checks is not an object")
    if problems:
        for p in problems:
            print(f"ci_proof verify: {p}", file=sys.stderr)
        return 2
    print(
        f"ci_proof verify: OK repo={proof['repo']} sha={proof['commit_sha'][:12]} "
        f"tests={tests['passed']}/{tests['collected']} passed "
        f"checks={json.dumps(checks, sort_keys=True)}"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build")
    b.add_argument(
        "--junit-glob",
        action="append",
        default=[],
        help="glob of JUnit XML reports to collect test counts from (repeatable)",
    )
    b.add_argument(
        "--status",
        action="append",
        default=[],
        help="named check result name:pass|fail|skipped|not-run (repeatable)",
    )
    b.add_argument("--output", default=None, help="output path (default stdout)")
    b.set_defaults(fn=build)

    v = sub.add_parser("verify")
    v.add_argument("path", help="path to ci-proof.json")
    v.set_defaults(fn=verify)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
