"""Daily version-staleness sweep (Phase F.8).

Walks every ``recipe.yaml`` in the catalog (here, every file under
``recipes/<slug>/recipe.yaml``), pulls each pinned binary's
``release_source``, and routes the diff:

  * patch within SemVer → auto-merge PR (mocked: prints intent)
  * minor                → publisher dashboard flag, 14 day ACK window
  * major                → human response always required

Designed to be stdlib + httpx only. The systemd timer is in
``deploy/recipes-version-staleness-check.{service,timer}``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class StalenessFinding:
    skill_slug: str
    binary: str
    pinned: str
    latest: str
    bump: str  # "patch" | "minor" | "major" | "none"
    action: str  # "auto-merge-pr" | "publisher-flag" | "human-required" | "noop"
    note: str = ""


_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


def parse_semver(s: str | None) -> tuple[int, int, int] | None:
    if not s:
        return None
    m = _SEMVER_RE.match(s.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def classify(pinned: str, latest: str) -> str:
    p = parse_semver(pinned)
    l = parse_semver(latest)
    if not p or not l:
        return "none"
    if l <= p:
        return "none"
    if l[0] != p[0]:
        return "major"
    if l[1] != p[1]:
        return "minor"
    return "patch"


def fetch_latest_github(release_source: str, *, _http=None) -> str | None:
    """``github.com/owner/repo`` → query GitHub releases for ``tag_name``."""
    if _http is None:
        import httpx

        _http = httpx
    src = release_source.strip()
    src = src.replace("https://", "").replace("http://", "")
    if not src.startswith("github.com/"):
        return None
    repo = src[len("github.com/") :].strip("/")
    if repo.count("/") != 1:
        return None
    try:
        r = _http.get(
            f"https://api.github.com/repos/{repo}/releases/latest",
            timeout=8.0,
            headers={"Accept": "application/vnd.github+json"},
        )
    # Rationale: network call to GitHub API; any HTTP/connection error → return None
    except Exception:  # noqa: BLE001
        return None
    if getattr(r, "status_code", 0) != 200:
        return None
    try:
        data = r.json()
    # Rationale: response parsing; malformed JSON → return None
    except Exception:  # noqa: BLE001
        return None
    return data.get("tag_name") or data.get("name")


def _route(bump: str) -> str:
    if bump == "patch":
        return "auto-merge-pr"
    if bump == "minor":
        return "publisher-flag"
    if bump == "major":
        return "human-required"
    return "noop"


def scan_recipe(path: Path, *, _http=None, _fetch=fetch_latest_github) -> list[StalenessFinding]:
    """Scan a single ``recipes/<slug>/recipe.yaml`` for stale binaries."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(doc, dict):
        return []
    slug = path.parent.name
    runtime = doc.get("runtime") or {}
    findings: list[StalenessFinding] = []

    for binary in runtime.get("binaries") or []:
        pinned = binary.get("version")
        source = binary.get("release_source")
        if not pinned or not source:
            continue
        latest = _fetch(source, _http=_http)
        if not latest:
            continue
        bump = classify(pinned, latest)
        if bump == "none":
            continue
        findings.append(
            StalenessFinding(
                skill_slug=slug,
                binary=binary.get("name") or "?",
                pinned=pinned,
                latest=latest,
                bump=bump,
                action=_route(bump),
            )
        )

    return findings


def open_auto_merge_pr(finding: StalenessFinding, *, _printer=print) -> None:
    """Print intent for now — real auto-merge needs CI hooks the plan defers."""
    _printer(
        json.dumps(
            {
                "intent": "auto-merge-pr",
                "skill": finding.skill_slug,
                "binary": finding.binary,
                "from": finding.pinned,
                "to": finding.latest,
            }
        )
    )


def flag_publisher(finding: StalenessFinding, *, _printer=print) -> None:
    _printer(
        json.dumps(
            {
                "intent": "publisher-flag",
                "skill": finding.skill_slug,
                "binary": finding.binary,
                "from": finding.pinned,
                "to": finding.latest,
                "ack_window_days": 14,
            }
        )
    )


def require_human(finding: StalenessFinding, *, _printer=print) -> None:
    _printer(
        json.dumps(
            {
                "intent": "human-required",
                "skill": finding.skill_slug,
                "binary": finding.binary,
                "from": finding.pinned,
                "to": finding.latest,
            }
        )
    )


def dispatch(findings: list[StalenessFinding], *, _printer=print) -> None:
    for f in findings:
        if f.action == "auto-merge-pr":
            open_auto_merge_pr(f, _printer=_printer)
        elif f.action == "publisher-flag":
            flag_publisher(f, _printer=_printer)
        elif f.action == "human-required":
            require_human(f, _printer=_printer)


def run(catalog_root: Path, *, _http=None, _fetch=fetch_latest_github, _printer=print) -> dict[str, Any]:
    findings: list[StalenessFinding] = []
    for recipe in sorted(catalog_root.rglob("recipe.yaml")):
        findings.extend(scan_recipe(recipe, _http=_http, _fetch=_fetch))
    dispatch(findings, _printer=_printer)
    return {"count": len(findings), "findings": [asdict(f) for f in findings]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="version_staleness", description="F.8 daily version-staleness sweep.")
    ap.add_argument(
        "--catalog",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "recipes",
        help="Root directory containing recipe.yaml files (default: <repo>/recipes).",
    )
    ap.add_argument("--json", action="store_true", help="Emit findings as JSON.")
    args = ap.parse_args(argv)

    if not args.catalog.exists():
        print(f"catalog not found: {args.catalog}", file=sys.stderr)
        return 2

    result = run(args.catalog)
    if args.json:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
