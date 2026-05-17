"""Catalog-wide linter backfill — issue #110.

Runs ``lint_tarball_with_runtime`` against every ``latest_version`` tarball in
the public, non-archived catalog and produces a markdown report under
``SPRINT_DOCS/CATALOG_LINTER_BACKFILL_<date>.md``.

The publisher pipeline enforces the linter on every NEW publish, but most
skills currently in the catalog were published BEFORE the linter shipped or
before specific rules tightened (e.g. ``no_hardcoded_home_paths`` from the
gitnexus 1.0.0 → 1.0.1 patch). This script catches the long-tail.

USAGE
-----

One-shot (from the recipes-api repo root):

    python scripts/catalog_linter_backfill.py

CI mode (used by ``.github/workflows/weekly-catalog-portability-audit.yml``):

    python scripts/catalog_linter_backfill.py --strict

In strict mode, the script exits non-zero if ANY violation is found. The CI
workflow uses this to fail the audit and post a GitHub issue listing the
offenders.

Authorisation
-------------
Reads tarballs directly from the local filesystem via ``SkillVersion.tarball_path``
(no signed-URL roundtrip). On environments where the tarball storage is remote
(e.g. S3), pass ``--storage=s3`` and set ``RECIPES_S3_BUCKET`` — that path is a
TODO; today the recipes-api host stores tarballs locally under ``buckets/``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow running from repo root without `pip install -e .`
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session, joinedload  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import Skill, SkillVersion  # noqa: E402
from runtime.linter_integration import lint_tarball_with_runtime  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("catalog-linter-backfill")


def _load_tarball_bytes(version: SkillVersion) -> bytes | None:
    """Read a SkillVersion's tarball from local storage; return None if absent."""
    if not version.tarball_path:
        return None
    p = Path(version.tarball_path)
    if not p.is_absolute():
        p = ROOT / p
    if not p.is_file():
        log.warning("tarball missing on disk: %s (slug=%s)", p, version.skill.slug)
        return None
    try:
        return p.read_bytes()
    except OSError as exc:
        log.warning("cannot read tarball %s: %s", p, exc)
        return None


def _violations_summary(blob: dict[str, Any]) -> list[str]:
    """Render lint output into compact ``rule | offender`` strings."""
    lines: list[str] = []
    for v in blob.get("violations", []):
        if isinstance(v, dict):
            rule = v.get("rule", "?")
            offender = v.get("offender") or v.get("evidence") or v.get("path") or ""
            lines.append(f"{rule}: {offender}".strip().rstrip(":"))
        else:
            lines.append(str(v))
    for e in blob.get("schema_errors", []):
        lines.append(f"schema_error: {e}")
    return lines


def audit_catalog(db: Session) -> dict[str, Any]:
    """Lint every latest_version tarball; return aggregated report dict."""
    stmt = (
        select(Skill)
        .options(joinedload(Skill.versions))
        .where(Skill.is_public == True, Skill.is_archived == False)  # noqa: E712
        .order_by(Skill.slug)
    )
    skills = db.execute(stmt).unique().scalars().all()

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "skills_scanned": 0,
        "skills_missing_tarball": [],
        "skills_clean": [],
        "skills_with_violations": [],
        "violation_counts_by_rule": defaultdict(int),
    }

    for skill in skills:
        latest = max(
            skill.versions,
            key=lambda v: v.created_at or datetime.min.replace(tzinfo=timezone.utc),
            default=None,
        )
        if latest is None:
            # Caught by issue #109 invariant — skip.
            continue
        report["skills_scanned"] += 1
        tar = _load_tarball_bytes(latest)
        if tar is None:
            report["skills_missing_tarball"].append(
                {"slug": skill.slug, "version": latest.semver}
            )
            continue

        try:
            lint = lint_tarball_with_runtime(tar)
        except Exception as exc:  # noqa: BLE001
            log.exception("lint failed for %s@%s", skill.slug, latest.semver)
            report["skills_with_violations"].append({
                "slug": skill.slug,
                "version": latest.semver,
                "violations": [f"lint_crash: {exc}"],
            })
            continue

        if lint.get("ok"):
            report["skills_clean"].append({"slug": skill.slug, "version": latest.semver})
            continue

        violations = _violations_summary(lint)
        report["skills_with_violations"].append({
            "slug": skill.slug,
            "version": latest.semver,
            "violations": violations,
        })
        for v in violations:
            rule = v.split(":", 1)[0]
            report["violation_counts_by_rule"][rule] += 1

    # Convert defaultdict so json.dumps is happy and dict ordering is stable.
    report["violation_counts_by_rule"] = dict(
        sorted(report["violation_counts_by_rule"].items(), key=lambda kv: -kv[1])
    )
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Catalog Linter Backfill — Audit Report",
        "",
        f"Generated at `{report['generated_at']}`",
        "",
        "## Summary",
        "",
        f"- Skills scanned: **{report['skills_scanned']}**",
        f"- Clean: **{len(report['skills_clean'])}**",
        f"- With violations: **{len(report['skills_with_violations'])}**",
        f"- Missing tarball: **{len(report['skills_missing_tarball'])}**",
        "",
    ]
    if report["violation_counts_by_rule"]:
        lines += [
            "## Violations by rule",
            "",
            "| Rule | Count |",
            "|------|-------|",
        ]
        for rule, count in report["violation_counts_by_rule"].items():
            lines.append(f"| `{rule}` | {count} |")
        lines.append("")
    if report["skills_with_violations"]:
        lines += [
            "## Skills with violations",
            "",
            "| Slug | Version | Violations |",
            "|------|---------|------------|",
        ]
        for row in report["skills_with_violations"]:
            vlist = "<br>".join(f"`{v}`" for v in row["violations"][:6])
            if len(row["violations"]) > 6:
                vlist += f"<br>… +{len(row['violations']) - 6} more"
            lines.append(f"| `{row['slug']}` | {row['version']} | {vlist} |")
        lines.append("")
    if report["skills_missing_tarball"]:
        lines += [
            "## Skills with missing tarballs (cannot lint)",
            "",
        ]
        for row in report["skills_missing_tarball"]:
            lines.append(f"- `{row['slug']}` @ {row['version']}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 if any violation is found. Used by the CI audit job.",
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        default=None,
        help="Write markdown report to this path (default: SPRINT_DOCS/CATALOG_LINTER_BACKFILL_<date>.md)",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Write raw JSON report to this path.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        report = audit_catalog(db)
    finally:
        db.close()

    md = render_markdown(report)
    if args.output_md is None:
        date = datetime.now(timezone.utc).strftime("%Y%m%d")
        args.output_md = ROOT / "SPRINT_DOCS" / f"CATALOG_LINTER_BACKFILL_{date}.md"
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.write_text(md, encoding="utf-8")
    log.info("markdown report: %s", args.output_md)

    if args.output_json:
        args.output_json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        log.info("json report: %s", args.output_json)

    # Always echo the summary so CI captures it.
    print(md)

    violations_total = len(report["skills_with_violations"])
    if args.strict and violations_total:
        log.error(
            "strict mode: %d skills have violations — failing.", violations_total
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
