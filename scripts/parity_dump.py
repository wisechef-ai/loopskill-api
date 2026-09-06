#!/usr/bin/env python3
"""Frozen-surface parity dump for loopskill-api (coldstart_0609 gate).

Dumps every externally observable, machine-facing surface of the API to a
deterministic JSON document and compares it against the committed baseline
``evals/parity/baseline.json``. The point: an autonomous fix lane may change a
surface ONLY by declaring it — an undeclared drift in a tool schema, a route, a
migration head or a discovery document is a regression even when every test is
green, because agents in the wild pin to these shapes.

Surfaces (all computed OFFLINE from the app factory on in-memory sqlite — no
server, no network — so this runs in CI and in a nightly cron identically):

  openapi        the full OpenAPI document (paths + components), key-sorted
  routes         "METHOD /path" for every HTTP route (mounts listed as MOUNT)
  mcp_tools      name + description + inputSchema for every MCP tool
  alembic_heads  the migration head revision(s)
  public_paths   the unauthenticated path allow-list
  cli_help       --help text of the shipped CLI (tools/recipes_cli.py)

Usage:
  parity_dump.py dump                 # print the current dump to stdout
  parity_dump.py check                # exit 1 and print a diff if any surface hash changed
  parity_dump.py update               # rewrite the baseline (do this in the PR that declares the change)
  parity_dump.py check --allow openapi,routes   # declared surfaces may differ
  parity_dump.py --self-test          # no app import: proves the hash/diff logic

Adapted from the verification discipline of hermes-agent PR #102117 ("frozen
surfaces byte-identical, checked per slice").
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, TypedDict

REPO = Path(__file__).resolve().parent.parent
if (
    str(REPO) not in sys.path
):  # runnable as `python scripts/parity_dump.py` from any cwd, like the other scripts
    sys.path.insert(0, str(REPO))
BASELINE = REPO / "evals" / "parity" / "baseline.json"


class Entry(TypedDict):
    sha256: str
    body: str


Dump = dict[str, Entry]
SURFACES = ("openapi", "routes", "mcp_tools", "alembic_heads", "public_paths", "cli_help")


def _canon(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, indent=1, default=str, ensure_ascii=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── surface collectors (each returns a JSON-able object) ──────────────────────


def _collect() -> dict[str, Any]:
    os.environ.setdefault("WR_DATABASE_URL", "sqlite:///:memory:")
    os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
    from app.main import create_app  # noqa: PLC0415 — deferred so --self-test needs no app

    app = create_app()
    openapi = app.openapi()
    openapi.pop("info", None)  # version string churns on every release; not a surface

    routes: list[str] = []
    for r in app.routes:
        path = getattr(r, "path", None)
        if path is None:
            continue
        methods = getattr(r, "methods", None)
        if methods:
            routes.extend(f"{m} {path}" for m in sorted(methods))
        else:
            routes.append(f"MOUNT {path}")
    routes = sorted(set(routes))

    from app.mcp.server import _tool_definitions  # noqa: PLC0415

    mcp_tools = sorted(
        (
            {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
            for t in _tool_definitions()
        ),
        key=lambda t: str(t["name"]),
    )

    from alembic.config import Config  # noqa: PLC0415
    from alembic.script import ScriptDirectory  # noqa: PLC0415

    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "alembic"))
    alembic_heads = sorted(ScriptDirectory.from_config(cfg).get_heads())

    from app.middleware import _public_paths as pp  # noqa: PLC0415

    public_paths = {
        k: sorted(v)
        for k, v in vars(pp).items()
        if k.isupper() and isinstance(v, (set, frozenset, list, tuple)) and all(isinstance(x, str) for x in v)
    }

    cli = subprocess.run(
        [sys.executable, str(REPO / "tools" / "recipes_cli.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
    )
    cli_help = cli.stdout.strip()

    return {
        "openapi": openapi,
        "routes": routes,
        "mcp_tools": mcp_tools,
        "alembic_heads": alembic_heads,
        "public_paths": public_paths,
        "cli_help": cli_help,
    }


# ── pure logic (self-testable) ────────────────────────────────────────────────


def build_dump(surfaces: dict[str, Any]) -> Dump:
    """Return {surface: {"sha256": ..., "body": <canonical json string>}} for every surface."""
    out: Dump = {}
    for name in SURFACES:
        body = _canon(surfaces[name])
        out[name] = Entry(sha256=_sha(body), body=body)
    return out


def compare(baseline: Dump, current: Dump, allow: set[str]) -> tuple[list[str], str]:
    """Return (undeclared_drifts, human diff). A surface in `allow` may differ."""
    drifted: list[str] = []
    report: list[str] = []
    for name in SURFACES:
        b = baseline.get(name, Entry(sha256="none", body=""))
        c = current[name]
        if b["sha256"] == c["sha256"]:
            continue
        tag = "declared" if name in allow else "UNDECLARED"
        report.append(f"== {name}: {tag} change ({b['sha256'][:12]} -> {c['sha256'][:12]})")
        diff = difflib.unified_diff(
            b["body"].splitlines(), c["body"].splitlines(), "baseline", "current", lineterm="", n=1
        )
        report.extend(list(diff)[:80])
        if name not in allow:
            drifted.append(name)
    return drifted, "\n".join(report)


def _self_test() -> int:
    s1 = {
        "openapi": {"paths": {"/a": 1}},
        "routes": ["GET /a"],
        "mcp_tools": [{"name": "x", "inputSchema": {}}],
        "alembic_heads": ["h1"],
        "public_paths": {"P": ["/a"]},
        "cli_help": "usage",
    }
    d1 = build_dump(s1)
    assert build_dump(s1) == d1, "dump must be deterministic"
    s2 = json.loads(json.dumps(s1))
    s2["routes"].append("POST /b")
    s2["mcp_tools"][0]["inputSchema"] = {"type": "object"}
    d2 = build_dump(s2)
    drifted, rep = compare(d1, d2, allow=set())
    assert drifted == ["routes", "mcp_tools"], drifted  # SURFACES order, not alphabetical
    assert "UNDECLARED" in rep and '+ "POST /b"' in rep, rep
    drifted, rep = compare(d1, d2, allow={"routes"})
    assert drifted == ["mcp_tools"] and "routes: declared" in rep, (drifted, rep)
    drifted, _ = compare(d1, d2, allow={"routes", "mcp_tools"})
    assert drifted == []
    # key order must not matter
    s3 = {k: s1[k] for k in reversed(list(s1))}
    s3["openapi"] = {"paths": {"/a": 1}}
    assert build_dump(s3)["openapi"]["sha256"] == d1["openapi"]["sha256"]
    print("self-test OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", choices=["dump", "check", "update"], default="check")
    ap.add_argument("--allow", default="", help="comma-separated surfaces whose change is declared")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    current = build_dump(_collect())
    if a.cmd == "dump":
        print(_canon({k: v["sha256"] for k, v in current.items()}))
        return 0
    if a.cmd == "update":
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(_canon(current) + "\n")
        summary = ", ".join(f"{k}={v['sha256'][:8]}" for k, v in current.items())
        print(f"baseline written: {BASELINE} ({summary})")
        return 0
    if not BASELINE.exists():
        print(f"RED: no baseline at {BASELINE} — run `parity_dump.py update` in a PR first", file=sys.stderr)
        return 1
    baseline: Dump = json.loads(BASELINE.read_text())
    allow = {x.strip() for x in a.allow.split(",") if x.strip()}
    drifted, report = compare(baseline, current, allow)
    if report:
        print(report)
    if drifted:
        print(
            f"RED: undeclared surface drift in {', '.join(drifted)} — declare with --allow or update the baseline in this PR",
            file=sys.stderr,
        )
        return 1
    print("parity OK: " + ", ".join(f"{k}={v['sha256'][:8]}" for k, v in current.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
