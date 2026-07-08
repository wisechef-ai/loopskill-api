"""``recipes-reconcile`` — the runnable thin reconcile client CLI (Phase J).

This is the executable the host's cron line invokes (see reconcile_host_detect.
cron_template). It closes the cold-path: an agent installs the
``recipes-cookbook-reconcile`` skill, whose cron runs THIS, and its skills stay
evergreen with atomic-apply + auto-rollback safety.

One reconcile cycle:
  1. Read the local ``recipes-lock.json`` → current generation + installed set.
  2. POST /api/reconcile with If-None-Match: <generation> + the local lockfile
     state. Server returns 304 (nothing to do — cheap) or 200 + a diff.
  3. On 200: build a CDN-fronted fetcher from the diff's signed tarball_urls,
     hand it to the atomic ReconcileClient, apply {add,update,remove,drift}.
  4. On success: write back the new generation + skill set to the lockfile.
     On reconcile_failed (auto-rollback fired): leave the lockfile untouched
     (resume-safe) and exit non-zero so the cron surfaces it.

Intelligence is server-side; this client only pulls + atomically swaps. It uses
the agent's own x-api-key (env RECIPES_API_KEY) — no inbound auth.

Usage (what the cron line runs):
  recipes-reconcile --cookbook <uuid> --api https://recipes.wisechef.ai \\
      --skills-dir ~/.hermes/skills --lockfile ~/.hermes/recipes-lock.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from app.reconcile_client import ReconcileClient, read_lockfile, write_lockfile
from app.reconcile_fetch import make_fetcher


def _post_reconcile(
    api_base: str,
    cookbook_id: str,
    generation: str,
    local: list[dict[str, Any]],
    api_key: str,
    *,
    opener: Any = None,
) -> tuple[int, dict[str, Any]]:
    """Call POST /api/bundles/{cookbook_id}/reconcile with conditional If-None-Match.

    Returns (status_code, body). 304 → (304, {}) means up-to-date (cheap path).

    URL contract (activate_0701 Phase 0 live-prod fix): the server mounts the
    handler as ``/{cookbook_id}/reconcile`` under ``/api/bundles`` (see
    app/reconcile_routes.py dual-mount) — the old flat ``/api/reconcile`` URL
    404s against every deployed instance.
    """
    open_url = opener or urllib.request.urlopen
    url = f"{api_base.rstrip('/')}/api/bundles/{cookbook_id}/reconcile"
    payload = json.dumps({"local": local}).encode()
    req = urllib.request.Request(url, data=payload, method="POST")  # noqa: S310  # Rationale: our own API.
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", api_key)
    if generation:
        req.add_header("If-None-Match", generation)
    try:
        with open_url(req) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        if exc.code == 304:
            return 304, {}
        raise


def reconcile_once(
    *,
    cookbook_id: str,
    api_base: str,
    skills_dir: Path,
    lockfile: Path,
    api_key: str,
    prune: bool = False,
    opener: Any = None,
    connector_config: Path | None = None,
    gateway_restart_cmd: list[str] | None = None,
    stage_only: bool = False,
) -> dict[str, Any]:
    """Run one reconcile cycle. Returns a structured result dict.

    Pure-ish: all I/O endpoints are injectable via ``opener`` for tests.
    """
    lock = read_lockfile(lockfile)
    generation = lock.get("generation", "")
    local = lock.get("skills", [])

    status, body = _post_reconcile(api_base, cookbook_id, generation, local, api_key, opener=opener)

    if status == 304:
        return {"status": "up_to_date", "generation": generation, "applied": [], "removed": []}

    diff = body.get("diff", {})
    new_generation = body.get("generation", generation)

    if not any(diff.get(k) for k in ("add", "update", "remove", "drift")):
        # 200 but empty diff — bump generation, nothing to apply.
        lock["generation"] = new_generation
        write_lockfile(lockfile, lock)
        return {"status": "no_changes", "generation": new_generation, "applied": [], "removed": []}

    staging = Path(tempfile.mkdtemp(prefix="recipes-reconcile-fetch-"))
    try:
        fetcher = make_fetcher(diff, staging, opener=opener)
        client = ReconcileClient(skills_dir, fetch_skill=fetcher)
        result = client.apply(diff, prune=prune)
    finally:
        import shutil

        shutil.rmtree(staging, ignore_errors=True)

    if result.reconcile_failed:
        # Auto-rollback fired — leave the lockfile untouched (resume-safe).
        return {
            "status": "reconcile_failed",
            "rolled_back": result.rolled_back,
            "failure_reason": result.failure_reason,
            "failed_slug": result.failed_slug,
            "generation": generation,  # unchanged
        }

    # Success — write back the new generation + installed set.
    installed = {s.get("slug"): s for s in local}
    for slug in result.applied:
        entry = next(
            (e for sec in ("add", "update", "drift") for e in diff.get(sec, []) if e["slug"] == slug),
            {"slug": slug},
        )
        installed[slug] = {
            "slug": slug,
            "pinned_version": entry.get("to") or entry.get("version", ""),
            "checksum_sha256": entry.get("checksum_sha256") or entry.get("expected_sha256", ""),
        }
    for slug in result.removed:
        installed.pop(slug, None)

    lock["cookbook_id"] = cookbook_id
    lock["generation"] = new_generation
    lock["skills"] = list(installed.values())
    write_lockfile(lockfile, lock)

    out: dict[str, Any] = {
        "status": "applied",
        "generation": new_generation,
        "applied": result.applied,
        "removed": result.removed,
    }

    # ── activate_0701 Phase B: connector apply (guarded, D8) ────────────────
    # The diff may carry a `connectors` section. When present AND the caller
    # passed --connector-config, apply via ConnectorApplier (snapshot → env
    # check → managed block → restart → probe → auto-rollback). Absent config
    # path = skills-only mode (backward compatible — connectors reported as
    # skipped so the fleet owner knows the agent isn't wired for them yet).
    conn_diff = body.get("connectors") or diff.get("connectors")
    if conn_diff and any(conn_diff.get(k) for k in ("add", "update", "remove")):
        if connector_config is None:
            out["connectors"] = {"status": "skipped", "reason": "no --connector-config passed"}
        else:
            from app.connector_apply import ConnectorApplier

            applier = ConnectorApplier(
                config_yaml_path=connector_config,
                gateway_restart_cmd=gateway_restart_cmd
                or ["systemctl", "--user", "restart", "hermes-gateway"],
                stage_only=stage_only,
            )
            conn_result = applier.apply(conn_diff)
            out["connectors"] = {
                "status": conn_result.outcome,
                "applied": conn_result.applied,
                "removed": conn_result.removed,
                "failure_reason": conn_result.failure_reason,
                "rolled_back": conn_result.rolled_back,
                "rollback_failed": conn_result.rollback_failed,
            }

    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recipes-reconcile", description=__doc__)
    parser.add_argument("--cookbook", required=True, help="Bundle UUID to reconcile.")
    parser.add_argument("--api", default="https://recipes.wisechef.ai", help="Recipes API base.")
    parser.add_argument("--skills-dir", required=True, type=Path, help="Live skills dir to keep evergreen.")
    parser.add_argument("--lockfile", required=True, type=Path, help="recipes-lock.json path.")
    parser.add_argument("--prune", action="store_true", help="Allow REMOVE (uninstall dropped skills).")
    parser.add_argument("--api-key", default=None, help="x-api-key (else env RECIPES_API_KEY).")
    parser.add_argument(
        "--connector-config",
        default=None,
        type=Path,
        help="Agent config.yaml for connector apply (Phase B). Absent = skills-only mode.",
    )
    parser.add_argument(
        "--gateway-restart-cmd",
        default=None,
        help="Gateway restart command (space-separated). Default: systemctl --user restart hermes-gateway",
    )
    parser.add_argument(
        "--stage-connectors",
        action="store_true",
        help="Stage connector changes to config.yaml.lsk-staged instead of applying (no restart).",
    )
    args = parser.parse_args(argv)

    api_key = args.api_key or os.environ.get(
        "RECIPES_API_KEY", ""
    )  # TODO(rename): env var still uses legacy name for prod compatibility
    if not api_key:
        print("loopskill-reconcile: no API key (pass --api-key or set RECIPES_API_KEY)", file=sys.stderr)
        return 2

    try:
        result = reconcile_once(
            cookbook_id=args.cookbook,
            api_base=args.api,
            skills_dir=args.skills_dir,
            lockfile=args.lockfile,
            api_key=api_key,
            prune=args.prune,
            connector_config=args.connector_config,
            gateway_restart_cmd=(args.gateway_restart_cmd or "").split() or None,
            stage_only=args.stage_connectors,
        )
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        # Rationale: surface server-side auth/availability errors cleanly to the
        # host cron instead of a traceback (e.g. 401/403 bad key, 5xx outage).
        print(
            json.dumps({"status": "http_error", "code": exc.code, "reason": str(exc.reason)}),
            file=sys.stderr,
        )
        return 3
    except urllib.error.URLError as exc:  # type: ignore[attr-defined]
        # Rationale: network unreachable / DNS / TLS — transient; cron retries next tick.
        print(json.dumps({"status": "network_error", "reason": str(exc.reason)}), file=sys.stderr)
        return 3
    print(json.dumps(result, indent=2))
    # Non-zero exit on reconcile_failed so the host cron surfaces it.
    return 1 if result.get("status") == "reconcile_failed" else 0


if __name__ == "__main__":
    sys.exit(main())
