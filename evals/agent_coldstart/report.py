#!/usr/bin/env python3
"""LoopSkill agent cold-start benchmark REPORT.

Reads a results directory of per-run JSON files (as written by run.py) and
prints a markdown report plus a JSON summary, per RUBRIC.md:
  - per-task outcome table, per harness
  - suite pass rate per harness
  - cost-to-value (median/p90 tool_calls + wall_minutes) over PASSED tasks
    only — failed-task effort is reported separately, never blended in.

Usage:
    python evals/agent_coldstart/report.py --results-dir evals/agent_coldstart/results
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any


def load_results(results_dir: Path) -> list[dict[str, Any]]:
    results = []
    for path in sorted(results_dir.glob("*.json")):
        try:
            results.append(json.loads(path.read_text()))
        except json.JSONDecodeError:
            continue
    return results


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    k = (len(values) - 1) * pct
    f = int(k)
    c = min(f + 1, len(values) - 1)
    if f == c:
        return values[f]
    return values[f] + (values[c] - values[f]) * (k - f)


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_harness: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_harness.setdefault(r["harness"], []).append(r)

    summary: dict[str, Any] = {"harnesses": {}}
    for harness, runs in sorted(by_harness.items()):
        scored = [r for r in runs if r["outcome"] in ("pass", "fail", "timeout")]
        passed = [r for r in runs if r["outcome"] == "pass"]
        failed = [r for r in runs if r["outcome"] in ("fail", "timeout")]
        errored = [r for r in runs if r["outcome"] == "error"]

        pass_tool_calls = [r["tool_calls"] for r in passed if r.get("tool_calls") is not None]
        pass_minutes = [r["wall_seconds"] / 60.0 for r in passed if r.get("wall_seconds") is not None]
        fail_tool_calls = [r["tool_calls"] for r in failed if r.get("tool_calls") is not None]

        summary["harnesses"][harness] = {
            "tasks": {r["task_id"]: r["outcome"] for r in runs},
            "pass_rate": f"{len(passed)}/{len(scored)}" if scored else "0/0",
            "pass_rate_frac": (len(passed) / len(scored)) if scored else None,
            "n_error": len(errored),
            "cost_to_value_passed_only": {
                "median_tool_calls": statistics.median(pass_tool_calls) if pass_tool_calls else None,
                "p90_tool_calls": _percentile([float(x) for x in pass_tool_calls], 0.9),
                "median_minutes": statistics.median(pass_minutes) if pass_minutes else None,
                "p90_minutes": _percentile(pass_minutes, 0.9),
            },
            "failed_task_effort": {
                "median_tool_calls_burned": statistics.median(fail_tool_calls) if fail_tool_calls else None,
                "n_failed": len(failed),
            },
        }
    return summary


def render_markdown(results: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = ["# LoopSkill Agent Cold-Start Benchmark — Report", ""]
    all_task_ids = sorted({r["task_id"] for r in results})
    harnesses = sorted(summary["harnesses"].keys())

    lines.append("## Outcomes (harness × task)")
    lines.append("")
    header = "| task | " + " | ".join(harnesses) + " |"
    sep = "|---|" + "---|" * len(harnesses)
    lines.append(header)
    lines.append(sep)
    for task_id in all_task_ids:
        row = [task_id]
        for h in harnesses:
            row.append(summary["harnesses"][h]["tasks"].get(task_id, "-"))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    lines.append("## Suite pass rate per harness")
    lines.append("")
    for h in harnesses:
        lines.append(f"- **{h}**: {summary['harnesses'][h]['pass_rate']} "
                      f"(errors excluded: {summary['harnesses'][h]['n_error']})")
    lines.append("")

    lines.append("## Cost-to-value (PASSED tasks only)")
    lines.append("")
    for h in harnesses:
        c = summary["harnesses"][h]["cost_to_value_passed_only"]
        lines.append(f"- **{h}**: median tool_calls={c['median_tool_calls']}, "
                      f"p90 tool_calls={c['p90_tool_calls']}, "
                      f"median minutes={c['median_minutes']}, "
                      f"p90 minutes={c['p90_minutes']}")
    lines.append("")

    lines.append("## Failed-task effort (reported separately, never blended into cost-to-value)")
    lines.append("")
    for h in harnesses:
        e = summary["harnesses"][h]["failed_task_effort"]
        lines.append(f"- **{h}**: median tool_calls burned on failed attempts="
                      f"{e['median_tool_calls_burned']} (n={e['n_failed']})")
    lines.append("")

    lines.append(
        "## Deleted candidate task (carried forward per RUBRIC.md)\n\n"
        "\"sync a fleet member\" was deleted from the original 11-task set — "
        "requires pre-existing multi-agent fleet state a single cold agent "
        "cannot construct in one bounded task. See tasks.yaml footer.\n"
    )

    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--json-out", default=None, help="optional path to also write the JSON summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    results_dir = Path(args.results_dir)
    results = load_results(results_dir)
    summary = summarize(results)
    print(render_markdown(results, summary))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(summary, indent=2))
    else:
        print("\n```json")
        print(json.dumps(summary, indent=2))
        print("```")
    return 0


if __name__ == "__main__":
    sys.exit(main())
