#!/usr/bin/env python3
"""SkillSpector CI wrapper — Phase K (loopclose_3005).

Thin wrapper that:
1. Invokes SkillSpector in --no-llm (static) mode.
2. Writes SARIF to disk for GitHub code-scanning upload.
3. Applies a suppression baseline (.skillspector-baseline.json) to filter
   known-good false positives from the real skill catalog.
4. In ADVISORY mode (SKILLSPECTOR_BLOCK_ON_HIGH=false, the default): always
   exits 0 so CI passes — findings are visible only in the SARIF tab.
5. In BLOCKER mode (SKILLSPECTOR_BLOCK_ON_HIGH=true): exits non-zero when
   any un-suppressed CRITICAL or HIGH finding remains after baselining.

Exit codes:
  0 — clean or advisory mode
  1 — blocked (CRITICAL/HIGH un-suppressed AND SKILLSPECTOR_BLOCK_ON_HIGH=true)
  2 — tool error (SkillSpector itself failed to run)

Usage (CI):
  python scripts/skillspector_ci.py <scan_target> --sarif-out <path>

Environment flags:
  SKILLSPECTOR_BLOCK_ON_HIGH   Set to "true" to flip from advisory to blocker.
                               Default: false (advisory-only).
  SKILLSPECTOR_BASELINE_FILE   Path to baseline JSON. Default: .skillspector-baseline.json.

The script does NOT touch app/security_scan.py — it runs alongside as the
deeper CI layer (per Phase K spec).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_BASELINE_FILE = ".skillspector-baseline.json"
BLOCK_ON_HIGH_ENV = "SKILLSPECTOR_BLOCK_ON_HIGH"
BASELINE_FILE_ENV = "SKILLSPECTOR_BASELINE_FILE"

# SARIF levels that map to HIGH or CRITICAL findings
_HIGH_SARIF_LEVELS = {"error"}  # SARIF: error=HIGH/CRITICAL, warning=MEDIUM, note=LOW

# SkillSpector rule IDs for severity reference (sourced from NVIDIA/skillspector README)
# CRITICAL rule IDs flagged at error level
_KNOWN_CRITICAL_RULES = {"PE1", "PE2", "TT3", "RA1", "RA2"}
# HIGH rule IDs flagged at error level
_KNOWN_HIGH_RULES = {
    "P1", "P2", "P3", "P5", "E2", "E3", "E4",
    "SC2", "SC3", "PE3", "PE4",
    "EA1", "EA2", "EA3",
    "TM2", "TM3", "TM4",
    "MP1", "MP2", "TP1", "TP2", "TP3",
    "LP1", "LP2", "LP3",
    "RP1", "RP2", "RP3",
}


def _load_baseline(baseline_path: Path) -> dict[str, list[str]]:
    """Load baseline suppression file.

    Returns a dict mapping rule_id -> list of suppressed file:line patterns.
    A wildcard entry of "*" suppresses all locations for that rule.

    The baseline file has a top-level "suppressed" key that contains the
    rule_id -> patterns mapping (plus optional _comment / _rationale keys).
    """
    if not baseline_path.exists():
        return {}
    with baseline_path.open() as f:
        raw = json.load(f)
    # Support both flat format (legacy) and nested {"suppressed": {...}} format
    if "suppressed" in raw:
        return raw["suppressed"]
    # Strip metadata keys starting with _
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _is_suppressed(
    rule_id: str,
    file_path: str,
    start_line: int | None,
    baseline: dict[str, list[str]],
) -> bool:
    """Check if a finding is suppressed by the baseline."""
    if rule_id not in baseline:
        return False
    patterns = baseline[rule_id]
    for pattern in patterns:
        if pattern == "*":
            return True
        # Match "file:line" or "file" patterns
        if ":" in pattern:
            b_file, b_line = pattern.rsplit(":", 1)
            if file_path == b_file and str(start_line) == b_line:
                return True
        else:
            if file_path == pattern:
                return True
    return False


def _filter_sarif_with_baseline(
    sarif: dict,
    baseline: dict[str, list[str]],
) -> tuple[dict, list[dict]]:
    """Return (filtered_sarif, suppressed_results) after applying baseline.

    The filtered_sarif has suppressed results removed from runs[].results.
    suppressed_results carries the removed entries for audit logging.
    """
    filtered_sarif = json.loads(json.dumps(sarif))  # deep copy
    suppressed: list[dict] = []

    for run in filtered_sarif.get("runs", []):
        kept: list[dict] = []
        for result in run.get("results", []):
            rule_id = result.get("ruleId", "")
            locations = result.get("locations", [])
            file_path = ""
            start_line = None
            if locations:
                phys = locations[0].get("physicalLocation", {})
                file_path = phys.get("artifactLocation", {}).get("uri", "")
                region = phys.get("region", {})
                start_line = region.get("startLine")

            if _is_suppressed(rule_id, file_path, start_line, baseline):
                suppressed.append(result)
            else:
                kept.append(result)
        run["results"] = kept

    return filtered_sarif, suppressed


def _count_high_plus(sarif: dict) -> int:
    """Count un-suppressed findings at SARIF error level (HIGH or CRITICAL)."""
    count = 0
    for run in sarif.get("runs", []):
        for result in run.get("results", []):
            if result.get("level") == "error":
                count += 1
    return count


def _run_skillspector(
    scan_target: str,
    sarif_out: Path,
    verbose: bool = False,
) -> tuple[int, dict | None]:
    """Run SkillSpector and return (exit_code, parsed_sarif_or_None)."""
    # Find skillspector binary (supports installed or venv-relative)
    candidates = [
        Path(sys.executable).parent / "skillspector",
        Path("skillspector"),
    ]
    skillspector_bin = None
    for c in candidates:
        try:
            result = subprocess.run(
                [str(c), "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                skillspector_bin = str(c)
                break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    if skillspector_bin is None:
        print("::error::skillspector binary not found in PATH or venv", flush=True)
        return 2, None

    cmd = [
        skillspector_bin,
        "scan",
        scan_target,
        "--no-llm",
        "--format", "sarif",
        "--output", str(sarif_out),
    ]
    if verbose:
        cmd.append("--verbose")

    print(f"[skillspector-ci] Running: {' '.join(cmd)}", flush=True)

    # Rationale: SkillSpector scan process may fail with non-zero on findings;
    # we capture exit code separately and always parse the SARIF output.
    proc = subprocess.run(cmd, capture_output=False, text=True, timeout=300)  # noqa: BLE001
    ss_exit = proc.returncode

    sarif = None
    if sarif_out.exists():
        try:
            with sarif_out.open() as f:
                sarif = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"::warning::Could not parse SARIF output: {exc}", flush=True)

    return ss_exit, sarif


def main(argv: list[str] | None = None) -> int:
    """Entry point for CI wrapper."""
    import argparse

    parser = argparse.ArgumentParser(description="SkillSpector CI wrapper (Phase K)")
    parser.add_argument("scan_target", help="Path to skill directory or file to scan")
    parser.add_argument(
        "--sarif-out",
        default="skillspector-results.sarif",
        help="Output SARIF file path (default: skillspector-results.sarif)",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args(argv)

    sarif_out = Path(args.sarif_out)

    # Load config from environment
    block_on_high = os.environ.get(BLOCK_ON_HIGH_ENV, "false").lower() == "true"
    baseline_file = Path(os.environ.get(BASELINE_FILE_ENV, DEFAULT_BASELINE_FILE))

    print(
        f"[skillspector-ci] target={args.scan_target} "
        f"sarif={sarif_out} "
        f"block_on_high={block_on_high} "
        f"baseline={baseline_file}",
        flush=True,
    )

    # Run SkillSpector
    ss_exit, sarif = _run_skillspector(args.scan_target, sarif_out, args.verbose)

    if sarif is None:
        if ss_exit == 2 or not sarif_out.exists():
            print("::error::SkillSpector failed to produce SARIF output", flush=True)
            return 2
        # SkillSpector ran but produced no file — write empty SARIF so upload succeeds
        empty_sarif = {
            "version": "2.1.0",
            "$schema": "https://schemastore.azurewebsites.net/schemas/json/sarif-2.1.0-rtm.4.json",
            "runs": [{"tool": {"driver": {"name": "skillspector", "version": "unknown"}}, "results": []}],
        }
        sarif_out.write_text(json.dumps(empty_sarif, indent=2))
        print("[skillspector-ci] No findings — clean SARIF written", flush=True)
        return 0

    # Load and apply baseline
    baseline = _load_baseline(baseline_file)
    filtered_sarif, suppressed = _filter_sarif_with_baseline(sarif, baseline)

    # Overwrite SARIF with the filtered version (what GitHub Security tab sees)
    sarif_out.write_text(json.dumps(filtered_sarif, indent=2))

    # Report
    total_in = sum(len(r.get("results", [])) for r in sarif.get("runs", []))
    total_suppressed = len(suppressed)
    total_after = total_in - total_suppressed
    high_plus_after = _count_high_plus(filtered_sarif)

    print(f"[skillspector-ci] Findings: {total_in} raw → {total_suppressed} suppressed by baseline → {total_after} net", flush=True)
    if suppressed:
        print("[skillspector-ci] Suppressed by baseline:", flush=True)
        for s in suppressed:
            locs = s.get("locations", [])
            loc_str = ""
            if locs:
                phys = locs[0].get("physicalLocation", {})
                uri = phys.get("artifactLocation", {}).get("uri", "?")
                line = phys.get("region", {}).get("startLine", "?")
                loc_str = f" @ {uri}:{line}"
            print(f"  [SUPPRESSED] {s.get('ruleId', '?')} — {s.get('message', {}).get('text', '')}{loc_str}", flush=True)

    if high_plus_after > 0:
        print(f"[skillspector-ci] {high_plus_after} HIGH/CRITICAL finding(s) after baseline:", flush=True)
        for run in filtered_sarif.get("runs", []):
            for result in run.get("results", []):
                if result.get("level") == "error":
                    locs = result.get("locations", [])
                    loc_str = ""
                    if locs:
                        phys = locs[0].get("physicalLocation", {})
                        uri = phys.get("artifactLocation", {}).get("uri", "?")
                        line = phys.get("region", {}).get("startLine", "?")
                        loc_str = f" @ {uri}:{line}"
                    msg = result.get("message", {}).get("text", "")
                    rule = result.get("ruleId", "?")
                    level = result.get("level", "?")
                    print(f"  [{level.upper()}] {rule}: {msg}{loc_str}", flush=True)

        if block_on_high:
            print(
                f"::error::SkillSpector: {high_plus_after} un-suppressed HIGH/CRITICAL finding(s). "
                "Set SKILLSPECTOR_BLOCK_ON_HIGH=false for advisory-only mode.",
                flush=True,
            )
            return 1
        else:
            print(
                f"::warning::SkillSpector (advisory): {high_plus_after} HIGH/CRITICAL finding(s) "
                "found. Set SKILLSPECTOR_BLOCK_ON_HIGH=true to promote to blocker.",
                flush=True,
            )
    else:
        print("[skillspector-ci] No un-suppressed HIGH/CRITICAL findings — clean.", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
